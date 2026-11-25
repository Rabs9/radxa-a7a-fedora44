"""Tests for the kernel stage: it drives tools/build-kernel.sh and build-extras.sh and inspects the payload."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import kernel


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


FAKE_BUILD = r'''
PAY="$A7A_PAYLOAD"; rm -rf "$PAY"; mkdir -p "$PAY/boot/dtb/allwinner" "$PAY/lib/modules/6.6.98+/kernel/net" "$PAY/lib/firmware/aic8800D80"
printf 'kernel\0RGDB\0stuff' > "$PAY/boot/Image-6.6.98+"
printf 'dtb-bytes' > "$PAY/boot/dtb/allwinner/sun60i-a733-cubie-a7a.dtb"
for m in a b c; do printf 'ko' > "$PAY/lib/modules/6.6.98+/kernel/net/$m.ko"; done
printf 'fw' > "$PAY/lib/firmware/aic8800D80/fmacfw.bin"
echo "6.6.98+" > "$PAY/kernelrelease"
echo "== done in 1 min: 3 modules"
'''
FAKE_EXTRAS = r'''
PAY="$A7A_PAYLOAD"; mkdir -p "$PAY/lib/modules/6.6.98+/extra"
printf 'ko' > "$PAY/lib/modules/6.6.98+/extra/pvrsrvkm.ko"; printf 'ko' > "$PAY/lib/modules/6.6.98+/extra/aic8800_fdrv.ko"
echo "== done"
'''


class TestKernel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          mirror_dir=root / "mirror", tools_dir=root / "tools", registry_path=root / "registry.json")
        self.reg = StageRegistry(self.cfg.registry_path)
        (self.cfg.work_dir / "kernel-6.6").mkdir(parents=True)
        self.cfg.mirror_dir.mkdir()
        self.cfg.cross_compile = ""            # "gcc" exists everywhere the tests run
        _stub(self.cfg.tools_dir / "build-kernel.sh", FAKE_BUILD)
        _stub(self.cfg.tools_dir / "build-extras.sh", FAKE_EXTRAS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_then_extras_then_payload_summary(self):
        r = kernel.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["release"], "6.6.98+")
        self.assertEqual(r["modules"], 3)
        self.assertEqual(sorted(r["extra_modules"]), ["aic8800_fdrv.ko", "pvrsrvkm.ko"])
        self.assertTrue(r["regulatory_db_embedded"])
        self.assertEqual(len(r["dtbs"]["sun60i-a733-cubie-a7a.dtb"]), 64)     # sha256
        self.assertEqual(r["firmware"], ["aic8800D80/fmacfw.bin"])
        argvs = [Path(c["argv"][0]).name for c in self.reg.get_stage("kernel")["commands"]]
        self.assertEqual(argvs, ["build-kernel.sh", "build-extras.sh"])
        entry = self.reg.get_stage("kernel")
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["release"], "6.6.98+")
        self.assertTrue(Path(r["logs"][0]).exists())

    def test_missing_cross_compiler_is_a_clean_failure(self):
        self.cfg.cross_compile = "no-such-toolchain-"
        r = kernel.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("no-such-toolchain-gcc", r["reason"])
        self.assertIsNone(self.reg.get_stage("kernel").get("commands"))

    def test_missing_source_tree_points_at_the_sources_stage(self):
        (self.cfg.work_dir / "kernel-6.6").rmdir()
        r = kernel.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("sources", r["reason"])

    def test_build_script_failure_is_reported_with_its_log(self):
        _stub(self.cfg.tools_dir / "build-kernel.sh", 'echo "error: something"; exit 2\n')
        r = kernel.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("build-kernel.sh", r["reason"])
        self.assertIn("something", Path(r["logs"][0]).read_text())
        self.assertFalse(any("build-extras" in c["argv"][0] for c in self.reg.get_stage("kernel")["commands"]))

    def test_dry_run_lists_both_commands_and_builds_nothing(self):
        r = kernel.run(self.cfg, self.reg, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertFalse(self.cfg.payload_dir.exists())
        self.assertEqual(len(r["commands"]), 2)

    def test_no_extras_flag(self):
        r = kernel.run(self.cfg, self.reg, extras=False)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["extra_modules"], [])

    def test_clean_build_runs_mrproper_first(self):
        r = kernel.run(self.cfg, self.reg, clean=True, dry_run=True)
        self.assertIn("mrproper", r["commands"][0])

    def test_rpm_flag_runs_the_packager_and_lists_rpms(self):
        _stub(self.cfg.tools_dir / "build-rpm.sh", 'mkdir -p "$A7A_OUTPUT/rpms"; touch "$A7A_OUTPUT/rpms/kernel-a7a-6.6.98+-1.aarch64.rpm"; echo done\n')
        r = kernel.run(self.cfg, self.reg, rpm=True)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(len(r["rpms"]), 1)
        self.assertTrue(r["rpms"][0].endswith(".aarch64.rpm"))

    def test_payload_info_alone(self):
        kernel.run(self.cfg, self.reg)
        info = kernel.payload_info(self.cfg)
        self.assertEqual(info["release"], "6.6.98+")
        self.assertEqual(info["image_bytes"], len(b"kernel\0RGDB\0stuff"))

    def test_payload_info_without_payload(self):
        info = kernel.payload_info(self.cfg)
        self.assertIsNone(info["release"])


if __name__ == "__main__":
    unittest.main()
