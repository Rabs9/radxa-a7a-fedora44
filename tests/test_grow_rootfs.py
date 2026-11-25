"""Tests for tools/board/a7a-grow-rootfs: the first-boot step that grows the root partition and its ext4 to the end of
the card, so an image written with any imager (not only our flash stage, which grows it on the laptop) fills the card.
findmnt/lsblk/blockdev/sfdisk/partx/resize2fs are faked on PATH; A7A_GROW_ROOT is a fake root with the sysfs files."""
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "board" / "a7a-grow-rootfs"

# sizes in 512-byte sectors, as sysfs and blockdev --getsz give them
P3_START = 679936
IMAGE_SECTORS = 12 * 1024 * 1024 * 2          # the 12 GB image
CARD_SECTORS = 244277248                      # the 125 GB card

FAKES = {
    "findmnt": '#!/bin/bash\ncat "$A7A_GROW_ROOT/state/rootdev"\n',
    "lsblk": '#!/bin/bash\necho "lsblk $*" >> "$A7A_GROW_ROOT/calls.log"\ncat "$A7A_GROW_ROOT/state/pkname"\n',
    "blockdev": '#!/bin/bash\ncat "$A7A_GROW_ROOT/state/disk-sectors"\n',
    # sfdisk: --relocate succeeds silently; a grow of partition N rewrites that partition's sysfs size the way the new
    # table would read (everything up to the end of the disk minus the 33 sectors of the backup GPT), unless told to fail
    "sfdisk": r"""#!/bin/bash
    R="$A7A_GROW_ROOT"; echo "sfdisk $*" >> "$R/calls.log"
    case " $* " in *" --relocate "*) exit 0 ;; esac
    [ -e "$R/state/sfdisk-fails" ] && exit 1
    cat > "$R/state/sfdisk-stdin"
    echo $(( $(cat "$R/state/disk-sectors") - 33 - $(cat "$R/state/start") )) > "$R/state/table-size"
    """,
    # partx: the kernel takes the new size (sysfs follows the table), unless told to fail
    "partx": r"""#!/bin/bash
    R="$A7A_GROW_ROOT"; echo "partx $*" >> "$R/calls.log"
    [ -e "$R/state/partx-fails" ] && exit 1
    cp "$R/state/table-size" "$R/sys/block/mmcblk0/mmcblk0p3/size"
    """,
    "resize2fs": '#!/bin/bash\necho "resize2fs $*" >> "$A7A_GROW_ROOT/calls.log"\n[ ! -e "$A7A_GROW_ROOT/state/resize2fs-fails" ]\n',
}


class TestGrowRootfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text(textwrap.dedent(body))
            f.chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir(parents=True)
        self.card(root_sectors=IMAGE_SECTORS - 33 - P3_START, disk_sectors=CARD_SECTORS)

    def tearDown(self):
        self.tmp.cleanup()

    def card(self, root_sectors, disk_sectors, extra_partition_after=False):
        """mmcblk0 with the image's three partitions: config, boot, rootfs (the last one, mounted on /)."""
        blk = self.root / "sys/block/mmcblk0"
        parts = {"mmcblk0p1": (1, 32768, 32768), "mmcblk0p2": (2, 65536, 614400), "mmcblk0p3": (3, P3_START, root_sectors)}
        if extra_partition_after:
            parts["mmcblk0p4"] = (4, P3_START + root_sectors, 2048)
        for name, (num, start, size) in parts.items():
            d = blk / name
            d.mkdir(parents=True, exist_ok=True)
            (d / "partition").write_text(f"{num}\n"); (d / "start").write_text(f"{start}\n"); (d / "size").write_text(f"{size}\n")
        (self.state / "rootdev").write_text("/dev/mmcblk0p3\n")
        (self.state / "pkname").write_text("mmcblk0\n")
        (self.state / "disk-sectors").write_text(f"{disk_sectors}\n")
        (self.state / "start").write_text(f"{P3_START}\n")

    def run_tool(self):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", A7A_GROW_ROOT=str(self.root))
        return subprocess.run([str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60)

    def calls(self, prefix=""):
        f = self.root / "calls.log"
        return [c for c in (f.read_text().splitlines() if f.exists() else []) if c.startswith(prefix)]

    def marker(self):
        return self.root / "var/lib/a7a/rootfs-grown"

    def test_grows_the_partition_and_the_filesystem_to_the_end_of_the_card(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        grow = [c for c in self.calls("sfdisk") if "--relocate" not in c]
        self.assertEqual(len(grow), 1, self.calls())
        self.assertIn("-N 3", grow[0])
        self.assertIn("--no-reread", grow[0])          # the disk is in use: no BLKRRPART, the busy check would refuse
        self.assertTrue(grow[0].endswith("/dev/mmcblk0"), grow[0])
        self.assertEqual((self.state / "sfdisk-stdin").read_text().strip(), ", +")   # same start, all the free space
        self.assertEqual(self.calls("partx"), ["partx -u --nr 3 /dev/mmcblk0"])
        self.assertEqual(self.calls("resize2fs"), ["resize2fs /dev/mmcblk0p3"])
        # order: the table, the kernel's view, the filesystem
        order = [c.split()[0] for c in self.calls() if c.split()[0] in ("sfdisk", "partx", "resize2fs") and "--relocate" not in c]
        self.assertEqual(order, ["sfdisk", "partx", "resize2fs"])
        self.assertTrue(self.marker().exists())
        self.assertIn("grown", self.marker().read_text())

    def test_second_boot_does_nothing(self):
        self.assertEqual(self.run_tool().returncode, 0)
        (self.root / "calls.log").unlink()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertIn("already", r.stdout)

    def test_card_already_filled_only_checks_the_filesystem(self):
        # written by our flash stage (it grows p3 on the laptop): no table change, but resize2fs still runs, so a
        # first boot that lost power between the two steps is completed by the next one
        self.card(root_sectors=CARD_SECTORS - 33 - P3_START, disk_sectors=CARD_SECTORS)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls("sfdisk") if "--relocate" not in c], [])
        self.assertEqual(self.calls("partx"), [])
        self.assertEqual(self.calls("resize2fs"), ["resize2fs /dev/mmcblk0p3"])
        self.assertTrue(self.marker().exists())

    def test_root_that_is_not_the_last_partition_is_left_alone(self):
        self.card(root_sectors=IMAGE_SECTORS - 33 - P3_START, disk_sectors=CARD_SECTORS, extra_partition_after=True)
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([c for c in self.calls("sfdisk")], [])
        self.assertEqual(self.calls("partx"), [])
        self.assertIn("not the last partition", r.stdout)
        self.assertTrue(self.marker().exists())

    def test_failed_table_change_is_retried_at_the_next_boot(self):
        (self.state / "sfdisk-fails").touch()
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.calls("resize2fs"), [])
        self.assertFalse(self.marker().exists())

    def test_kernel_that_does_not_take_the_new_size_finishes_at_the_next_boot(self):
        # the table is written, partx could not tell the kernel: resize2fs has nothing to grow into yet, and the
        # marker must not be set, so the next boot (which reads the new table) runs resize2fs
        (self.state / "partx-fails").touch()
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls("resize2fs"), [])
        self.assertFalse(self.marker().exists())
        self.assertIn("next boot", r.stdout)

    def test_failed_filesystem_resize_is_retried(self):
        (self.state / "resize2fs-fails").touch()
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse(self.marker().exists())

    def test_unknown_disk_size_is_not_taken_for_a_full_card(self):
        # blockdev printing nothing made the free space negative = "nothing to grow", marked done at 12 GB for good
        (self.state / "disk-sectors").write_text("")
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual([c for c in self.calls() if c.split()[0] in ("sfdisk", "partx", "resize2fs")], [])
        self.assertFalse(self.marker().exists())

    def test_table_change_that_changed_nothing_is_not_reported_as_grown(self):
        # sfdisk exits 0 but the partition is as big as before (the kernel's view after partx): no marker, try again
        (self.bin / "partx").write_text('#!/bin/bash\necho "partx $*" >> "$A7A_GROW_ROOT/calls.log"\n')   # size unchanged
        r = self.run_tool()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.calls("resize2fs"), [])
        self.assertFalse(self.marker().exists())
        self.assertIn("did not grow", r.stdout)

    def test_root_that_is_no_partition_is_not_touched(self):
        (self.state / "rootdev").write_text("/dev/mapper/root\n")
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([c for c in self.calls() if not c.startswith("lsblk")], [])
        self.assertTrue(self.marker().exists())


if __name__ == "__main__":
    unittest.main()
