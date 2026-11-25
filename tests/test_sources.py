"""Tests for the sources stage: mirror + four Radxa trees, pinned by work/manifest.json."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import sources


def _git(*args, cwd):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


def _repo(path: Path, branch: str, remote_name=None) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", branch, cwd=path)
    (path / "README").write_text("x\n")
    _git("add", ".", cwd=path)
    _git("commit", "-q", "-m", "one", cwd=path)
    if remote_name:
        _git("remote", "add", "origin", remote_name, cwd=path)
    return path


class TestSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          mirror_dir=root / "mirror", tools_dir=root / "tools", registry_path=root / "registry.json")
        self.reg = StageRegistry(self.cfg.registry_path)
        # upstreams the stage may clone from (local, so no network)
        up = root / "upstream"
        self.cfg.mirror_repo = f"file://{_repo(up / 'rabs9', 'main')}"
        self.cfg.kernel_repo = f"file://{_repo(up / 'kernel', self.cfg.kernel_branch)}"
        self.cfg.bsp_repo = f"file://{_repo(up / 'bsp', self.cfg.bsp_branch)}"
        self.cfg.device_repo = f"file://{_repo(up / 'device', self.cfg.device_branch)}"
        self.cfg.target_repo = f"file://{_repo(up / 'target', self.cfg.target_branch)}"

    def tearDown(self):
        self.tmp.cleanup()

    def _all_present(self):
        _repo(self.cfg.mirror_dir, "main", "https://github.com/Rabs9/radxa-cubie-a7a-kernel")
        for name, _repo_url, branch in sources.trees(self.cfg):
            _repo(self.cfg.work_dir / name, branch)

    def test_first_run_writes_manifest_with_every_commit(self):
        self._all_present()
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        m = json.loads(self.cfg.manifest_path.read_text())
        self.assertEqual(set(m["trees"]), {"kernel-6.6", "allwinner-bsp-1.4.8", "allwinner-device-1.4.8", "allwinner-target"})
        for name, info in m["trees"].items():
            self.assertRegex(info["commit"], r"^[0-9a-f]{40}$")
            self.assertEqual(info["commit"], _git("rev-parse", "HEAD", cwd=self.cfg.work_dir / name))
        self.assertRegex(m["mirror"]["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(self.reg.get_stage("sources")["status"], "passed")

    def test_refuses_a_tree_whose_commit_drifted(self):
        self._all_present()
        sources.run(self.cfg, self.reg)
        k = self.cfg.work_dir / "kernel-6.6"
        (k / "new").write_text("y")
        _git("add", ".", cwd=k)
        _git("commit", "-q", "-m", "two", cwd=k)
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["drift"], ["kernel-6.6"])
        self.assertIn("update", r["reason"])
        self.assertEqual(self.reg.get_stage("sources")["status"], "failed")

    def test_update_sources_accepts_the_drift_and_rewrites_the_manifest(self):
        self._all_present()
        sources.run(self.cfg, self.reg)
        k = self.cfg.work_dir / "kernel-6.6"
        (k / "new").write_text("y")
        _git("add", ".", cwd=k)
        _git("commit", "-q", "-m", "two", cwd=k)
        r = sources.run(self.cfg, self.reg, update=True)
        self.assertEqual(r["status"], "passed")
        m = json.loads(self.cfg.manifest_path.read_text())
        self.assertEqual(m["trees"]["kernel-6.6"]["commit"], _git("rev-parse", "HEAD", cwd=k))

    def test_clones_missing_trees_from_the_configured_repos(self):
        _repo(self.cfg.mirror_dir, "main", "https://github.com/Rabs9/radxa-cubie-a7a-kernel")
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        for name, _u, branch in sources.trees(self.cfg):
            self.assertTrue((self.cfg.work_dir / name / ".git").exists(), name)
            self.assertEqual(_git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.cfg.work_dir / name), branch)
        argvs = [c["argv"] for c in self.reg.get_stage("sources")["commands"]]
        self.assertTrue(any(a[:2] == ["git", "clone"] for a in argvs))

    def test_clones_the_mirror_too(self):
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue((self.cfg.mirror_dir / ".git").exists())

    def test_dry_run_clones_nothing_and_lists_the_commands(self):
        r = sources.run(self.cfg, self.reg, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertFalse(self.cfg.work_dir.joinpath("kernel-6.6").exists())
        self.assertTrue(any("git clone" in c for c in r["commands"]))
        self.assertTrue(all(c.get("dry_run") for c in self.reg.get_stage("sources")["commands"]))

    def test_wrong_branch_is_reported(self):
        self._all_present()
        d = self.cfg.work_dir / "allwinner-target"
        _git("checkout", "-q", "-b", "other", cwd=d)
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["trees"]["allwinner-target"]["branch"], "other")
        self.assertIn("allwinner-target", r["warnings"][0])

    def test_patch_state_is_reported(self):
        self._all_present()
        r = sources.run(self.cfg, self.reg)
        self.assertFalse(r["patches"]["applied"])
        (self.cfg.work_dir / ".patches-applied").write_text("")
        r = sources.run(self.cfg, self.reg)
        self.assertTrue(r["patches"]["applied"])

    def test_patch_checks_read_the_trees(self):
        self._all_present()
        dts = self.cfg.work_dir / "allwinner-device-1.4.8" / "configs" / "cubie_a7a" / "linux-6.6" / "board.dts"
        dts.parent.mkdir(parents=True)
        dts.write_text("tx-delay = <9>;\n")
        c = self.cfg.work_dir / "kernel-6.6" / "drivers" / "cpufreq" / "cpufreq-dt-platdev.c"
        c.parent.mkdir(parents=True)
        c.write_text('{ .compatible = "allwinner,sun50i-h6", },\n')
        r = sources.run(self.cfg, self.reg)
        self.assertEqual(r["patches"]["checks"]["rgmii-tx-delay"], "applied")
        self.assertEqual(r["patches"]["checks"]["cpufreq-blocklist"], "not applied")

    def test_radxa_fix_checks_read_the_trees(self):
        self._all_present()
        inet = self.cfg.work_dir / "kernel-6.6" / "net" / "ipv4" / "af_inet.c"
        inet.parent.mkdir(parents=True)
        inet.write_text("\tkfree(rcu_dereference_protected(inet->inet_opt, 1));\n")
        tcp = inet.with_name("tcp_ipv4.c")
        tcp.write_text("\tkfree(rcu_dereference_protected(inet_rsk(req)->ireq_opt, 1));\n")
        i2cm = self.cfg.work_dir / "allwinner-bsp-1.4.8" / "drivers" / "drm" / "sunxi_device" / "hardware" / "lowlevel_hdmi20" / "dw_i2cm.c"
        i2cm.parent.mkdir(parents=True)
        i2cm.write_text("\t\t\t\tlength++;\n\t\t\t\ti2cm->slave_reg--;\n")
        dtsi = self.cfg.work_dir / "kernel-6.6" / "arch" / "arm64" / "boot" / "dts" / "allwinner" / "sun60iw2p1.dtsi"
        dtsi.parent.mkdir(parents=True)
        zones = ("cpul_thermal_zone: cpul_thermal_zone {\n\tpolling-delay = <1000>;\n%s\n\tcpul_trips: trips {\n\t};\n};\n"
                 "cpub_thermal_zone: cpub_thermal_zone {\n\tpolling-delay = <1000>;\n%s\n\tcpub_trips: trips {\n\t};\n};\n")
        sensor = "\tthermal-sensors = <&ths %d>;"
        checks = lambda: sources.run(self.cfg, self.reg)["patches"]["checks"]
        dtsi.write_text(zones % (sensor % 3, sensor % 0))
        self.assertEqual(checks()["ipv4-inet-opt-rcu"], "not applied")
        self.assertEqual(checks()["hdmi-ddc-retry"], "not applied")
        self.assertEqual(checks()["cpu-thermal-sensors"], "not applied")
        # half an ipv4 patch is neither state
        inet.write_text("\tkfree_rcu(rcu_dereference_protected(inet->inet_opt, 1), rcu);\n")
        self.assertEqual(checks()["ipv4-inet-opt-rcu"], "unknown")
        tcp.write_text("\tkfree_rcu(rcu_dereference_protected(inet_rsk(req)->ireq_opt, 1), rcu);\n")
        i2cm.write_text("\t\t\t\tlength++;\n\t\t\t\tbuf--;\n\t\t\t\ti2cm->slave_reg--;\n")
        dtsi.write_text(zones % (sensor % 0, sensor % 3))
        self.assertEqual(checks()["ipv4-inet-opt-rcu"], "applied")
        self.assertEqual(checks()["hdmi-ddc-retry"], "applied")
        self.assertEqual(checks()["cpu-thermal-sensors"], "applied")
        # the little zone without a sensor line must not borrow the big zone's
        dtsi.write_text(zones % ("", sensor % 0))
        self.assertEqual(checks()["cpu-thermal-sensors"], "unknown")
        dtsi.write_text("/ { };\n")
        self.assertEqual(checks()["cpu-thermal-sensors"], "unknown")


if __name__ == "__main__":
    unittest.main()
