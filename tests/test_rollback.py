"""Tests for src/stages/rollback.py."""

import tempfile
from pathlib import Path
import unittest

from src.config import Config
from src.registry import StageRegistry
from src.stages.rollback import (
    check_boot_complete,
    check_boot_failed,
    rollback_boot,
)


class TestRollback(unittest.TestCase):
    """Test rollback stage."""

    def setUp(self):
        self.config = Config()
        self.registry = StageRegistry()

    def test_check_boot_complete(self):
        """Test boot completion detection."""
        self.assertTrue(check_boot_complete("login:"))
        self.assertTrue(check_boot_complete("Welcome to Fedora"))
        self.assertTrue(check_boot_complete("systemd[1]: Started"))
        self.assertTrue(check_boot_complete("Reached target"))
        self.assertTrue(check_boot_complete("Booting custom kernel"))
        self.assertFalse(check_boot_complete(""))
        self.assertFalse(check_boot_complete("kernel panic"))

    def test_check_boot_failed(self):
        """Test boot failure detection."""
        self.assertTrue(check_boot_failed("Kernel panic"))
        self.assertTrue(check_boot_failed("Oops: 0002"))
        self.assertTrue(check_boot_failed("Unable to handle"))
        self.assertFalse(check_boot_failed("login:"))
        self.assertFalse(check_boot_failed(""))

    def test_rollback_boot_no_backups(self):
        """Test rollback when no backups exist."""
        self.config.sd_card = "/dev/mmcblk0"
        result = rollback_boot(self.config)
        # Should return False when no backups found
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
