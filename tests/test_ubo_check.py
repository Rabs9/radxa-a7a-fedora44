"""Tests for tools/gpu-debug/ubo-check.py, the device check of build-pvr-vulkan.sh patch 6 (13 Vulkan uniform
buffers per stage): a GL program reads 12 uniform blocks in one stage (fragment, then vertex) and the pixel it
writes must come back exactly. On this laptop's GL it is the positive control; on the board it reports
'unavailable' without GL_ARB_uniform_buffer_object (before the patch) and must pass after it."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "gpu-debug" / "ubo-check.py"


class UboCheck(unittest.TestCase):
    def run_check(self, *args):
        r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=120)
        lines = [l for l in r.stdout.splitlines() if l.startswith("{")]
        return r.returncode, (json.loads(lines[-1]) if lines else {"stderr": r.stderr[-500:]})

    # skip only when the check itself says so: a missing or crashing script also exits non-zero (python: 2)
    def test_twelve_blocks_read_back_exactly_on_this_machine(self):
        rc, d = self.run_check()
        if d.get("result") == "unavailable":
            self.skipTest("no GL context with uniform buffers here: %s" % d.get("reason"))
        self.assertEqual(rc, 0, d)
        self.assertEqual(d["result"], "PASS")
        # blue = the first block's .z (100) + a plain uniform (50): zink keeps GL's default uniform block in a buffer
        # of its own, which IS the 13th Vulkan uniform buffer of the stage (third reviewer pass)
        for stage in ("fragment", "vertex"):
            self.assertEqual(d["stages"][stage]["pixel"], [78, 200, 150, 255], stage)
            self.assertEqual(d["stages"][stage]["result"], "PASS", stage)
        self.assertGreaterEqual(d["limits"]["GL_MAX_FRAGMENT_UNIFORM_BLOCKS"], 12)

    def test_fewer_blocks_is_a_different_pixel(self):
        # the expected value is not a constant the check always gets: 11 blocks give another sum
        rc, d = self.run_check("--blocks", "11")
        if d.get("result") == "unavailable":
            self.skipTest("no GL context with uniform buffers here")
        self.assertEqual(d["stages"]["fragment"]["expected"], [66, 200, 150, 255])
        self.assertEqual(d["stages"]["fragment"]["pixel"], [66, 200, 150, 255])

    def test_the_comparison_is_exact(self):
        # a neighbour block's data (block 11 reading block 10's) is off by one in red: that must FAIL, so no tolerance
        src = SCRIPT.read_text()
        self.assertNotIn("<= 1", src)


if __name__ == "__main__":
    unittest.main()
