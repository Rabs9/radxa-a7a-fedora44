"""Stage 8, update: a newer kernel onto a board that already runs a7a-fedora, with a way back.

Over ssh (LAN first, Tailscale second): read the running release, refuse a board that is not
a7a-fedora, ship the kernel as its RPM (the newest build in output/rpms for the payload's
release, or any kernel package named with --rpm) plus the newest a7a-extlinux package for a
board that lacks it (an installed a7a-extlinux is kept as it is), install with `A7A_DEFAULT=1 dnf install` (the package's %post adds the
extlinux entry and makes it the default, the previous kernel stays as entry 2, depmod and the
marker files are the package's business; --force reinstalls a build that is already there),
check that the new kernel really is the default entry, reboot, then watch. With a serial port
configured the boot is watched on the console and a failure is rolled back by selecting entry 2
at the menu on the next power cycle and making it permanent with `a7a-extlinux fallback`;
without one the stage polls ssh and, if the board does not come back, prints the manual
rollback. Nothing prunes unless --prune-keep says so (dnf's installonly_limit retires the oldest
kernel package, whose %postun drops its entry). Everything the stage runs on the board is one
script so the transcript shows exactly what happened. Since 2026-09-10 (before: a tarball plus
a hand-installed a7a-extlinux; docs/PACKAGING-2026-09-09.md).
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.image import build_stamp, image_rpms
from src.stages.kernel import payload_info
from src.stages.serial import Console, SerialError, SubprocessRunner

logger = logging.getLogger("a7a-fedora.update")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=6", "-o", "LogLevel=ERROR"]
REMOTE_DIR = "/tmp/a7a-update"

# bash -s <release> <force 0|1> <prune-keep 0|N>; the packages are in /tmp/a7a-update
INSTALL_SCRIPT = r"""set -e
REL="$1"; FORCE="$2"; PRUNE_KEEP="$3"; SRC=/tmp/a7a-update
echo "a7a-update-install $REL"
KERNEL=""; INSTALL=""; REINSTALL=""
for r in "$SRC"/*.rpm; do
  N=$(rpm -qp --qf '%{NAME}' "$r"); NEVRA=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}' "$r")
  case "$N" in
    kernel-a7a|kernel-a7apvr)
      # the package must be the kernel it is named after (the entry the %post adds is fedora-<kernel-uname-r>)
      rpm -qp --provides "$r" | grep -qx "kernel-uname-r = $REL" || { echo "$(basename "$r") does not provide kernel-uname-r = $REL"; exit 1; }
      KERNEL=$NEVRA
      if rpm -q "$NEVRA" >/dev/null 2>&1; then echo "already installed: $NEVRA"; REINSTALL="$REINSTALL $r"; else INSTALL="$INSTALL $r"; fi ;;
    *)
      # a7a-extlinux travels along for a board without it (the kernel requires it); any installed version stays as
      # it is (no upgrade and no downgrade inside a kernel transaction: the tools set is updated on its own)
      if rpm -q "$N" >/dev/null 2>&1; then echo "keeping the installed $(rpm -q "$N")"; else INSTALL="$INSTALL $r"; fi ;;
  esac
done
[ -n "$KERNEL" ] || { echo "no kernel package in $SRC"; exit 1; }
# repos off: the kernel needs kmod (Fedora KDE) and a7a-extlinux (shipped alongside when the board lacks it)
if [ -n "$INSTALL" ]; then sudo env A7A_DEFAULT=1 dnf install -y --disablerepo='*' $INSTALL; fi
if [ -n "$REINSTALL" ]; then
  if [ "$FORCE" = 1 ]; then sudo env A7A_DEFAULT=1 dnf reinstall -y --disablerepo='*' $REINSTALL
  else
    # an installed kernel that is not the default (e.g. the DDK flavour on entry 2): the same call the package's %post
    # makes, idempotent on an existing entry, moves it first, the previous default to entry 2 and recovery with it,
    # so the "press 2 at the menu" rollback still means the kernel that ran before (`default <label>` would only
    # rewrite the default line and leave the failed kernel at entry 2)
    echo "kernel $KERNEL is already installed: making fedora-$REL the default entry (force reinstalls the package)"
    sudo /usr/local/bin/a7a-extlinux --check-files add "$REL" --default
  fi
fi
if [ "${PRUNE_KEEP:-0}" != 0 ]; then sudo /usr/local/bin/a7a-extlinux prune --keep "$PRUNE_KEEP"; fi
sudo /usr/local/bin/a7a-extlinux list
sudo /usr/local/bin/a7a-extlinux list | awk -v l="fedora-$REL" '$1 == "*" && $2 == l {f = 1} END {exit !f}' || { echo "fedora-$REL is not the default extlinux entry: not rebooting"; exit 1; }
sudo rm -rf "$SRC"
sync; echo "installed $REL"
"""


def kernel_rpm_for(config: Config, release: str) -> Optional[Path]:
    """The newest build in output/rpms of the kernel package for `release` (tools/build-rpm.sh: flavour from the
    release string, Version = release with '+' -> '.plus' and '-' -> '_'), None when there is none."""
    flavour = "a7apvr" if "-a7apvr." in release else "a7a"
    version = release.replace("+", ".plus").replace("-", "_")
    d = config.output_dir / "rpms"
    found = sorted(d.glob(f"kernel-{flavour}-{version}-*.aarch64.rpm"), key=lambda p: (build_stamp(p), p.name)) if d.exists() else []
    return found[-1] if found else None


def release_of(rpm: Path) -> Optional[str]:
    """The kernel release a kernel package is named after (the reverse of the mapping above); None for other packages."""
    m = re.match(r"kernel-(a7a|a7apvr)-(.+)-\d{12}\.[a-z0-9_]+\.rpm$", rpm.name)
    if not m:
        return None
    return m.group(2).replace(".plus", "+").replace("_", "-")


def _ident(config: Config) -> List[str]:
    return ["-i", str(config.ssh_identity)] if config.ssh_identity else []


def _ssh(config: Config, host: str, command: str) -> List[str]:
    return ["ssh", *SSH_OPTS, *_ident(config), f"{config.board_user}@{host}", command]


def _scp(config: Config, host: str, local: List[Path], remote: str) -> List[str]:
    return ["scp", *SSH_OPTS, *_ident(config), *[str(p) for p in local], f"{config.board_user}@{host}:{remote}"]


def reach(config: Config, runner) -> Optional[Dict]:
    """The first host that answers with `uname -r; hostname; os-release`."""
    for host in [h for h in (config.board_host, config.board_tailscale) if h]:
        rc, out = runner.run(_ssh(config, host, "uname -r; hostname; grep ^PRETTY_NAME= /etc/os-release"), timeout=30)
        if rc == 0:
            lines = out.strip().splitlines()
            pretty = next((l.split("=", 1)[1].strip('"') for l in lines if l.startswith("PRETTY_NAME=")), "?")
            return {"host": host, "release": lines[0].strip() if lines else "?", "hostname": lines[1].strip() if len(lines) > 1 else "?", "os": pretty}
    return None


def poll_release(config: Config, runner, host: str, timeout: float, every: float) -> Optional[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out = runner.run(_ssh(config, host, "uname -r"), timeout=20)
        if rc == 0 and out.strip():
            return out.strip().splitlines()[0]
        time.sleep(every)
    return None


def run(config: Config, registry: StageRegistry, dry_run: bool = False, force: bool = False, rpm: Optional[str] = None,
        prune_keep: Optional[int] = None, runner=None, watcher: Optional[Callable[..., Dict]] = None, on_line=None) -> Dict:
    t0 = time.time()
    registry.begin_stage("update", dry_run=dry_run)
    runner = runner or SubprocessRunner()
    result: Dict = {"stage": "update", "status": "passed", "commands": []}
    say = on_line or (lambda s: None)

    def finish(status: str, reason: str, **extra) -> Dict:
        result.update(status=status, reason=reason, **extra)
        registry.record_stage("update", status, notes=reason, duration_seconds=time.time() - t0,
                              extra={k: v for k, v in result.items() if k in ("release", "previous_release", "host", "serial", "rpms")})
        return result

    # which kernel: the package named, else the newest build for the payload's release
    if rpm:
        kernel_rpm = Path(rpm)
        if not kernel_rpm.is_file():
            return finish("failed", f"no such file: {kernel_rpm}")
        rel = release_of(kernel_rpm)
        if not rel:
            return finish("failed", f"{kernel_rpm.name} is not a kernel-a7a / kernel-a7apvr package (tools/build-rpm.sh names them kernel-<flavour>-<version>-<build>.aarch64.rpm)")
    else:
        info = payload_info(config)
        if not info["release"]:
            return finish("failed", "no payload in output/ (run the kernel stage first), and no --rpm given")
        rel = info["release"]
        kernel_rpm = kernel_rpm_for(config, rel)
        if not kernel_rpm:
            return finish("failed", f"no kernel package for {rel} in output/rpms (tools/build-rpm.sh packages output/payload), or name one with --rpm")
    result["release"] = rel
    if prune_keep is not None and prune_keep < 2:
        return finish("failed", f"prune_keep={prune_keep}: at least 2 entries must stay (the new kernel and the previous one as the fallback)")
    extlinux_rpm = image_rpms(config).get("a7a-extlinux")
    rpms = [kernel_rpm] + ([extlinux_rpm] if extlinux_rpm else [])
    result["rpms"] = [p.name for p in rpms]
    if not extlinux_rpm:
        result.setdefault("warnings", []).append("no a7a-extlinux package in output/rpms (tools/build-tools-rpm.sh): a board without it cannot install the kernel")
    if not (config.board_host or config.board_tailscale):
        return finish("failed", "no board_host or board_tailscale configured (a7a-fedora.json)")
    board = reach(config, runner)
    if not board:
        return finish("failed", "board not reachable over ssh on " + ", ".join(h for h in (config.board_host, config.board_tailscale) if h))
    result.update(host=board["host"], previous_release=board["release"], board=board)
    if "Fedora" not in board["os"]:
        return finish("refused", f"{board['hostname']} runs {board['os']}, not a7a-fedora: this stage only updates a Fedora board")
    if board["release"] == rel and not force:
        return finish("skipped", f"{board['hostname']} already runs {rel} (force=True reinstalls)")

    host = board["host"]
    args = f"{shlex.quote(rel)} {1 if force else 0} {int(prune_keep or 0)}"
    steps = [("prepare", _ssh(config, host, f"rm -rf {REMOTE_DIR} && mkdir -p {REMOTE_DIR}")),
             ("scp packages", _scp(config, host, rpms, f"{REMOTE_DIR}/")),
             ("install", _ssh(config, host, f"bash -s {args} <<'A7A'\n{INSTALL_SCRIPT}A7A")),
             ("reboot", _ssh(config, host, "sudo systemctl reboot"))]
    result["commands"] = [shlex.join(a) for _, a in steps]
    if dry_run:
        result["commands"].append(f"(then watch {config.serial_port or 'ssh'} and roll back with: a7a-extlinux fallback)")
        return finish("dry_run", f"would update {board['hostname']} from {board['release']} to {rel} via {host} with {', '.join(result['rpms'])}")

    for name, argv in steps:
        say(f"{name}: {shlex.join(argv)[:120]}")
        rc, out = runner.run(argv, timeout=1800 if name != "reboot" else 30)
        registry.record_command("update", argv, rc, stdout=out[-4000:])
        if name == "reboot":
            break
        if rc != 0:
            return finish("failed", f"{name} failed (exit {rc}): {out.strip()[-300:]}")
        if name == "install":
            result["install_output"] = out.strip()[-2000:]

    # the boot after the reboot
    if config.serial_port:
        watch = watcher or (lambda choose_entry=None: Console(config.serial_port, config.serial_baud,
                                                              config.logs_dir / f"serial-update-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log",
                                                              hang_seconds=config.serial_hang_seconds, on_line=on_line)
                            .watch(timeout=config.serial_boot_timeout, choose_entry=choose_entry, wait_for_output=config.serial_wait_seconds))
        try:
            w = watch(choose_entry=None)
        except SerialError as e:
            return finish("failed", f"rebooted, but the serial port failed: {e}; check the board over ssh")
        result["serial"] = w
        if w["verdict"] != "PASS":
            say(f"boot on {rel} failed ({w['reason']}); power-cycle the board: selecting entry 2 (the previous kernel) at the menu")
            try:
                w2 = watch(choose_entry="2")
            except SerialError as e:
                return finish("failed", f"{rel} failed to boot ({w['reason']}) and the serial port failed during the fallback: {e}")
            result["serial_fallback"] = w2
            if w2["verdict"] != "PASS":
                return finish("failed", f"{rel} failed ({w['reason']}) and the fallback entry failed too ({w2['reason']}): "
                                        "power off and pick another entry at the extlinux menu (`serial --entry N`; recovery is the "
                                        "last one) or swap the backup card",
                              next_step="recovery entry or backup card")
            rc, out = runner.run(_ssh(config, host, "sudo /usr/local/bin/a7a-extlinux fallback && uname -r"), timeout=60)
            registry.record_command("update", _ssh(config, host, "a7a-extlinux fallback"), rc, stdout=out)
            back = poll_release(config, runner, host, config.update_poll_timeout, config.update_poll_seconds) if rc != 0 else out.strip().splitlines()[-1]
            registry.data["board"] = {"host": host, "release": board["release"], "previous_release": rel, "updated": datetime.now().isoformat(timespec="seconds"),
                                      "rolled_back": True, "rpms": result["rpms"]}
            registry.save()
            return finish("rolled_back", f"{rel} did not boot ({w['reason']}); the board is back on {board['release']} via the fallback entry"
                                         + ("" if rc == 0 else " (making it permanent failed: run a7a-extlinux fallback on the board)"))
    # confirm over ssh
    say("waiting for the board to answer over ssh")
    new = poll_release(config, runner, host, config.update_poll_timeout, config.update_poll_seconds)
    result["ssh_after"] = {"release": new, "host": host}
    if new != rel:
        if new is None:
            return finish("failed", f"the board did not come back over ssh within {int(config.update_poll_timeout)}s after rebooting on {rel}",
                          next_step=f"power-cycle; at the extlinux menu press 2 within 5 s (entry 2 = the previous kernel {board['release']}), then run "
                                    f"`sudo a7a-extlinux fallback` on the board; the serial stage can do this for you with a console attached")
        return finish("failed", f"the board came back on {new}, not {rel}: U-Boot may have booted the fallback entry; run a7a-extlinux list on the board")
    registry.data["board"] = {"host": host, "release": rel, "previous_release": board["release"], "updated": datetime.now().isoformat(timespec="seconds"),
                              "rpms": result["rpms"]}
    registry.save()
    return finish("passed", f"{board['hostname']} now runs {rel} (was {board['release']}); previous kernel kept as entry 2")
