"""a7a-fedora as MCP tools over stdio, so an LLM (LM Studio, Cline, Claude) can drive the pipeline.

JSON-RPC 2.0, one message per line: initialize, notifications/initialized, ping, tools/list,
tools/call. The long stages (kernel, rootfs, image, emulate, flash, serial, update) run as
jobs in a background thread and the client polls `job` for the log tail and the result; the
short ones (sources, status, candidates, images, log_tail, config) answer inline. One job at
a time: these stages share output/ and the registry. The gates are the stages' own: flash
still needs the device's name typed back and an emulator PASS, nothing here bypasses them.

    python3 -m src.mcp_server [--config a7a-fedora.json]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.config import Config
from src.registry import StageRegistry

logger = logging.getLogger("a7a-fedora.mcp")

PROTOCOL = "2024-11-05"
LONG_STAGES = ("kernel", "rootfs", "image", "emulate", "update")
INLINE_STAGES = ("sources",)


def _stage_module(name: str):
    import importlib
    return importlib.import_module(f"src.stages.{name}")


class Job:
    def __init__(self, name: str, fn: Callable[[Callable[[str], None]], Dict]):
        self.job_id = uuid.uuid4().hex[:10]
        self.name = name
        self.status = "running"
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished: Optional[str] = None
        self.result: Optional[Dict] = None
        self.error: Optional[str] = None
        self.log: deque = deque(maxlen=400)
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)

    def _run(self, fn):
        try:
            self.result = fn(self.log.append)
        except Exception as e:  # noqa: BLE001 - the job must report, not crash the server
            self.error = f"{type(e).__name__}: {e}"
            logger.exception("job %s failed", self.name)
        self.status = "done"
        self.finished = datetime.now().isoformat(timespec="seconds")

    def to_dict(self, tail: int = 40) -> Dict:
        return {"job_id": self.job_id, "stage": self.name, "status": self.status, "started": self.started, "finished": self.finished,
                "result": self.result, "error": self.error, "log_tail": list(self.log)[-tail:]}


class Jobs:
    def __init__(self):
        self.jobs: List[Job] = []
        self.lock = threading.Lock()

    def running(self) -> Optional[Job]:
        return next((j for j in self.jobs if j.status == "running"), None)

    def start(self, name: str, fn) -> Job:
        with self.lock:
            r = self.running()
            if r:
                raise RuntimeError(f"job {r.job_id} ({r.name}) is still running; one stage at a time")
            job = Job(name, fn)
            self.jobs.append(job)
            job.thread.start()
            return job

    def get(self, job_id: str) -> Optional[Job]:
        return next((j for j in self.jobs if j.job_id == job_id), None)

    def wait_all(self, timeout: float) -> None:
        for j in self.jobs:
            j.thread.join(timeout)


TOOLS = [
    {"name": "status", "description": "What has run and how it went: stage results, the registry of images with their emulator/board tests, the board's known kernel, card backups.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "run_stage", "description": "Run one pipeline stage. sources answers inline; kernel/rootfs/image/emulate/update start a job (poll `job`). dry_run lists the exact commands and runs nothing. options are the stage's keyword arguments (kernel: clean, extras; image: compress, size_gb; emulate: mode stock|kernel, image; sources: update; update: force, rpm, prune_keep).",
     "inputSchema": {"type": "object", "properties": {"stage": {"type": "string", "enum": list(INLINE_STAGES + LONG_STAGES)},
                                                      "options": {"type": "object"}, "dry_run": {"type": "boolean"}}, "required": ["stage"]}},
    {"name": "job", "description": "A job's status (running|done), its result when done, and the tail of its log.",
     "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["job_id"]}},
    {"name": "jobs", "description": "Every job this server started, newest first.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "candidates", "description": "Removable whole disks the flash stage would accept (path, model, size, bus, mounted filesystems).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "flash", "description": "Write a tested image to a card: refuses an untested image (unless force), a non-candidate, a mounted or root disk, and any call whose `confirm` is not the device's name (e.g. 'sdz'). Backs the card up first unless backup=false. Runs as a job unless dry_run.",
     "inputSchema": {"type": "object", "properties": {"device": {"type": "string"}, "confirm": {"type": "string"}, "image": {"type": "string"},
                                                      "backup": {"type": "boolean"}, "force": {"type": "boolean"}, "dry_run": {"type": "boolean"}}, "required": ["device"]}},
    {"name": "serial_watch", "description": "Watch the board's boot on the configured serial console (job): milestones, panic/hang detection, the first-boot checklist, ssh confirmation. fallback=true types 2 at the extlinux menu (the image's other kernel).",
     "inputSchema": {"type": "object", "properties": {"fallback": {"type": "boolean"}, "image": {"type": "string"}, "dry_run": {"type": "boolean"}}}},
    {"name": "serial_run", "description": "Log in on the board's serial console (user radxa) and run commands, one at a time; returns each command's output and exit code. For when ssh is not available. Blocks until done (timeout per command).",
     "inputSchema": {"type": "object", "properties": {"commands": {"type": "array", "items": {"type": "string"}}, "timeout": {"type": "integer"}}, "required": ["commands"]}},
    {"name": "serial_probe", "description": "Log in on the console and collect the board's facts: kernel, addresses, links, CPU max, root, failed units, modules, DRM/NPU nodes, WiFi, sleep masked, first-boot checklist, display manager. Records them against the image's board test and the registry's board entry.",
     "inputSchema": {"type": "object", "properties": {"image": {"type": "string"}}}},
    {"name": "images", "description": "The registry of built images: sha256, size, kernel release, source commits, tests, flashes.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "log_tail", "description": "The last lines of the newest log of a stage (kernel, image, emulate, flash, serial...) or of a given file under output/.",
     "inputSchema": {"type": "object", "properties": {"stage": {"type": "string"}, "path": {"type": "string"}, "lines": {"type": "integer"}}}},
    {"name": "config", "description": "The effective configuration: paths, branches, serial port, board hosts, timeouts.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Server:
    def __init__(self, config: Config):
        self.config = config
        self.registry = StageRegistry(config.registry_path)
        self.jobs = Jobs()

    # -- JSON-RPC ---------------------------------------------------------------------
    def handle(self, line: str) -> Optional[str]:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return self._error(None, -32700, "parse error")
        rid = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        if method.startswith("notifications/"):
            return None
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {}},
                          "serverInfo": {"name": "a7a-fedora", "version": "1.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.call_tool(params.get("name", ""), params.get("arguments") or {})
            else:
                return self._error(rid, -32601, f"method not found: {method}")
        except Exception as e:  # noqa: BLE001
            logger.exception("request failed")
            return self._error(rid, -32603, f"{type(e).__name__}: {e}")
        return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})

    @staticmethod
    def _error(rid, code: int, message: str) -> str:
        return json.dumps({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # -- tools -------------------------------------------------------------------------
    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict:
        fn = getattr(self, f"tool_{name}", None)
        if fn is None:
            return self._content({"error": f"unknown tool {name}"}, error=True)
        try:
            data = fn(**args)
        except TypeError as e:
            return self._content({"error": f"bad arguments for {name}: {e}"}, error=True)
        except RuntimeError as e:
            return self._content({"error": str(e)}, error=True)
        error = isinstance(data, dict) and data.get("status") in ("failed", "refused") or (isinstance(data, dict) and "error" in data)
        return self._content(data, error=bool(error))

    @staticmethod
    def _content(data, error: bool = False) -> Dict:
        return {"content": [{"type": "text", "text": json.dumps(data, indent=1, default=str)}], "isError": error}

    def _fresh_registry(self) -> StageRegistry:
        return StageRegistry(self.config.registry_path)

    def tool_status(self) -> Dict:
        reg = self._fresh_registry()
        return {"summary": reg.summary(), "stages": reg.all_stages(), "images": reg.images(), "board": reg.data.get("board"),
                "backups": reg.data.get("backups", []), "running_job": (self.jobs.running().to_dict(10) if self.jobs.running() else None)}

    def tool_config(self) -> Dict:
        return self.config.to_dict()

    def tool_images(self) -> Dict:
        return self._fresh_registry().images()

    def tool_candidates(self) -> List[Dict]:
        from src.stages.flash import candidates
        return candidates(self.config)

    def tool_jobs(self) -> List[Dict]:
        return [j.to_dict(5) for j in reversed(self.jobs.jobs)]

    def tool_job(self, job_id: str, tail: int = 40) -> Dict:
        j = self.jobs.get(job_id)
        if not j:
            return {"error": f"no job {job_id}"}
        return j.to_dict(tail)

    def tool_run_stage(self, stage: str, options: Optional[Dict] = None, dry_run: bool = False) -> Dict:
        options = dict(options or {})
        if stage not in INLINE_STAGES + LONG_STAGES:
            return {"error": f"stage {stage!r} is not runnable here; use the flash and serial_watch tools for those, or one of {', '.join(INLINE_STAGES + LONG_STAGES)}"}
        mod = _stage_module(stage)
        if dry_run or stage in INLINE_STAGES:
            return mod.run(self.config, self._fresh_registry(), dry_run=dry_run, **options)
        job = self.jobs.start(stage, lambda log: mod.run(self.config, self._fresh_registry(), on_line=log, **options))
        return {"status": "running", "job_id": job.job_id, "stage": stage, "hint": "poll the job tool; kernel builds take minutes, emulate a few, image ~5"}

    def tool_flash(self, device: str, confirm: Optional[str] = None, image: Optional[str] = None, backup: bool = True,
                   force: bool = False, dry_run: bool = False) -> Dict:
        from src.stages import flash
        if dry_run:
            return flash.run(self.config, self._fresh_registry(), image=image, device=device, confirm=confirm, backup=backup, force=force, dry_run=True)
        # the gates run inline first so a refusal is immediate and no job is started for it
        probe = flash.run(self.config, self._fresh_registry(), image=image, device=device, confirm=confirm, backup=backup, force=force, dry_run=True)
        if probe["status"] != "dry_run":
            return probe
        job = self.jobs.start("flash", lambda log: flash.run(self.config, self._fresh_registry(), image=image, device=device, confirm=confirm,
                                                            backup=backup, force=force, on_line=log))
        return {"status": "running", "job_id": job.job_id, "stage": "flash", "plan": probe.get("plan")}

    def tool_serial_watch(self, fallback: bool = False, image: Optional[str] = None, dry_run: bool = False) -> Dict:
        from src.stages import serial
        if dry_run:
            return serial.run(self.config, self._fresh_registry(), image=image, fallback=fallback, dry_run=True)
        job = self.jobs.start("serial", lambda log: serial.run(self.config, self._fresh_registry(), image=image, fallback=fallback, on_line=log))
        return {"status": "running", "job_id": job.job_id, "stage": "serial", "port": self.config.serial_port}

    def tool_serial_run(self, commands: List[str], timeout: int = 60) -> Dict:
        from src.stages import serial
        return serial.run_commands(self.config, self._fresh_registry(), list(commands), timeout=timeout)

    def tool_serial_probe(self, image: Optional[str] = None) -> Dict:
        from src.stages import serial
        return serial.probe(self.config, self._fresh_registry(), image=image)

    def tool_log_tail(self, stage: Optional[str] = None, path: Optional[str] = None, lines: int = 40) -> Dict:
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = self.config.output_dir / p
            if self.config.output_dir.resolve() not in p.resolve().parents:
                return {"error": "only files under output/ can be read"}
        else:
            logs = sorted(self.config.logs_dir.glob(f"{stage}-*.log")) if stage and self.config.logs_dir.exists() else []
            if not logs:
                return {"error": f"no log for stage {stage!r} under {self.config.logs_dir}"}
            p = logs[-1]
        if not p.exists():
            return {"error": f"{p} does not exist"}
        with open(p, errors="replace") as f:
            tail = deque(f, maxlen=max(1, int(lines)))
        return {"path": str(p), "lines": [l.rstrip("\n") for l in tail]}


def serve(config: Config, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = Server(config)
    for line in stdin:
        if not line.strip():
            continue
        out = server.handle(line)
        if out is not None:
            stdout.write(out + "\n")
            stdout.flush()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="a7a-fedora MCP server (stdio)")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    serve(Config.load(Path(args.config) if args.config else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
