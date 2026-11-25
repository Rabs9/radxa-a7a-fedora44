"""Stage 2, kernel: cross-compile the Rabs9 kernel and build the out-of-tree modules.

The build itself is tools/build-kernel.sh (patches once per checkout, the overclocked DTS
and defconfig from the mirror's configs/, virtio and nftables for Fedora, Image + modules,
the board DTB with the BSP bindings on the include path, the payload under output/payload)
followed by tools/build-extras.sh (AIC8800 WiFi driver from BSP 1.4.6, PowerVR pvrsrvkm,
depmod). This stage runs them, records both logs, and summarises the payload: kernel release,
DTB checksums, module count, firmware, whether the regulatory database is embedded.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.base import run_tool, sha256_of, tool

logger = logging.getLogger("a7a-fedora.kernel")


def payload_info(config: Config) -> Dict:
    """What is in output/payload right now (works on a payload made by any build)."""
    pay = config.payload_dir
    rel_file = pay / "kernelrelease"
    info: Dict = {"payload": str(pay), "release": None, "image_bytes": 0, "dtbs": {}, "modules": 0,
                  "extra_modules": [], "firmware": [], "regulatory_db_embedded": False}
    if not rel_file.exists():
        return info
    rel = rel_file.read_text().strip()
    info["release"] = rel
    image = pay / "boot" / f"Image-{rel}"
    if image.exists():
        info["image_bytes"] = image.stat().st_size
        with open(image, "rb") as f:
            info["regulatory_db_embedded"] = b"RGDB" in f.read()
        info["image_sha256"] = sha256_of(image)
    for dtb in sorted((pay / "boot" / "dtb" / "allwinner").glob("*.dtb")) if (pay / "boot" / "dtb" / "allwinner").exists() else []:
        info["dtbs"][dtb.name] = sha256_of(dtb)
    mods = pay / "lib" / "modules" / rel
    if mods.exists():
        info["modules"] = sum(1 for p in mods.rglob("*.ko*") if "extra" not in p.parts)
        info["extra_modules"] = sorted(p.name for p in (mods / "extra").glob("*.ko*")) if (mods / "extra").exists() else []
    fw = pay / "lib" / "firmware"
    if fw.exists():
        info["firmware"] = sorted(str(p.relative_to(fw)) for p in fw.rglob("*") if p.is_file())
    if shutil.which("dtc") and info["dtbs"]:
        info["dtb_summary"] = dtb_summary(pay / "boot" / "dtb" / "allwinner" / next(iter(info["dtbs"])))
    return info


def dtb_summary(dtb: Path) -> Dict:
    """The two things to check in the DTB: the RGMII tx-delay and the top CPU OPP."""
    import re
    import subprocess
    try:
        out = subprocess.run(["dtc", "-I", "dtb", "-O", "dts", str(dtb)], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    delays = re.findall(r"tx-delay = <(0x[0-9a-f]+|\d+)>", out)
    opps = [int(h, 16) for h in re.findall(r"opp-hz = <0x0+ (0x[0-9a-f]+)>", out)]     # dtc prints <0x00 0x...>
    return {"tx_delay": int(delays[0], 0) if delays else None, "max_opp_mhz": max(opps) // 1_000_000 if opps else None}


def run(config: Config, registry: StageRegistry, dry_run: bool = False, clean: bool = False,
        extras: bool = True, rpm: bool = False, on_line=None) -> Dict:
    t0 = time.time()
    registry.begin_stage("kernel", dry_run=dry_run)
    result: Dict = {"stage": "kernel", "status": "passed", "commands": [], "logs": []}
    cc = config.cross_compile + "gcc"
    problems = []
    if not shutil.which(cc):
        problems.append(f"cross compiler {cc} not on PATH (dnf install gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu)")
    if not config.kernel_dir.exists():
        problems.append(f"no kernel tree at {config.kernel_dir}: run the sources stage first")
    if problems and not dry_run:
        result.update(status="failed", reason="; ".join(problems))
        registry.record_stage("kernel", "failed", notes=result["reason"])
        return result
    if problems:
        result["warnings"] = problems
    env = {"A7A_RUN": "1", "A7A_PAYLOAD": str(config.payload_dir), "A7A_WORK": str(config.work_dir), "A7A_MIRROR": str(config.mirror_dir),
           "A7A_OUTPUT": str(config.output_dir)}

    steps: List[List[str]] = []
    if clean:
        steps.append(["make", "-C", str(config.kernel_dir), "ARCH=arm64", "BSP_TOP=bsp/", "mrproper"])
    steps.append([str(tool(config, "build-kernel.sh"))])
    if extras:
        steps.append([str(tool(config, "build-extras.sh"))])
    if rpm:
        steps.append([str(tool(config, "build-rpm.sh"))])

    for argv in steps:
        r = run_tool(config, registry, "kernel", argv, env=env, dry_run=dry_run, on_line=on_line)
        result["commands"].append(r.command_line)
        result["logs"].append(str(r.log_path))
        if not r.ok:
            result.update(status="failed", reason=f"{Path(argv[0]).name} exited {r.exit_code}" + (" (timed out)" if r.timed_out else "") + f"; log {r.log_path}")
            registry.record_stage("kernel", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
            return result

    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("kernel", "dry_run", notes="; ".join(result["commands"]), duration_seconds=time.time() - t0)
        return result

    info = payload_info(config)
    result.update(info)
    rpms = sorted((config.output_dir / "rpms").glob("kernel-a7a*-*.rpm")) if (config.output_dir / "rpms").exists() else []
    result["rpms"] = [str(r) for r in rpms]
    if not info["release"]:
        result.update(status="failed", reason=f"build finished but {config.payload_dir}/kernelrelease is missing")
        registry.record_stage("kernel", "failed", notes=result["reason"], duration_seconds=time.time() - t0)
        return result
    notes = (f"{info['release']}: {info['modules']} modules + {len(info['extra_modules'])} extra, "
             f"{len(info['dtbs'])} dtb, regdb {'embedded' if info['regulatory_db_embedded'] else 'MISSING'}")
    registry.record_stage("kernel", "passed", artifacts=[str(config.payload_dir)], notes=notes,
                          duration_seconds=time.time() - t0,
                          extra={"release": info["release"], "dtbs": info["dtbs"], "modules": info["modules"],
                                 "extra_modules": info["extra_modules"], "logs": result["logs"],
                                 "regulatory_db_embedded": info["regulatory_db_embedded"],
                                 "dtb_summary": info.get("dtb_summary")})
    return result
