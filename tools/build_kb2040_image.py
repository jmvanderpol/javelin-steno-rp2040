#!/usr/bin/env python3
"""Adapt one hash-pinned official Plover image to the pinned November 2026 core.

This is an initial-install image builder, not a general dictionary compiler.
It deliberately erases editable scripts, assets and the user dictionary.
No network access or third-party Python packages are required.
"""
import argparse
import bz2
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import zipfile
import zlib

BASE = 0x10000000
CONFIG = 0x1004A000
SCRIPT = 0x1004A100
SYSTEM = 0x10052000
HOSTS = 0x103DD000
STORAGE = 0x103E0000
DICTIONARY = 0x10400000
USER = 0x107C0000
END = 0x10800000
FAMILY = 0xE48BFF56
CORE = '7efd1e13a74f00ba4d2e793198546e6dc83c7fbe'
SOURCE_URL = 'https://lim.au/assets/data/uni_v4_plover-2026-11-11.uf2.zip'
SOURCE_SHA256 = 'cd7eec7fa36b52df5d45b3e2d10ee248eafb836d65252248e4e5dc5d4459f33a'
HARDWARE_SHA256 = '3365d1465fb4d81e7898b015fd59ff9aaafdf19468aa049562aecd1e2fb8a48c'

# StenoKey enum values, in verified Uni PCB physical-button order.
KEYS = [0, 2, 4, 6, 10, 12, 16, 18, 20, 22, 24,
        1, 3, 5, 7, 11, 13, 17, 19, 21, 23, 25, 26, 8, 9, 14, 15, 27]
KEY_NAMES = ['S1', 'TL', 'PL', 'HL', 'Star1', 'Star3', 'FR', 'PR', 'LR', 'TR', 'DR',
             'S2', 'KL', 'WL', 'RL', 'Star2', 'Star4', 'RR', 'BR', 'GR', 'SR', 'ZR',
             'Num1', 'A', 'O', 'E', 'U', 'Num2']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_uf2(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b'PK'):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith('.uf2')]
            require(len(names) == 1, 'Expected one UF2 in archive')
            raw = archive.read(names[0])
    require(len(raw) > 0 and len(raw) % 512 == 0, 'Invalid UF2 length')
    blocks, indices = {}, set()
    count = len(raw) // 512
    for offset in range(0, len(raw), 512):
        m1, m2, flags, addr, size, index, total, family = struct.unpack_from('<8I', raw, offset)
        require((m1, m2) == (0x0A324655, 0x9E5D5157), 'Bad UF2 magic')
        require(struct.unpack_from('<I', raw, offset + 508)[0] == 0x0AB16F30, 'Bad UF2 trailer')
        require(flags == 0x2000 and family == FAMILY, 'Expected plain RP2040 flash UF2')
        require(size == 256 and addr % 256 == 0, 'Expected aligned 256-byte payloads')
        require(total == count and index < count and index not in indices, 'Invalid UF2 block sequence')
        require(BASE <= addr < addr + size <= BASE + 0x1000000, 'Invalid flash address')
        require(addr not in blocks, 'Duplicate UF2 address')
        blocks[addr] = raw[offset + 32:offset + 32 + size]
        indices.add(index)
    return blocks, sha(raw)


def write_uf2(path, blocks):
    with Path(path).open('wb') as out:
        for index, (addr, data) in enumerate(sorted(blocks.items())):
            require(len(data) == 256, 'Internal UF2 payload error')
            header = struct.pack('<8I', 0x0A324655, 0x9E5D5157, 0x2000,
                                 addr, 256, index, len(blocks), FAMILY)
            out.write(header + data + bytes(220) + struct.pack('<I', 0x0AB16F30))


class Image:
    def __init__(self, blocks):
        self.data = bytearray(b'\xff' * (END - BASE))
        for addr, data in blocks.items():
            require(addr + len(data) <= END, 'Source does not fit 8 MiB')
            self.put(addr, data)

    def get(self, addr, size):
        require(BASE <= addr <= addr + size <= END, f'Out-of-range read {addr:#x}+{size:#x}')
        return bytes(self.data[addr - BASE:addr - BASE + size])

    def put(self, addr, data):
        require(BASE <= addr <= addr + len(data) <= END, f'Out-of-range write {addr:#x}')
        self.data[addr - BASE:addr - BASE + len(data)] = data

    def u32(self, addr):
        return struct.unpack('<I', self.get(addr, 4))[0]

    def word(self, addr, value):
        self.put(addr, struct.pack('<I', value))

    def string(self, addr, limit=4096):
        raw = self.get(addr, min(limit, END - addr))
        require(b'\0' in raw, f'Unterminated string at {addr:#x}')
        return raw.split(b'\0', 1)[0]


def dictionary_records(image):
    require(image.get(DICTIONARY, 4) == b'JSC4', 'Wrong dictionary magic')
    count = struct.unpack('<H', image.get(DICTIONARY + 4, 2))[0]
    require(count == 3, 'Unexpected pinned source dictionary count')
    text_size, text = struct.unpack('<II', image.get(DICTIONARY + 8, 8))
    end = text + text_size + 4
    require(DICTIONARY <= text < end <= USER, 'Dictionary text exceeds capacity')
    require(image.u32(DICTIONARY + 32) == image.u32(end - 4), 'Dictionary timestamp mismatch')
    records = []
    for i in range(count):
        addr = image.u32(DICTIONARY + 36 + i * 4)
        enabled, maximum, kind, options = image.get(addr, 4)
        require(enabled in (0, 1) and kind in (0, 3), 'Unsupported source dictionary type')
        records.append((addr, maximum, kind))
    return records, end


def prepare_dictionaries(image):
    """Validate modern map descriptors; the pinned core consumes them verbatim."""
    records, end = dictionary_records(image)
    report = []
    for addr, maximum, kind in records:
        if kind == 3:  # Jeff numbers is implemented by the firmware itself.
            report.append({'name': 'jeff-numbers', 'type': 'native'})
            continue
        name, mask, text, strokes = struct.unpack('<4I', image.get(addr + 4, 16))
        name = image.string(name).decode()
        require(name in ('commands.json', 'main.json'), 'Unexpected source map dictionary')
        rows = [struct.unpack('<5I', image.get(strokes + i * 20, 20)) for i in range(maximum)]
        for hash_mask, first_mask, last_mask, data, offsets in rows:
            require(hash_mask > 0 and (hash_mask & (hash_mask + 1)) == 0, 'Unexpected hash table mask')
            require(DICTIONARY <= data <= offsets < end, 'Invalid stroke-data pointers')
            require(first_mask & ~mask == 0 and last_mask & ~mask == 0,
                    'Stroke masks exceed dictionary mask')
        report.append({'name': name, 'type': 'compact_map', 'maximum_outline_length': maximum})
    return report, end


def prepare_system(image):
    # The new core uses the source's 24-bit character and byte-3 key type.
    # Applying the old 2025 converter here would corrupt separator keys.
    count, keys = struct.unpack('<II', image.get(SYSTEM + 12, 8))
    require(0 < count < 128 and SYSTEM <= keys < keys + count * 8 < 0x10054000,
            'Invalid system key list')
    for i in range(count):
        record = image.get(keys + 8 * i, 8)
        codepoint = int.from_bytes(record[:3], 'little')
        kind = record[3]
        require(codepoint <= 0x10ffff and kind <= 2, 'Unsupported stroke key')
    return count


def select_hosts(image):
    count = image.u32(HOSTS)
    require(0 < count < 32, 'Invalid host-layout count')
    found = {}
    for i in range(count):
        addr = image.u32(HOSTS + 4 + i * 4)
        require(HOSTS <= addr < STORAGE, 'Invalid host-layout address')
        name = image.string(addr, 30).decode()
        found[name] = addr
    order = ['us_macos', 'us']
    require(all(name in found for name in order), 'Required host layouts missing')
    # The first layout is selected by HostLayouts::SetData on startup. Keeping
    # existing record addresses also preserves any relative script offsets.
    image.word(HOSTS, 2)
    image.put(HOSTS + 4, struct.pack('<2I', *(found[name] for name in order)))
    for name in order:
        addr = found[name]
        script_offset = struct.unpack('<H', image.get(addr + 30, 2))[0]
        entries = image.u32(addr + 288)
        require(addr + 292 + entries * 16 <= STORAGE, 'Host entries exceed region')
        require(script_offset == 0 or image.get(addr + script_offset, 4) == b'JSS4',
                'Unexpected host script format')
    return order


def make_button_script():
    # 60 script offsets: init, tick, and press/release for all 29 buttons.
    # Generate only well-defined v4 bytecode; no newer builder internals used.
    offsets = [0] * 60
    code = bytearray(6 + len(offsets) * 2)
    code[:4] = b'JSS4'
    offsets[0] = len(code)
    code.extend([1, 0x90, 86, 0x92])  # setEnableButtonStates(true)
    for button, key in enumerate(KEYS):
        for release in (False, True):
            offsets[2 + 2 * button + release] = len(code)
            code.extend([key, 0x90, 5 if release else 4, 0x92])
    offsets[58] = len(code)
    string_offset = len(code) + 7
    # PUSH_BYTES_2S command-offset; CALL_INTERNAL Console; POP result; RETURN.
    code.extend(b'\x3d' + struct.pack('<h', string_offset) + b'\x90\x0b\x93\x92')
    code.extend(b'launch_bootloader\0')
    if len(code) % 2:
        code.append(0)
    hash_table = len(code)
    # One empty hash bucket: console return strings fall back to their pointer.
    code.extend(struct.pack('<HH', 1, 0))
    struct.pack_into('<H', code, 4, hash_table)
    struct.pack_into('<60H', code, 6, *offsets)
    keys = []
    for name in KEY_NAMES:
        keys.append([{'t': action, 'd': {'a': {'t': action, 'c': 'steno' + name}}}
                     for action in ('p', 'r')])
    keys.append([{'t': 'p', 'd': {'a': {'t': 'c', 'c': 'launch_bootloader'}}}])
    # This is the editable representation of this deliberately simple script.
    layout = {'layers': [{'id': 0, 'name': 'Default', 'keys': keys,
                         'scripts': {}, 'user_scripts': {}, 'options': {}, 'sequences': {}}],
              'global': {'scripts': [], 'layer': {
                  'id': -1, 'name': 'Global', 'keys': [None] * 29,
                  'scripts': {'activateLayer': 'setEnableButtonStates(true);\n'},
                  'user_scripts': {}, 'options': {}, 'sequences': {}}}}
    return bytes(code), layout


def install_button_script(image):
    script, layout = make_button_script()
    require(len(script) <= SYSTEM - SCRIPT, 'Button script too large')
    image.put(SCRIPT, b'\xff' * (SYSTEM - SCRIPT))
    image.put(SCRIPT, script)
    # Store compressed editable layout in the same version-1 envelope as the
    # official builder. CRC covers exactly ScriptByteCode::GetLength().
    packed = b'\x01' + bz2.compress(json.dumps(layout, separators=(',', ':')).encode())
    image.put(STORAGE, b'\xff' * (DICTIONARY - STORAGE))
    image.put(STORAGE, struct.pack('<II', zlib.crc32(script), len(packed)) + packed)
    return script, layout


def build(source, firmware, output):
    source_blocks, source_hash = read_uf2(source)
    require(source_hash == SOURCE_SHA256, 'Source hash mismatch; this converter supports only the pinned official image')
    firmware_blocks, firmware_hash = read_uf2(firmware)
    require(firmware_hash == HARDWARE_SHA256,
            'Hardware hash mismatch; revalidate this converter before using a different firmware')
    require(all(BASE <= addr < addr + 256 <= CONFIG for addr in firmware_blocks),
            'Hardware firmware overlaps data')
    image = Image(source_blocks)
    dictionaries, dictionary_end = prepare_dictionaries(image)
    key_count = prepare_system(image)
    hosts = select_hosts(image)
    script, layout = install_button_script(image)
    # Source config contains newer extended key assignments. Keep standard
    # English assignments; unassigned keys stay -1 for this core.
    keymap = [1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11, 12,
              13, 14, 15, 16, 17, 18, 19, 20, 21, 22] + [0] * 12 + [-1] * 26
    require(len(keymap) == 64, 'Invalid configuration keymap size')
    config = bytearray(96)
    config[4] = 1  # Enable native Jeff modifiers.
    config[32:96] = struct.pack('<64b', *keymap)
    image.put(CONFIG, bytes(256))
    image.put(CONFIG, config)
    # Every initialization image explicitly clears old assets/user entries.
    clear_from = (dictionary_end + 4095) & ~4095
    image.put(clear_from, b'\xff' * (END - clear_from))
    data_blocks = {addr: image.get(addr, 256) for addr in range(CONFIG, END, 256)}
    full_blocks = {**firmware_blocks, **data_blocks}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    data_path = output / 'uni_v2_kb2040_plover_initial_data.uf2'
    full_path = output / 'uni_v2_kb2040_plover_initial_full.uf2'
    write_uf2(data_path, data_blocks)
    write_uf2(full_path, full_blocks)
    (output / 'uni_v2_kb2040.javelin-layout').write_text(json.dumps(layout, indent=2) + '\n')
    (output / 'button_script.bin').write_bytes(script)
    manifest = {
        'purpose': 'initial installation; resets scripts, assets and user dictionary',
        'data_abi': 'modern map masks and 24-bit StrokeKey; no legacy conversion',
        'source_url': SOURCE_URL, 'source_uf2_sha256': source_hash,
        'hardware_uf2_sha256': firmware_hash, 'core_revision': CORE,
        'host_layouts': hosts, 'default_host_layout': hosts[0],
        'dictionaries': dictionaries, 'native_jeff_modifiers': True,
        'dictionary_start': hex(DICTIONARY), 'dictionary_end': hex(dictionary_end),
        'dictionary_limit': hex(USER), 'dictionary_bytes': dictionary_end - DICTIONARY,
        'available_asset_bytes': USER - clear_from,
        'user_dictionary': [hex(USER), hex(END)], 'system_key_count': key_count,
        'button_script_bytes': len(script), 'button_script_crc32': hex(zlib.crc32(script)),
        'outputs': {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())}
                    for p in (data_path, full_path)},
        'hardware_validation': 'not performed',
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Pinned official Uni v4 Plover UF2 or ZIP')
    parser.add_argument('--firmware', required=True, help='Matching locally built KB2040 hardware UF2')
    parser.add_argument('--output', default='build/uni_v2_kb2040_upstream_complete')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(['git', '-C', str(root / 'javelin'), 'rev-parse', 'HEAD'], text=True).strip()
    require(revision == CORE, 'Wrong core revision; rebuild/revalidate the converter for this core')
    result = build(args.source, args.firmware, args.output)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
