"""Stage registry — tracks what has been run, with logs."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("a7a-fedora.registry")


class StageRegistry:
    """Records stage runs, their commands, exit codes, and results."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or Path("registry.json")
        self.data: Dict[str, Any] = {
            "version": 1,
            "created": datetime.now(timezone.utc).isoformat(),
            "stages": {},
            "images": {},
            "summary": {},
        }
        self._load()
        self.data.setdefault("images", {})

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, indent=2, default=str))

    def record_stage(
        self,
        name: str,
        status: str,
        commands: Optional[List[Dict[str, Any]]] = None,
        artifacts: Optional[List[str]] = None,
        notes: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a stage run (extra: stage-specific facts such as the kernel release)."""
        entry: Dict[str, Any] = {
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if commands:
            entry["commands"] = commands
        if artifacts:
            entry["artifacts"] = artifacts
        if notes:
            entry["notes"] = notes
        if duration_seconds is not None:
            entry["duration_seconds"] = duration_seconds
        if extra:
            entry.update(extra)
        previous = self.data["stages"].get(name) or {}
        if status == "dry_run" and previous.get("status") in self.REAL:
            # a dry run never replaces what really happened; it is noted beside it
            previous["last_dry_run"] = {"timestamp": entry["timestamp"], "notes": notes}
            self.data["stages"][name] = previous
            self._update_summary()
            self.save()
            return
        if "commands" not in entry and previous.get("commands"):
            entry["commands"] = previous["commands"]      # recorded by run_tool during this run
        if previous.get("last_dry_run") and status != "dry_run":
            pass                                           # a real run supersedes the note
        self.data["stages"][name] = entry
        self._update_summary()
        self.save()
        logger.info("Stage '%s' recorded: %s", name, status)

    REAL = ("passed", "failed", "refused", "skipped", "rolled_back")

    def begin_stage(self, name: str, dry_run: bool = False) -> None:
        """A real run is starting: forget the previous run's commands, mark it in progress. A dry run touches nothing."""
        if dry_run:
            return
        self.data["stages"][name] = {"status": "in_progress", "timestamp": datetime.now(timezone.utc).isoformat(),
                                     "commands": []}
        self.save()

    def _update_summary(self) -> None:
        counts = {"passed": 0, "failed": 0, "skipped": 0, "dry_run": 0}
        for stage in self.data["stages"].values():
            s = stage.get("status", "unknown")
            if s in counts:
                counts[s] += 1
        self.data["summary"] = counts

    def get_stage(self, name: str) -> Optional[Dict[str, Any]]:
        return self.data["stages"].get(name)

    def stage_passed(self, name: str) -> bool:
        entry = self.get_stage(name)
        return entry is not None and entry.get("status") == "passed"

    def all_stages(self) -> Dict[str, Any]:
        return self.data["stages"]

    def summary(self) -> Dict[str, int]:
        return self.data.get("summary", {})

    def record_command(
        self,
        stage_name: str,
        argv: List[str],
        exit_code: int,
        cwd: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        log: Optional[str] = None,
        dry_run: bool = False,
    ) -> None:
        """Record a single command within a stage."""
        if stage_name not in self.data["stages"]:
            self.record_stage(stage_name, "dry_run" if dry_run else "in_progress")
        entry = self.data["stages"][stage_name]
        if dry_run and entry.get("status") in self.REAL:
            entry.setdefault("dry_run_commands", [])
            entry["dry_run_commands"] = (entry["dry_run_commands"] + [{"argv": argv, "log": log}])[-20:]
            self.save()
            return
        if "commands" not in entry:
            entry["commands"] = []
        rec: Dict[str, Any] = {"argv": argv, "exit_code": exit_code, "cwd": cwd,
                               "timestamp": datetime.now(timezone.utc).isoformat()}
        if stdout is not None:
            rec["stdout"] = stdout
        if stderr is not None:
            rec["stderr"] = stderr
        if log is not None:
            rec["log"] = log
        if dry_run:
            rec["dry_run"] = True
        entry["commands"].append(rec)
        self.save()

    # -- the registry of built images and what happened to them -------------------------

    def images(self) -> Dict[str, Any]:
        return self.data.setdefault("images", {})

    def record_image(self, name: str, info: Dict[str, Any]) -> None:
        """A newly built image: name is the file's basename; info = sha256, size, kernel_release..."""
        img = self.images().setdefault(name, {"tests": [], "flashes": []})
        if info.get("sha256") and img.get("sha256") and img["sha256"] != info["sha256"]:
            img["previous_builds"] = img.get("previous_builds", []) + [{"sha256": img["sha256"], "tests": img.get("tests", []), "flashes": img.get("flashes", []),
                                                                        "created": img.get("created")}]
            img["tests"], img["flashes"] = [], []          # the bytes changed: the old verdicts do not apply
            img.pop("created", None)
        img.update(info)
        img.setdefault("created", datetime.now(timezone.utc).isoformat())
        self.save()

    def record_test(self, name: str, mode: str, verdict: str, log: str, **extra: Any) -> None:
        """An emulator (or serial) run against an image: mode = stock|kernel|board, verdict = PASS|FAIL."""
        img = self.images().setdefault(name, {"tests": [], "flashes": []})
        img["tests"].append({"mode": mode, "verdict": verdict, "log": log,
                             "timestamp": datetime.now(timezone.utc).isoformat(), **extra})
        self.save()

    def record_flash(self, name: str, device: str, verified: bool, backup: Optional[str] = None, **extra: Any) -> None:
        img = self.images().setdefault(name, {"tests": [], "flashes": []})
        img["flashes"].append({"device": device, "verified": verified, "backup": backup,
                               "timestamp": datetime.now(timezone.utc).isoformat(), **extra})
        self.save()

    def _kernel_runs(self, name: str) -> List[Dict[str, Any]]:
        return [t for t in self.images().get(name, {}).get("tests", []) if t["mode"] == "kernel"]

    def image_tested(self, name: str) -> bool:
        """True when the image's LAST emulator boot with the built kernel passed (the flash gate): a PASS from before
        a later FAIL says nothing about the file as it is now."""
        runs = self._kernel_runs(name)
        return bool(runs) and runs[-1]["verdict"] == "PASS"

    def tested_sha256(self, name: str) -> Optional[str]:
        """The sha256 of the bytes that passed that boot: what the emulate stage hashed before the run; for a record
        older than 2026-11-16 the checksum of the build. None: not tested, or nothing says which bytes were."""
        if not self.image_tested(name):
            return None
        return self._kernel_runs(name)[-1].get("sha256") or self.images()[name].get("sha256")

    def log_dry_run(self, stage_name: str, reason: str) -> None:
        """Log a dry-run stage."""
        self.record_stage(
            stage_name,
            "dry_run",
            notes=f"Dry run: {reason}",
        )
