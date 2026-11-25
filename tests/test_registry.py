"""Tests for src/registry.py."""

import json
import tempfile
from pathlib import Path
import unittest

from src.registry import StageRegistry


class TestRegistry(unittest.TestCase):
    """Test stage registry."""

    def setUp(self):
        self.tmpfile = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False
        )
        self.tmpfile.close()
        self.registry = StageRegistry(Path(self.tmpfile.name))

    def tearDown(self):
        Path(self.tmpfile.name).unlink(missing_ok=True)

    def test_initial_state(self):
        """Test initial registry state."""
        self.assertIn("version", self.registry.data)
        self.assertIn("created", self.registry.data)
        self.assertIn("stages", self.registry.data)
        self.assertIn("summary", self.registry.data)
        self.assertEqual(self.registry.data["stages"], {})

    def test_record_stage(self):
        """Test recording a stage."""
        self.registry.record_stage(
            "test_stage", "passed",
            artifacts=["/tmp/file1", "/tmp/file2"],
            notes="Test notes",
            duration_seconds=42.0,
        )
        entry = self.registry.get_stage("test_stage")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["artifacts"], ["/tmp/file1", "/tmp/file2"])
        self.assertEqual(entry["notes"], "Test notes")
        self.assertEqual(entry["duration_seconds"], 42.0)

    def test_record_command(self):
        """Test recording a command within a stage."""
        self.registry.record_command(
            "my_stage",
            ["make", "-j4", "Image"],
            0,
            cwd="/tmp/build",
            stdout="Building...\n",
        )
        entry = self.registry.get_stage("my_stage")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "in_progress")
        self.assertEqual(len(entry["commands"]), 1)
        self.assertEqual(entry["commands"][0]["argv"], ["make", "-j4", "Image"])
        self.assertEqual(entry["commands"][0]["exit_code"], 0)

    def test_stage_passed(self):
        """Test checking if a stage passed."""
        self.registry.record_stage("s1", "passed")
        self.registry.record_stage("s2", "failed")
        self.registry.record_stage("s3", "skipped")
        self.assertTrue(self.registry.stage_passed("s1"))
        self.assertFalse(self.registry.stage_passed("s2"))
        self.assertFalse(self.registry.stage_passed("s3"))

    def test_summary(self):
        """Test summary calculation."""
        self.registry.record_stage("a", "passed")
        self.registry.record_stage("b", "passed")
        self.registry.record_stage("c", "failed")
        self.registry.record_stage("d", "skipped")
        summary = self.registry.summary()
        self.assertEqual(summary["passed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)

    def test_all_stages(self):
        """Test listing all stages."""
        self.registry.record_stage("x", "passed")
        self.registry.record_stage("y", "failed")
        stages = self.registry.all_stages()
        self.assertEqual(len(stages), 2)
        self.assertIn("x", stages)
        self.assertIn("y", stages)

    def test_dry_run(self):
        """Test dry run logging."""
        self.registry.log_dry_run("test", "reason: tool missing")
        entry = self.registry.get_stage("test")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "dry_run")
        self.assertIn("reason", entry["notes"])

    def test_save_and_reload(self):
        """Test persistence."""
        self.registry.record_stage("persisted", "passed", notes="test")
        self.registry.save()

        # Reload from disk
        fresh = StageRegistry(Path(self.tmpfile.name))
        entry = fresh.get_stage("persisted")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "passed")

    def test_get_nonexistent_stage(self):
        """Test getting a stage that doesn't exist."""
        self.assertIsNone(self.registry.get_stage("nonexistent"))


if __name__ == "__main__":
    unittest.main()
