"""Tests for src/cli.py: every stage has a subcommand, --json and --dry-run work, exit codes mean something."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from src.cli import cli


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfgfile = root / "a7a-fedora.json"
        self.cfg = {"project_root": str(root), "output_dir": str(root / "output"), "work_dir": str(root / "work"),
                    "mirror_dir": str(root / "mirror"), "tools_dir": str(root / "tools"), "registry_path": str(root / "registry.json"),
                    "sudo_prefix": [], "fedora_gpg_key": None,
                    # no network in tests: every upstream points at a path that does not exist
                    "mirror_repo": "file:///nonexistent/mirror", "kernel_repo": "file:///nonexistent/kernel", "bsp_repo": "file:///nonexistent/bsp",
                    "device_repo": "file:///nonexistent/device", "target_repo": "file:///nonexistent/target"}
        self.cfgfile.write_text(json.dumps(self.cfg))
        (root / "output").mkdir()
        (root / "tools").mkdir()
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return self.runner.invoke(cli, ["--config", str(self.cfgfile), *args], catch_exceptions=False)

    def test_help_lists_every_stage(self):
        r = self.run_cli("--help")
        self.assertEqual(r.exit_code, 0)
        for cmd in ["sources", "kernel", "rootfs", "image", "emulate", "flash", "serial", "update", "all", "status", "images", "candidates", "dashboard", "mcp"]:
            self.assertIn(cmd, r.stdout)

    def test_status_json(self):
        r = self.run_cli("--json", "status")
        self.assertEqual(r.exit_code, 0, r.stdout)
        data = json.loads(r.stdout)
        self.assertIn("stages", data)
        self.assertIn("images", data)

    def test_dry_run_sources_prints_commands_and_exits_zero(self):
        r = self.run_cli("--json", "--dry-run", "sources")
        self.assertEqual(r.exit_code, 0, r.stdout)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "dry_run")
        self.assertTrue(any("git clone" in c for c in data["commands"]))
        self.assertFalse((self.root / "work" / "kernel-6.6").exists())

    def test_emulate_with_a_passing_stub(self):
        img = self.root / "output" / "a7a-fedora-44-kde-20260906.img"
        img.write_bytes(b"\0")
        _stub(self.root / "tools" / "emulate.sh", 'echo "  milestone: login:"; echo "VERDICT: PASS - ok"\n')
        r = self.run_cli("--json", "emulate", "--mode", "kernel")
        self.assertEqual(r.exit_code, 0, r.stdout)
        self.assertEqual(json.loads(r.stdout)["verdict"], "PASS")
        r = self.run_cli("images")
        self.assertIn("a7a-fedora-44-kde-20260906.img", r.stdout)
        self.assertIn("PASS", r.stdout)

    def test_failed_stage_exits_one_with_the_reason(self):
        r = self.run_cli("--json", "kernel")            # no source tree in the fixture
        self.assertEqual(r.exit_code, 1)
        self.assertIn("sources", json.loads(r.stdout)["reason"])
        r = self.run_cli("kernel")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("failed", r.stdout)

    def test_flash_refuses_without_confirmation_exit_two(self):
        img = self.root / "output" / "a7a-fedora-44-kde-20260906.img"
        img.write_bytes(b"\0")
        lsblk = _stub(self.root / "lsblk", 'echo \'{"blockdevices": []}\'\n')
        self.cfg["lsblk_argv"] = [str(lsblk)]
        self.cfgfile.write_text(json.dumps(self.cfg))
        r = self.run_cli("--json", "flash", "--device", "/dev/sdz", "--force")
        self.assertEqual(r.exit_code, 2, r.stdout)
        self.assertEqual(json.loads(r.stdout)["status"], "refused")

    def test_candidates_json(self):
        lsblk = _stub(self.root / "lsblk", 'echo \'{"blockdevices": [{"name":"sdz","path":"/dev/sdz","size":64000000000,"model":"Reader","tran":"usb","rm":true,"hotplug":true,"type":"disk","mountpoints":[null]}]}\'\n')
        self.cfg["lsblk_argv"] = [str(lsblk)]
        self.cfg["root_device"] = "/dev/nvme0n1"
        self.cfgfile.write_text(json.dumps(self.cfg))
        r = self.run_cli("--json", "candidates")
        self.assertEqual(r.exit_code, 0, r.stdout)
        self.assertEqual(json.loads(r.stdout)[0]["path"], "/dev/sdz")

    def test_all_stops_at_the_first_failure_and_never_flashes(self):
        r = self.run_cli("--json", "--dry-run", "all")
        self.assertEqual(r.exit_code, 0, r.stdout)
        data = json.loads(r.stdout)
        self.assertEqual([s["stage"] for s in data["stages"]], ["sources", "kernel", "rootfs", "image", "emulate"])
        r = self.run_cli("--json", "all")
        self.assertEqual(r.exit_code, 1)
        data = json.loads(r.stdout)
        self.assertEqual(data["stopped_at"], "sources")

    def test_serial_without_port(self):
        r = self.run_cli("--json", "serial")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("serial_port", json.loads(r.stdout)["reason"])

    def test_config_option_overrides(self):
        r = self.run_cli("--json", "config")
        self.assertEqual(json.loads(r.stdout)["tools_dir"], str(self.root / "tools"))


if __name__ == "__main__":
    unittest.main()
