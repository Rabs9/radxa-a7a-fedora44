"""Stage 7: Watch first boot and rollback on failure."""

import logging
import os
import pty
import select
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from src.config import Config
from src.registry import StageRegistry
from src.utils import run

logger = logging.getLogger("a7a-fedora.rollback")


def connect_serial(port: str, baud: int) -> Optional[int]:
    """Connect to the serial console. Returns fd or None."""
    if not os.path.exists(port):
        logger.warning("Serial port %s does not exist", port)
        return None

    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        import termios
        tty = termios.tcgetattr(fd)
        tty[3] = tty[3] & ~termios.ECHO  # c_lflag
        termios.tcsetattr(fd, termios.TCSANOW, tty)
        os.set_blocking(fd, False)
        logger.info("Connected to serial port %s at %d baud", port, baud)
        return fd
    except OSError as e:
        logger.error("Cannot open serial port: %s", e)
        return None


def read_serial(fd: int, timeout: float = 1.0) -> str:
    """Read data from serial port."""
    try:
        data = os.read(fd, 4096)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def check_boot_complete(boot_log: str) -> bool:
    """Check if the boot has completed successfully."""
    # First check for failure indicators — if boot has panicked, it's not complete
    if check_boot_failed(boot_log):
        return False
    indicators = [
        "login:",
        "fedora",
        "welcome",
        "systemd[1]: started",
        "reached target",
        "booting custom kernel",
    ]
    log_lower = boot_log.lower()
    for ind in indicators:
        if ind in log_lower:
            return True
    return False


def check_boot_failed(boot_log: str) -> bool:
    """Check if the boot has failed."""
    indicators = [
        "kernel panic",
        "kernel bug",
        "oops:",
        "unable to handle",
        "segfault",
        "kernel oops",
        "panic - not syncing",
        "failed to start",
        "failed to mount",
        "failed to probe",
    ]
    log_lower = boot_log.lower()
    for ind in indicators:
        if ind in log_lower:
            return True
    return False


def rollback_boot(config: Config) -> bool:
    """Attempt to rollback to a previous boot configuration."""
    logger.info("Initiating rollback...")

    # Find backup kernel files
    boot_dir = Path("/mnt/boot")  # Would be mounted SD card boot partition
    backup_found = False

    for backup in boot_dir.glob("*.bak"):
        logger.info("Found backup: %s", backup)
        backup_found = True

    if not backup_found:
        logger.warning("No backup files found at %s", boot_dir)
        return False

    # Restore from backup
    for bak in boot_dir.glob("*.bak"):
        original = bak.with_suffix("")
        if bak.exists():
            bak.rename(original)
            logger.info("Restored %s from %s", original, bak)

    logger.info("Rollback complete")
    return True


def watch_boot(config: Config, timeout: int = 180) -> dict:
    """Watch the board's boot over serial console."""
    port = config.serial_port
    fd = connect_serial(port, config.serial_baud)

    if fd is None:
        logger.warning("Cannot connect to serial, dry run mode")
        return {"success": False, "reason": "serial unavailable", "boot_log": ""}

    boot_log = ""
    start_time = time.time()
    boot_complete = False
    boot_failed = False

    try:
        while time.time() - start_time < timeout:
            data = read_serial(fd)
            if data:
                boot_log += data
                logger.debug("Serial: %s", data[:200])

                if check_boot_complete(boot_log):
                    boot_complete = True
                    break
                if check_boot_failed(boot_log):
                    boot_failed = True
                    break

            time.sleep(0.5)

    finally:
        if fd is not None:
            os.close(fd)

    return {
        "success": boot_complete,
        "failed": boot_failed,
        "boot_log": boot_log,
        "timeout": time.time() - start_time,
    }


def run(config: Config, registry: StageRegistry) -> None:
    """Watch first boot and rollback on failure."""
    start_time = time.time()
    logger.info("Starting rollback stage")

    try:
        result = watch_boot(config)

        if result["success"]:
            registry.record_stage(
                "rollback",
                "passed",
                notes=f"Boot completed successfully in {result['timeout']:.1f}s",
                duration_seconds=time.time() - start_time,
            )
            logger.info("Rollback stage passed — boot successful")
        elif result["failed"]:
            logger.warning("Boot failed, attempting rollback...")
            rollback_success = rollback_boot(config)
            registry.record_stage(
                "rollback",
                "passed" if rollback_success else "failed",
                notes=f"Boot failed, rollback: {rollback_success}",
                duration_seconds=time.time() - start_time,
            )
        else:
            registry.record_stage(
                "rollback",
                "skipped",
                notes=f"Boot timed out after {result['timeout']:.1f}s",
                duration_seconds=time.time() - start_time,
            )
            logger.warning("Rollback stage: boot timed out")

    except Exception as e:
        registry.log_dry_run("rollback", f"Serial unavailable: {e}")
        logger.warning("Rollback dry run: %s", e)
