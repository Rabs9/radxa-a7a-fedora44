"""Tests for src/config.py."""

import unittest
from pathlib import Path

from src.config import Config


class TestConfig(unittest.TestCase):
    """Test configuration defaults and properties."""

    def setUp(self):
        self.config = Config()

    def test_project_root_exists(self):
        """Test project root is a valid path."""
        self.assertTrue(self.config.project_root.exists())

    def test_mirror_dir(self):
        """Test mirror directory path."""
        self.assertIsInstance(self.config.mirror_dir, Path)
        self.assertEqual(self.config.mirror_dir.name, "mirror")

    def test_work_dir(self):
        """Test work directory path."""
        self.assertIsInstance(self.config.work_dir, Path)
        self.assertEqual(self.config.work_dir.name, "work")

    def test_output_dir(self):
        """Test output directory path."""
        self.assertIsInstance(self.config.output_dir, Path)
        self.assertEqual(self.config.output_dir.name, "output")

    def test_kernel_dir_property(self):
        """Test kernel directory property."""
        self.assertEqual(
            self.config.kernel_dir,
            self.config.work_dir / "kernel-6.6"
        )

    def test_bsp_dir_property(self):
        """Test BSP directory property."""
        self.assertEqual(
            self.config.bsp_dir,
            self.config.work_dir / "allwinner-bsp-1.4.8"
        )

    def test_device_dir_property(self):
        """Test device directory property."""
        self.assertEqual(
            self.config.device_dir,
            self.config.work_dir / "allwinner-device-1.4.8"
        )

    def test_target_dir_property(self):
        """Test target directory property."""
        self.assertEqual(
            self.config.target_dir,
            self.config.work_dir / "allwinner-target"
        )

    def test_image_path(self):
        """Test image path property."""
        self.assertEqual(
            self.config.image_path,
            self.config.output_dir / "a7a-fedora-44.raw"
        )

    def test_image_xz_path(self):
        """Test compressed image path."""
        self.assertEqual(
            self.config.image_xz_path,
            self.config.output_dir / "a7a-fedora-44.raw.xz"
        )

    def test_output_boot(self):
        """Test output boot directory."""
        self.assertEqual(
            self.config.output_boot,
            self.config.output_dir / "boot"
        )

    def test_output_rootfs(self):
        """Test output rootfs directory."""
        self.assertEqual(
            self.config.output_rootfs,
            self.config.output_dir / "rootfs"
        )

    def test_kernel_source(self):
        """Test kernel source path."""
        self.assertEqual(
            self.config.kernel_source,
            self.config.project_root / "mirror"
        )

    def test_default_values(self):
        """Test default configuration values."""
        self.assertEqual(self.config.cross_compile, "aarch64-linux-gnu-")
        self.assertEqual(self.config.arch, "arm64")
        self.assertEqual(self.config.board_name, "cubie_a7a")
        self.assertEqual(self.config.fedora_version, "44")
        self.assertEqual(self.config.fedora_arch, "aarch64")
        self.assertEqual(self.config.serial_baud, 115200)
        self.assertEqual(self.config.qemu_machine, "virt")

    def test_repo_urls(self):
        """Test repository URLs."""
        self.assertIn("radxa", self.config.kernel_repo)
        self.assertIn("allwinner-bsp", self.config.bsp_repo)
        self.assertIn("allwinner-device", self.config.device_repo)
        self.assertIn("allwinner-target", self.config.target_repo)

    def test_partition_sizes(self):
        """Test partition size configuration."""
        self.assertEqual(self.config.config_partition_size, 16 * 1024 * 1024)
        self.assertEqual(self.config.boot_partition_size, 300 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
