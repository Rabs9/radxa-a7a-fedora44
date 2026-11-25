"""Stage 1, sources: the Rabs9 kernel repo (the mirror) and the four Radxa trees under work/, pinned.

BUILDING.md names the trees and branches; this stage clones what is missing, records every
tree's commit in work/manifest.json the first time, and afterwards refuses to build from a
tree whose HEAD moved unless it is run with update=True (which rewrites the manifest). It
also reports the patch state: tools/build-kernel.sh applies scripts/apply-patches.sh once per
checkout and leaves work/.patches-applied; some of the patches can be checked by reading files.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool

logger = logging.getLogger("a7a-fedora.sources")

MIRROR_REMOTE = "https://github.com/Rabs9/radxa-cubie-a7a-kernel"


def trees(config: Config) -> List[Tuple[str, str, str]]:
    """(directory under work/, repo url, branch) for the four Radxa trees."""
    return [
        ("kernel-6.6", config.kernel_repo, config.kernel_branch),
        ("allwinner-bsp-1.4.8", config.bsp_repo, config.bsp_branch),
        ("allwinner-device-1.4.8", config.device_repo, config.device_branch),
        ("allwinner-target", config.target_repo, config.target_branch),
    ]


def _git(config: Config, registry: StageRegistry, path: Path, *args: str) -> Optional[str]:
    r = run_tool(config, registry, "sources", ["git", "-C", str(path), *args])
    return r.output.strip() if r.ok else None


def tree_state(config: Config, registry: StageRegistry, path: Path) -> Dict:
    if not (path / ".git").exists():
        return {"path": str(path), "present": False, "commit": None, "branch": None}
    return {
        "path": str(path), "present": True,
        "commit": _git(config, registry, path, "rev-parse", "HEAD"),
        "branch": _git(config, registry, path, "rev-parse", "--abbrev-ref", "HEAD"),
        "remote": _git(config, registry, path, "remote", "get-url", "origin"),
    }


def patch_state(config: Config) -> Dict:
    """What tools/build-kernel.sh's patch step has done to the trees, from the files themselves."""
    checks: Dict[str, str] = {}
    dts = config.device_dir / "configs" / "cubie_a7a" / "linux-6.6" / "board.dts"
    if dts.exists():
        t = dts.read_text()
        checks["rgmii-tx-delay"] = "applied" if "tx-delay = <9>" in t else ("not applied" if "tx-delay = <12>" in t else "unknown")
    c = config.kernel_dir / "drivers" / "cpufreq" / "cpufreq-dt-platdev.c"
    if c.exists():
        checks["cpufreq-blocklist"] = "applied" if "sun60iw2p1" in c.read_text() else "not applied"
    autogen = config.bsp_dir / "include" / "sunxi-autogen.h"
    if config.bsp_dir.exists():
        checks["sunxi-autogen.h"] = "applied" if autogen.exists() else "not applied"
    # Radxa's fixes from after the manifest's pin (tools/patches/kernel/0001, tools/patches/0006, the sed in build-kernel.sh)
    inet, tcp = (config.kernel_dir / "net" / "ipv4" / f for f in ("af_inet.c", "tcp_ipv4.c"))
    if inet.exists() and tcp.exists():
        fixed = ["kfree_rcu(rcu_dereference_protected(inet->inet_opt, 1), rcu)" in inet.read_text(),
                 "kfree_rcu(rcu_dereference_protected(inet_rsk(req)->ireq_opt, 1), rcu)" in tcp.read_text()]
        checks["ipv4-inet-opt-rcu"] = "applied" if all(fixed) else ("unknown" if any(fixed) else "not applied")
    c = config.bsp_dir / "drivers" / "drm" / "sunxi_device" / "hardware" / "lowlevel_hdmi20" / "dw_i2cm.c"
    if c.exists():
        checks["hdmi-ddc-retry"] = "applied" if "buf--;" in c.read_text() else "not applied"
    dtsi = config.kernel_dir / "arch" / "arm64" / "boot" / "dts" / "allwinner" / "sun60iw2p1.dtsi"
    if dtsi.exists():
        # [^{}]: the sensor line of the zone itself, never the next zone's (little cluster = ths 0, big = ths 3)
        t = dtsi.read_text()
        ths = [m.group(1) if m else None for m in (re.search(z + ": " + z + r" \{[^{}]*?thermal-sensors = <&ths (\d+)>", t)
                                                   for z in ("cpul_thermal_zone", "cpub_thermal_zone"))]
        checks["cpu-thermal-sensors"] = {("0", "3"): "applied", ("3", "0"): "not applied"}.get(tuple(ths), "unknown")
    return {"applied": (config.work_dir / ".patches-applied").exists(),
            "marker": str(config.work_dir / ".patches-applied"), "checks": checks}


def run(config: Config, registry: StageRegistry, dry_run: bool = False, update: bool = False,
        on_line=None) -> Dict:
    t0 = time.time()
    registry.begin_stage("sources", dry_run=dry_run)
    result: Dict = {"stage": "sources", "status": "passed", "trees": {}, "drift": [], "warnings": [],
                    "commands": [], "manifest": str(config.manifest_path)}
    config.work_dir.mkdir(parents=True, exist_ok=True)

    def clone(url: str, branch: str, dest: Path) -> bool:
        r = run_tool(config, registry, "sources", ["git", "clone", "--branch", branch, "--depth", "1", url, str(dest)],
                     dry_run=dry_run, on_line=on_line)
        result["commands"].append(r.command_line)
        if not r.ok:
            result["warnings"].append(f"clone of {dest.name} failed (exit {r.exit_code}, see {r.log_path})")
        return r.ok

    # the mirror of the Rabs9 kernel repo
    if not (config.mirror_dir / ".git").exists():
        clone(config.mirror_repo, "main", config.mirror_dir)
    mirror = tree_state(config, registry, config.mirror_dir) if not dry_run else {"present": (config.mirror_dir / ".git").exists()}
    result["mirror"] = mirror
    if mirror.get("present") and mirror.get("remote") and "radxa-cubie-a7a-kernel" not in mirror["remote"]:
        result["warnings"].append(f"mirror remote is {mirror['remote']}, expected {MIRROR_REMOTE}")

    # the four Radxa trees
    for name, url, branch in trees(config):
        dest = config.work_dir / name
        if not (dest / ".git").exists():
            clone(url, branch, dest)
        state = tree_state(config, registry, dest) if not dry_run else {"present": (dest / ".git").exists(), "commit": None, "branch": None}
        state["expected_branch"] = branch
        if state.get("present") and state.get("branch") not in (branch, None, "HEAD"):
            result["warnings"].append(f"{name} is on branch {state['branch']}, BUILDING.md pins {branch}")
        result["trees"][name] = state

    result["patches"] = patch_state(config)

    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("sources", "dry_run", notes="dry run: " + "; ".join(result["commands"] or ["nothing to clone"]),
                              duration_seconds=time.time() - t0)
        return result

    missing = [n for n, s in result["trees"].items() if not s.get("present")]
    if missing or not mirror.get("present"):
        result["status"] = "failed"
        result["reason"] = "missing: " + ", ".join((["mirror"] if not mirror.get("present") else []) + missing)
        registry.record_stage("sources", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result

    # the manifest: written once, then enforced
    current = {n: {"commit": s["commit"], "branch": s["branch"]} for n, s in result["trees"].items()}
    if config.manifest_path.exists() and not update:
        pinned = json.loads(config.manifest_path.read_text())
        for n, info in pinned.get("trees", {}).items():
            if n in current and current[n]["commit"] != info.get("commit"):
                result["drift"].append(n)
        if result["drift"]:
            result["status"] = "failed"
            result["reason"] = ("commit differs from the manifest for " + ", ".join(result["drift"]) +
                                "; run `sources --update` to accept the new commits")
            registry.record_stage("sources", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
            return result
    else:
        manifest = {"written": datetime.now(timezone.utc).isoformat(), "mirror": {"commit": mirror.get("commit"), "remote": mirror.get("remote")},
                    "trees": current}
        config.manifest_path.write_text(json.dumps(manifest, indent=2))
        result["manifest_written"] = True

    notes = "mirror " + str(mirror.get("commit"))[:9] + "; " + ", ".join(f"{n} {s['commit'][:9]}" for n, s in result["trees"].items())
    registry.record_stage("sources", "passed", artifacts=[str(config.mirror_dir), str(config.work_dir)], notes=notes,
                          duration_seconds=time.time() - t0)
    return result
