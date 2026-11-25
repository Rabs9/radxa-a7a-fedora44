"""Tests for the PySide6 dashboard, offscreen: it builds, shows the stages and images, and can say what a button would run."""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.config import Config
from src.registry import StageRegistry


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work", mirror_dir=root / "mirror",
                          tools_dir=root / "tools", registry_path=root / "registry.json",
                          mirror_repo="file:///nonexistent", kernel_repo="file:///nonexistent", bsp_repo="file:///nonexistent",
                          device_repo="file:///nonexistent", target_repo="file:///nonexistent")
        self.cfg.output_dir.mkdir()
        reg = StageRegistry(self.cfg.registry_path)
        reg.record_stage("kernel", "passed", notes="6.6.98+: 1354 modules")
        reg.record_image("a7a-fedora-44-kde-20260906.img", {"kernel_release": "6.6.98+", "size": 12, "sha256": "ab" * 32})
        reg.record_test("a7a-fedora-44-kde-20260906.img", mode="kernel", verdict="PASS", log="/l")

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_builds_with_every_stage_and_the_images(self):
        from src.dashboard.app import DashboardWindow
        w = DashboardWindow(self.cfg)
        self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())], ["Pipeline", "Images", "Serial console", "Logs"])
        self.assertEqual(w.stage_table.rowCount(), 8)
        names = [w.stage_table.item(i, 0).text() for i in range(8)]
        self.assertEqual(names, ["sources", "kernel", "rootfs", "image", "emulate", "flash", "serial", "update"])
        self.assertEqual(w.stage_table.item(1, 1).text(), "passed")
        self.assertEqual(w.images_table.rowCount(), 1)
        self.assertIn("kernel:PASS", w.images_table.item(0, 3).text())

    def test_show_command_is_a_dry_run(self):
        from src.dashboard.app import DashboardWindow
        w = DashboardWindow(self.cfg)
        text = w.commands_for("sources")
        self.assertIn("git clone", text)
        self.assertFalse((self.cfg.work_dir / "kernel-6.6").exists())
        text = w.commands_for("flash")
        self.assertIn("refused", text)

    def test_stage_runner_streams_lines_and_result(self):
        from src.dashboard.app import StageRunner
        lines, done = [], []
        runner = StageRunner(lambda on_line: (on_line("hello"), {"status": "passed"})[1])
        runner.line.connect(lines.append)
        runner.finished_with.connect(done.append)
        runner.run()                     # synchronously, no event loop needed
        self.assertEqual(lines, ["hello"])
        self.assertEqual(done[0]["status"], "passed")

    def test_main_is_callable(self):
        from src.dashboard.app import main
        self.assertTrue(callable(main))


if __name__ == "__main__":
    unittest.main()
