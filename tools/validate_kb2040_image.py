#!/usr/bin/env python3
"""Validate a converted image, including tests against actual ARM ELF functions.

Requires unicorn==2.1.4 and pyelftools==0.32. This emulates selected functions,
not an RP2040 boot or USB device. Hardware validation remains separate.
"""
import argparse
import bz2
import hashlib
import json
from pathlib import Path
import struct
import zlib

from build_kb2040_image import (BASE, CONFIG, DICTIONARY, END, HARDWARE_SHA256,
                                HOSTS, KEYS, SCRIPT, STORAGE, SYSTEM, USER,
                                Image, dictionary_records, read_uf2, require)


def check_maps(image):
    """Verify every entry is reachable with the pinned core's CRC/probing rules."""
    records, end = dictionary_records(image)
    report, samples = [], []
    for definition, maximum, kind in records:
        if kind != 0:
            continue
        name_ptr, dictionary_mask, text, rows = struct.unpack('<4I', image.get(definition + 4, 16))
        name = image.string(name_ptr).decode()
        total = 0
        for length in range(1, maximum + 1):
            mask, first_mask, last_mask, data, offsets = struct.unpack('<5I', image.get(rows + 20 * (length - 1), 20))
            size = mask + 1
            require(mask > 0 and size % 128 == 0 and (mask & size) == 0, 'Bad hash table size')
            raw_blocks = [struct.unpack('<5I', image.get(offsets + i * 20, 20)) for i in range(size // 128)]
            bits = [int.from_bytes(image.get(offsets + i * 20, 16), 'little') for i in range(size // 128)]
            slots = [i * 128 + j for i, value in enumerate(bits) for j in range(128) if value & (1 << j)]
            stride = 3 * (length + 1)
            require(data + len(slots) * stride <= offsets, 'Entry data overlaps hash table')
            require(offsets + len(raw_blocks) * 20 <= end, 'Hash table outside dictionary')

            def occupied(slot):
                return bits[slot // 128] & (1 << (slot % 128))

            # Validate rank metadata independently of a particular lookup.
            previous = 0
            for row, bitfield in zip(raw_blocks, bits):
                require(row[4] == (previous - 1) & 0xffffffff, 'Incorrect cumulative entry offset')
                previous += bitfield.bit_count()
            for index, slot in enumerate(slots):
                addr = data + index * stride
                raw = image.get(addr, stride)
                text_offset = int.from_bytes(raw[:3], 'little')
                strokes = [int.from_bytes(raw[3 + i * 3:6 + i * 3], 'little') for i in range(length)]
                require(all(s & ~dictionary_mask == 0 for s in strokes), 'Dictionary mask rejects a real entry')
                require(strokes[0] & ~first_mask == 0 and strokes[-1] & ~last_mask == 0,
                        'First/last stroke mask rejects a real entry')
                words = struct.pack('<' + 'I' * length, *strokes)
                probe = zlib.crc32(words) & mask
                for _ in range(size):
                    require(occupied(probe), f'Unreachable entry in {name}, length {length}')
                    if probe == slot:
                        break
                    probe = (probe + 1) & mask
                else:
                    raise ValueError('Hash probe did not terminate')
                require(text <= text + text_offset < end - 4, 'Text offset outside dictionary')
                expected = image.string(text + text_offset)
                require(expected, 'Unexpected empty translation')
                # Uniformly cover the complete table, every outline length,
                # and both edges in the ARM runtime sample.
                if index in (0, len(slots) - 1) or index % max(1, len(slots) // 48) == 0:
                    samples.append((definition, strokes, expected))
            total += len(slots)
        report.append({'name': name, 'entries_checked': total, 'maximum_outline_length': maximum})
    return report, samples


class ArmFirmware:
    def __init__(self, elf_path, blocks):
        from elftools.elf.elffile import ELFFile
        from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
        from unicorn.arm_const import (UC_CPU_ARM_CORTEX_M0, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                                       UC_ARM_REG_PC, UC_ARM_REG_LR)
        self.mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.mu.ctl_set_cpu_model(UC_CPU_ARM_CORTEX_M0)
        self.mu.mem_map(BASE, END - BASE)
        self.mu.mem_map(0x20000000, 0x42000)
        for addr, data in blocks.items():
            self.mu.mem_write(addr, data)
        self.types = {}
        with open(elf_path, 'rb') as source:
            elf = ELFFile(source)
            require(elf['e_machine'] == 'EM_ARM', 'Expected ARM ELF')
            self.symbols = {symbol.name: symbol['st_value']
                            for symbol in elf.get_section_by_name('.symtab').iter_symbols()}
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_LOAD':
                    addr = segment['p_vaddr']
                    data = segment.data()
                    if BASE <= addr < END:
                        # Bind validation to the exact executable contained in
                        # the full UF2, not merely to a similarly named ELF.
                        require(bytes(self.mu.mem_read(addr, len(data))) == data,
                                'ELF code differs from the packaged hardware firmware')
                    elif 0x20000000 <= addr < 0x20042000:
                        self.mu.mem_write(addr, data)
            wanted = {b'ButtonScript', b'Script', b'StenoCompiledOrthography',
                      b'StenoCompactMapDictionaryDefinition', b'StenoCompactMapDictionaryStrokesDefinition',
                      b'StrokeKey', b'StenoDictionaryLookup'}
            for cu in elf.get_dwarf_info().iter_CUs():
                for die in cu.iter_DIEs():
                    attrs = die.attributes
                    name = attrs.get('DW_AT_name')
                    if name and name.value in wanted and 'DW_AT_byte_size' in attrs:
                        members = {}
                        for child in die.iter_children():
                            member = child.attributes
                            if (child.tag == 'DW_TAG_member' and 'DW_AT_name' in member
                                    and 'DW_AT_data_member_location' in member):
                                members[member['DW_AT_name'].value.decode()] = member['DW_AT_data_member_location'].value
                        self.types[name.value.decode()] = (attrs['DW_AT_byte_size'].value, members)
                if len(self.types) == len(wanted):
                    break
        for name, size in {'StenoCompactMapDictionaryDefinition': 20,
                           'StenoCompactMapDictionaryStrokesDefinition': 20,
                           'StrokeKey': 8, 'StenoDictionaryLookup': 24}.items():
            require(name in self.types and self.types[name][0] == size,
                    f'ELF data ABI does not match image adapter: {name}')
        self.heap = 0x20016000
        self.allocations = {}
        self.stop = 0x2003F000
        self.hooks = {}
        self.commands = []
        self.steno_events = []

        def malloc(uc):
            uc.reg_write(UC_ARM_REG_R0, self.allocate(uc.reg_read(UC_ARM_REG_R0)))

        for name in ('malloc', '__wrap_malloc'):
            self.intercept(name, malloc)
        for name in ('free', '__wrap_free'):
            self.intercept(name, lambda uc: None)

        def realloc(uc):
            old, size = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
            addr = self.allocate(size)
            if old:
                require(old in self.allocations, 'Unknown emulated allocation')
                self.mu.mem_write(addr, bytes(self.mu.mem_read(old, min(size, self.allocations[old]))))
            uc.reg_write(UC_ARM_REG_R0, addr)
        for name in ('realloc', '__wrap_realloc'):
            if name in self.symbols:
                self.intercept(name, realloc)

        def memcpy(uc):
            dest, source, size = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
            if size:
                uc.mem_write(dest, bytes(uc.mem_read(source, size)))
            uc.reg_write(UC_ARM_REG_R0, dest)
        for name in ('memcpy', '__wrap_memcpy', '__wrap___aeabi_memcpy', '__wrap___aeabi_memcpy4'):
            if name in self.symbols:
                self.intercept(name, memcpy)

        def memset(uc):
            dest, value, size = (uc.reg_read(r) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2))
            if size:
                uc.mem_write(dest, bytes([value & 255]) * size)
            uc.reg_write(UC_ARM_REG_R0, dest)
        for name in ('memset', '__wrap_memset'):
            if name in self.symbols:
                self.intercept(name, memset)
        # Replace only hardware-backed CRC DMA, heap allocation, and physical
        # output effects. Dictionary constructors/lookups and the VM run as ARM.
        def crc(uc):
            addr, size = uc.reg_read(UC_ARM_REG_R0), uc.reg_read(UC_ARM_REG_R1)
            uc.reg_write(UC_ARM_REG_R0, zlib.crc32(bytes(uc.mem_read(addr, size))))
        self.intercept('_ZN5Crc324HashEPKvj', crc)

        def dispatch(uc, addr, size, context):
            if addr in self.hooks:
                self.hooks[addr](uc)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
        self.mu.hook_add(UC_HOOK_CODE, dispatch)

    def intercept(self, name, handler):
        require(name in self.symbols, f'Required ARM symbol missing: {name}')
        self.hooks[self.symbols[name] & ~1] = handler

    def allocate(self, size):
        addr = self.heap
        self.heap = (addr + size + 7) & ~7
        require(self.heap < 0x20030000, 'Emulated heap exhausted')
        self.mu.mem_write(addr, bytes(size))
        self.allocations[addr] = size
        return addr

    def word(self, addr):
        return struct.unpack('<I', bytes(self.mu.mem_read(addr, 4)))[0]

    def string(self, addr):
        out = bytearray()
        while len(out) < 4096:
            c = self.mu.mem_read(addr + len(out), 1)[0]
            if c == 0:
                return bytes(out)
            out.append(c)
        raise ValueError('Unterminated emulated string')

    def call(self, name, *args):
        from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2,
                                       UC_ARM_REG_R3, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC)
        require(name in self.symbols, f'ARM symbol missing: {name}')
        for reg, value in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            self.mu.reg_write(reg, value)
        self.mu.reg_write(UC_ARM_REG_SP, 0x2003E000)
        self.mu.reg_write(UC_ARM_REG_LR, self.stop | 1)
        try:
            self.mu.emu_start(self.symbols[name] | 1, self.stop, count=2000000)
        except Exception as error:
            raise RuntimeError(f'{name}: PC={self.mu.reg_read(UC_ARM_REG_PC):#x}, '
                               f'LR={self.mu.reg_read(UC_ARM_REG_LR):#x}') from error
        require(self.mu.reg_read(UC_ARM_REG_PC) == self.stop, 'ARM function did not return')
        return self.mu.reg_read(UC_ARM_REG_R0)


def arm_checks(arm, image, samples):
    from unicorn.arm_const import UC_ARM_REG_R0
    objects = {}
    for definition, strokes, expected in samples:
        if definition not in objects:
            objects[definition] = arm.call('_ZNK25StenoDictionaryDefinition6CreateEv', definition)
        words = struct.pack('<' + 'I' * len(strokes), *strokes)
        arm.mu.mem_write(0x20031000, words)
        mask = 0
        for stroke in strokes:
            mask |= stroke
        arm.mu.mem_write(0x20031100, struct.pack('<6I', 0x20031000, len(strokes), mask,
                                               strokes[0], strokes[-1], zlib.crc32(words)))
        result = arm.call('_ZNK25StenoCompactMapDictionary6LookupERK21StenoDictionaryLookup',
                          objects[definition], 0x20031100)
        require(result and arm.string(result) == expected, 'ARM dictionary lookup mismatch')
    print(f'ARM dictionary lookups passed: {len(samples)}', flush=True)

    # Run the pinned core's system parser/formatter against converted keys.
    arm.call('_ZN11StenoStroke11SetLanguageERK9SizedListI9StrokeKeyE', SYSTEM + 12)
    for outline in (b'S', b'KAT', b'TEFT', b'H-L', b'STKPWHR', b'-FRPBLGTSDZ', b'#S-T'):
        arm.mu.mem_write(0x20031000, outline + b'\0')
        arm.call('_ZN11StenoStroke3SetEPKc', 0x20031200, 0x20031000)
        arm.call('_ZNK11StenoStroke8ToStringEPc', 0x20031200, 0x20031300)
        require(arm.string(0x20031300) == outline, f'ARM stroke roundtrip failed: {outline}')

    arm.call('_ZN11HostLayouts7SetDataERKS_', HOSTS)
    active = arm.word(arm.symbols['_ZN11HostLayouts12activeLayoutE'])
    require(arm.string(active) == b'us_macos', 'Wrong default host layout')
    for name in (b'us', b'us_macos'):
        arm.mu.mem_write(0x20031000, name + b'\0')
        require(arm.call('_ZN11HostLayouts15SetActiveLayoutEPKc', 0x20031000), 'Host layout selection failed')
    print('ARM stroke parsing/formatting and both host selections passed', flush=True)

    # Compile the converted system's real orthography patterns and consume the
    # supplied word list, exercising both data regions in the pinned runtime.
    for name in ('_ZN24StenoCompiledOrthography9LockCacheEv',
                 '_ZN24StenoCompiledOrthography11UnlockCacheEv'):
        arm.intercept(name, lambda uc: None)
    arm.call('_ZN8WordList7SetDataERK12WordListData', arm.symbols['_ZN8WordList8instanceE'], 0x10054000)
    orthography = arm.allocate(arm.types['StenoCompiledOrthography'][0])
    arm.call('_ZN24StenoCompiledOrthographyC1ERK16StenoOrthography', orthography, SYSTEM + 20)
    suffix_cases = [('cat', 's', 'cats'), ('cherry', 's', 'cherries'),
                    ('love', 'ing', 'loving'), ('run', 'ing', 'running')]
    for word, suffix, expected in suffix_cases:
        arm.mu.mem_write(0x20031000, word.encode() + b'\0')
        arm.mu.mem_write(0x20031100, suffix.encode() + b'\0')
        result = arm.call('_ZNK24StenoCompiledOrthography9AddSuffixEPKcS1_',
                          orthography, 0x20031000, 0x20031100)
        require(arm.string(result) == expected.encode(), 'ARM orthography suffix mismatch')
    print('ARM word-list and orthography suffix tests passed', flush=True)

    arm.intercept('_ZN12ButtonScript17OnStenoKeyPressedEv', lambda uc: arm.steno_events.append('press'))
    arm.intercept('_ZN12ButtonScript18OnStenoKeyReleasedEv', lambda uc: arm.steno_events.append('release'))
    size, members = arm.types['ButtonScript']
    obj = arm.allocate(size)
    arm.call('_ZN12ButtonScriptC1EPKh', obj, SCRIPT)
    arm.call('_ZN6Script5ResetEv', obj)
    arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 0, 122)
    for button, key in enumerate(KEYS):
        arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 2 + 2 * button, 123)
        state = int.from_bytes(arm.mu.mem_read(obj + members['stenoState'], 8), 'little')
        require(state == 1 << key, f'Wrong steno press for button {button}')
        arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 3 + 2 * button, 124)
        require(not any(arm.mu.mem_read(obj + members['stenoState'], 8)), 'Stuck steno key')
    # Exercise a real chord: press three keys, release in a different order.
    selected = (0, 6, 23)
    for button in selected:
        arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 2 + 2 * button, 125)
    actual = int.from_bytes(arm.mu.mem_read(obj + members['stenoState'], 8), 'little')
    require(actual == sum(1 << KEYS[b] for b in selected), 'Incorrect chord state')
    for button in reversed(selected):
        arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 3 + 2 * button, 126)
    require(not any(arm.mu.mem_read(obj + members['stenoState'], 8)), 'Chord release left a key down')
    # Console dispatch is intercepted to avoid entering a physical boot ROM.
    def console(uc):
        arm.commands.append(arm.string(uc.reg_read(UC_ARM_REG_R0)).decode())
        uc.reg_write(UC_ARM_REG_R0, 0)
    arm.intercept('_ZN7Console10RunCommandEPKcR7IWriter', console)
    arm.call('_ZN12ButtonScript18ExecuteScriptIndexEjm', obj, 58, 127)
    require(arm.commands == ['launch_bootloader'], 'Boot button command mismatch')
    stack = arm.types['Script'][1]
    require(arm.word(obj + stack['stackTop']) == obj + stack['stack'], 'VM stack did not balance')
    print('ARM VM: all 28 presses/releases, chord and boot command passed', flush=True)
    return {'dictionary_lookups': len(samples), 'button_press_release_pairs': 28,
            'chord': 'passed', 'boot_command': 'launch_bootloader',
            'default_host_layout': 'us_macos', 'stroke_roundtrips': 7,
            'orthography_suffix_cases': len(suffix_cases)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--elf', required=True)
    parser.add_argument('--report', default='build/uni_v2_kb2040_upstream_complete/validation.json')
    args = parser.parse_args()
    blocks, digest = read_uf2(args.image)
    image = Image(blocks)
    hardware = {a: data for a, data in blocks.items() if a < CONFIG}
    # Recreate the exact hardware UF2 numbering to verify the pinned firmware.
    from build_kb2040_image import write_uf2
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'hardware.uf2'
        write_uf2(path, hardware)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == HARDWARE_SHA256, 'Wrong hardware firmware')
    require(set(a for a in blocks if a >= CONFIG) == set(range(CONFIG, END, 256)), 'Incomplete initialization data')
    require(image.get(USER, END - USER) == b'\xff' * (END - USER), 'User region is not erased')
    report, samples = check_maps(image)
    print('Exhaustive dictionary checks:', report, flush=True)
    crc, size = struct.unpack('<II', image.get(STORAGE, 8))
    script_size = struct.unpack('<H', image.get(SCRIPT + 4, 2))[0] + 4
    require(crc == zlib.crc32(image.get(SCRIPT, script_size)), 'Editable script CRC mismatch')
    packed = image.get(STORAGE + 8, size)
    require(packed[0] == 1, 'Wrong editable script storage version')
    layout = json.loads(bz2.decompress(packed[1:]))
    require(len(layout['layers'][0]['keys']) == 29, 'Wrong editable layout key count')
    arm_report = arm_checks(ArmFirmware(args.elf, blocks), image, samples)
    result = {'image_sha256': digest, 'dictionary_checks': report, 'arm_runtime_checks': arm_report,
              'hardware_validation': 'not performed; this is selected-function emulation, not an RP2040 boot',
              'emulator_stubs': ['heap allocator', 'ROM memcpy/memset', 'DMA CRC32',
                                 'orthography cache locks', 'steno output callbacks', 'boot console dispatch']}
    Path(args.report).write_text(json.dumps(result, indent=2) + '\n')
    print('Validation passed. Report:', args.report)


if __name__ == '__main__':
    main()
