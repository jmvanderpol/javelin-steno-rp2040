"""Negative and round-trip checks for the initialization-image packager."""
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_kb2040_image as builder


class ImageBuilderTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'test.uf2'
        self.blocks = {builder.BASE: bytes(range(256)),
                       builder.CONFIG: bytes([0xff]) * 256}
        builder.write_uf2(self.path, self.blocks)

    def corrupt(self, offset, data):
        raw = bytearray(self.path.read_bytes())
        raw[offset:offset + len(data)] = data
        self.path.write_bytes(raw)

    def test_round_trip_preserves_sparse_addresses_and_data(self):
        blocks, digest = builder.read_uf2(self.path)
        self.assertEqual(blocks, self.blocks)
        self.assertEqual(digest, builder.sha(self.path.read_bytes()))

    def test_wrong_family_rejected(self):
        self.corrupt(28, struct.pack('<I', 0xe48bff57))
        with self.assertRaisesRegex(ValueError, 'RP2040'):
            builder.read_uf2(self.path)

    def test_duplicate_address_rejected(self):
        self.corrupt(512 + 12, struct.pack('<I', builder.BASE))
        with self.assertRaisesRegex(ValueError, 'Duplicate UF2 address'):
            builder.read_uf2(self.path)

    def test_duplicate_block_number_rejected(self):
        self.corrupt(512 + 20, struct.pack('<I', 0))
        with self.assertRaisesRegex(ValueError, 'block sequence'):
            builder.read_uf2(self.path)

    def test_truncated_uf2_rejected(self):
        self.path.write_bytes(self.path.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'length'):
            builder.read_uf2(self.path)

    def test_unrecognized_source_cannot_produce_an_image(self):
        with self.assertRaisesRegex(ValueError, 'Source hash mismatch'):
            builder.build(self.path, self.path, Path(self.directory.name) / 'out')
        self.assertFalse((Path(self.directory.name) / 'out').exists())

    def test_unvalidated_hardware_cannot_produce_an_image(self):
        from unittest.mock import patch
        _, source_hash = builder.read_uf2(self.path)
        with patch.object(builder, 'SOURCE_SHA256', source_hash):
            with self.assertRaisesRegex(ValueError, 'Hardware hash mismatch'):
                builder.build(self.path, self.path, Path(self.directory.name) / 'out')
        self.assertFalse((Path(self.directory.name) / 'out').exists())

    def test_recorded_dependency_revision_matches_adapter(self):
        import json
        root = Path(__file__).resolve().parents[1]
        lock = json.loads((root / 'dependencies.lock.json').read_text())
        self.assertEqual(builder.CORE, lock['javelin']['revision'])

    def test_eight_megabyte_boundary_enforced(self):
        image = builder.Image(self.blocks)
        with self.assertRaisesRegex(ValueError, 'Out-of-range write'):
            image.put(builder.END - 1, b'ab')
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            builder.Image({builder.END: bytes(256)})

    def test_incomplete_source_archive_rejected(self):
        import zipfile
        path = Path(self.directory.name) / 'empty.zip'
        with zipfile.ZipFile(path, 'w'):
            pass
        with self.assertRaises(ValueError):
            builder.read_uf2(path)

    def modern_fixture(self):
        image = builder.Image({})
        base = builder.DICTIONARY
        image.put(base, b'JSC4' + struct.pack('<HBB', 3, 0, 0))
        image.put(base + 8, struct.pack('<II', 4, base + 0x400))
        image.word(base + 32, 1234)
        image.put(base + 36, struct.pack('<III', base + 0x40, base + 0x50, base + 0x70))
        image.put(base + 0x40, bytes([1, 0, 3, 0]))
        for addr, name, name_addr, rows in [
                (base + 0x50, b'commands.json', base + 0x300, base + 0x100),
                (base + 0x70, b'main.json', base + 0x320, base + 0x120)]:
            image.put(addr, bytes([1, 1, 0, 0]) +
                      struct.pack('<IIII', name_addr, 1, base + 0x400, rows))
            image.put(name_addr, name + b'\0')
            image.put(rows, struct.pack('<IIIII', 127, 1, 1, base + 0x200, base + 0x208))
        image.put(base + 0x400, b'cat\0' + struct.pack('<I', 1234))
        image.put(builder.SYSTEM + 12, struct.pack('<II', 2, builder.SYSTEM + 0x80))
        image.put(builder.SYSTEM + 0x80, struct.pack('<IIII', ord('S'), 2,
                                                    ord('-') | (1 << 24), 0x1f00))
        return image

    def test_modern_dictionary_and_separator_records_are_preserved(self):
        image = self.modern_fixture()
        before = bytes(image.data)
        records, end = builder.prepare_dictionaries(image)
        self.assertEqual(len(records), 3)
        self.assertEqual(end, builder.DICTIONARY + 0x408)
        self.assertEqual(builder.prepare_system(image), 2)
        # The old adapter compacted 20-byte descriptors and moved the separator
        # type into byte 2. Either change corrupts the updated core's inputs.
        self.assertEqual(bytes(image.data), before)

    def test_incompatible_stroke_masks_are_rejected(self):
        image = self.modern_fixture()
        image.word(builder.DICTIONARY + 0x104, 2)
        with self.assertRaisesRegex(ValueError, 'Stroke masks exceed'):
            builder.prepare_dictionaries(image)


if __name__ == '__main__':
    unittest.main()
