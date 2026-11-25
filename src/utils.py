"""Shared utilities for a7a-fedora."""

import logging
import os
import shlex
import subprocess
import tempfile
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("a7a-fedora")


def run(
    argv: List[str],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """Run a command, log its full argv and exit status.

    Every external command is recorded with its full argv and exit status.
    Nothing is run silently.
    """
    cmd_str = shlex.join(argv)
    logger.info("RUN: %s (cwd=%s)", cmd_str, cwd or os.getcwd())
    merged_env = {**os.environ, **(env or {})}
    result = subprocess.run(
        argv,
        cwd=cwd,
        env=merged_env,
        capture_output=capture,
        text=True,
        check=False,
    )
    logger.info(
        "EXIT: %s → %d", cmd_str, result.returncode
    )
    if result.stdout:
        logger.debug("STDOUT:\n%s", result.stdout)
    if result.stderr:
        logger.debug("STDERR:\n%s", result.stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, argv, result.stdout, result.stderr
        )
    return result


def run_sudo(argv: List[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run a command with sudo, logging it."""
    return run(["sudo"] + argv, cwd=cwd, check=False)


def create_sparse_file(path: Path, size_bytes: int) -> Path:
    """Create a sparse file of given size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    return path


def create_temp_dir(prefix: str = "a7a-fedora") -> Path:
    """Create a temporary directory and return its Path."""
    return Path(tempfile.mkdtemp(prefix=prefix))


def file_exists_or_error(path: Path) -> Path:
    """Assert a file exists, raise FileNotFoundError otherwise."""
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    return path


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_text(path: Path, content: str) -> Path:
    """Write text to a file, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def read_text(path: Path) -> str:
    """Read text from a file."""
    return path.read_text()


def is_root() -> bool:
    """Check if running as root."""
    return os.geteuid() == 0


def check_tool(name: str) -> Optional[str]:
    """Check if a tool is available. Returns path or None."""
    return subprocess.run(
        ["which", name], capture_output=True, text=True
    ).stdout.strip() or None


def qemu_available() -> bool:
    """Check if qemu-system-aarch64 is available."""
    return check_tool("qemu-system-aarch64") is not None


def qemu_user_static_available() -> bool:
    """Check if qemu-user-static is available."""
    return check_tool("qemu-user-static") is not None


def cross_compiler_available() -> bool:
    """Check if the cross compiler is available."""
    return check_tool("aarch64-linux-gnu-gcc") is not None


def podman_available() -> bool:
    """Check if podman is available."""
    return check_tool("podman") is not None


def dpkg_available() -> bool:
    """Check if dpkg is available."""
    return check_tool("dpkg") is not None


def fakeroot_available() -> bool:
    """Check if fakeroot is available."""
    return check_tool("fakeroot") is not None


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger."""
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=lvl, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", stream=sys.stderr)
    logging.getLogger().setLevel(lvl)      # basicConfig is a no-op after the first call; the level must still follow --json/-v
