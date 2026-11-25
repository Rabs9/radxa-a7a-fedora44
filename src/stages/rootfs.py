"""Stage 3, rootfs: the official Fedora 44 KDE aarch64 raw image, fetched and verified.

Fedora's KDE image is the rootfs source (its userspace, dnf, Plasma); the Rabs9 kernel goes in at
the image stage. Here: download the .raw.xz and its CHECKSUM with curl unless the raw is
already in output/, compare the SHA-256 the CHECKSUM names, verify the CHECKSUM's PGP
signature when gpg and the Fedora 44 key are on this machine, then xz -d -k to the raw.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, sha256_of

logger = logging.getLogger("a7a-fedora.rootfs")


def expected_sha256(checksum_text: str, filename: str) -> Optional[str]:
    """The hash a Fedora CHECKSUM file gives for one file (BSD-style lines)."""
    m = re.search(r"^SHA256 \(" + re.escape(filename) + r"\) = ([0-9a-f]{64})$", checksum_text, re.M)
    return m.group(1) if m else None


def is_signed(checksum_text: str) -> bool:
    return checksum_text.lstrip().startswith("-----BEGIN PGP SIGNED MESSAGE-----")


def verify_signature(config: Config, checksum_file: Path) -> str:
    """'verified', 'not signed', 'gpg missing', 'key missing', or 'BAD signature ...'."""
    text = checksum_file.read_text(errors="replace")
    if not is_signed(text):
        return "not signed"
    if not shutil.which("gpg"):
        return "gpg missing"
    key = config.fedora_gpg_key
    if not key or not Path(key).exists():
        return "key missing"
    with tempfile.TemporaryDirectory() as home:
        env = {"GNUPGHOME": home}
        subprocess.run(["gpg", "--batch", "--import", str(key)], env=env, capture_output=True)
        r = subprocess.run(["gpg", "--batch", "--verify", str(checksum_file)], env=env, capture_output=True, text=True)
    if r.returncode == 0:
        return "verified"
    return "BAD signature: " + (r.stderr.strip().splitlines() or ["?"])[-1]


def run(config: Config, registry: StageRegistry, dry_run: bool = False, on_line=None) -> Dict:
    t0 = time.time()
    registry.begin_stage("rootfs", dry_run=dry_run)
    result: Dict = {"stage": "rootfs", "status": "passed", "commands": [], "raw": str(config.fedora_raw)}
    config.output_dir.mkdir(parents=True, exist_ok=True)
    xz = config.output_dir / config.fedora_url.rsplit("/", 1)[-1]
    checksum_file = config.output_dir / config.fedora_checksum_url.rsplit("/", 1)[-1]

    if config.fedora_raw.exists() and not dry_run:
        result.update(checksum="skipped (raw already present)", signature="skipped", raw_bytes=config.fedora_raw.stat().st_size)
        registry.record_stage("rootfs", "passed", artifacts=[str(config.fedora_raw)], notes="raw already present", duration_seconds=time.time() - t0)
        return result

    def step(argv, **kw):
        r = run_tool(config, registry, "rootfs", argv, dry_run=dry_run, on_line=on_line, **kw)
        result["commands"].append(r.command_line)
        return r

    r = step(["curl", "-L", "--fail", "--retry", "3", "-C", "-", "-o", str(xz), config.fedora_url])
    if not r.ok:
        result.update(status="failed", reason=f"download failed (curl exit {r.exit_code}): {config.fedora_url}; log {r.log_path}")
        registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result
    r = step(["curl", "-L", "--fail", "-o", str(checksum_file), config.fedora_checksum_url])
    if not r.ok:
        result.update(status="failed", reason=f"download of the CHECKSUM failed (curl exit {r.exit_code}); log {r.log_path}")
        registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result

    if not dry_run:
        text = checksum_file.read_text(errors="replace")
        want = expected_sha256(text, xz.name)
        if not want:
            result.update(status="failed", reason=f"no entry for {xz.name} in {checksum_file.name}")
            registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
            return result
        have = sha256_of(xz)
        result["sha256"] = have
        if have != want:
            bad = xz.with_name(xz.name + ".bad")
            xz.rename(bad)
            result.update(status="failed", reason=f"checksum mismatch for {xz.name}: expected {want[:12]}..., got {have[:12]}...; moved to {bad.name}")
            registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
            return result
        result["checksum"] = "verified"
        result["signature"] = verify_signature(config, checksum_file)
        if result["signature"].startswith("BAD"):
            result.update(status="failed", reason=result["signature"])
            registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
            return result

    r = step(["xz", "-d", "-k", "-f", str(xz)])
    if not r.ok:
        result.update(status="failed", reason=f"xz exited {r.exit_code}; log {r.log_path}")
        registry.record_stage("rootfs", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result
    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("rootfs", "dry_run", notes="; ".join(result["commands"]), duration_seconds=time.time() - t0)
        return result
    extracted = xz.with_name(xz.name[:-3])
    if extracted != config.fedora_raw and extracted.exists():
        extracted.rename(config.fedora_raw)
    result["raw_bytes"] = config.fedora_raw.stat().st_size
    registry.record_stage("rootfs", "passed", artifacts=[str(config.fedora_raw), str(checksum_file)],
                          notes=f"{xz.name}: checksum {result['checksum']}, signature {result['signature']}",
                          duration_seconds=time.time() - t0,
                          extra={"sha256": result.get("sha256"), "signature": result.get("signature")})
    return result
