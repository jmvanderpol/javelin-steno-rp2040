"""Protocol and decision checks; no device access or typing during unit tests."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import hardware_smoke_test as hw


class Log:
    def __init__(self):
        self.messages, self.events = [], []

    def log(self, direction, value):
        self.messages.append((direction, value))

    def event(self, value):
        self.events.append(value)


class FakePort:
    def __init__(self, *args, **kwargs):
        self.incoming = []
        self.closed = False
        self.writes = []

    def write(self, data):
        text = data.decode().strip()
        self.writes.append(text)
        if text.startswith('hello '):
            self.incoming += [(f'c07 ID Hello {text[6:]}\n\n').encode()]
        elif text == 'c07 info':
            self.incoming += [b'EV e: b\nd: AQAAAA==\n', b'\nc07 System\n  Configuration: uni_v2_kb2040\n\n']
        elif text == 'c07 bad':
            self.incoming += [b'c07 ERR Invalid command\n\n']

    def flush(self):
        pass

    def read(self, size):
        return self.incoming.pop(0) if self.incoming else b''

    def close(self):
        self.closed = True


class HardwareTests(unittest.TestCase):
    def test_fragmented_and_coalesced_frames(self):
        frames = hw.Frames()
        self.assertEqual(frames.feed(b'c01 O'), [])
        self.assertEqual(frames.feed(b'K\r\n\r\nEV e: b\nd: AAAA'), ['c01 OK'])
        self.assertEqual(frames.feed(b'AA==\n\n'), ['EV e: b\nd: AAAAAA=='])

    def test_unbounded_response_is_rejected(self):
        with self.assertRaises(hw.TestFailure):
            hw.Frames().feed(b'x' * 262145)

    def test_console_handshake_and_interleaved_events(self):
        log = Log()
        console = hw.Console('/unused', log, timeout=.01, serial_factory=FakePort)
        console.open()
        self.assertEqual(console.channel, '07')
        self.assertIn('Configuration: uni_v2_kb2040', console.command('info'))
        self.assertEqual(log.events, ['EV e: b\nd: AQAAAA=='])
        self.assertIn('start_javelin_console', console.port.writes)
        console.close()
        self.assertTrue(console.port.closed)

    def test_console_error_and_timeout_are_not_passes(self):
        console = hw.Console('/unused', Log(), timeout=.001, serial_factory=FakePort)
        console.open()
        with self.assertRaisesRegex(hw.TestFailure, 'Invalid command'):
            console.command('bad')
        with self.assertRaisesRegex(hw.TestFailure, 'timed out'):
            console.command('unanswered')

    def test_button_payloads_are_little_endian_and_width_checked(self):
        mask = (1 << 27) | (1 << 1)
        encoded = base64.b64encode(mask.to_bytes(8, 'little')).decode()
        self.assertEqual(hw.button_mask('EV e: b\nd: ' + encoded), mask)
        self.assertIsNone(hw.button_mask('EV e: p\no: TEFT'))
        for payload in ('?', base64.b64encode(bytes(1)).decode(),
                        base64.b64encode((1 << 29).to_bytes(4, 'little')).decode()):
            with self.assertRaises(hw.TestFailure):
                hw.button_mask('EV e: b\nd: ' + payload)

    def test_key_requires_press_and_release_and_rejects_wrong_key(self):
        tracker = hw.KeyTracker(1 << 11)
        tracker.add(0)
        self.assertFalse(tracker.passed)
        tracker.add(1 << 11)
        self.assertFalse(tracker.passed)
        tracker.add(0)
        self.assertTrue(tracker.passed)
        tracker.add(1 << 12)
        self.assertFalse(tracker.passed)

    def test_chord_requires_complete_overlap_and_final_release(self):
        tracker = hw.KeyTracker(hw.TEFT_MASK)
        tracker.add(1 << 1)
        tracker.add(hw.TEFT_MASK)
        tracker.add(1 << 25)
        self.assertFalse(tracker.passed)
        tracker.add(0)
        self.assertTrue(tracker.passed)

    def test_timeout_diagnostics_distinguish_missing_wrong_and_stuck_keys(self):
        tracker = hw.KeyTracker(hw.TEFT_MASK)
        self.assertEqual(tracker.reason(), 'no_button_events_before_timeout')
        tracker.add(1 << 1)
        self.assertEqual(tracker.reason(), 'expected_keys_never_held_together')
        tracker.add(hw.TEFT_MASK)
        self.assertEqual(tracker.reason(), 'release_not_observed')
        tracker.add(0)
        self.assertIsNone(tracker.reason())
        tracker.add(1 << 11)
        self.assertEqual(tracker.reason(), 'unexpected_button_indices')

    def test_typing_script_has_only_expected_taps_and_no_enter(self):
        text = 'az-190'
        code = hw.typing_script(text)
        self.assertEqual(code[:4], b'JSS4')
        self.assertEqual(struct.unpack_from('<H', code, 6)[0], 8)
        expected_keys = [4, 29, 45, 30, 38, 39]
        self.assertEqual(code[8:8 + 3 * len(text)],
                         bytes(v for key in expected_keys for v in (key, 0x90, 2)))
        self.assertEqual(code[8 + 3 * len(text)], 0x92)
        table = struct.unpack_from('<H', code, 4)[0]
        self.assertEqual(code[table:], b'\x01\0\0\0')
        for unsafe in ('A', 'a\n', 'a;b', '', 'x' * 49):
            with self.assertRaises(ValueError):
                hw.typing_script(unsafe)

    def test_hid_report_count_is_not_visual_confirmation(self):
        self.assertEqual(hw.keyboard_reports('HID reports sent\n  Keyboard: 28\n'), 28)
        with self.assertRaises(hw.TestFailure):
            hw.keyboard_reports('no reports')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            report = {'test': 'typing', 'status': 'awaiting_visual_confirmation',
                      'expected_text': 'test', 'checks': {'keyboard_reports_increased': True}}
            path.write_text(json.dumps(report))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(hw.confirm(directory, 'wrong'), 1)
            self.assertEqual(json.loads(path.read_text())['status'], 'failed')
            report['status'] = 'awaiting_visual_confirmation'
            path.write_text(json.dumps(report))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(hw.confirm(directory, 'test'), 0)
            self.assertEqual(json.loads(path.read_text())['status'], 'passed')


if __name__ == '__main__':
    unittest.main()
