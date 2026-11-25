"""Tests for tools/gpu-debug/stk-bench.sh, SuperTuxKart's --benchmark on the board (docs/SPEED-2026-09-10.md: the
black_forest replay, a fixed 762-frame workload, wall time is the metric; baseline 4.13 fps). The script times the
run, reads which renderer STK chose from its own log (the fixed-function fallback or the modern one) and counts the
frames in its profiler CSV. Driven here with a fake supertuxkart that writes those files."""

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "gpu-debug" / "stk-bench.sh"

FIXED = textwrap.dedent("""\
    [info   ] IrrDriver: OpenGL renderer: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
    [info   ] IrrDriver: OpenGL version: 2.1            OpenGL ES 2.0 Mesa 26.1.8
    [warn   ] irr_driver: Using the fixed pipeline (old GPU, or shaders disabled in options)
    [warn   ] OpenGL: OpenGL version is too old!
    """)
MODERN = textwrap.dedent("""\
    [info   ] IrrDriver Logger: Using renderer: OpenGL ES 3.0 Mesa 26.1.8
    [info   ] IrrDriver: OpenGL renderer: zink Vulkan 1.2(PowerVR B-Series BXM-4-64 MC1 (IMAGINATION_OPEN_SOURCE_MESA))
    [info   ] IrrDriver: OpenGL version: 3.0            OpenGL version string: OpenGL ES 3.0 Mesa 26.1.8
    """)


class StkBench(unittest.TestCase):
    def run_fake(self, log, frames=762):
        t = tempfile.mkdtemp()
        cfg = Path(t) / "config-0.10"
        fake = Path(t) / "bin" / "supertuxkart"
        fake.parent.mkdir()
        fake.write_text("#!/bin/bash\n[ \"$1\" = --benchmark ] || exit 9\nmkdir -p %s\ncat > %s/stdout.log <<'EOF'\n%sEOF\n"
                        "{ echo 'frame,Main loop'; for i in $(seq 1 %d); do echo \"$i,242.7\"; done; } > "
                        "%s/stdout.log.profile-black_forest-cpu-0.csv\nsleep 0.2\n" % (cfg, cfg, log, frames, cfg))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH="%s:%s" % (fake.parent, os.environ["PATH"]), STK_CONFIG_DIR=str(cfg),
                   WAYLAND_DISPLAY="wayland-test", XDG_RUNTIME_DIR=t)
        r = subprocess.run(["bash", str(SCRIPT), "--label", "t"], capture_output=True, text=True, env=env, timeout=60)
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        return r, (json.loads(lines[-1]) if lines else {"stdout": r.stdout, "stderr": r.stderr})

    def test_fixed_pipeline_run_is_reported_as_such(self):
        r, d = self.run_fake(FIXED)
        self.assertEqual(r.returncode, 0, d)
        self.assertEqual(d["renderer_path"], "fixed-function")
        self.assertIn("zink Vulkan 1.2(PowerVR", d["gl_renderer"])
        self.assertTrue(d["version_too_old"])
        self.assertEqual(d["frames"], 762)
        self.assertGreater(d["seconds"], 0)
        self.assertAlmostEqual(d["fps"], 762 / d["seconds"], places=1)
        self.assertEqual(d["label"], "t")

    def test_modern_renderer_run_is_reported_as_such(self):
        r, d = self.run_fake(MODERN)
        self.assertEqual(d["renderer_path"], "modern")
        self.assertIn("OpenGL ES 3.0", d["using_renderer"])
        self.assertFalse(d["version_too_old"])


if __name__ == "__main__":
    unittest.main()
