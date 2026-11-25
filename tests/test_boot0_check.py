"""tools/boot0-check.py: the boot firmware of an image must hold a whole boot0 with the memory clock of its variant."""
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "tools" / "boot0-check.py"


def region(clock=2040, length=0x3C000):
    """A 16 MiB firmware region with a boot0 at sector 256 and its copy at 2064."""
    b = bytearray(length)
    b[4:12] = b"eGON.BT0"
    b[16:20] = struct.pack("<I", length)
    b[2 * 512 + 116: 2 * 512 + 118] = struct.pack("<H", clock)
    b[40:48] = b"codecode"
    b[12:16] = struct.pack("<I", 0x5F0A6C39)
    b[12:16] = struct.pack("<I", sum(struct.unpack(f"<{length // 4}I", bytes(b))) & 0xFFFFFFFF)
    d = bytearray(16 * 1024 * 1024)
    for sector in (256, 2064):
        d[sector * 512: sector * 512 + length] = b
    return d


class TestBoot0Check(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.f = Path(self.tmp.name) / "fw.img"

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, data, *args):
        self.f.write_bytes(data)
        return subprocess.run([sys.executable, str(TOOL), str(self.f), *args], capture_output=True, text=True, timeout=30)

    def test_a_whole_boot0_with_the_promised_clock_passes(self):
        r = self.check(region(2040), "2040")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout.count("valid, memory clock 2040 MHz"), 2)
        self.assertEqual(self.check(region(2136)).returncode, 0)          # no clock asked for: whole is enough

    def test_the_other_variants_boot0_is_refused(self):
        r = self.check(region(2136), "2040")
        self.assertEqual(r.returncode, 1)
        self.assertIn("memory clock 2136 MHz, not 2040", r.stdout)

    def test_a_changed_byte_is_noticed_in_either_copy(self):
        for sector in (256, 2064):
            d = region(2040)
            d[sector * 512 + 2 * 512 + 116] ^= 0x60                       # the clock patched without the checksum
            r = self.check(d, "2040")
            self.assertEqual(r.returncode, 1, sector)
            self.assertIn("checksum does not match", r.stdout)

    def test_something_that_is_not_a_firmware_region(self):
        self.assertEqual(self.check(bytes(16 * 1024 * 1024), "2040").returncode, 1)
        self.assertEqual(self.check(b"short").returncode, 1)
        d = region(2040)
        d[256 * 512 + 16: 256 * 512 + 20] = struct.pack("<I", 0x7FFFFFFC)   # a length beyond the file
        self.assertEqual(self.check(d).returncode, 1)


if __name__ == "__main__":
    unittest.main()
