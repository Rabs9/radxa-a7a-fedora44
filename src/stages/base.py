"""The one way a stage runs an external program.

Every stage of a7a-fedora drives a script under tools/ (or git, qemu, ssh, curl...) through
`run_tool`: the full argv and the exit status go into the registry, everything the program
prints goes to a per-run log under output/logs/, and `dry_run=True` prints the exact command
instead of running it. Nothing is run silently; nothing is run twice by accident.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry

logger = logging.getLogger("a7a-fedora.tools")

TAIL_CHARS = 400000  # how much of the END of the output a ToolRun keeps in memory (the log has all of it)


@dataclass
class ToolRun:
    """What happened when a stage ran one program."""

    argv: List[str]
    exit_code: int
    log_path: Path
    duration: float = 0.0
    dry_run: bool = False
    timed_out: bool = False
    output: str = ""
    cwd: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def command_line(self) -> str:
        return shlex.join(self.argv)

    def to_dict(self) -> Dict:
        return {
            "argv": self.argv, "command": self.command_line, "exit_code": self.exit_code,
            "log": str(self.log_path), "duration_seconds": round(self.duration, 2),
            "dry_run": self.dry_run, "timed_out": self.timed_out, "cwd": self.cwd,
        }


def new_log_path(config: Config, stage: str) -> Path:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.logs_dir / f"{stage}-{stamp}.log"
    n = 1
    while path.exists():  # two runs in the same second
        n += 1
        path = config.logs_dir / f"{stage}-{stamp}-{n}.log"
    return path


def run_tool(
    config: Config,
    registry: StageRegistry,
    stage: str,
    argv: List[str],
    *,
    cwd: Optional[Path | str] = None,
    env: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
    timeout: Optional[float] = None,
    on_line: Optional[Callable[[str], None]] = None,
    stdin_text: Optional[str] = None,
) -> ToolRun:
    """Run `argv`, logging every line to output/logs/<stage>-<time>.log and the registry.

    Never raises for a failing or missing program: the exit code says what happened
    (127 = not found, -9 = killed on timeout) so a stage can record it and stop cleanly.
    """
    argv = [str(a) for a in argv]
    log_path = new_log_path(config, stage)
    cwd_s = str(cwd) if cwd else None
    started = time.time()
    header = f"# {datetime.now().isoformat(timespec='seconds')} stage={stage} cwd={cwd_s or os.getcwd()}\n# {shlex.join(argv)}\n"
    if dry_run:
        text = header + "# DRY RUN: not executed\n"
        log_path.write_text(text)
        logger.info("DRY RUN: %s", shlex.join(argv))
        if on_line:
            on_line(f"DRY RUN: {shlex.join(argv)}")
        registry.record_command(stage, argv, 0, cwd=cwd_s, log=str(log_path), dry_run=True)
        return ToolRun(argv, 0, log_path, 0.0, dry_run=True, output="DRY RUN: " + shlex.join(argv), cwd=cwd_s)

    logger.info("RUN: %s", shlex.join(argv))
    from collections import deque
    chunks: deque = deque()
    kept = 0
    timed_out = False
    merged_env = {**os.environ, **(env or {})}
    with open(log_path, "w") as log:
        log.write(header)
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd_s, env=merged_env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
            )
        except FileNotFoundError as e:
            msg = f"not found: {e.filename or argv[0]}"
            log.write(msg + "\n# exit 127\n")
            registry.record_command(stage, argv, 127, cwd=cwd_s, log=str(log_path))
            return ToolRun(argv, 127, log_path, time.time() - started, output=msg, cwd=cwd_s)
        except OSError as e:
            msg = f"cannot run {argv[0]}: {e}"
            log.write(msg + "\n# exit 126\n")
            registry.record_command(stage, argv, 126, cwd=cwd_s, log=str(log_path))
            return ToolRun(argv, 126, log_path, time.time() - started, output=msg, cwd=cwd_s)
        if stdin_text is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        assert proc.stdout is not None
        deadline = started + timeout if timeout else None
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        buf = ""
        while True:
            wait = None if deadline is None else max(0.0, deadline - time.time())
            events = sel.select(timeout=wait if wait is not None else 1.0)
            if events:
                data = os.read(proc.stdout.fileno(), 65536).decode("utf-8", "replace")
                if not data:
                    break
                buf += data.replace("\r\n", "\n").replace("\r", "\n")   # dd/rsync progress uses carriage returns
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    log.write(line + "\n")
                    log.flush()
                    if on_line:
                        on_line(line)
                    chunks.append(line + "\n")
                    kept += len(line) + 1
                    while kept > TAIL_CHARS and len(chunks) > 1:   # keep the tail: the verdict comes last
                        kept -= len(chunks.popleft())
            elif proc.poll() is not None and not events:
                break
            if deadline is not None and time.time() >= deadline:
                timed_out = True
                proc.kill()
                break
        if buf:
            log.write(buf + "\n")
            chunks.append(buf + "\n")
            if on_line:
                on_line(buf)
        try:
            exit_code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            exit_code = proc.wait()
        if timed_out:
            log.write(f"# TIMED OUT after {timeout}s\n")
        log.write(f"# exit {exit_code}\n")
    duration = time.time() - started
    registry.record_command(stage, argv, exit_code, cwd=cwd_s, log=str(log_path))
    logger.info("EXIT %d (%.1fs): %s", exit_code, duration, argv[0])
    return ToolRun(argv, exit_code, log_path, duration, timed_out=timed_out, output="".join(chunks), cwd=cwd_s)


def tool(config: Config, name: str) -> Path:
    """Path of one of the scripts under tools/."""
    return config.tools_dir / name


def sudo_priv(config: Config, *args: str) -> List[str]:
    """argv for one privileged-helper operation: sudo -n tools/priv.sh <op> <args> (config.sudo_prefix is [] in tests)."""
    return [*config.sudo_prefix, str(config.priv_helper), *args]


def sha256_of(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()
