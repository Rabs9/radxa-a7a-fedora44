"""Tests for the flash stage: candidates, the gates, the backup, the write through tools/priv.sh."""

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import flash


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


LSBLK = {"blockdevices": [
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 1000000000000, "model": "Samsung SSD", "tran": "nvme", "rm": False, "hotplug": False, "type": "disk",
     "mountpoints": [None], "children": [{"name": "nvme0n1p3", "path": "/dev/nvme0n1p3", "type": "part", "mountpoints": ["/"]}]},
    {"name": "sdz", "path": "/dev/sdz", "size": 64000000000, "model": "SD Reader", "tran": "usb", "rm": True, "hotplug": True, "type": "disk",
     "mountpoints": [None], "children": [{"name": "sdz1", "path": "/dev/sdz1", "type": "part", "mountpoints": [None], "start": 2048, "size": 1000000},
                                          {"name": "sdz2", "path": "/dev/sdz2", "type": "part", "mountpoints": [None], "start": 4096, "size": 2000000}]},
    {"name": "sdy", "path": "/dev/sdy", "size": 32000000000, "model": "Mounted Card", "tran": "usb", "rm": True, "hotplug": True, "type": "disk",
     "mountpoints": [None], "children": [{"name": "sdy1", "path": "/dev/sdy1", "type": "part", "mountpoints": ["/run/media/x"]}]},
    {"name": "sdx", "path": "/dev/sdx", "size": 100, "model": "Tiny Card", "tran": "usb", "rm": True, "hotplug": True, "type": "disk",
     "mountpoints": [None]},
    {"name": "loop0", "path": "/dev/loop0", "size": 4096, "model": None, "tran": None, "rm": False, "hotplug": False, "type": "loop", "mountpoints": [None]},
]}

# the fake privileged helper: `flash img dev` copies img over the "device" file, `read-card dev out` the reverse
FAKE_PRIV = r'''
case "$1" in
  "") exit 2 ;;
  flash) cp "$2" "$3"; echo "verified $(stat -c %s "$2") bytes" ;;
  read-card) if [ -n "$4" ]; then head -c "$4" "$2" > "$3"; else cp "$2" "$3"; fi; echo "read $(stat -c %s "$3") bytes" ;;
  grow-rootfs) echo "grown" >> "$2"; echo "rootfs: 64000000000 bytes" ;;
  *) echo "priv: unknown $1" >&2; exit 2 ;;
esac
'''


class TestFlash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.sudo_prefix = []
        self.cfg.root_device = "/dev/nvme0n1"
        self.reg = StageRegistry(self.cfg.registry_path)
        self.cfg.output_dir.mkdir()
        lsblk = _stub(root / "lsblk", "cat <<'J'\n" + json.dumps(LSBLK) + "\nJ\n")
        self.cfg.lsblk_argv = [str(lsblk)]
        _stub(self.cfg.tools_dir / "priv.sh", FAKE_PRIV)
        self.img = self.cfg.output_dir / "a7a-fedora-44-kde-20260906.img"
        self.img.write_bytes(b"image-bytes" * 100)
        self.sha = hashlib.sha256(self.img.read_bytes()).hexdigest()
        self.reg.record_image(self.img.name, {"size": self.img.stat().st_size, "sha256": self.sha})
        self.reg.record_test(self.img.name, mode="kernel", verdict="PASS", log="", sha256=self.sha)
        # the "device" is a file the stub copies to; the stage treats /dev/sdz as its path, so map it
        self.dev = root / "sdz"
        self.cfg.device_path_override = {"/dev/sdz": str(self.dev)}
        self.dev.write_bytes(b"old-debian-card")

    def tearDown(self):
        self.tmp.cleanup()

    def test_candidates_are_removable_whole_disks_only(self):
        c = flash.candidates(self.cfg)
        self.assertEqual([d["path"] for d in c], ["/dev/sdz", "/dev/sdy", "/dev/sdx"])
        self.assertEqual(c[0]["model"], "SD Reader")
        self.assertEqual(c[0]["mounted"], [])
        self.assertEqual(c[1]["mounted"], ["/run/media/x"])
        self.assertEqual(c[0]["size"], 64000000000)

    def test_refuses_untested_image_unless_forced(self):
        self.reg.images()[self.img.name]["tests"] = []
        self.reg.save()
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)
        self.assertEqual(r["status"], "refused")
        self.assertIn("emulator", r["reason"])
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False, force=True)
        self.assertEqual(r["status"], "passed", r)

    def test_refuses_a_file_that_is_not_the_bytes_that_were_tested(self):
        # the image stage deletes and recreates the file under its dated name; a rebuild that fails half way leaves a
        # partial file whose name still has a PASS in the registry (which only a finished build resets)
        self.assertNotEqual(flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)["status"], "refused")
        self.img.write_bytes(b"half a rebuild")
        self.dev.write_bytes(b"old-debian-card")
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("not the image that was tested", r["reason"])
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")

    def test_refuses_when_nothing_says_which_bytes_were_tested(self):
        # a record from before 2026-11-16: a PASS for a name, no checksum of the build and none in the test. The name
        # alone is what let a half-written file through
        self.reg.images()[self.img.name].pop("sha256")
        self.reg.images()[self.img.name]["tests"][-1].pop("sha256")
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("which bytes", r["reason"])
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")
        # the checksum of the build is enough for a test recorded without one (the registry forgets the tests when it changes)
        self.reg.images()[self.img.name]["sha256"] = self.sha
        self.assertEqual(flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)["status"], "passed")

    def test_the_last_test_counts_not_an_earlier_pass(self):
        self.reg.record_test(self.img.name, mode="kernel", verdict="FAIL", log="", sha256=self.sha)
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("emulator", r["reason"])
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")

    def test_a_file_that_changes_while_the_card_is_read_is_not_written(self):
        # the backup of a 128 GB card takes two hours on a USB 2.0 reader: time enough for a build to replace the file
        _stub(self.cfg.tools_dir / "priv.sh", FAKE_PRIV.replace('read-card) ', f'read-card) echo rebuilt >> "{self.img}"; '))
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz")
        self.assertEqual(r["status"], "refused", r)
        self.assertIn("not the image that was tested", r["reason"])
        self.assertIn("backup", r["reason"])
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")
        self.assertTrue(Path(r["backup"]).exists())

    def test_refuses_bad_devices(self):
        for dev, why in [("/dev/nvme0n1", "root"), ("/dev/sdy", "mounted"), ("/dev/sdx", "small"), ("/dev/sdq", "candidate")]:
            r = flash.run(self.cfg, self.reg, image=self.img, device=dev, confirm=dev.split("/")[-1], backup=False)
            self.assertEqual(r["status"], "refused", dev)
            self.assertIn(why, r["reason"], dev)

    def test_refuses_without_the_typed_confirmation(self):
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm=None, backup=False)
        self.assertEqual(r["status"], "refused")
        self.assertIn("type sdz", r["reason"])
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdy", backup=False)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")

    def test_flash_with_backup_verifies_and_records(self):
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz")
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue(self.dev.read_bytes().startswith(self.img.read_bytes()))
        self.assertTrue(r["verified"])
        self.assertTrue(r["backup"].endswith(".img.xz"))
        self.assertTrue(Path(r["backup"]).exists())
        self.assertFalse(Path(r["backup"][:-3]).exists())            # the raw copy is gone after xz
        f = self.reg.images()[self.img.name]["flashes"][0]
        self.assertEqual(f["device"], "/dev/sdz")
        self.assertTrue(f["verified"])
        self.assertEqual(f["backup"], r["backup"])
        self.assertEqual(self.reg.data["backups"][0]["device"], "/dev/sdz")
        ops = [c["argv"][1] for c in self.reg.get_stage("flash")["commands"] if c["argv"][0].endswith("priv.sh")]
        self.assertEqual(ops, ["read-card", "flash", "grow-rootfs"])
        self.assertTrue(r["grown"])
        self.assertTrue(self.dev.read_bytes().endswith(b"grown\n"))

    def test_backup_reads_only_up_to_the_last_partition(self):
        self.dev.write_bytes(b"x" * 5_000_000)
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz")
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["backup_bytes"], 4096 * 512 + 2000000)
        ops = [c["argv"] for c in self.reg.get_stage("flash")["commands"] if c["argv"][0].endswith("priv.sh") and c["argv"][1] == "read-card"]
        self.assertEqual(ops[0][-1], str(4096 * 512 + 2000000))
        self.assertEqual(flash.candidates(self.cfg)[0]["partitioned_bytes"], 4096 * 512 + 2000000)

    def test_backup_name_is_used_and_never_overwritten(self):
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup_name="deb 13 a7a")
        self.assertTrue(r["backup"].endswith("/deb-13-a7a.img.xz"), r["backup"])
        r2 = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup_name="deb 13 a7a")
        self.assertNotEqual(r2["backup"], r["backup"])
        self.assertIn("deb-13-a7a-", r2["backup"])

    def test_existing_backup_is_recorded_and_the_card_not_read(self):
        (self.cfg.output_dir / "backups").mkdir()
        eb = self.cfg.output_dir / "backups" / "deb-13-a7a.img.xz"
        eb.write_bytes(b"xz")
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", existing_backup="deb-13-a7a.img.xz")
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["backup"], str(eb))
        ops = [c["argv"][1] for c in self.reg.get_stage("flash")["commands"] if c["argv"][0].endswith("priv.sh")]
        self.assertEqual(ops, ["flash", "grow-rootfs"])
        self.assertTrue(self.reg.data["backups"][0]["reused"])
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", existing_backup="nope.img.xz")
        self.assertEqual(r["status"], "refused")

    def test_xz_warning_exit_2_still_counts_when_the_archive_exists(self):
        _stub(self.cfg.tools_dir / "priv.sh", FAKE_PRIV)
        xz = _stub(Path(self.tmp.name) / "xz", 'out="${@: -1}"; cp "$out" "$out.xz"; echo "xz: cannot set the file group" >&2; exit 2\n')
        os.environ["PATH"] = f"{Path(self.tmp.name)}:{os.environ['PATH']}"
        try:
            r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup_name="w")
        finally:
            os.environ["PATH"] = os.environ["PATH"].split(":", 1)[1]
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue(Path(r["backup"]).exists())
        self.assertIn("xz warned", r["warnings"][0])

    def test_no_backup(self):
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False, grow=False)
        self.assertEqual(r["status"], "passed")
        self.assertIsNone(r["backup"])
        self.assertEqual(self.dev.read_bytes(), self.img.read_bytes())
        self.assertNotIn("grown", r)

    def test_dry_run_shows_the_plan_and_touches_nothing(self):
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual(self.dev.read_bytes(), b"old-debian-card")
        self.assertTrue(any("priv.sh flash" in c for c in r["commands"]))
        self.assertIn("/dev/sdz", r["plan"]["commands"][1])
        self.assertEqual(r["plan"]["device"]["model"], "SD Reader")

    def test_helper_failure_is_not_verified(self):
        _stub(self.cfg.tools_dir / "priv.sh", '[ -z "$1" ] && exit 2; echo "priv: verification failed" >&2; exit 2\n')
        r = flash.run(self.cfg, self.reg, image=self.img, device="/dev/sdz", confirm="sdz", backup=False)
        self.assertEqual(r["status"], "failed")
        self.assertFalse(r["verified"])
        self.assertFalse(self.reg.images()[self.img.name]["flashes"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
