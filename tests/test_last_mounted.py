"""tools/ext4-last-mounted.sh: the superblock of an image must not name the directory it was built in."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "ext4-last-mounted.sh"
OFFSET = 1048576                       # the filesystem does not start at byte 0 in an image either
BUILD_PATH = "/home/owner/Projects/a7a-fedora/output/mnt/mroot"


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("debugfs") and shutil.which("tune2fs"), "e2fsprogs needed")
class TestLastMounted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = Path(self.tmp.name) / "fs.img"
        with open(self.img, "wb") as f:
            f.truncate(64 * 1024 * 1024)
        subprocess.run(["mkfs.ext4", "-q", "-F", "-E", f"offset={OFFSET}", str(self.img), "60M"], check=True, capture_output=True)
        # what the kernel leaves behind after the build's mount: the mount point, NUL-padded
        subprocess.run(["debugfs", "-w", "-R", f"ssv last_mounted {BUILD_PATH}", f"{self.img}?offset={OFFSET}"], check=True, capture_output=True)
        self.assertIn(BUILD_PATH.encode(), self.field())

    def tearDown(self):
        self.tmp.cleanup()

    def field(self) -> bytes:
        with open(self.img, "rb") as f:
            f.seek(OFFSET + 1024 + 136)
            return f.read(64)

    def test_the_whole_field_is_cleared_not_its_first_two_bytes(self):
        r = subprocess.run(["bash", str(SCRIPT), str(self.img), str(OFFSET)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.field(), b"/" + b"\0" * 63)
        self.assertNotIn(b"owner", self.img.read_bytes())
        # and the filesystem is still sound (the superblock carries a checksum)
        fsck = subprocess.run(["e2fsck", "-fn", f"{self.img}?offset={OFFSET}"], capture_output=True, text=True)
        self.assertEqual(fsck.returncode, 0, fsck.stdout + fsck.stderr)

    def test_what_debugfs_does_is_not_enough_and_is_noticed(self):
        # the way it was done until 2026-11-16: two bytes written, the rest of the path left, and debugfs reads "/" back
        subprocess.run(["debugfs", "-w", "-R", "ssv last_mounted /", f"{self.img}?offset={OFFSET}"], check=True, capture_output=True)
        shown = subprocess.run(["debugfs", "-R", "show_super_stats -h", f"{self.img}?offset={OFFSET}"], capture_output=True, text=True).stdout
        self.assertIn("Last mounted on:          /\n", shown)
        self.assertIn(BUILD_PATH[2:].encode(), self.field())
        # the script's own check reads the bytes: with tune2fs taken out of it, it must refuse
        broken = Path(self.tmp.name) / "no-tune2fs.sh"
        broken.write_text(SCRIPT.read_text().replace('tune2fs -M / "$IMG?offset=$OFF" > /dev/null', "true"))
        r = subprocess.run(["bash", str(broken), str(self.img), str(OFFSET)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not NUL", r.stdout)

    def test_refuses_what_is_not_an_image_file(self):
        self.assertEqual(subprocess.run(["bash", str(SCRIPT), "/dev/null", "0"], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run(["bash", str(SCRIPT), str(self.img), "12k"], capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
