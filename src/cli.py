"""a7a-fedora's command line: one subcommand per stage, one for the chain, JSON for every result.

    python3 -m src.cli [--config a7a-fedora.json] [--json] [--dry-run] [-v] <command> [options]

Exit codes: 0 the stage passed (or dry-run), 1 it failed, 2 it refused (a gate said no),
3 it was skipped. With --json the result dict is the only thing on stdout, so another program
(or an LLM driving this tool) can read it. Without it, a short human summary and the reason.
Nothing here writes a card or talks to the board unless you ask for flash/serial/update.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import click

from src.config import Config
from src.registry import StageRegistry
from src.utils import setup_logging

logger = logging.getLogger("a7a-fedora.cli")

EXIT = {"passed": 0, "dry_run": 0, "failed": 1, "refused": 2, "skipped": 3, "rolled_back": 1}
CHAIN = ["sources", "kernel", "rootfs", "image", "emulate"]


def _ctx(ctx) -> (Config, StageRegistry, bool, bool):
    o = ctx.obj
    return o["config"], o["registry"], o["json"], o["dry_run"]


def _emit(ctx, result: Dict[str, Any], summary: Optional[str] = None) -> None:
    if ctx.obj["json"]:
        click.echo(json.dumps(result, indent=1, default=str))
    else:
        status = result.get("status", "?")
        line = f"{result.get('stage', 'stage')}: {status}"
        if result.get("reason"):
            line += f" - {result['reason']}"
        click.echo(line)
        if summary:
            click.echo(summary)
        if result.get("next_step"):
            click.echo("next: " + result["next_step"])
        if status == "dry_run" and result.get("commands"):
            for c in result["commands"]:
                click.echo("  would run: " + str(c))
    ctx.exit(EXIT.get(result.get("status"), 1))


def _printer(ctx):
    return None if ctx.obj["json"] else (lambda line: click.echo(line, err=True))


@click.group()
@click.option("--config", "config_path", type=click.Path(), default=None, help="a7a-fedora.json with overrides (serial_port, board_host...)")
@click.option("--json", "as_json", is_flag=True, help="Print the stage's result as JSON on stdout")
@click.option("--dry-run", is_flag=True, help="Print every command a stage would run; run nothing")
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def cli(ctx, config_path, as_json, dry_run, verbose):
    """a7a-fedora: Fedora 44 KDE on the Radxa Cubie A7A, built, emulated, flashed, watched."""
    setup_logging("DEBUG" if verbose else ("WARNING" if as_json else "INFO"))
    cfg = Config.load(Path(config_path) if config_path else None)
    ctx.ensure_object(dict)
    ctx.obj.update(config=cfg, registry=StageRegistry(cfg.registry_path), json=as_json, dry_run=dry_run)


@cli.command()
@click.option("--update", is_flag=True, help="Accept trees whose commit moved and rewrite work/manifest.json")
@click.pass_context
def sources(ctx, update):
    """Mirror of the Rabs9 kernel repo + the four Radxa trees, pinned by a manifest."""
    from src.stages import sources as st
    cfg, reg, _, dry = _ctx(ctx)
    r = st.run(cfg, reg, dry_run=dry, update=update, on_line=_printer(ctx))
    _emit(ctx, r, None if r["status"] != "passed" else "trees: " + ", ".join(f"{n} {s['commit'][:9]}" for n, s in r["trees"].items()))


@cli.command()
@click.option("--clean", is_flag=True, help="make mrproper first")
@click.option("--no-extras", is_flag=True, help="skip the WiFi driver and PowerVR module build")
@click.option("--rpm", is_flag=True, help="also package the payload as kernel-a7a[pvr]-<release>.aarch64.rpm (tools/build-rpm.sh, flavour from the release string)")
@click.pass_context
def kernel(ctx, clean, no_extras, rpm):
    """Cross-compile the kernel, DTB, modules; build the out-of-tree modules; make the payload."""
    from src.stages import kernel as st
    cfg, reg, _, dry = _ctx(ctx)
    r = st.run(cfg, reg, dry_run=dry, clean=clean, extras=not no_extras, rpm=rpm, on_line=_printer(ctx))
    _emit(ctx, r, None if r["status"] != "passed" else
          f"release {r['release']}: {r['modules']} modules + {len(r['extra_modules'])} extra, regdb {'embedded' if r['regulatory_db_embedded'] else 'MISSING'}, dtb {r.get('dtb_summary')}")


@cli.command()
@click.pass_context
def rootfs(ctx):
    """Download and verify the official Fedora 44 KDE aarch64 raw image."""
    from src.stages import rootfs as st
    cfg, reg, _, dry = _ctx(ctx)
    r = st.run(cfg, reg, dry_run=dry, on_line=_printer(ctx))
    _emit(ctx, r, None if r["status"] != "passed" else f"{r['raw']}: checksum {r.get('checksum')}, signature {r.get('signature')}")


@cli.command()
@click.option("--compress", is_flag=True, help="also write the .img.xz with its sha256")
@click.option("--size-gb", default=12, show_default=True)
@click.option("--default-stack", type=click.Choice(["open", "ddk"]), default="open", show_default=True,
              help="the kernel the image boots by default: open = drm/imagination + Mesa, ddk = the vendor driver with mesa-img-a7a switched on")
@click.option("--no-keys", is_flag=True, help="leave the owner's ssh public keys out (an image for other people's boards)")
@click.option("--previous-kernels", is_flag=False, flag_value="1", default=None, metavar="[VENDOR_REL,OPEN_REL]",
              help="also install earlier kernels as boot entries 3 and 4 (a hardware test of new kernels: the way back); "
                   "without a value the newest other release of each flavour in output/rpms")
@click.option("--update", is_flag=True, help="run dnf upgrade inside the image (online): Fedora's updates as of today instead of the release as published")
@click.pass_context
def image(ctx, compress, size_gb, default_stack, no_keys, previous_kernels, update):
    """Assemble the card image (firmware region, layout, Fedora root, the a7a-fedora RPMs from output/rpms: both kernels, mesa-pvr-a7a, the board tools; extlinux)."""
    from src.stages import image as st
    cfg, reg, _, dry = _ctx(ctx)
    r = st.run(cfg, reg, dry_run=dry, compress=compress, size_gb=size_gb, default_stack=default_stack, keys=not no_keys,
               previous_kernels=(True if previous_kernels == "1" else previous_kernels or False), update=update, on_line=_printer(ctx))
    _emit(ctx, r, None if r["status"] != "passed" else f"{r['path']} ({r['size']} bytes, kernel {r['kernel_release']}); release note {r['release_note']}")


@cli.command()
@click.option("--mode", type=click.Choice(["stock", "kernel", "interactive", "interactive-stock"]), default="kernel", show_default=True)
@click.option("--image", "image_path", default=None, help="an image in output/ (default: the newest)")
@click.option("--timeout", default=None, type=int, help="seconds to wait for the login prompt")
@click.pass_context
def emulate(ctx, mode, image_path, timeout):
    """Boot an image in QEMU (stock = Fedora's kernel, kernel = the Rabs9 kernel) and record the verdict."""
    from src.stages import emulate as st
    cfg, reg, _, dry = _ctx(ctx)
    if timeout:
        cfg.emulate_timeout = timeout
    r = st.run(cfg, reg, mode=mode, image=image_path, dry_run=dry, on_line=_printer(ctx))
    _emit(ctx, r, None if "verdict" not in r else f"{r['verdict']}: milestones {', '.join(r.get('milestones', []))}; checks " +
          ", ".join(f"{k}={v}" for k, v in (r.get("checks") or {}).items()))


@cli.command()
@click.option("--device", required=False, help="/dev/sdX of the card (see `candidates`)")
@click.option("--image", "image_path", default=None)
@click.option("--no-backup", is_flag=True, help="do not read the current card into output/backups first")
@click.option("--force", is_flag=True, help="write an image without a kernel-mode emulator PASS")
@click.option("--yes", "confirm", default=None, help="the device's name typed back (sdz), or you are asked")
@click.option("--backup-name", default=None, help="name for the backup of the current card (output/backups/<name>.img.xz)")
@click.option("--existing-backup", default=None, help="a backup already made of this card: record it and skip the read")
@click.option("--no-grow", is_flag=True, help="leave the rootfs partition at the image's size instead of growing it to the card")
@click.pass_context
def flash(ctx, device, image_path, no_backup, force, confirm, backup_name, existing_backup, no_grow):
    """Write a tested image to the SD card (backup first, verify after)."""
    from src.stages import flash as st
    cfg, reg, as_json, dry = _ctx(ctx)
    if device and confirm is None and not dry and not as_json and sys.stdin.isatty():
        match, problems = st.check_device(cfg, device, 0)
        if match and not problems:
            click.echo(f"About to write to {device}: {match['model']}, {match['size']} bytes" + ("" if no_backup else " (after backing it up)"))
            confirm = click.prompt(f"Type {match['name']} to confirm", default="", show_default=False)
    r = st.run(cfg, reg, image=image_path, device=device, confirm=confirm, backup=not no_backup, force=force, dry_run=dry, on_line=_printer(ctx),
               backup_name=backup_name, existing_backup=existing_backup, grow=not no_grow)
    _emit(ctx, r, None if r["status"] != "passed" else f"verified; backup {r['backup']}")


@cli.command()
@click.option("--port", default=None, help="serial device (else serial_port from the config)")
@click.option("--fallback", is_flag=True, help="type 2 at the extlinux menu: the image's other kernel")
@click.option("--entry", default=None, type=click.IntRange(1, 9), help="type this number at the extlinux menu (an image built with --previous-kernels: 3 and 4 are the earlier kernels; the last entry is recovery)")
@click.option("--image", "image_path", default=None, help="the image this boot is recorded against")
@click.option("--wait", default=None, type=int, help="seconds to wait for the first console output (the power-on), default 900")
@click.option("--run", "commands", multiple=True, help="log in on the console and run this command (repeatable) instead of watching a boot")
@click.option("--probe", is_flag=True, help="log in on the console and collect the board's facts (kernel, addresses, failed units, checklist)")
@click.option("--record", is_flag=True, help="log everything on the console with timestamps until stopped (Ctrl-C / SIGTERM): catches panics")
@click.pass_context
def serial(ctx, port, fallback, entry, image_path, wait, commands, probe, record):
    """Watch the board's first boot on the serial console; confirm over ssh; or log in and run commands."""
    from src.stages import serial as st
    cfg, reg, as_json, dry = _ctx(ctx)
    if port:
        cfg.serial_port = port
    if wait:
        cfg.serial_wait_seconds = wait
    if record:
        if dry:
            _emit(ctx, {"stage": "serial", "status": "dry_run", "commands": [f"record {cfg.serial_port} -> {cfg.logs_dir}/serial-record-<time>.log"]})
        import threading
        stop = threading.Event()
        try:
            r = st.record(cfg, stop=stop, on_line=_printer(ctx))
        except KeyboardInterrupt:
            stop.set()
            r = {"stage": "serial", "status": "passed", "reason": "recording stopped"}
        _emit(ctx, r)
    if probe:
        r = st.probe(cfg, reg, image=image_path) if not dry else {"stage": "serial", "status": "dry_run", "commands": list(st.PROBE_COMMANDS.values())}
        _emit(ctx, r, None if "facts" not in r else "\n".join(f"  {k}: {v}" for k, v in r["facts"].items()))
    if commands:
        r = st.run_commands(cfg, reg, list(commands)) if not dry else {"stage": "serial", "status": "dry_run", "commands": list(commands)}
        _emit(ctx, r, None if "results" not in r else "\n".join(f"  $ {c}\n{v['output']}\n  [rc {v['rc']}]" for c, v in r["results"].items()))
    r = st.run(cfg, reg, image=image_path, fallback=fallback, entry=entry, dry_run=dry, on_line=_printer(ctx))
    _emit(ctx, r, None if "verdict" not in r else f"{r['verdict']} after {r.get('seconds')}s; events {', '.join(r.get('milestones', []))}; checklist {r.get('checklist')}")


@cli.command()
@click.option("--force", is_flag=True, help="reinstall (dnf reinstall) even if the board already runs or has this build")
@click.option("--rpm", type=click.Path(), default=None, help="a kernel-a7a / kernel-a7apvr package to ship instead of the newest build for output/payload's release")
@click.option("--prune-keep", type=int, default=None, help="after the install, a7a-extlinux prune --keep N (default: no prune; dnf's installonly_limit retires old kernel packages)")
@click.pass_context
def update(ctx, force, rpm, prune_keep):
    """Install the payload's kernel RPM (or --rpm) on a running a7a-fedora board over ssh, previous kernel kept as fallback."""
    from src.stages import update as st
    cfg, reg, _, dry = _ctx(ctx)
    r = st.run(cfg, reg, dry_run=dry, force=force, rpm=rpm, prune_keep=prune_keep, on_line=_printer(ctx))
    _emit(ctx, r)


@cli.command(name="all")
@click.option("--stop-at", type=click.Choice(CHAIN), default=None, help="run the chain up to and including this stage")
@click.option("--compress", is_flag=True)
@click.pass_context
def all_stages(ctx, stop_at, compress):
    """sources -> kernel (packaged as an RPM) -> rootfs -> image -> emulate; stops at the first failure; never flashes.
    The image stage takes the newest RPM of each package from output/rpms: the kernel just built plus the other
    flavour, mesa-pvr-a7a and the board tools from earlier runs (tools/build-rpm.sh, build-mesa-rpm.sh, build-tools-rpm.sh)."""
    cfg, reg, as_json, dry = _ctx(ctx)
    from src.stages import sources as s1, kernel as s2, rootfs as s3, image as s4, emulate as s5
    runners = {"sources": lambda: s1.run(cfg, reg, dry_run=dry, on_line=_printer(ctx)),
               "kernel": lambda: s2.run(cfg, reg, dry_run=dry, rpm=True, on_line=_printer(ctx)),
               "rootfs": lambda: s3.run(cfg, reg, dry_run=dry, on_line=_printer(ctx)),
               "image": lambda: s4.run(cfg, reg, dry_run=dry, compress=compress, on_line=_printer(ctx)),
               "emulate": lambda: s5.run(cfg, reg, mode="kernel", dry_run=dry, on_line=_printer(ctx))}
    results = []
    stopped = None
    for name in CHAIN:
        r = runners[name]()
        results.append(r)
        if not as_json:
            click.echo(f"{name}: {r['status']}" + (f" - {r.get('reason')}" if r.get("reason") else ""))
        if r["status"] not in ("passed", "dry_run"):
            stopped = name
            break
        if name == stop_at:
            break
    out = {"stage": "all", "status": "failed" if stopped else ("dry_run" if dry else "passed"), "stages": results, "stopped_at": stopped}
    if stopped:
        out["reason"] = f"{stopped}: {results[-1].get('reason')}"
    if as_json:
        click.echo(json.dumps(out, indent=1, default=str))
    ctx.exit(1 if stopped else 0)


@cli.command()
@click.pass_context
def status(ctx):
    """What has run, what passed, which images exist and how they tested."""
    cfg, reg, as_json, _ = _ctx(ctx)
    data = {"summary": reg.summary(), "stages": reg.all_stages(), "images": reg.images(), "board": reg.data.get("board"),
            "backups": reg.data.get("backups", []), "registry": str(reg.path)}
    if as_json:
        click.echo(json.dumps(data, indent=1, default=str))
        return
    click.echo(f"registry {reg.path}")
    for name in ["sources", "kernel", "rootfs", "image", "emulate", "flash", "serial", "update"]:
        e = reg.get_stage(name)
        click.echo(f"  {name:8s} {e.get('status', '-') if e else 'not run':10s} {e.get('timestamp', '')[:19] if e else '':20s} {e.get('notes', '')[:90] if e else ''}")
    _print_images(reg)


def _print_images(reg: StageRegistry) -> None:
    for name, img in reg.images().items():
        tests = ", ".join(f"{t['mode']}:{t['verdict']}" for t in img.get("tests", [])) or "not tested"
        click.echo(f"  image {name}: kernel {img.get('kernel_release')}, {img.get('size')} bytes, tests [{tests}], flashed {len(img.get('flashes', []))}x")


@cli.command()
@click.pass_context
def images(ctx):
    """The registry of built images and their test results."""
    cfg, reg, as_json, _ = _ctx(ctx)
    if as_json:
        click.echo(json.dumps(reg.images(), indent=1, default=str))
    else:
        _print_images(reg) if reg.images() else click.echo("no images registered")


@cli.command()
@click.pass_context
def candidates(ctx):
    """Removable disks the flash stage would accept, with size, model and mounts."""
    from src.stages.flash import candidates as cands
    cfg, reg, as_json, _ = _ctx(ctx)
    c = cands(cfg)
    if as_json:
        click.echo(json.dumps(c, indent=1))
    else:
        for d in c:
            click.echo(f"  {d['path']}: {d['model'] or '?'} {d['size'] / 1e9:.1f} GB via {d['tran']}" + (f", MOUNTED {d['mounted']}" if d["mounted"] else "") + (" ROOT DISK" if d["root_disk"] else ""))
        if not c:
            click.echo("no removable disks found")


@cli.command()
@click.pass_context
def config(ctx):
    """The effective configuration (defaults + a7a-fedora.json)."""
    cfg, reg, as_json, _ = _ctx(ctx)
    click.echo(json.dumps(cfg.to_dict(), indent=1, default=str))


@cli.command()
@click.pass_context
def dashboard(ctx):
    """The PySide6 dashboard: stages, images, serial console, buttons that show their commands."""
    from src.dashboard.app import main as dashboard_main
    dashboard_main(ctx.obj["config"])


@cli.command()
@click.pass_context
def mcp(ctx):
    """Serve the stages as MCP tools on stdio (for LM Studio, Cline, or any MCP client)."""
    from src.mcp_server import serve
    serve(ctx.obj["config"])


if __name__ == "__main__":
    cli()
