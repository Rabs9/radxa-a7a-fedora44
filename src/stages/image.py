"""Stage 4, image: assemble the card image and register it.

tools/assemble-image.sh does the work (card layout from the Rabs9 repo's flash-image.sh, the vendor
firmware region, Fedora's root copied onto ext4, the a7a-fedora packages installed with dnf
inside the image: both kernels, the a7a Mesa PowerVR driver, the board tools; extlinux with the
open-stack kernel as the default, the DDK kernel as entry 2 and a recovery entry, fstab, user,
sleep masked, first-boot checklist), all privileged steps through tools/priv.sh. This stage
checks the prerequisites (the newest RPM of each package in output/rpms), runs it, then records
the image (sha256, size, default kernel release, packages, source commits) in the registry,
optionally compresses it, and writes the release note next to it.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, sha256_of, tool

logger = logging.getLogger("a7a-fedora.image")


def sudo_works(config: Config) -> Optional[str]:
    """None when `sudo -n tools/priv.sh` answers (exit 2 = the helper's usage error), else the reason."""
    argv = [*config.sudo_prefix, str(config.priv_helper)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"cannot run {argv[0]}: {e}"
    if r.returncode == 2:
        return None
    return f"sudo without a password is not set up for {config.priv_helper} (exit {r.returncode}: {r.stderr.strip() or r.stdout.strip()}); " \
           f"see the sudoers line in its header"


PACKAGES = {"a7a-extlinux": "a7a-extlinux-*.noarch.rpm", "a7a-board-tools": "a7a-board-tools-*.noarch.rpm",
            "a7a-gpu-tools": "a7a-gpu-tools-*.noarch.rpm",
            "mesa-pvr-a7a": "mesa-pvr-a7a-*.aarch64.rpm", "mesa-img-a7a": "mesa-img-a7a-*.aarch64.rpm",
            "kernel-a7a": "kernel-a7a-*.aarch64.rpm",
            "kernel-a7apvr": "kernel-a7apvr-*.aarch64.rpm"}
BUILDERS = {"a7a-extlinux": "tools/build-tools-rpm.sh", "a7a-board-tools": "tools/build-tools-rpm.sh",
            "a7a-gpu-tools": "tools/build-tools-rpm.sh",
            "mesa-pvr-a7a": "tools/build-mesa-rpm.sh", "mesa-img-a7a": "tools/build-mesa-img-rpm.sh",
            "kernel-a7a": "tools/build-rpm.sh (vendor flavour payload)",
            "kernel-a7apvr": "tools/build-rpm.sh (A7A_PVR=1 payload)"}


def build_stamp(rpm: Path) -> str:
    """The Release field every a7a-fedora package carries: a fixed-width YYYYMMDDHHMM build stamp ('' if not so named)."""
    m = re.search(r"-(\d{12})\.[a-z0-9_]+\.rpm$", rpm.name)
    return m.group(1) if m else ""


def image_rpms(config: Config) -> Dict[str, Optional[Path]]:
    """The newest BUILD of each package in output/rpms, the way assemble-image.sh picks them: by the Release stamp,
    not by the Version, which is not fixed width (6.6.98 < 6.6.100, 26.1.8 < 26.1.10 sort wrong as text)."""
    d = config.output_dir / "rpms"
    found = {name: sorted(d.glob(glob), key=lambda q: (build_stamp(q), q.name)) if d.exists() else [] for name, glob in PACKAGES.items()}
    return {name: (rpms[-1] if rpms else None) for name, rpms in found.items()}


def rpm_kernel_facts(rpm: Path) -> Dict:
    """What the release note used to take from output/payload, read from the kernel package instead: the extra
    (out-of-tree) modules and the DTB names with their sha256 (the DTB is extracted with rpm2cpio, a few KB)."""
    facts: Dict = {"extra_modules": [], "dtbs": {}}
    try:
        files = subprocess.run(["rpm", "-qp", "-l", str(rpm)], capture_output=True, text=True, timeout=120, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return facts
    facts["extra_modules"] = sorted(Path(f).name for f in files if "/extra/" in f and f.endswith((".ko", ".ko.xz", ".ko.zst")))
    for f in (f for f in files if f.endswith(".dtb")):
        try:
            with subprocess.Popen(["rpm2cpio", str(rpm)], stdout=subprocess.PIPE) as r2c:
                dtb = subprocess.run(["cpio", "-i", "--quiet", "--to-stdout", "." + f], stdin=r2c.stdout, capture_output=True, timeout=120).stdout
                r2c.stdout.close()
            if dtb:
                import hashlib
                facts["dtbs"][Path(f).name] = hashlib.sha256(dtb).hexdigest()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return facts


def prerequisites(config: Config) -> List[str]:
    missing = []
    if not config.fedora_raw.exists():
        missing.append(f"{config.fedora_raw.name} (run the rootfs stage)")
    for name, rpm in image_rpms(config).items():
        if rpm is None:
            missing.append(f"output/rpms/{PACKAGES[name]} ({BUILDERS[name]})")
    if not config.firmware_region.exists():
        missing.append(f"{config.firmware_region.name} (the vendor boot firmware region from a Debian card)")
    return missing


def newest_image(config: Config) -> Optional[Path]:
    imgs = sorted(config.output_dir.glob("a7a-fedora-*.img"), key=lambda p: p.stat().st_mtime)
    return imgs[-1] if imgs else None


def register_existing(config: Config, registry: StageRegistry, path: Path, log: Optional[str] = None, sha256: Optional[str] = None) -> Dict:
    """Put an image that already exists in output/ into the registry (sha256: the caller's, when it has just hashed
    the file; else from its .sha256 file when present)."""
    sha_file = Path(str(path) + ".sha256")
    sha = sha256 or (sha_file.read_text().split()[0] if sha_file.exists() else sha256_of(path))
    manifest = json.loads(config.manifest_path.read_text()) if config.manifest_path.exists() else {}
    from src.stages.kernel import payload_info
    pay = payload_info(config)
    # an image assembled from the RPM set names its default kernel and packages in sidecars, and its kernel facts come
    # from that package; older images came from output/payload
    kernel_file, rpms_file = Path(str(path) + ".kernel"), Path(str(path) + ".rpms")
    release = kernel_file.read_text().strip() if kernel_file.exists() else pay["release"]
    rpms = rpms_file.read_text().split() if rpms_file.exists() else []
    if kernel_file.exists():
        kernel_rpm = next((config.output_dir / "rpms" / r for r in rpms if r.startswith("kernel-") and f"-{release.replace('-', '_')}-" in r), None)
        if kernel_rpm and kernel_rpm.exists():
            facts = rpm_kernel_facts(kernel_rpm)
        else:
            logger.warning("%s: the kernel package the image was built from (%s) is no longer in output/rpms; no module/DTB facts for the note",
                           path.name, kernel_rpm.name if kernel_rpm else "?")
            facts = {"extra_modules": [], "dtbs": {}}
    else:
        facts = {"extra_modules": pay["extra_modules"], "dtbs": pay["dtbs"]}
    info = {"path": str(path), "sha256": sha, "size": path.stat().st_size, "kernel_release": release, "rpms": rpms,
            "dtbs": facts["dtbs"], "extra_modules": facts["extra_modules"], "fedora_raw": config.fedora_raw_name,
            "kernel_commit": manifest.get("trees", {}).get("kernel-6.6", {}).get("commit"),
            "mirror_commit": manifest.get("mirror", {}).get("commit"), "created": datetime.now(timezone.utc).isoformat()}
    # what the script built (the .build sidecar, since 2026-11-15); without it: an open-stack image with the owner's keys
    build_file, build = Path(str(path) + ".build"), {}
    if build_file.exists():
        build = dict(l.split("=", 1) for l in build_file.read_text().split() if "=" in l)
    info["default_stack"] = build.get("default_stack", "open")
    info["ssh_keys"] = int(build["ssh_keys"]) if build.get("ssh_keys", "").isdigit() else None
    info["previous_kernels"] = [k for k in build.get("previous_kernels", "").split(",") if k]
    info["updated"] = build.get("updated") or None
    if log:
        info["log"] = log
    registry.record_image(path.name, info)
    return info


def write_release_note(config: Config, registry: StageRegistry, name: str) -> Path:
    img = registry.images().get(name, {})
    manifest = json.loads(config.manifest_path.read_text()) if config.manifest_path.exists() else {}
    lines = [f"# {name}", "",
             f"Built {img.get('created', '?')} on this laptop by a7a-fedora.", "",
             "## Contents",
             f"- rootfs: {config.fedora_raw_name} (official Fedora 44 KDE Plasma aarch64 image)",
             f"- kernel: {img.get('kernel_release', '?')} from radxa kernel {img.get('kernel_commit', '?')} + Rabs9 patches (mirror {img.get('mirror_commit', '?')})",
             f"- device tree: the overclocked sun60i-a733-cubie-a7a (tx-delay 9, CPU OPP to 3000 MHz)",
             f"- extra modules: {', '.join(img.get('extra_modules', [])) or '?'}",
             f"- packages installed with dnf inside the image: {', '.join(img.get('rpms', [])) or 'none (payload image)'}",
             f"- sha256: {img.get('sha256', '?')}", f"- size: {img.get('size', '?')} bytes",
             "", "## Source trees (work/manifest.json)"]
    for tname, info in manifest.get("trees", {}).items():
        lines.append(f"- {tname}: {info.get('commit', '?')} ({info.get('branch', '?')})")
    lines += ["", "## Tests"]
    tests = img.get("tests", [])
    if not tests:
        lines.append("- not yet tested")
    for t in tests:
        lines.append(f"- {t['timestamp'][:19]} {t['mode']}: {t['verdict']}" + (f" ({t['reason']})" if t.get("reason") else "") +
                     (f", milestones: {', '.join(t['milestones'])}" if t.get("milestones") else ""))
    if img.get("flashes"):
        lines += ["", "## Flashed"]
        for f in img["flashes"]:
            lines.append(f"- {f['timestamp'][:19]} {f['device']} verified={f['verified']} backup={f.get('backup')}")
    if img.get("default_stack") == "ddk":
        entries = ("- extlinux entries: fedora-<DDK kernel> (default), fedora-<open-stack kernel> (entry 2, the fallback; "
                   "prepare616.sh boots it), fedora-recovery (multi-user, loglevel 7)")
        gpu = ("- GPU: vendor DDK by default (pvrsrvkm + the closed userspace in /opt/pvr), with mesa-img-a7a switched on for "
               "the whole desktop (/etc/a7a/pvr-wl.enabled): Plasma and GLES programs on the PowerVR; the open stack "
               "(drm/imagination + the a7a Mesa PowerVR Vulkan driver in /usr/local) is the fallback entry; Fedora's own "
               "kernels excluded in dnf.conf, kernel-a7a/kernel-a7apvr allowed")
    else:
        entries = ("- extlinux entries: fedora-<open-stack kernel> (default), fedora-<DDK kernel> (entry 2, the fallback; backtoddk.sh "
                   "switches the session config to it), fedora-recovery (multi-user, loglevel 7)")
        gpu = ("- GPU: open stack by default (drm/imagination kernel driver + the a7a Mesa PowerVR Vulkan driver in /usr/local, "
               "Fedora's PowerVR ICD masked); Fedora's own kernels excluded in dnf.conf, kernel-a7a/kernel-a7apvr allowed")
    keys = img.get("ssh_keys")
    ssh = ("- ssh: no ssh keys in the image (password login only)" if keys == 0 else
           f"- ssh: {keys} of the owner's public keys in the image (a build for his own board, not for publication)" if keys else
           "- ssh: the owner's public keys are in the image when they were on the build machine")
    if img.get("previous_kernels"):
        entries += (f"\n- extlinux entries 3 and 4: the previous kernels {', '.join(img['previous_kernels'])}, the way back at the "
                    "first boot of newly built kernels on a board (a hardware test build, not for publication)")
    fedora = (f"- Fedora packages: the release with its updates as of {img['updated']} (dnf upgrade at build time; the package list "
              "is in the .packages file beside the image)" if img.get("updated") else
              "- Fedora packages: the release as Fedora published it, no updates applied (the first dnf upgrade is a large one)")
    lines += ["", "## Notes", "- root by device path (/dev/mmcblk0p3), no initramfs: the same boot chain as the Debian card",
              fedora, entries, gpu,
              "- user radxa / radxa (sudo); sleep, suspend, hibernate masked; sshd and serial-getty on ttyAS0 enabled", ssh, ""]
    note = config.output_dir / (name[:-4] + ".release.md")
    note.write_text("\n".join(lines))
    return note


def run(config: Config, registry: StageRegistry, dry_run: bool = False, compress: bool = False,
        size_gb: int = 12, default_stack: str = "open", keys: bool = True, previous_kernels: bool | str = False,
        update: bool = False, on_line=None) -> Dict:
    # default_stack: the kernel the image boots by default, "open" (drm/imagination + Mesa, every image until
    # 2026-11-15) or "ddk" (the vendor driver with mesa-img-a7a switched on: docs/RADXA-SYNC-2026-11-15.md)
    # keys: the owner's ssh public keys in the image (False for an image that leaves the house: password login only)
    # previous_kernels: earlier kernels as boot entries 3 and 4 (a hardware test build of kernels that have never run
    # on the board: the way back without another card). True = the newest other release of each flavour in
    # output/rpms, or "<vendor release>,<open release>" to name them
    # update: `dnf upgrade` inside the image at build time (online): Fedora's updates as of the build day
    if default_stack not in ("open", "ddk"):
        raise ValueError(f"default_stack must be 'open' or 'ddk', not {default_stack!r}")
    t0 = time.time()
    registry.begin_stage("image", dry_run=dry_run)
    result: Dict = {"stage": "image", "status": "passed", "commands": []}
    missing = prerequisites(config)
    if missing and not dry_run:
        result.update(status="failed", reason="missing: " + "; ".join(missing))
        registry.record_stage("image", "failed", notes=result["reason"])
        return result
    if missing:
        result["warnings"] = ["missing: " + m for m in missing]
    reason = None if dry_run else sudo_works(config)
    if reason:
        result.update(status="dry_run", reason=reason)
        run_tool(config, registry, "image", [str(tool(config, "assemble-image.sh")), str(size_gb)], dry_run=True)
        registry.record_stage("image", "dry_run", notes=reason)
        return result

    env = {"A7A_RUN": "1", "A7A_OUTPUT": str(config.output_dir), "A7A_PAYLOAD": str(config.payload_dir),
           "A7A_IMAGE_DEFAULT": default_stack, "A7A_IMAGE_KEYS": "1" if keys else "0",
           "A7A_IMAGE_PREVIOUS_KERNELS": previous_kernels if isinstance(previous_kernels, str) else ("1" if previous_kernels else "0"),
           "A7A_IMAGE_UPDATE": "1" if update else "0"}
    r = run_tool(config, registry, "image", [str(tool(config, "assemble-image.sh")), str(size_gb)], env=env,
                 dry_run=dry_run, on_line=on_line)
    result["commands"].append(r.command_line)
    result["log"] = str(r.log_path)
    if dry_run:
        if compress:
            result["commands"].append("xz -T0 -k -6 <image>")
        result["status"] = "dry_run"
        registry.record_stage("image", "dry_run", notes="; ".join(result["commands"]))
        return result
    if not r.ok:
        result.update(status="failed", reason=f"assemble-image.sh exited {r.exit_code}; log {r.log_path}")
        registry.record_stage("image", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result

    m = re.search(r"done: (\S+\.img)", r.output)
    path = Path(m.group(1)) if m else newest_image(config)
    if not path or not path.exists():
        result.update(status="failed", reason="the script finished but no image was found in output/")
        registry.record_stage("image", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result
    info = register_existing(config, registry, path, log=str(r.log_path))
    sha, pay = info["sha256"], {"release": info["kernel_release"]}
    result.update(image=path.name, path=str(path), sha256=sha, size=info["size"], kernel_release=pay["release"])

    if compress:
        rx = run_tool(config, registry, "image", ["xz", "-T0", "-k", "-6", "-f", str(path)], on_line=on_line)
        result["commands"].append(rx.command_line)
        if rx.ok:
            xz = Path(str(path) + ".xz")
            xsha = sha256_of(xz)
            Path(str(xz) + ".sha256").write_text(f"{xsha}  {xz.name}\n")
            registry.record_image(path.name, {"xz": str(xz), "xz_sha256": xsha, "xz_size": xz.stat().st_size})
            result.update(xz=str(xz), xz_sha256=xsha)
        else:
            result["warnings"] = [f"xz exited {rx.exit_code}; the .img is fine, the .xz is not there"]
    note = write_release_note(config, registry, path.name)
    result["release_note"] = str(note)
    registry.record_stage("image", "passed", artifacts=[str(path), str(note)],
                          notes=f"{path.name}: {info['size']} bytes, kernel {pay['release']}", duration_seconds=time.time() - t0,
                          extra={"image": path.name, "sha256": sha})
    return result
