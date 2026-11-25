"""Stage 6, flash: write a tested image to the SD card in the laptop's reader.

Gates, in this order, every one of them refusing with a reason:
  1. the image passed a kernel-mode emulator boot (the registry says so; force=True overrides),
  2. the device is a candidate: a removable whole disk, not the disk that holds /, nothing
     mounted from it, at least as big as the image,
  3. the person typed the device's name back (confirm == "sdz"), so a wrong flag cannot pick a disk.
Then: back up the current card through tools/priv.sh read-card (compressed into output/backups,
recorded, so the Debian card can be put back), write with tools/priv.sh flash (it refuses the
same things again, writes with fsync, reads back and compares), record the flash against the image.
Nothing here writes a block device itself; only the privileged helper does.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, sha256_of, sudo_priv
from src.stages.emulate import pick_image

logger = logging.getLogger("a7a-fedora.flash")


def _mounted(node: Dict) -> List[str]:
    out = [m for m in (node.get("mountpoints") or []) if m]
    for child in node.get("children") or []:
        out += _mounted(child)
    return out


def root_device(config: Config) -> Optional[str]:
    """The whole disk that holds / (e.g. /dev/nvme0n1), or the configured override."""
    if config.root_device:
        return config.root_device
    try:
        src = subprocess.run(["findmnt", "-n", "-o", "SOURCE", "/"], capture_output=True, text=True, timeout=10).stdout.strip()
        pk = subprocess.run(["lsblk", "-n", "-o", "PKNAME", src], capture_output=True, text=True, timeout=10).stdout.strip()
        return f"/dev/{pk}" if pk else src
    except (OSError, subprocess.TimeoutExpired):
        return None


def candidates(config: Config) -> List[Dict]:
    """Removable whole disks with what lsblk knows about them (size in bytes, model, bus, mounts)."""
    argv = config.lsblk_argv or ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,TRAN,RM,HOTPLUG,TYPE,MOUNTPOINTS,START"]
    try:
        data = json.loads(subprocess.run(argv, capture_output=True, text=True, timeout=20, check=True).stdout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.warning("lsblk failed: %s", e)
        return []
    root = root_device(config)
    out = []
    for d in data.get("blockdevices", []):
        if d.get("type") != "disk" or not (d.get("rm") or d.get("hotplug")):
            continue
        path = d.get("path") or f"/dev/{d['name']}"
        out.append({"path": path, "name": d["name"], "size": int(d.get("size") or 0), "model": (d.get("model") or "").strip(),
                    "tran": d.get("tran"), "removable": bool(d.get("rm")), "mounted": _mounted(d), "root_disk": path == root,
                    "partitioned_bytes": partitioned_bytes(d)})
    return out


def partitioned_bytes(disk: Dict) -> int:
    """Where the last partition ends (START is in 512-byte sectors, SIZE in bytes): what a backup has to read."""
    end = 0
    for child in disk.get("children") or []:
        if child.get("type") == "part" and child.get("start") is not None:
            end = max(end, int(child["start"]) * 512 + int(child.get("size") or 0))
    return end


def check_device(config: Config, device: str, image_size: int) -> (Optional[Dict], List[str]):
    """The candidate record for device and the list of reasons it must not be written."""
    problems: List[str] = []
    match = next((c for c in candidates(config) if c["path"] == device), None)
    if device == root_device(config):
        return match, [f"{device} holds the root filesystem"]
    if not match:
        return None, [f"{device} is not a candidate (removable whole disks only: {', '.join(c['path'] for c in candidates(config)) or 'none found'})"]
    if match["root_disk"] or device == root_device(config):
        problems.append(f"{device} holds the root filesystem")
    if match["mounted"]:
        problems.append(f"{device} has mounted filesystems: {', '.join(match['mounted'])} (unmount or eject first)")
    if match["size"] < image_size:
        problems.append(f"{device} is too small: {match['size']} bytes < image {image_size} bytes")
    return match, problems


def run(config: Config, registry: StageRegistry, image: Optional[Path | str] = None, device: Optional[str] = None,
        confirm: Optional[str] = None, backup: bool = True, force: bool = False, dry_run: bool = False, on_line=None,
        backup_name: Optional[str] = None, existing_backup: Optional[str] = None, grow: bool = True) -> Dict:
    t0 = time.time()
    registry.begin_stage("flash", dry_run=dry_run)
    result: Dict = {"stage": "flash", "status": "passed", "commands": [], "verified": False, "backup": None}

    def refuse(reason: str, status: str = "refused") -> Dict:
        result.update(status=status, reason=reason)
        registry.record_stage("flash", status, notes=reason, duration_seconds=time.time() - t0)
        return result

    path = pick_image(config, image)
    if not path or not path.exists():
        return refuse(f"no image ({path or 'output/ has no a7a-fedora-*.img'})")
    result.update(image=path.name, path=str(path))
    if not registry.image_tested(path.name) and not force:
        return refuse(f"{path.name} has no kernel-mode emulator PASS in the registry; run `emulate` first (force=True overrides)")
    if not device:
        return refuse("no device given; candidates: " + (", ".join(f"{c['path']} ({c['model']}, {c['size'] // 10**9} GB)" for c in candidates(config)) or "none"))
    size = path.stat().st_size
    match, problems = check_device(config, device, size)
    if problems:
        return refuse("; ".join(problems))
    assert match is not None
    # the PASS belongs to bytes, not to a name: the image stage recreates the file under its dated name, and a rebuild
    # that failed half way leaves a partial file there while the registry still shows the last finished build's test
    recorded = registry.tested_sha256(path.name)

    def not_the_tested_bytes() -> Optional[str]:
        actual = sha256_of(path)
        if actual != recorded:
            return (f"{path.name} is not the image that was tested: its sha256 is {actual[:16]}..., the registry tested {recorded[:16]}... "
                    "(a rebuild that did not finish?); build and emulate again")
        return None

    if not force and not dry_run and confirm == match["name"]:
        if not recorded:
            return refuse(f"the registry does not say which bytes of {path.name} were tested (a test from before 2026-11-16 of an image "
                          "without a checksum); run `emulate` again (force=True overrides)")
        changed = not_the_tested_bytes()
        if changed:
            return refuse(changed)
    result["plan"] = {"image": path.name, "bytes": size, "device": match, "backup": backup,
                      "backup_bytes": (match.get("partitioned_bytes") or match["size"]) if backup else 0,
                      "commands": [" ".join(sudo_priv(config, "read-card", device, "<backup>", str(match.get("partitioned_bytes") or match["size"]))) if backup else None,
                                   " ".join(sudo_priv(config, "flash", str(path), device)),
                                   " ".join(sudo_priv(config, "grow-rootfs", device)) if grow else None]}
    if confirm != match["name"]:
        return refuse(f"confirmation missing: to write {path.name} ({size} bytes) to {device} ({match['model']}, {match['size']} bytes) "
                      f"type {match['name']} back (confirm='{match['name']}')")

    real_device = (config.device_path_override or {}).get(device, device)
    if existing_backup:
        # a backup made earlier (a run that stopped after reading the card): record it, do not read the card again
        eb = Path(existing_backup)
        if not eb.is_absolute():
            eb = config.output_dir / "backups" / eb
        if not eb.exists() or eb.stat().st_size == 0:
            return refuse(f"existing backup {eb} is missing or empty")
        result["backup"] = str(eb)
        registry.data.setdefault("backups", []).append({"device": device, "model": match["model"], "file": str(eb), "size": eb.stat().st_size,
                                                         "timestamp": datetime.now().isoformat(timespec="seconds"), "reused": True})
        registry.save()
        backup = False
    if backup:
        bdir = config.output_dir / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", backup_name).strip("-") if backup_name else ""
        raw = bdir / (f"{safe}.img" if safe else f"card-{match['name']}-{(match['model'] or 'card').replace(' ', '_')}-{stamp}.img")
        if raw.exists() or Path(str(raw) + ".xz").exists():
            raw = raw.with_name(f"{raw.stem}-{stamp}.img")
        limit = match.get("partitioned_bytes") or 0
        read_args = ["read-card", real_device, str(raw)] + ([str(limit)] if 0 < limit < match["size"] else [])
        result["backup_bytes"] = limit if 0 < limit < match["size"] else match["size"]
        r = run_tool(config, registry, "flash", sudo_priv(config, *read_args), dry_run=dry_run, on_line=on_line)
        result["commands"].append(r.command_line)
        if not r.ok:
            return refuse(f"backup of {device} failed (exit {r.exit_code}); nothing written; log {r.log_path}", status="failed")
        if not dry_run:
            rx = run_tool(config, registry, "flash", ["xz", "-T0", "-1", "-f", str(raw)], on_line=on_line)
            result["commands"].append(rx.command_line)
            xz = Path(str(raw) + ".xz")
            # xz exits 2 for warnings (e.g. it could not copy the file's group) after writing a complete archive
            if not (xz.exists() and xz.stat().st_size > 0 and rx.exit_code in (0, 2)):
                return refuse(f"xz of the backup failed (exit {rx.exit_code}); nothing written; log {rx.log_path}", status="failed")
            if rx.exit_code == 2:
                result.setdefault("warnings", []).append(f"xz warned (exit 2), archive complete: {rx.output.strip().splitlines()[-1] if rx.output.strip() else ''}")
                raw.unlink(missing_ok=True)
            result["backup"] = str(xz)
            registry.data.setdefault("backups", []).append({"device": device, "model": match["model"], "file": str(xz),
                                                             "size": xz.stat().st_size, "timestamp": datetime.now().isoformat(timespec="seconds")})
            registry.save()
        else:
            result["commands"].append(f"xz -T0 -1 {raw}")
    if backup and not force and not dry_run:
        # reading the card took hours on a slow reader: the file is checked again right before it is written
        changed = not_the_tested_bytes()
        if changed:
            return refuse(changed + f"; nothing written, the card's backup is {result['backup']}")
    r = run_tool(config, registry, "flash", sudo_priv(config, "flash", str(path), real_device), dry_run=dry_run, on_line=on_line)
    result["commands"].append(r.command_line)
    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("flash", "dry_run", notes="; ".join(result["commands"]), duration_seconds=time.time() - t0)
        return result
    result["verified"] = r.ok and "verified" in r.output
    registry.record_flash(path.name, device=device, verified=result["verified"], backup=result["backup"], log=str(r.log_path),
                          model=match["model"])
    if not result["verified"]:
        return refuse(f"flash of {device} did not verify (exit {r.exit_code}); do not boot this card; log {r.log_path}", status="failed")
    result["log"] = str(r.log_path)
    if grow:
        rg = run_tool(config, registry, "flash", sudo_priv(config, "grow-rootfs", real_device), on_line=on_line)
        result["commands"].append(rg.command_line)
        result["grown"] = rg.ok
        if not rg.ok:
            result.setdefault("warnings", []).append(f"the card is written and verified, but growing the rootfs failed (exit {rg.exit_code}); "
                                                     f"it boots with the image's rootfs size; log {rg.log_path}")
    registry.record_stage("flash", "passed", notes=f"{path.name} -> {device} ({match['model']}), verified" + (f", backup {result['backup']}" if result["backup"] else ""),
                          duration_seconds=time.time() - t0, extra={"image": path.name, "device": device, "backup": result["backup"]})
    return result
