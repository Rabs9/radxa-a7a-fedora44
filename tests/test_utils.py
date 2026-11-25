"""Tests for src/utils.py."""

import os
import subprocess
import tempfile
from pathlib import Path
import unittest

from src.utils import (
    create_sparse_file,
    create_temp_dir,
    file_exists_or_error,
    ensure_dir,
    is_root,
    check_tool,
    write_text,
    read_text,
    run,
    setup_logging,
    qemu_available,
    cross_compiler_available,
)


class TestUtils(unittest.TestCase):
    """Test shared utilities."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_create_sparse_file(self):
        """Test creating a sparse file."""
        path = self.work_dir / "sparse.img"
        result = create_sparse_file(path, 1024 * 1024)
        self.assertTrue(result.exists())
        self.assertEqual(result.stat().st_size, 1024 * 1024)
        # Should be sparse
        self.assertLess(result.stat().st_blocks * 512, 1024 * 1024)

    def test_create_temp_dir(self):
        """Test creating a temp directory."""
        td = create_temp_dir("test-")
        self.assertTrue(td.exists())
        self.assertTrue(td.is_dir())

    def test_file_exists_or_error(self):
        """Test file existence check."""
        test_file = self.work_dir / "exists.txt"
        test_file.write_text("hello")
        result = file_exists_or_error(test_file)
        self.assertEqual(result, test_file)

        with self.assertRaises(FileNotFoundError):
            file_exists_or_error(self.work_dir / "missing.txt")

    def test_ensure_dir(self):
        """Test directory creation."""
        new_dir = self.work_dir / "sub" / "dir"
        result = ensure_dir(new_dir)
        self.assertTrue(result.exists())
        self.assertTrue(result.is_dir())

    def test_write_and_read_text(self):
        """Test text file I/O."""
        test_file = self.work_dir / "test.txt"
        content = "Hello, a7a-fedora!"
        result = write_text(test_file, content)
        self.assertEqual(result, test_file)
        self.assertEqual(read_text(test_file), content)

    def test_is_root(self):
        """Test root check."""
        # Just verify it returns a bool
        self.assertIsInstance(is_root(), bool)

    def test_check_tool(self):
        """Test tool availability check."""
        # 'ls' should be available
        path = check_tool("ls")
        self.assertIsNotNone(path)
        # 'nonexistent_tool_xyz' should not be available
        self.assertIsNone(check_tool("nonexistent_tool_xyz"))

    def test_qemu_available(self):
        """Test qemu availability check."""
        result = qemu_available()
        self.assertIsInstance(result, bool)

    def test_cross_compiler_available(self):
        """Test cross compiler availability check."""
        result = cross_compiler_available()
        self.assertIsInstance(result, bool)

    def test_run(self):
        """Test running a command."""
        result = run(["echo", "hello"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("hello", result.stdout)

    def test_run_fails_on_error(self):
        """Test that run raises on non-zero exit."""
        with self.assertRaises(subprocess.CalledProcessError):
            run(["false"], check=True)

    def test_run_no_check(self):
        """Test that run doesn't raise when check=False."""
        result = run(["false"], check=False)
        self.assertEqual(result.returncode, 1)

    def test_setup_logging(self):
        """Test logging setup."""
        setup_logging("DEBUG")
        # Should not raise


if __name__ == "__main__":
    unittest.main()
