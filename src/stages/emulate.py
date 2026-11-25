"""Stage 5, emulate: boot an image in qemu-system-aarch64 -M virt before anything touches a card.

tools/emulate.sh runs QEMU with -snapshot (the image is never written), watches the serial
transcript for milestones, runs the ssh checks once a login prompt appears, and prints one
VERDICT line. Modes: stock (Fedora's own kernel via EDK2: proves the rootfs), kernel (the Rabs9
kernel with virtio built in, root=/dev/vda3: proves the kernel, modules and the injected
userspace), interactive (console attached, ssh on 127.0.0.1:2222; not a test). A kernel-mode
PASS is what marks an image tested; the flash stage refuses anything else.
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from pathlib import Path
from typing import Dict, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, sha256_of, tool

logger = logging.getLogger("a7a-fedora.emulate")

MODES = ("stock", "kernel", "interactive", "interactive-stock")


def parse_output(text: str) -> Dict:
    milestones = re.findall(r"milestone: (.+?)\s*$", text, re.M)
    checks = {m.group(1): m.group(2).strip() for m in re.finditer(r"^\s*check: ([\w-]+) = (.*)$", text, re.M)}
    v = re.search(r"^VERDICT: (PASS|FAIL)(?: - (.*))?$", text, re.M)
    return {"milestones": milestones, "checks": checks, "verdict": v.group(1) if v else None,
            "reason": (v.group(2) or "").strip() if v else None}


PACKAGES = ("a7a-extlinux", "a7a-board-tools", "a7a-gpu-tools", "mesa-pvr-a7a", "mesa-img-a7a", "kernel-a7a", "kernel-a7apvr")


def expected_packages(image: Path) -> tuple:
    """The package names an image was assembled with (its <image>.rpms sidecar), else today's set: an image built
    before a package existed is not failed for lacking it."""
    sidecar = Path(str(image) + ".rpms")
    names = []
    if sidecar.exists():
        for line in sidecar.read_text().split():
            parts = line.split("-")
            name = "-".join(itertools.takewhile(lambda w: not w[:1].isdigit(), parts))
            if name:
                names.append(name)
    return tuple(names) or PACKAGES


def packaged_image_problem(image: Path, checks: Dict[str, str]) -> Optional[str]:
    """An image assembled from the RPM set (it has a <image>.kernel sidecar naming its default kernel) must show its
    packages installed, that kernel as the extlinux default, Fedora's PowerVR ICD masked and, since a7a-gpu-tools
    carries a7a-gpu-session.service (2026-09-10, images that also carry a7a-board-tools), the session config matching
    the booted kernel; None when fine or when the image predates the packages (docs/PACKAGING-2026-09-09.md)."""
    sidecar = Path(str(image) + ".kernel")
    if not sidecar.exists():
        return None
    rel = sidecar.read_text().strip()
    pk = checks.get("a7a_packages", "")
    expected = expected_packages(image)
    # a package the guest reports "not installed" counts only when the image was built with it (the guest is asked
    # for every package there is; an image from before a7a-panel and a7a-repo lacks them: 2026-11-24)
    missing = [p for p in expected if f"{p}-" not in pk.replace("package ", "")] + \
              [n for w in pk.split("package ")[1:] if "not installed" in w and (n := w.split()[0]) in expected]
    if missing or not pk:
        return f"a7a-fedora packages missing in the image: {', '.join(sorted(set(missing))) or 'none of them reported'} (check a7a_packages = {pk!r})"
    default = checks.get("extlinux_default", "")
    if default != f"fedora-{rel}":
        return f"the extlinux default is {default or 'unknown'}, not fedora-{rel} (the default kernel the image was built for)"
    icd = checks.get("pvr_icd", "")
    if "disabled-by-a7a" not in icd or icd.replace("powervr_mesa_icd.aarch64.json.disabled-by-a7a", "").count("powervr_mesa_icd.aarch64.json") != 1:
        return f"Fedora's PowerVR ICD is not masked in favour of the a7a driver (check pvr_icd = {icd!r})"
    if "a7a-board-tools" in expected:
        session, unit = checks.get("gpu_session", ""), checks.get("gpu_session_unit", "")
        # the config must match AND the unit must have run to completion (a config that happens to be right on disk
        # while the unit timed out would hide exactly the failure the unit exists to prevent)
        if "yes" not in session.split() or "success" not in unit.split():
            return f"a7a-gpu-session.service did not leave the session config matching the booted kernel (check gpu_session = {session!r}, " \
                   f"gpu_session_unit = {unit!r})"
    if "mesa-img-a7a" in expected:
        # its loader entry is written at boot only when pvrsrvkm drives the GPU. The emulator has no GPU device: the
        # open-stack kernel gets the open config and the vendor kernel the software session (a7a-gpu-session), so
        # with either default kernel the entry and the loader cache must be clean (docs/DDK-WAYLAND-2026-09-10.md)
        pwl = checks.get("pvr_wl", "")
        if not pwl.startswith("absent"):
            return f"mesa-img-a7a's loader entry is not absent in the emulator (check pvr_wl = {pwl!r})"
    # the image has its user (radxa) and promises the login screen: KDE's first-run wizard (plasma-setup.service, on by
    # Fedora's preset until /etc/plasma-setup-done exists) must not hold the seat; a run without the check is not judged
    sessions = checks.get("sessions")
    if sessions is not None and "plasma-setup:" in sessions:
        return f"KDE's first-run wizard (plasma-setup) holds the seat at the first boot instead of the login greeter (check sessions = {sessions!r})"
    # every directory a package owns under /var must be there: Fedora's raw image keeps /var in a subvolume of its own,
    # and an image assembled without it boots all the same (tmpfiles rebuilds the skeleton) while logrotate never
    # rotates and package scriptlets lose /var/lib/rpm-state (every image before 2026-11-15). "N missing of M, T
    # tested: ..."; a run without the check is not judged. Not a pass: a check that reports nothing, an rpm that lists
    # nothing (M under 100), a root half that did not run or saw a part of the list (T is not M: sudo failed)
    var_dirs = checks.get("var_dirs")
    if var_dirs is not None:
        m = re.match(r"(\d+) missing of (\d+), (\d+) tested", var_dirs)
        if not m or int(m.group(1)) > 0 or int(m.group(2)) < 100 or m.group(3) != m.group(2):
            return f"directories that packages own are missing under /var: was Fedora's var subvolume copied? (check var_dirs = {var_dirs!r})"
    # the login screen must really be there. 2026-11-16, the first image with Fedora's updates: KWin 6.8 has no QPainter
    # backend, the greeter's compositor exited 40 s after it started, and every check above passed (a login prompt on
    # the serial console, a greeter session in logind, plasmalogin active) in front of a black screen. Runs without
    # these checks are not judged; a check that is there and reports nothing is a failure
    comp = checks.get("greeter_compositor")
    if comp is not None and not comp.startswith("running"):
        return f"the login screen's compositor is not running (check greeter_compositor = {comp!r}): nothing is drawn"
    dumps = checks.get("coredumps")
    if dumps is not None and "kwin_wayland" in dumps:
        return f"kwin_wayland dumped core during the boot (check coredumps = {dumps!r})"
    screen = checks.get("screen")
    if screen is not None:
        m = re.search(r"colours=(\d+)", screen)
        if not m or int(m.group(1)) < 256:      # a login screen has thousands; black is 1 or 2, a text console a handful
            return f"the screen does not show a login screen (check screen = {screen!r}; the picture is beside the transcript)"
    # logrotate, started once by the check, must not fail (a run without the check is not judged)
    rotate = checks.get("logrotate")
    if rotate is not None and rotate.split()[:1] == ["failed"]:
        return f"logrotate.service fails in this image (check logrotate = {rotate!r}): no log would ever be rotated"
    # the device tree of every boot entry must be there. The emulator boots its own machine and never reads the file:
    # a missing one would show on the board only. A run without the check is not judged
    dtb = checks.get("boot_dtb")
    if dtb is not None and ("MISSING" in dtb or not dtb.strip()):
        return f"a boot entry names a device tree that is not in the image (check boot_dtb = {dtb!r})"
    # an image built with earlier kernels (its .build sidecar names them, vendor first) must show them as entries 3
    # and 4 of the menu: they are the way back at the first boot of new kernels, and the package names cannot tell
    # (two kernel-a7a, two kernel-a7apvr). A run without the check is not judged
    build = Path(str(image) + ".build")
    previous = []
    if build.exists():
        previous = [k for l in build.read_text().split() if l.startswith("previous_kernels=") for k in l.split("=", 1)[1].split(",") if k]
    entries = checks.get("extlinux_entries")
    if previous and entries is not None and entries.split()[2:2 + len(previous)] != [f"fedora-{k}" for k in previous]:
        return (f"the earlier kernels {', '.join(previous)} are not entries 3 and 4 of the boot menu "
                f"(check extlinux_entries = {entries!r})")
    return None


def pick_image(config: Config, image: Optional[Path | str]) -> Optional[Path]:
    if image:
        p = Path(image)
        return p if p.is_absolute() else config.output_dir / p
    from src.stages.image import newest_image
    return newest_image(config)


def run(config: Config, registry: StageRegistry, mode: str = "kernel", image: Optional[Path | str] = None,
        dry_run: bool = False, on_line=None) -> Dict:
    t0 = time.time()
    registry.begin_stage("emulate", dry_run=dry_run)
    result: Dict = {"stage": "emulate", "status": "passed", "mode": mode, "commands": []}
    if mode not in MODES:
        result.update(status="failed", reason=f"mode {mode!r} is not one of {', '.join(MODES)}")
        registry.record_stage("emulate", "failed", notes=result["reason"])
        return result
    path = pick_image(config, image)
    if not path or not path.exists():
        if not dry_run:
            result.update(status="failed", reason=f"no image to boot ({path or 'output/ has no a7a-fedora-*.img'}); run the image stage")
            registry.record_stage("emulate", "failed", notes=result["reason"])
            return result
        result["warnings"] = ["no image in output/ yet; the image stage makes one"]
        path = path or (config.output_dir / "a7a-fedora-44-kde-<date>.img")
    result.update(image=path.name, path=str(path))
    sha = None
    if not dry_run and not mode.startswith("interactive"):
        # the verdict belongs to bytes, so the file is hashed before it boots (QEMU runs with -snapshot and does not
        # change it). A file the registry does not describe (made by tools/master-image.sh, or rebuilt outside the image
        # stage under the same name) is described first: another file's verdicts must not count for this one
        sha = sha256_of(path)
        if registry.images().get(path.name, {}).get("sha256") != sha:
            from src.stages.image import register_existing
            register_existing(config, registry, path, sha256=sha)
    env = {"TIMEOUT": str(config.emulate_timeout), "A7A_OUTPUT": str(config.output_dir), "A7A_PAYLOAD": str(config.payload_dir)}
    r = run_tool(config, registry, "emulate", [str(tool(config, "emulate.sh")), mode, str(path)], env=env, dry_run=dry_run,
                 timeout=config.emulate_timeout + 120, on_line=on_line)
    result["commands"].append(r.command_line)
    result["log"] = str(r.log_path)
    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("emulate", "dry_run", notes=r.command_line)
        return result
    parsed = parse_output(r.output)
    result.update(parsed)
    transcript = re.search(r"transcript (\S+)", r.output)
    result["transcript"] = transcript.group(1) if transcript else None
    if mode.startswith("interactive"):
        result["status"] = "passed" if r.ok else "failed"
        registry.record_stage("emulate", result["status"], notes=f"interactive session exit {r.exit_code}", duration_seconds=time.time() - t0)
        return result
    verdict = parsed["verdict"] or ("FAIL" if not r.ok else None)
    if verdict is None:
        verdict = "FAIL"
        result["reason"] = f"no VERDICT line from emulate.sh (exit {r.exit_code}); log {r.log_path}"
    if r.timed_out:
        result["reason"] = f"emulate.sh did not finish within {config.emulate_timeout + 120}s"
        verdict = "FAIL"
    if verdict == "PASS" and parsed["checks"].get("repart_masked") not in (None, "masked"):
        verdict = "FAIL"
        result["reason"] = ("systemd-repart is not masked in this image: on the board it creates a partition over the vendor boot "
                            "firmware area and destroys boot0 (2026-09-07)")
    if verdict == "PASS":
        problem = packaged_image_problem(path, parsed["checks"])
        if problem:
            verdict = "FAIL"
            result["reason"] = problem
    result["verdict"] = verdict
    registry.record_test(path.name, mode=mode, verdict=verdict, log=str(r.log_path), milestones=parsed["milestones"],
                         checks=parsed["checks"], reason=result.get("reason"), transcript=result["transcript"],
                         duration_seconds=round(r.duration, 1), sha256=sha)
    result["status"] = "passed" if verdict == "PASS" else "failed"
    result["image_tested"] = registry.image_tested(path.name)
    registry.record_stage("emulate", result["status"],
                          notes=f"{mode} on {path.name}: {verdict}" + (f" - {result.get('reason')}" if result.get("reason") else ""),
                          duration_seconds=time.time() - t0,
                          extra={"image": path.name, "verdict": verdict, "milestones": parsed["milestones"], "checks": parsed["checks"]})
    return result
