"""Tests for src/stages/base.py: the tool runner every stage goes through."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, ToolRun


def _stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestRunTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.tools_dir.mkdir()
        self.reg = StageRegistry(self.cfg.registry_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_argv_exit_code_and_log(self):
        tool = _stub(self.cfg.tools_dir / "t.sh", 'echo hello "$1"; echo oops >&2; exit 3\n')
        r = run_tool(self.cfg, self.reg, "kernel", [str(tool), "world"])
        self.assertIsInstance(r, ToolRun)
        self.assertEqual(r.exit_code, 3)
        self.assertTrue(r.log_path.exists())
        text = r.log_path.read_text()
        self.assertIn("hello world", text)
        self.assertIn("oops", text)                      # stderr is in the same log
        self.assertIn("exit 3", text)
        cmds = self.reg.get_stage("kernel")["commands"]
        self.assertEqual(cmds[-1]["argv"], [str(tool), "world"])
        self.assertEqual(cmds[-1]["exit_code"], 3)
        self.assertEqual(cmds[-1]["log"], str(r.log_path))
        self.assertIn("hello world", r.output)

    def test_dry_run_runs_nothing_and_says_what_it_would_run(self):
        marker = Path(self.tmp.name) / "ran"
        tool = _stub(self.cfg.tools_dir / "t.sh", f'touch {marker}\n')
        r = run_tool(self.cfg, self.reg, "image", [str(tool), "a b"], dry_run=True)
        self.assertFalse(marker.exists())
        self.assertTrue(r.dry_run)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("DRY RUN", r.log_path.read_text())
        self.assertIn("'a b'", r.command_line)             # shell-quoted for copy/paste
        self.assertEqual(self.reg.get_stage("image")["commands"][-1]["dry_run"], True)

    def test_streams_lines_as_they_arrive(self):
        tool = _stub(self.cfg.tools_dir / "t.sh", 'echo one; echo two\n')
        seen = []
        r = run_tool(self.cfg, self.reg, "emulate", [str(tool)], on_line=seen.append)
        self.assertEqual(seen, ["one", "two"])
        self.assertEqual(r.exit_code, 0)

    def test_carriage_return_progress_is_streamed_as_lines(self):
        tool = _stub(self.cfg.tools_dir / "t.sh", 'printf "1 MB copied\\r2 MB copied\\r3 MB copied\\n"\n')
        seen = []
        run_tool(self.cfg, self.reg, "flash", [str(tool)], on_line=seen.append)
        self.assertEqual(seen, ["1 MB copied", "2 MB copied", "3 MB copied"])

    def test_timeout_kills_and_flags(self):
        tool = _stub(self.cfg.tools_dir / "t.sh", 'echo start; sleep 20; echo never\n')
        r = run_tool(self.cfg, self.reg, "emulate", [str(tool)], timeout=1)
        self.assertTrue(r.timed_out)
        self.assertNotEqual(r.exit_code, 0)
        self.assertNotIn("never", r.output)
        self.assertLess(r.duration, 10)

    def test_missing_tool_is_a_recorded_failure_not_an_exception(self):
        r = run_tool(self.cfg, self.reg, "kernel", [str(self.cfg.tools_dir / "absent.sh")])
        self.assertEqual(r.exit_code, 127)
        self.assertIn("not found", r.output.lower())
        self.assertEqual(self.reg.get_stage("kernel")["commands"][-1]["exit_code"], 127)

    def test_log_files_are_per_stage_under_output_logs(self):
        tool = _stub(self.cfg.tools_dir / "t.sh", 'true\n')
        r1 = run_tool(self.cfg, self.reg, "sources", [str(tool)])
        r2 = run_tool(self.cfg, self.reg, "sources", [str(tool)])
        self.assertEqual(r1.log_path.parent, self.cfg.logs_dir)
        self.assertTrue(r1.log_path.name.startswith("sources-"))
        self.assertNotEqual(r1.log_path, r2.log_path)


class TestConfigOverrides(unittest.TestCase):
    def test_no_real_device_by_default(self):
        cfg = Config()
        self.assertIsNone(cfg.serial_port)
        self.assertIsNone(cfg.sd_card)
        self.assertIsNone(cfg.board_host)

    def test_load_json_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a7a-fedora.json"
            p.write_text(json.dumps({"serial_port": "/dev/ttyACM9", "board_host": "10.0.0.9", "unknown_key": 1}))
            cfg = Config.load(p)
            self.assertEqual(cfg.serial_port, "/dev/ttyACM9")
            self.assertEqual(cfg.board_host, "10.0.0.9")

    def test_load_missing_file_is_defaults(self):
        cfg = Config.load(Path("/nonexistent/a7a-fedora.json"))
        self.assertIsNone(cfg.serial_port)


class TestRegistryImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = StageRegistry(Path(self.tmp.name) / "r.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_image_then_tests_and_flashes_accumulate(self):
        self.reg.record_image("a7a-fedora-44-kde-20260906.img", {"sha256": "ab" * 32, "size": 12, "kernel_release": "6.6.98+"})
        self.reg.record_test("a7a-fedora-44-kde-20260906.img", mode="kernel", verdict="PASS", log="/l/1.log",
                             milestones=["login:"])
        self.reg.record_test("a7a-fedora-44-kde-20260906.img", mode="stock", verdict="FAIL", log="/l/2.log")
        self.reg.record_flash("a7a-fedora-44-kde-20260906.img", device="/dev/sdz", verified=True, backup="/b.img.xz")
        img = self.reg.images()["a7a-fedora-44-kde-20260906.img"]
        self.assertEqual(img["sha256"], "ab" * 32)
        self.assertEqual([t["verdict"] for t in img["tests"]], ["PASS", "FAIL"])
        self.assertEqual(img["tests"][0]["milestones"], ["login:"])
        self.assertEqual(img["flashes"][0]["device"], "/dev/sdz")
        reloaded = StageRegistry(self.reg.path)
        self.assertEqual(len(reloaded.images()["a7a-fedora-44-kde-20260906.img"]["tests"]), 2)

    def test_rebuilt_image_with_new_sha_forgets_old_verdicts(self):
        self.reg.record_image("x.img", {"sha256": "a" * 64})
        self.reg.record_test("x.img", mode="kernel", verdict="PASS", log="")
        self.reg.record_image("x.img", {"sha256": "b" * 64})
        self.assertFalse(self.reg.image_tested("x.img"))
        self.assertEqual(self.reg.images()["x.img"]["previous_builds"][0]["sha256"], "a" * 64)
        self.reg.record_image("x.img", {"sha256": "b" * 64, "size": 1})     # same bytes again: nothing forgotten
        self.reg.record_test("x.img", mode="kernel", verdict="PASS", log="")
        self.reg.record_image("x.img", {"sha256": "b" * 64})
        self.assertTrue(self.reg.image_tested("x.img"))

    def test_image_tested_means_the_last_boot_with_the_kernel_passed(self):
        self.reg.record_image("x.img", {"sha256": "a" * 64})
        self.reg.record_test("x.img", mode="kernel", verdict="PASS", log="", sha256="c" * 64)
        self.assertEqual(self.reg.tested_sha256("x.img"), "c" * 64)        # the bytes the test hashed, not the build's
        self.reg.record_test("x.img", mode="stock", verdict="FAIL", log="")
        self.assertTrue(self.reg.image_tested("x.img"))                    # another mode does not take the verdict away
        self.reg.record_test("x.img", mode="kernel", verdict="FAIL", log="", sha256="c" * 64)
        self.assertFalse(self.reg.image_tested("x.img"))
        self.assertIsNone(self.reg.tested_sha256("x.img"))
        self.reg.record_test("x.img", mode="kernel", verdict="PASS", log="")
        self.assertEqual(self.reg.tested_sha256("x.img"), "a" * 64)        # a test recorded without one: the build's
        self.assertIsNone(self.reg.tested_sha256("unknown.img"))
        self.assertFalse(self.reg.image_tested("unknown.img"))

    def test_image_tested_means_a_pass_with_the_kernel(self):
        self.reg.record_image("x.img", {})
        self.assertFalse(self.reg.image_tested("x.img"))
        self.reg.record_test("x.img", mode="stock", verdict="PASS", log="")
        self.assertFalse(self.reg.image_tested("x.img"))
        self.reg.record_test("x.img", mode="kernel", verdict="PASS", log="")
        self.assertTrue(self.reg.image_tested("x.img"))

    def test_dry_run_never_replaces_a_real_result(self):
        self.reg.begin_stage("kernel")
        self.reg.record_command("kernel", ["make"], 0)
        self.reg.record_stage("kernel", "passed", notes="6.6.98+")
        self.reg.begin_stage("kernel", dry_run=True)
        self.reg.record_command("kernel", ["make"], 0, dry_run=True)
        self.reg.record_stage("kernel", "dry_run", notes="would run make")
        e = self.reg.get_stage("kernel")
        self.assertEqual(e["status"], "passed")
        self.assertEqual(e["notes"], "6.6.98+")
        self.assertEqual(len(e["commands"]), 1)
        self.assertEqual(e["last_dry_run"]["notes"], "would run make")
        self.assertEqual(len(e["dry_run_commands"]), 1)
        self.assertEqual(self.reg.summary()["passed"], 1)

    def test_record_command_keeps_log_and_dry_run(self):
        self.reg.record_command("kernel", ["make"], 0, cwd="/w", log="/l.log", dry_run=True)
        c = self.reg.get_stage("kernel")["commands"][0]
        self.assertEqual(c["log"], "/l.log")
        self.assertTrue(c["dry_run"])


if __name__ == "__main__":
    unittest.main()
