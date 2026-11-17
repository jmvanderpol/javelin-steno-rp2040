#!/usr/bin/env python3
"""Three bounded hardware checks for Uni v2 + KB2040, without flashing.

Requires pyserial==3.5. Only `typing` injects a RAM-only script. Physical-key
tests can also generate normal typing, so both require a ready editor target.
All runs save a JSON report and timestamped serial transcript.
"""
import argparse
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import struct
import subprocess
import time
import uuid

VID, PID = 0x9000, 0x400D
BUTTONS = (
    'top row 1: S', 'top row 2: T', 'top row 3: P', 'top row 4: H',
    'top row 5: *', 'top row 6: *', 'top row 7: F', 'top row 8: P',
    'top row 9: L', 'top row 10: T', 'top row 11: D',
    'middle row 1: S', 'middle row 2: K', 'middle row 3: W', 'middle row 4: R',
    'middle row 5: *', 'middle row 6: *', 'middle row 7: R', 'middle row 8: B',
    'middle row 9: G', 'middle row 10: S', 'middle row 11: Z',
    'thumb row 1: #', 'thumb row 2: A', 'thumb row 3: O',
    'thumb row 4: E', 'thumb row 5: U', 'thumb row 6: #',
)
TEFT_BUTTONS = {1, 6, 9, 25}
TEFT_MASK = sum(1 << key for key in TEFT_BUTTONS)


class TestFailure(RuntimeError):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    def __init__(self, directory, test):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.logfile = (self.directory / 'serial.jsonl').open('w')
        try:
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            revision = None
        self.report = {'test': test, 'started_utc': utc(), 'git_revision': revision,
                       'status': 'running', 'checks': {}, 'events': []}
        self.save()

    def log(self, direction, text):
        self.logfile.write(json.dumps({'utc': utc(), 'direction': direction, 'text': text}) + '\n')
        self.logfile.flush()

    def save(self):
        temporary = self.directory / 'report.tmp'
        temporary.write_text(json.dumps(self.report, indent=2) + '\n')
        temporary.replace(self.directory / 'report.json')

    def event(self, frame):
        self.report['events'].append({'utc': utc(), 'frame': frame})
        self.save()


class Frames:
    def __init__(self):
        self.buffer = b''

    def feed(self, data):
        self.buffer += data.replace(b'\r', b'')
        if len(self.buffer) > 262144:
            raise TestFailure('Console response exceeded limit')
        result = []
        while b'\n\n' in self.buffer:
            packet, self.buffer = self.buffer.split(b'\n\n', 1)
            if packet.strip():
                result.append(packet.decode('utf-8', errors='replace').strip())
        return result


def button_mask(frame):
    if not frame.startswith('EV e: b\n'):
        return None
    field = re.search(r'^d: (\S+)$', frame, re.MULTILINE)
    if field is None:
        raise TestFailure('Button event has no data')
    try:
        raw = base64.b64decode(field[1], validate=True)
    except ValueError as error:
        raise TestFailure('Malformed button-state base64') from error
    if not 4 <= len(raw) <= 16:
        raise TestFailure('Unexpected button-state width')
    value = int.from_bytes(raw, 'little')
    if value >> 29:
        raise TestFailure('Button state exceeds the 29-button board')
    return value


class KeyTracker:
    def __init__(self, expected):
        self.expected = expected
        self.previous = 0
        self.saw_down = False
        self.released = False
        self.unexpected = 0
        self.events = 0
        self.seen = 0

    def add(self, mask):
        self.events += 1
        self.seen |= mask
        self.unexpected |= mask & ~self.expected
        if mask == self.expected:
            self.saw_down = True
        if self.saw_down and self.previous and mask == 0:
            self.released = True
        self.previous = mask

    @property
    def passed(self):
        return self.saw_down and self.released and not self.unexpected

    def reason(self):
        if not self.events:
            return 'no_button_events_before_timeout'
        if self.unexpected:
            return 'unexpected_button_indices'
        if not self.saw_down:
            return 'expected_keys_never_held_together'
        if not self.released:
            return 'release_not_observed'
        return None


class Console:
    def __init__(self, port, recorder, timeout=3, serial_factory=None):
        if serial_factory is None:
            import serial
            serial_factory = serial.Serial
        self.recorder, self.timeout = recorder, timeout
        self.framer, self.pending, self.channel = Frames(), [], None
        # 115200 avoids the RP2040 ecosystem's special 1200-baud reset behavior.
        self.port = serial_factory(port, baudrate=115200, timeout=.1,
                                   write_timeout=1, exclusive=True)

    def close(self):
        self.port.close()  # Dropping DTR leaves the serial-console session.

    def send(self, command):
        self.recorder.log('tx', command)
        self.port.write((command + '\n').encode())
        self.port.flush()

    def read(self, deadline):
        while time.monotonic() < deadline:
            if self.pending:
                return self.pending.pop(0)
            data = self.port.read(4096)
            if data:
                self.pending.extend(self.framer.feed(data))
        return None

    def open(self):
        self.send('start_javelin_console')
        token = 'hardware-smoke-' + uuid.uuid4().hex[:12]
        self.send('hello ' + token)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = self.read(deadline)
            if frame is None:
                break
            self.recorder.log('rx', frame)
            match = re.fullmatch(r'c(\d\d) ID Hello ' + token, frame)
            if match:
                self.channel = match[1]
                return
            if frame.startswith('EV '):
                self.recorder.event(frame)
        raise TestFailure('No matching Javelin hello response before timeout')

    def command(self, command):
        self.send(f'c{self.channel} {command}')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            frame = self.read(deadline)
            if frame is None:
                break
            self.recorder.log('rx', frame)
            if frame.startswith('EV '):
                self.recorder.event(frame)
                continue
            prefix = f'c{self.channel} '
            if frame.startswith(prefix):
                response = frame[len(prefix):]
                if response.startswith('ERR '):
                    raise TestFailure(f'{command}: {response}')
                return response
        raise TestFailure(f'Console timed out on: {command}')

    def observe(self, seconds, callback):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = self.read(deadline)
            if frame is None:
                break
            self.recorder.log('rx', frame)
            self.recorder.event(frame)
            if callback(frame):
                return True
        return False


def discover(port=None):
    from serial.tools import list_ports
    matches = [p for p in list_ports.comports() if p.vid == VID and p.pid == PID]
    if port:
        matches = [p for p in matches if p.device == port]
    if len(matches) != 1:
        raise TestFailure(f'Expected one 9000:400d board; found {len(matches)}. '
                          'Connect it or select an identified device with --port.')
    device = matches[0]
    return {'port': device.device, 'vid': device.vid, 'pid': device.pid,
            'product': device.product, 'serial_number': device.serial_number}


def health(console, recorder, dictionary_set='plover'):
    commands = ['info', 'get_parameter button_count', 'get_parameter steno_mode',
                'get_parameter host_layout', 'get_parameter maximum_dictionary_size',
                'list_dictionaries', 'lookup_stroke TEFT']
    responses = {cmd: console.command(cmd) for cmd in commands}
    info = responses['info']
    checks = {
        'board_configuration': 'Configuration: uni_v2_kb2040' in info,
        'button_count': int(responses[commands[1]], 0) == 29,
        'embedded_mode': responses[commands[2]] == 'embedded',
        'us_macos_host': responses[commands[3]] == 'us_macos',
        'dictionary_capacity': int(responses[commands[4]], 0) == 0x3c0000,
        'main_dictionary': ('lapwing-base.json' if dictionary_set == 'lapwing' else 'main.json') in responses[commands[5]],
        'commands_dictionary': ('lapwing-commands.json' if dictionary_set == 'lapwing' else 'commands.json') in responses[commands[5]],
        'known_lookup': bool(re.search(r'\bt:\s*(?:"test"|test)\s*[,}]', responses[commands[6]])),
    }
    recorder.report['dictionary_set'] = dictionary_set
    recorder.report['responses'] = responses
    recorder.report['checks'].update(checks)
    recorder.save()
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise TestFailure('Health checks failed: ' + ', '.join(failed))
    print('PASS: console, board, mode, host layout and dictionary lookup', flush=True)
    return info


def keyboard_reports(info):
    match = re.search(r'^\s*Keyboard: (\d+)\s*$', info, re.MULTILINE)
    if not match:
        raise TestFailure('Keyboard report count is missing from info')
    return int(match[1])


def typing_script(text):
    # Only lowercase letters, decimal digits and hyphens; no newline, modifiers,
    # shell commands, flash writes, timers, callbacks or persistent changes.
    if not re.fullmatch(r'[a-z0-9-]{1,48}', text):
        raise ValueError('Diagnostic text must be 1..48 lowercase letters/digits/hyphens')
    code = bytearray(b'JSS4\0\0\x08\0')
    for c in text:
        key = ord(c) - ord('a') + 4 if c.isalpha() else (39 if c == '0' else int(c) + 29) if c.isdigit() else 45
        code.extend([key, 0x90, 2])  # PUSH key; CALL_INTERNAL TapScanCode.
    code.append(0x92)
    if len(code) % 2:
        code.append(0)
    struct.pack_into('<H', code, 4, len(code))
    code.extend(struct.pack('<HH', 1, 0))
    return bytes(code)


def countdown(seconds):
    print(f'Focus the blank editor now. Starting in {seconds} seconds.', flush=True)
    for left in range(seconds, 0, -1):
        print(left, flush=True)
        time.sleep(1)


def typing(console, recorder, info, seconds):
    marker = 'javelin-usb-test-' + uuid.uuid4().hex[:6]
    recorder.report['expected_text'] = marker
    recorder.save()
    print('Expected text:', marker, flush=True)
    countdown(seconds)
    code = typing_script(marker)
    result = console.command('run_script ' + base64.b64encode(code).decode())
    if result != 'OK':
        raise TestFailure('RAM script was not accepted: ' + result)
    # A short bounded delay lets the normal HID output loop drain.
    time.sleep(.5)
    after = console.command('info')
    before_count, after_count = keyboard_reports(info), keyboard_reports(after)
    recorder.report['checks']['ram_script_accepted'] = True
    recorder.report['checks']['keyboard_reports_increased'] = after_count > before_count
    recorder.report['keyboard_report_counts'] = [before_count, after_count]
    recorder.report['post_typing_info'] = after
    recorder.report['status'] = 'awaiting_visual_confirmation'
    if after_count <= before_count:
        raise TestFailure('Script accepted but keyboard HID report count did not increase')
    print('RAM script accepted and keyboard reports increased. Visual confirmation is still required.', flush=True)


def keys(console, recorder, buttons, seconds, chord_seconds, delay, chord_only):
    recorder.report['physical'] = []
    console.command('enable_events button_state,paper_tape')
    try:
        # Reassert key-state reporting in RAM without replacing the saved script.
        code = b'JSS4\x0c\0\x08\0\x01\x90\x56\x92\x01\0\0\0'
        console.command('run_script ' + base64.b64encode(code).decode())
        countdown(delay)
        for button in ([] if chord_only else buttons):
            print(f'PRESS AND RELEASE button {button}: {BUTTONS[button]} (left to right).', flush=True)
            tracker = KeyTracker(1 << button)
            recorder.report['phase'] = f'button_{button}'
            recorder.save()

            def check(frame):
                mask = button_mask(frame)
                if mask is not None:
                    tracker.add(mask)
                return tracker.passed

            console.observe(seconds, check)
            result = {'button': button, 'position': BUTTONS[button], 'passed': tracker.passed,
                      'saw_press': tracker.saw_down, 'saw_release': tracker.released,
                      'unexpected_mask': hex(tracker.unexpected), 'reason': tracker.reason()}
            recorder.report['physical'].append(result)
            recorder.save()
            print(('PASS' if tracker.passed else 'FAIL'), BUTTONS[button], flush=True)
        if chord_seconds:
            print('CHORD: press T-left, E, F-right, T-right together, then release all.', flush=True)
            print('Positions: top row 2, top row 7, top row 10, thumb row 4. Expected: TEFT -> test.', flush=True)
            tracker, outlines = KeyTracker(TEFT_MASK), []
            recorder.report['phase'] = 'chord_TEFT'
            recorder.save()

            def chord(frame):
                mask = button_mask(frame)
                if mask is not None:
                    tracker.add(mask)
                if frame.startswith('EV e: p\n'):
                    outlines.append(frame)
                return tracker.passed and any(re.search(r'^o: TEFT$', p, re.MULTILINE) and
                                              re.search(r'^t: (?:"test"|test)$', p, re.MULTILINE)
                                              for p in outlines)

            ok = console.observe(chord_seconds, chord)
            recorder.report['chord'] = {'passed': ok, 'buttons': sorted(TEFT_BUTTONS),
                                        'saw_chord_press_release': tracker.passed,
                                        'seen_button_indices': [b for b in range(29) if tracker.seen >> b & 1],
                                        'reason': tracker.reason() or (None if ok else 'expected_translation_not_observed'),
                                        'paper_tape': outlines}
            recorder.save()
        passed = (all(r['passed'] for r in recorder.report['physical']) and
                  (not chord_seconds or recorder.report['chord']['passed']))
        if not passed:
            raise TestFailure('Physical key or known-chord check failed; inspect captured events')
        recorder.report['status'] = 'passed'
    finally:
        # Disable the test event streams. The normal saved layout already
        # enables button reporting; no flash data is modified.
        try:
            console.command('disable_events button_state,paper_tape')
        except Exception as error:
            recorder.report['cleanup_error'] = str(error)


def confirm(directory, observed):
    path = Path(directory) / 'report.json'
    report = json.loads(path.read_text())
    if report.get('test') != 'typing' or report.get('status') != 'awaiting_visual_confirmation':
        raise TestFailure('This run is not awaiting typing confirmation')
    report['observed_text'] = observed
    report['checks']['exact_text_observed'] = observed == report['expected_text']
    report['status'] = 'passed' if all(report['checks'].values()) else 'failed'
    report['confirmed_utc'] = utc()
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(report['status'], path)
    return 0 if report['status'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('test', choices=['health', 'typing', 'keys', 'confirm-typing'])
    parser.add_argument('--dictionary-set', choices=['plover', 'lapwing'], default='plover')
    parser.add_argument('--port', help='Must identify the expected 9000:400d board')
    parser.add_argument('--output', help='New directory for this run')
    parser.add_argument('--timeout', type=float, default=3)
    parser.add_argument('--ready', action='store_true', help='Blank editor target prepared; safe to type into it')
    parser.add_argument('--countdown', type=int, default=10)
    parser.add_argument('--buttons', default='0,11,22', help='Physical indices, comma separated; default tests all three rows')
    parser.add_argument('--all-buttons', action='store_true')
    parser.add_argument('--key-seconds', type=float, default=20)
    parser.add_argument('--chord-seconds', type=float, default=30, help='Zero skips chord test')
    parser.add_argument('--chord-only', action='store_true')
    parser.add_argument('--observed-text', help='Exact text the user saw in the editor')
    args = parser.parse_args()
    if args.test == 'confirm-typing':
        if not args.output or args.observed_text is None:
            parser.error('confirm-typing requires --output and --observed-text')
        return confirm(args.output, args.observed_text)
    if args.test in ('typing', 'keys') and not args.ready:
        parser.error('Prepare a blank editor target, then explicitly pass --ready')
    if args.timeout <= 0 or not 0 <= args.countdown <= 60 or args.key_seconds <= 0 or args.chord_seconds < 0:
        parser.error('Invalid timeout/countdown')
    try:
        buttons = list(range(28)) if args.all_buttons else [int(v) for v in args.buttons.split(',')]
        if not buttons or len(set(buttons)) != len(buttons) or any(not 0 <= b < 28 for b in buttons):
            raise ValueError()
    except ValueError:
        parser.error('Select unique physical switch indices 0..27; BOOT is excluded')
    if args.test == 'keys' and args.chord_only and not args.chord_seconds:
        parser.error('A chord-only test needs a positive chord timeout')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    directory = args.output or f'build/hardware-tests/{stamp}-{args.test}-{uuid.uuid4().hex[:6]}'
    recorder = Recorder(directory, args.test)
    console = None
    try:
        device = discover(args.port)
        recorder.report['device'] = device
        recorder.save()
        console = Console(device['port'], recorder, args.timeout)
        console.open()
        info = health(console, recorder, args.dictionary_set)
        if args.test == 'typing':
            typing(console, recorder, info, args.countdown)
        elif args.test == 'keys':
            keys(console, recorder, buttons, args.key_seconds, args.chord_seconds,
                 args.countdown, args.chord_only)
        else:
            recorder.report['status'] = 'passed'
    except (Exception, KeyboardInterrupt) as error:
        recorder.report['status'] = 'failed'
        recorder.report['error'] = str(error) or type(error).__name__
        print('FAIL:', recorder.report['error'], flush=True)
    finally:
        if console:
            try:
                console.close()
            except Exception as error:
                recorder.report['close_error'] = str(error)
                recorder.report['status'] = 'failed'
        recorder.report['finished_utc'] = utc()
        recorder.save()
        recorder.logfile.close()
    print('Report:', recorder.directory / 'report.json', flush=True)
    return 0 if recorder.report['status'] == 'passed' else 2 if recorder.report['status'] == 'awaiting_visual_confirmation' else 1


if __name__ == '__main__':
    raise SystemExit(main())
