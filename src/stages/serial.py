"""Stage 7, serial: own the board's console and drive the first real boot, with a way back.

The CH343 adapter (/dev/ttyACM0, 115200 8N1) is the only view into a board that may freeze.
`Console` opens the port raw, logs every line with a timestamp, and recognises the boot's
events: U-Boot, the extlinux menu, the kernel start, root mounted, systemd's targets, a panic
or oops, a hang (no bytes for N seconds), the login prompt, and the first-boot checklist the
image writes to the console. `watch` returns a verdict; `choose_entry` types a menu number
when U-Boot asks, which is how another kernel or the recovery entry is selected without touching the card.
`run` is the stage: watch the boot, confirm over ssh (LAN first, Tailscale second), record the
result against the image, and say what to do next when it did not come up.
"""

from __future__ import annotations

import logging
import os
import re
import select
import shlex
import subprocess
import termios
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry
from src.stages.emulate import pick_image

logger = logging.getLogger("a7a-fedora.serial")


class SerialError(Exception):
    pass


EVENTS = [  # (name, regex on a line or a prompt fragment)
    ("uboot", re.compile(r"U-Boot (SPL )?\d{4}\.\d{2}")),
    ("autoboot", re.compile(r"Hit any key to stop autoboot")),
    ("extlinux", re.compile(r"Retrieving file: /boot/extlinux/extlinux\.conf|Found /boot/extlinux/extlinux\.conf")),
    ("menu", re.compile(r"Enter choice:")),
    ("kernel", re.compile(r"Starting kernel|Linux version \S+")),
    ("root-mounted", re.compile(r"VFS: Mounted root")),
    ("systemd", re.compile(r"systemd\[1\]: (systemd \d|Detected|Welcome)|Welcome to ")),
    ("multi-user", re.compile(r"Reached target.*(Multi-User|multi-user)")),
    ("graphical", re.compile(r"Reached target.*(Graphical|graphical)")),
    ("checklist", re.compile(r"^a7a-firstboot:")),
    ("login", re.compile(r"login: ?$")),
    ("panic", re.compile(r"Kernel panic|end Kernel panic")),
    ("oops", re.compile(r"Internal error: Oops|Unable to handle kernel|BUG: ")),
    ("emergency", re.compile(r"emergency mode|Emergency Shell|rescue mode")),
    ("uboot-prompt", re.compile(r"^=> ?$")),
]
CHECKLIST_KEYS = ("cpu max", "root", "net", "gpu node", "modules")


class Console:
    def __init__(self, port: str, baud: int, log_path: Path, hang_seconds: float = 120, on_line=None):
        self.port, self.baud, self.log_path, self.hang_seconds, self.on_line = port, baud, Path(log_path), hang_seconds, on_line
        self.fd: Optional[int] = None

    def open(self) -> None:
        try:
            self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise SerialError(f"cannot open {self.port}: {e.strerror}") from e
        try:
            attrs = termios.tcgetattr(self.fd)
            speed = getattr(termios, f"B{self.baud}", termios.B115200)
            attrs[0] = termios.IGNBRK                     # iflag: raw
            attrs[1] = 0                                   # oflag
            attrs[2] = (attrs[2] & ~(termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.CRTSCTS)) | termios.CS8 | termios.CLOCAL | termios.CREAD
            attrs[3] = 0                                   # lflag: no echo, no canonical
            attrs[4] = attrs[5] = speed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except termios.error as e:      # a plain file or a pty that refuses some flags: still usable
            logger.debug("termios on %s: %s", self.port, e)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def send(self, text: str) -> None:
        if self.fd is not None:
            os.write(self.fd, text.encode())

    def watch(self, timeout: float, choose_entry: Optional[str] = None, stop_on: str = "checklist-or-login",
              wait_for_output: Optional[float] = None) -> Dict:
        """Read until a verdict: PASS on the checklist (or a login prompt), FAIL on panic, hang, or timeout.

        Before the first byte arrives (the board is not powered yet) only `wait_for_output` applies (default: timeout);
        the hang detector and the boot `timeout` both count from the first byte.
        """
        self.open()
        t0 = time.time()
        last_bytes = t0
        first_bytes: Optional[float] = None
        wait_for_output = wait_for_output if wait_for_output is not None else timeout
        events: List[Dict] = []
        checklist: Dict[str, str] = {}
        seen_names: List[str] = []
        entry: Optional[str] = None
        chose = False
        buf = ""
        verdict, reason = None, None
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a") as log:
            log.write(f"# {datetime.now().isoformat(timespec='seconds')} watching {self.port} @ {self.baud}\n")

            def note(name: str, line: str) -> None:
                if name not in seen_names or name in ("panic", "oops"):
                    seen_names.append(name)
                    events.append({"t": round(time.time() - t0, 3), "event": name, "line": line.strip()[:200]})
                    log.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} == {name}: {line.strip()[:120]}\n")
                    if self.on_line:
                        self.on_line(f"== {name}: {line.strip()[:120]}")

            def handle(line: str, partial: bool = False) -> None:
                nonlocal entry, chose, verdict, reason
                stamped = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {line}\n"
                if not partial:
                    log.write(stamped)
                    log.flush()
                    if self.on_line:
                        self.on_line(line)
                for name, rx in EVENTS:
                    if rx.search(line):
                        note(name, line)
                        if name == "menu":
                            if choose_entry and not chose:
                                self.send(choose_entry + "\r")
                                chose = True
                                log.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} -> sent menu choice {choose_entry}\n")
                        if name == "panic":
                            verdict, reason = "FAIL", "kernel panic: " + line.strip()[:160]
                        if name == "oops" and verdict is None:
                            verdict, reason = "FAIL", "kernel oops: " + line.strip()[:160]
                        if name == "emergency":
                            verdict, reason = "FAIL", "systemd dropped to " + line.strip()[:120]
                        if name == "uboot-prompt" and seen_names.count("uboot") and "kernel" not in seen_names:
                            verdict, reason = "FAIL", "U-Boot stopped at its prompt (autoboot interrupted or no bootable entry)"
                m = re.match(r"^(\d+):\t", line)
                if m and "menu" in seen_names and entry is None and not partial:
                    entry = m.group(1)
                m = re.match(r"^(cpu max|root|net|gpu node|modules): ?(.*)$", line.strip())
                if m and "checklist" in seen_names and not partial:
                    checklist[m.group(1)] = m.group(2).strip()
                    if m.group(1) == "modules":       # "modules: 41  failed units: 0" closes the checklist
                        mm = re.match(r"(\d+)\s+failed units: (\d+)", m.group(2).strip())
                        if mm:
                            checklist["modules"], checklist["failed units"] = mm.group(1), mm.group(2)
                        verdict, reason = verdict or "PASS", reason or "first-boot checklist reported"
                if "login" in seen_names and verdict is None and stop_on == "checklist-or-login" and ("checklist" in seen_names or partial):
                    verdict, reason = "PASS", "login prompt" + (" after the checklist" if "checklist" in seen_names else " (no checklist seen)")

            try:
                while verdict is None:
                    now = time.time()
                    if first_bytes is None:
                        if now - t0 > wait_for_output:
                            verdict, reason = "FAIL", f"nothing on the console in {int(wait_for_output)}s (board not powered, wrong port, or no console output)"
                            break
                    else:
                        if now - first_bytes > timeout:
                            verdict, reason = "FAIL", f"timeout {int(timeout)}s after the first output; last event: {seen_names[-1] if seen_names else 'nothing'}"
                            break
                        if now - last_bytes > self.hang_seconds:
                            verdict, reason = "FAIL", f"hang: no output for {int(self.hang_seconds)}s after {seen_names[-1] if seen_names else 'nothing'}"
                            break
                    r, _, _ = select.select([self.fd], [], [], 0.2)
                    if not r:
                        continue
                    try:
                        data = os.read(self.fd, 4096)
                    except OSError as e:
                        verdict, reason = "FAIL", f"serial port went away: {e.strerror}"
                        break
                    if not data:
                        if now - last_bytes > self.hang_seconds:
                            verdict, reason = "FAIL", "port closed"
                            break
                        time.sleep(0.05)
                        continue
                    last_bytes = time.time()
                    if first_bytes is None:
                        first_bytes = last_bytes
                        log.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} == first output after {round(first_bytes - t0, 1)}s\n")
                    buf += data.decode("utf-8", "replace").replace("\r", "")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        handle(line)
                    if buf.rstrip().endswith(("login:", "choice:", "=>")):
                        handle(buf, partial=True)
                        if verdict is not None or "menu" in seen_names:
                            log.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {buf}\n")
                            buf = ""
            finally:
                if buf:
                    log.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]} {buf}\n")
                log.write(f"# verdict {verdict}: {reason}\n")
                self.close()
        return {"verdict": verdict, "reason": reason, "events": events, "milestones": seen_names, "entry": entry,
                "checklist": checklist, "seconds": round(time.time() - t0, 1), "log": str(self.log_path)}


    # -- a shell on the console: log in at the getty, run commands, log out --------------------------------
    def login_and_run(self, user: str, password: str, commands: List[str], timeout: float = 60, prompt_wait: float = 20) -> Dict:
        """Log in at the console's getty and run each command, returning {command: {"output", "rc"}}.

        Output is bracketed by markers the shell computes (so the echo of the typed line never matches them),
        which makes this robust to line wrapping and prompts. The session is closed with `exit` at the end.
        """
        self.open()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        results: Dict[str, Dict] = {}
        buf = ""
        with open(self.log_path, "a") as log:
            log.write(f"# {datetime.now().isoformat(timespec='seconds')} console shell on {self.port} as {user}\n")

            def read_until(patterns: List[str], seconds: float) -> Optional[str]:
                nonlocal buf
                deadline = time.time() + seconds
                while time.time() < deadline:
                    for pat in patterns:
                        if re.search(pat, buf):
                            return pat
                    r, _, _ = select.select([self.fd], [], [], 0.2)
                    if not r:
                        continue
                    try:
                        data = os.read(self.fd, 4096)
                    except OSError as e:
                        raise SerialError(f"serial port went away: {e.strerror}") from e
                    if data:
                        text = data.decode("utf-8", "replace").replace("\r", "")
                        buf += text
                        log.write(text)
                        log.flush()
                for pat in patterns:
                    if re.search(pat, buf):
                        return pat
                return None

            try:
                self.send("\n")
                got = read_until([r"login: ?$", r"\$ ?$", r"# ?$"], prompt_wait)
                if got is None:
                    raise SerialError(f"no login prompt or shell on {self.port} within {int(prompt_wait)}s")
                if got.startswith("login"):
                    buf = ""
                    self.send(user + "\n")
                    if read_until([r"[Pp]assword: ?$"], 15) is None:
                        raise SerialError("no password prompt after the user name")
                    buf = ""
                    self.send(password + "\n")
                    got = read_until([r"\$ ?$", r"# ?$", r"Login incorrect", r"login: ?$"], 20)
                    if got is None or got in (r"Login incorrect", r"login: ?$"):
                        raise SerialError("login failed (wrong user or password)")
                for i, cmd in enumerate(commands):
                    tag = f"A7A{i}{int(time.time()) % 100000}"
                    buf = ""
                    self.send(f'echo "$((40+2))BEGIN_{tag}"; {cmd}; echo "$((40+2))END_{tag}=$?"\n')
                    if read_until([rf"42END_{tag}=\d+"], timeout) is None:
                        results[cmd] = {"output": buf[-4000:], "rc": None, "error": f"no result within {int(timeout)}s"}
                        self.send("\x03")          # ^C whatever is still running
                        read_until([r"\$ ?$", r"# ?$"], 5)
                        continue
                    m = re.search(rf"42BEGIN_{tag}\n(.*?)\n?42END_{tag}=(\d+)", buf, re.S)
                    results[cmd] = {"output": m.group(1).rstrip("\n") if m else buf[-4000:], "rc": int(m.group(2)) if m else None}
                    log.write(f"\n# {cmd} -> rc {results[cmd]['rc']}\n")
                self.send("exit\n")
                read_until([r"login: ?$"], 5)
            finally:
                self.close()
        return results


PROBE_COMMANDS = {
    "kernel": "uname -r",
    "model": "cat /proc/device-tree/model 2>/dev/null | tr -d '\\0'",
    "addresses": "ip -4 -br addr | grep -v '^lo' | tr -s ' ' | tr '\\n' ';'",
    "links": "ip -br link | grep -v '^lo' | tr -s ' ' | tr '\\n' ';'",
    "cpu_max_khz": "cat /sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq /sys/devices/system/cpu/cpufreq/policy6/cpuinfo_max_freq 2>/dev/null | tr '\\n' ' '",
    "root": "findmnt -n -o SOURCE,FSTYPE,SIZE,USED /",
    "failed_units": "systemctl --failed --no-legend | tr -s ' ' | cut -d' ' -f1 | tr '\\n' ' '",
    "modules_loaded": "lsmod | tail -n +2 | wc -l",
    "drm": "ls /dev/dri 2>/dev/null | tr '\\n' ' '",
    "npu": "ls /dev/vipcore 2>/dev/null",
    "wifi": "lsmod | grep -oE '^aic8800_fdrv|^aic_load_fw' | tr '\\n' ' '; ip -br link | grep -oE 'wlan[0-9]+' | head -1",
    "gpu_module": "lsmod | grep -oE '^pvrsrvkm'",
    "sleep_masked": "systemctl is-enabled sleep.target 2>&1",
    "firstboot": "cat /var/log/a7a-firstboot.log 2>/dev/null | tail -6 | tr '\\n' '|'",
    "display_manager": "systemctl is-active display-manager 2>&1; systemctl show -p Id display-manager 2>/dev/null",
    "uptime": "uptime -p",
    "dnf": "dnf --version 2>/dev/null | head -1",
}


def probe(config: Config, registry: StageRegistry, image: Optional[Path | str] = None, user: str = "radxa", password: str = "radxa") -> Dict:
    """Log in on the console and collect the facts the first-boot checklist and ssh would give; record them."""
    registry.begin_stage("serial")
    result: Dict = {"stage": "serial", "mode": "probe", "status": "passed"}
    if not config.serial_port:
        result.update(status="failed", reason="no serial_port configured")
        registry.record_stage("serial", "failed", notes=result["reason"])
        return result
    log = config.logs_dir / f"serial-probe-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    console = Console(config.serial_port, config.serial_baud, log)
    try:
        raw = console.login_and_run(user, password, list(PROBE_COMMANDS.values()))
    except SerialError as e:
        result.update(status="failed", reason=str(e), log=str(log))
        registry.record_stage("serial", "failed", notes=result["reason"])
        return result
    facts = {name: raw.get(cmd, {}).get("output", "").strip() for name, cmd in PROBE_COMMANDS.items()}
    result.update(facts=facts, log=str(log))
    ips = re.findall(r"(\d+\.\d+\.\d+\.\d+)/\d+", facts.get("addresses", ""))
    result["ips"] = ips
    path = pick_image(config, image) if image else flashed_image(config, registry)
    if path:
        img = registry.images().get(path.name)
        if img and img.get("tests"):
            last = img["tests"][-1]
            if last.get("mode") == "board":
                last["probe"] = facts
                last["ips"] = ips
        registry.save()
    registry.data["board"] = {**registry.data.get("board", {}), "release": facts.get("kernel"), "ips": ips, "model": facts.get("model"),
                              "probed": datetime.now().isoformat(timespec="seconds"), "failed_units": facts.get("failed_units")}
    registry.record_stage("serial", "passed", notes=f"probe: kernel {facts.get('kernel')}, ips {ips}, failed units [{facts.get('failed_units')}]",
                          duration_seconds=0, extra={"facts": facts, "log": str(log)})
    return result


def run_commands(config: Config, registry: StageRegistry, commands: List[str], user: str = "radxa", password: str = "radxa",
                 timeout: float = 60) -> Dict:
    """Run arbitrary commands on the board over the console (for when ssh is not there yet)."""
    registry.begin_stage("serial")
    if not config.serial_port:
        return {"stage": "serial", "status": "failed", "reason": "no serial_port configured"}
    log = config.logs_dir / f"serial-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    console = Console(config.serial_port, config.serial_baud, log)
    try:
        results = console.login_and_run(user, password, commands, timeout=timeout)
    except SerialError as e:
        registry.record_stage("serial", "failed", notes=str(e))
        return {"stage": "serial", "status": "failed", "reason": str(e), "log": str(log)}
    registry.record_stage("serial", "passed", notes=f"ran {len(commands)} command(s) on the console", extra={"log": str(log)})
    return {"stage": "serial", "status": "passed", "results": results, "log": str(log)}


def record(config: Config, stop=None, log: Optional[Path] = None, on_line=None, hang_seconds: float = 0) -> Dict:
    """Own the console and log every line with a timestamp until `stop` (a threading.Event) is set or SIGTERM.

    This is what catches a panic that happens hours after the boot: a watch ends at its verdict, a recording does not.
    """
    import signal
    import threading
    stop = stop or threading.Event()
    if not config.serial_port:
        return {"stage": "serial", "status": "failed", "reason": "no serial_port configured"}
    log = log or (config.logs_dir / f"serial-record-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log")
    console = Console(config.serial_port, config.serial_baud, log, hang_seconds=hang_seconds, on_line=on_line)
    try:
        console.open()
    except SerialError as e:
        return {"stage": "serial", "status": "failed", "reason": str(e)}
    try:
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    except ValueError:
        pass                                   # not the main thread (tests)
    log.parent.mkdir(parents=True, exist_ok=True)
    lines = 0
    events: List[Dict] = []
    buf = ""
    t0 = time.time()
    with open(log, "a") as f:
        f.write(f"# {datetime.now().isoformat(timespec='seconds')} recording {config.serial_port} @ {config.serial_baud}\n")
        f.flush()
        while not stop.is_set():
            r, _, _ = select.select([console.fd], [], [], 0.3)
            if not r:
                continue
            try:
                data = os.read(console.fd, 4096)
            except OSError as e:
                f.write(f"# port error: {e.strerror}\n")
                break
            if not data:
                time.sleep(0.05)
                continue
            buf += data.decode("utf-8", "replace").replace("\r", "")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                f.write(f"{stamp} {line}\n")
                lines += 1
                for name, rx in EVENTS:
                    if name in ("panic", "oops", "uboot", "login", "checklist") and rx.search(line):
                        events.append({"t": round(time.time() - t0, 1), "event": name, "line": line.strip()[:160]})
                        f.write(f"{stamp} == {name}\n")
                        if on_line:
                            on_line(f"== {name}: {line.strip()[:120]}")
                f.flush()
    console.close()
    return {"stage": "serial", "status": "passed", "log": str(log), "lines": lines, "events": events, "seconds": round(time.time() - t0, 1)}


class SubprocessRunner:
    def run(self, argv: List[str], timeout: float = 30):
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return r.returncode, (r.stdout + r.stderr)
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except OSError as e:
            return 127, str(e)


def flashed_image(config: Config, registry: StageRegistry) -> Optional[Path]:
    """The image most recently flashed to a card (what the board boots), else the newest image in output/."""
    best = None
    for name, img in registry.images().items():
        for f in img.get("flashes", []):
            if f.get("verified") and (best is None or f["timestamp"] > best[0]):
                best = (f["timestamp"], name, img.get("path"))
    if best:
        return Path(best[2]) if best[2] else config.output_dir / best[1]
    return pick_image(config, None)


def ssh_confirm(config: Config, runner, command: str = "uname -r; hostname; uptime -p") -> Dict:
    """LAN first, Tailscale second; key auth (BatchMode), then the image's radxa/radxa via sshpass if present."""
    hosts = [h for h in (config.board_host, config.board_tailscale) if h]
    tried = []
    for host in hosts:
        target = f"{config.board_user}@{host}"
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=6", "-o", "LogLevel=ERROR", *(["-i", str(config.ssh_identity)] if config.ssh_identity else []), target, command]
        rc, out = runner.run(argv, timeout=30)
        tried.append({"host": target, "rc": rc})
        if rc == 0:
            return {"ok": True, "host": target, "output": out.strip(), "tried": tried}
    return {"ok": False, "host": None, "output": "", "tried": tried}


def run(config: Config, registry: StageRegistry, image: Optional[Path | str] = None, fallback: bool = False,
        dry_run: bool = False, runner=None, wait_for_power: bool = True, on_line=None, entry: Optional[int] = None) -> Dict:
    """Watch the board's boot on the serial console and record it against the image. entry = the number to type at
    the extlinux menu (fallback = 2: the image's other kernel; an image built with earlier kernels has them at 3 and 4)."""
    t0 = time.time()
    registry.begin_stage("serial", dry_run=dry_run)
    choose = str(entry) if entry else ("2" if fallback else None)
    result: Dict = {"stage": "serial", "status": "passed", "mode": f"entry {entry}" if entry else ("fallback" if fallback else "watch")}
    path = pick_image(config, image) if image else flashed_image(config, registry)
    result["image"] = path.name if path else None
    if not config.serial_port:
        result.update(status="failed", reason="no serial_port configured (a7a-fedora.json: \"serial_port\": \"/dev/ttyACM0\", or --port)")
        registry.record_stage("serial", "failed", notes=result["reason"])
        return result
    log = config.logs_dir / f"serial-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    result["plan"] = {"port": f"{config.serial_port} @ {config.serial_baud} 8N1", "log": str(log),
                      "timeout": config.serial_boot_timeout, "hang_seconds": config.serial_hang_seconds, "wait_for_power": config.serial_wait_seconds,
                      "choose_entry": choose,
                      "ssh": [f"{config.board_user}@{h}" for h in (config.board_host, config.board_tailscale) if h]}
    if dry_run:
        result["status"] = "dry_run"
        registry.record_stage("serial", "dry_run", notes=f"would watch {config.serial_port} for {config.serial_boot_timeout}s")
        return result
    if wait_for_power and on_line:
        on_line(f"Power the board on now (card inserted); watching {config.serial_port} for up to {config.serial_wait_seconds}s for the first output")
    console = Console(config.serial_port, config.serial_baud, log, hang_seconds=config.serial_hang_seconds, on_line=on_line)
    try:
        w = console.watch(timeout=config.serial_boot_timeout, choose_entry=choose, wait_for_output=config.serial_wait_seconds)
    except SerialError as e:
        result.update(status="failed", reason=str(e))
        registry.record_stage("serial", "failed", notes=result["reason"])
        return result
    result.update(verdict=w["verdict"], reason=w["reason"], events=w["events"], milestones=w["milestones"], entry=w["entry"],
                  checklist=w["checklist"], seconds=w["seconds"], log=str(log))
    if not w["events"] and w["reason"] and w["reason"].startswith("nothing on the console"):
        # the board never spoke: not a verdict on the image, so nothing is recorded against it
        result.update(status="skipped", verdict=None,
                      next_step=f"no console output in {int(config.serial_wait_seconds)}s: power the board on with the card inserted and run "
                                f"`serial` again (check the adapter is {config.serial_port} and the cable is on the debug UART)")
        registry.record_stage("serial", "skipped", notes=w["reason"], duration_seconds=time.time() - t0, extra={"log": str(log)})
        return result
    ssh = {"ok": False, "host": None, "output": "", "tried": []}
    if w["verdict"] == "PASS":
        ssh = ssh_confirm(config, runner or SubprocessRunner())
    result["ssh"] = ssh
    if w["verdict"] == "PASS":
        result["status"] = "passed"
        result["next_step"] = "the board is up" + (f"; ssh works via {ssh['host']}" if ssh["ok"] else "; ssh did not answer on LAN or Tailscale yet (check the network, or wait for the first-boot services)")
    else:
        result["status"] = "failed"
        # what the menu of THIS image holds: 1 the default kernel, 2 its other kernel, then (an image built with them)
        # the earlier vendor and open kernels, and last recovery = the default kernel again, multi-user, loglevel 7
        prev = (registry.images().get(path.name, {}) if path else {}).get("previous_kernels") or []
        if prev and choose != "3":
            result["next_step"] = (f"power-cycle and run `serial --entry 3`: entry 3 is the earlier vendor kernel {prev[0]}"
                                   + (f", entry 4 the earlier open one ({prev[1]})" if len(prev) > 1 else "") +
                                   "; entry 2 is this image's other new kernel, the last entry is recovery (the default kernel, "
                                   "multi-user, loglevel 7). If those fail too, swap the backup card back")
        elif choose:
            result["next_step"] = (f"entry {choose} did not come up either: power off, swap the backup card back "
                                   "(output/backups holds the copy of the previous card), then read the serial log")
        else:
            result["next_step"] = ("power-cycle and run `serial --fallback`: it types 2 at the extlinux menu, this image's other kernel "
                                   "(the last entry is recovery: the default kernel, multi-user, loglevel 7; `serial --entry N` "
                                   "picks any); if that fails too, swap the backup card back")
    if path:
        registry.record_test(path.name, mode="board", verdict=w["verdict"], log=str(log), milestones=w["milestones"], reason=w["reason"],
                             entry=w["entry"], checklist=w["checklist"], ssh=ssh.get("host"), seconds=w["seconds"])
    registry.record_stage("serial", result["status"], notes=f"{w['verdict']}: {w['reason']}" + (f"; ssh {ssh['host']}" if ssh["ok"] else ""),
                          duration_seconds=time.time() - t0, extra={"image": result["image"], "verdict": w["verdict"], "log": str(log)})
    return result
