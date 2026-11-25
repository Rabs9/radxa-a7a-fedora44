"""Tests for the rootfs stage: the official Fedora raw image, downloaded, checksummed, extracted."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import rootfs


class TestRootfs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.fedora_gpg_key = None
        self.reg = StageRegistry(self.cfg.registry_path)
        # a tiny "raw image" compressed with real xz, served over file:// (curl handles it, no network)
        up = root / "upstream"; up.mkdir()
        raw = up / "Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw"
        raw.write_bytes(b"\0" * 4096 + b"fedora" + b"\0" * 4096)
        subprocess.run(["xz", "-k", str(raw)], check=True)
        self.xz = raw.with_suffix(".raw.xz")
        self.sha = hashlib.sha256(self.xz.read_bytes()).hexdigest()
        self.checksum = up / "CHECKSUM"
        self.checksum.write_text(f"# Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw.xz: {self.xz.stat().st_size} bytes\n"
                                 f"SHA256 (Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw.xz) = {self.sha}\n")
        self.cfg.fedora_url = f"file://{self.xz}"
        self.cfg.fedora_checksum_url = f"file://{self.checksum}"

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_verify_extract(self):
        r = rootfs.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue(self.cfg.fedora_raw.exists())
        self.assertEqual(r["checksum"], "verified")
        self.assertEqual(r["sha256"], self.sha)
        self.assertEqual(r["signature"], "not signed")
        self.assertEqual(r["raw_bytes"], 4096 * 2 + 6)
        argvs = [c["argv"][0] for c in self.reg.get_stage("rootfs")["commands"]]
        self.assertEqual(argvs, ["curl", "curl", "xz"])
        self.assertEqual(self.reg.get_stage("rootfs")["status"], "passed")

    def test_existing_raw_means_nothing_is_downloaded(self):
        self.cfg.output_dir.mkdir()
        self.cfg.fedora_raw.write_bytes(b"\0" * 100)
        r = rootfs.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["checksum"], "skipped (raw already present)")
        self.assertIsNone(self.reg.get_stage("rootfs").get("commands") or None)

    def test_checksum_mismatch_fails_and_quarantines_the_download(self):
        self.checksum.write_text(f"SHA256 (Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw.xz) = {'0' * 64}\n")
        r = rootfs.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("checksum", r["reason"])
        self.assertFalse(self.cfg.fedora_raw.exists())
        self.assertFalse((self.cfg.output_dir / self.xz.name).exists())
        self.assertTrue((self.cfg.output_dir / (self.xz.name + ".bad")).exists())

    def test_checksum_file_without_our_image_fails(self):
        self.checksum.write_text("SHA256 (Other.raw.xz) = " + "1" * 64 + "\n")
        r = rootfs.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("no entry", r["reason"])

    def test_download_failure_is_reported(self):
        self.cfg.fedora_url = f"file://{self.tmp.name}/absent.raw.xz"
        r = rootfs.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("download", r["reason"])

    def test_dry_run_lists_the_commands(self):
        r = rootfs.run(self.cfg, self.reg, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertEqual(len(r["commands"]), 3)
        self.assertIn("curl", r["commands"][0])
        self.assertFalse(self.cfg.fedora_raw.exists())

    def test_parse_checksum_line(self):
        text = "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\n# x\nSHA256 (a.raw.xz) = " + "a" * 64 + "\n-----BEGIN PGP SIGNATURE-----\n"
        self.assertEqual(rootfs.expected_sha256(text, "a.raw.xz"), "a" * 64)
        self.assertIsNone(rootfs.expected_sha256(text, "b.raw.xz"))
        self.assertTrue(rootfs.is_signed(text))


if __name__ == "__main__":
    unittest.main()
