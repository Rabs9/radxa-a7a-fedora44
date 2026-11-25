"""Tests for src/mcp_server.py: JSON-RPC over stdio, the tools, jobs for the long stages."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src import mcp_server
from src.config import Config


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work", mirror_dir=root / "mirror",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.sudo_prefix = []
        self.cfg.output_dir.mkdir(); self.cfg.tools_dir.mkdir()
        self.server = mcp_server.Server(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, method, params=None, id=1):
        return json.loads(self.server.handle(json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})))

    def tool(self, name, args=None):
        r = self.call("tools/call", {"name": name, "arguments": args or {}})
        self.assertIn("result", r, r)
        return json.loads(r["result"]["content"][0]["text"]), r["result"].get("isError", False)

    def test_initialize_and_tools_list(self):
        r = self.call("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(r["result"]["serverInfo"]["name"], "a7a-fedora")
        self.assertIn("tools", r["result"]["capabilities"])
        r = self.call("tools/list")
        names = [t["name"] for t in r["result"]["tools"]]
        for n in ["status", "run_stage", "job", "jobs", "candidates", "flash", "serial_watch", "serial_run", "serial_probe", "images", "log_tail", "config"]:
            self.assertIn(n, names)
        for t in r["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])

    def test_notification_has_no_response(self):
        self.assertIsNone(self.server.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})))

    def test_unknown_method_and_bad_json(self):
        r = self.call("nope")
        self.assertEqual(r["error"]["code"], -32601)
        r = json.loads(self.server.handle("{not json"))
        self.assertEqual(r["error"]["code"], -32700)

    def test_status_and_config_tools(self):
        data, err = self.tool("status")
        self.assertFalse(err)
        self.assertIn("stages", data)
        data, _ = self.tool("config")
        self.assertEqual(data["tools_dir"], str(self.cfg.tools_dir))

    def test_run_stage_dry_run_is_inline(self):
        data, err = self.tool("run_stage", {"stage": "sources", "dry_run": True})
        self.assertFalse(err)
        self.assertEqual(data["status"], "dry_run")
        self.assertTrue(any("git clone" in c for c in data["commands"]))

    def test_long_stage_runs_as_a_job(self):
        img = self.cfg.output_dir / "a7a-fedora-44-kde-20260906.img"
        img.write_bytes(b"\0")
        _stub(self.cfg.tools_dir / "emulate.sh", 'echo "  milestone: login:"; sleep 0.2; echo "VERDICT: PASS - ok"\n')
        data, err = self.tool("run_stage", {"stage": "emulate", "options": {"mode": "kernel"}})
        self.assertFalse(err)
        self.assertEqual(data["status"], "running")
        job_id = data["job_id"]
        for _ in range(100):
            j, _ = self.tool("job", {"job_id": job_id})
            if j["status"] == "done":
                break
            time.sleep(0.05)
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(j["result"]["verdict"], "PASS")
        self.assertTrue(any("VERDICT" in l for l in j["log_tail"]))
        jobs, _ = self.tool("jobs")
        self.assertEqual(jobs[0]["job_id"], job_id)
        imgs, _ = self.tool("images")
        self.assertEqual(imgs[img.name]["tests"][0]["verdict"], "PASS")

    def test_one_job_at_a_time(self):
        _stub(self.cfg.tools_dir / "build-kernel.sh", 'sleep 1\n')
        (self.cfg.work_dir / "kernel-6.6").mkdir(parents=True)
        self.cfg.cross_compile = ""
        d1, _ = self.tool("run_stage", {"stage": "kernel", "options": {"extras": False}})
        d2, err = self.tool("run_stage", {"stage": "kernel"})
        self.assertTrue(err)
        self.assertIn("running", d2["error"])
        self.server.jobs.wait_all(5)

    def test_flash_refuses_without_confirm(self):
        img = self.cfg.output_dir / "a7a-fedora-44-kde-20260906.img"
        img.write_bytes(b"\0")
        lsblk = _stub(Path(self.tmp.name) / "lsblk", 'echo \'{"blockdevices": []}\'\n')
        self.cfg.lsblk_argv = [str(lsblk)]
        data, err = self.tool("flash", {"device": "/dev/sdz", "force": True})
        self.assertTrue(err)
        self.assertEqual(data["status"], "refused")

    def test_unknown_stage_is_an_error(self):
        data, err = self.tool("run_stage", {"stage": "flash"})
        self.assertTrue(err)
        self.assertIn("flash", data["error"])

    def test_log_tail(self):
        (self.cfg.logs_dir).mkdir(parents=True)
        (self.cfg.logs_dir / "kernel-20260906-000000.log").write_text("\n".join(f"line {i}" for i in range(50)) + "\n")
        data, err = self.tool("log_tail", {"stage": "kernel", "lines": 5})
        self.assertFalse(err)
        self.assertEqual(data["lines"][-1], "line 49")
        self.assertEqual(len(data["lines"]), 5)


class TestStdio(unittest.TestCase):
    def test_round_trip_over_pipes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = {"project_root": d, "output_dir": str(root / "output"), "work_dir": str(root / "work"), "mirror_dir": str(root / "mirror"),
                   "tools_dir": str(root / "tools"), "registry_path": str(root / "registry.json")}
            cfgfile = root / "a7a-fedora.json"
            cfgfile.write_text(json.dumps(cfg))
            proc = subprocess.Popen([sys.executable, "-m", "src.mcp_server", "--config", str(cfgfile)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(Path(__file__).resolve().parent.parent))
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "status", "arguments": {}}}]
            out, err = proc.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=30)
            lines = [json.loads(l) for l in out.splitlines() if l.strip()]
            self.assertEqual([l["id"] for l in lines], [1, 2])
            self.assertIn("stages", json.loads(lines[1]["result"]["content"][0]["text"]))
            self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
