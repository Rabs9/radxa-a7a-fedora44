"""Tests for tools/gpu-debug/image-limit-check.py, the device check of build-pvr-vulkan.sh patches 7-8 (2048 array
layers, 2048-deep 3D textures): a GL program samples the last layer of a 2048-layer array texture and the last slice
of a 2048-deep 3D texture, and renders into the last layer; every layer holds a value that names it, so a wrong or
missing layer shows in the pixel. On this laptop's GL it is the positive control; on the board it reports
'unavailable' while the GL limits are below the request (before the patches) and must pass after them."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "gpu-debug" / "image-limit-check.py"


class ImageLimitCheck(unittest.TestCase):
    def run_check(self, *args):
        r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        return r.returncode, (json.loads(lines[-1]) if lines else {"stderr": r.stderr[-500:]})

    # skip only when the check itself says so: a missing or crashing script also exits non-zero
    def test_last_layer_and_last_slice_read_back_exactly_on_this_machine(self):
        rc, d = self.run_check()
        if d.get("result") == "unavailable":
            self.skipTest("this GL cannot hold 2048 layers: %s" % d.get("reason"))
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["result"], "PASS")
        for name in ("array", "3d", "render"):
            c = d["checks"][name]
            self.assertEqual(c["pixel"], c["expected"], name)
            self.assertEqual(c["layer"], 2047, name)
        # layer 2047 = 0x7ff: low byte 255, high byte 7
        self.assertEqual(d["checks"]["array"]["expected"], [255, 7, 90, 255])

    def test_another_layer_is_another_value(self):
        rc, d = self.run_check("--layer", "1000")
        if d.get("result") == "unavailable":
            self.skipTest("this GL cannot hold 2048 layers")
        self.assertEqual(d["checks"]["array"]["expected"], [1000 & 255, 1000 >> 8, 90, 255])
        self.assertEqual(d["checks"]["array"]["pixel"], d["checks"]["array"]["expected"])

    def test_the_comparison_is_exact_and_framebuffer_layers_are_recorded(self):
        # layer 2046 read instead of 2047 differs by one in red: a tolerance would pass it (third reviewer pass)
        self.assertNotIn("<= 1", SCRIPT.read_text())
        rc, d = self.run_check()
        if d.get("result") == "unavailable":
            self.skipTest("this GL cannot hold 2048 layers")
        self.assertIn("GL_MAX_FRAMEBUFFER_LAYERS", d["limits"])
        # more than the last layer: a sampler clamping to the last layer would pass a last-layer-only check
        self.assertEqual(sorted(d["checks"]["array"]["layers_checked"]), [256, 1024, 2046, 2047])

    def test_a_request_above_the_gl_limits_is_unavailable_not_a_failure(self):
        rc, d = self.run_check("--layers", "1000000")
        self.assertEqual(d.get("result"), "unavailable", d)
        self.assertEqual(rc, 3)


if __name__ == "__main__":
    unittest.main()
