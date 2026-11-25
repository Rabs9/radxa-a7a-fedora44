"""tools/standard-dtb.py: the standard image's device tree is the kernel's own minus what goes beyond the vendor's
tables. Small device trees are compiled here with dtc; the vendor's side is the same tree without the additions."""
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
TOOL = P / "tools" / "standard-dtb.py"

TREE = """/dts-v1/;
/ {
	compatible = "radxa,cubie-a7a"%(root_extra)s;
	cluster1-opp-table {
		compatible = "allwinner,sun50i-operating-points";
		opp@1800000000 { opp-hz = /bits/ 64 <1800000000>; opp-microvolt-vf0100 = <%(uv1800)s>; };
		opp@2002000000 { opp-hz = /bits/ 64 <2002000000>; opp-microvolt-vf0100 = <1050000>; };
%(cpu_extra)s
	};
	soc@3000000 {
		gpu@1800000 { compatible = "img,gpu"; operating-points-v2 = <&gpu_opp>;%(gpu_extra)s };
		gpu_opp: gpu-opp-table {
			opp@1008000000 { opp-hz = /bits/ 64 <1008000000>; opp-microvolt = <960000>; };
%(gpu_opp_extra)s
		};
		gmac@4500000 { tx-delay = <%(txdelay)s>; };
	};
	thermal-zones {
		cpub_thermal_zone {
			trips {
				trip-point@0 { temperature = <%(t0)s>; hysteresis = <%(h)s>; type = "passive"; };
				trip-point@1 { temperature = <%(t1)s>; hysteresis = <%(h)s>; type = "passive"; };
			};
		};
	};
%(more)s
};
"""
VENDOR = dict(root_extra=', "allwinner,sun60i-a733"', uv1800="1000000", cpu_extra="", gpu_extra="", gpu_opp_extra="", txdelay="12",
              t0="60000", t1="60000", h="2000", more="")
OURS = dict(root_extra="", uv1800="1000000", txdelay="9", t0="50000", t1="80000", h="3000", more="",
            cpu_extra='\t\topp@2500000000 { opp-hz = /bits/ 64 <2500000000>; opp-microvolt-vf0100 = <1150000>; };\n'
                      '\t\topp@3000000000 { opp-hz = /bits/ 64 <3000000000>; opp-microvolt-vf0100 = <1250000>; };',
            gpu_extra=" clk_rate = <1008000000>;",
            gpu_opp_extra='\t\t\topp@1600000000 { opp-hz = /bits/ 64 <1600000000>; opp-microvolt = <1150000>; };')


@unittest.skipUnless(shutil.which("dtc") and shutil.which("fdtput"), "dtc and fdtput needed")
class TestStandardDtb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("standard_dtb", TOOL)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def tearDown(self):
        self.tmp.cleanup()

    def dtb(self, name, **values):
        src = self.t / f"{name}.dts"
        src.write_text(TREE % values)
        out = self.t / f"{name}.dtb"
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(out), str(src)], check=True, capture_output=True)
        return out

    def run_tool(self, ours, vendor):
        out = self.t / "standard.dtb"
        r = subprocess.run([sys.executable, str(TOOL), str(ours), str(vendor), str(out)], capture_output=True, text=True, timeout=60)
        return r, out

    def test_the_clocks_and_limits_become_the_vendors_and_nothing_else_changes(self):
        r, out = self.run_tool(self.dtb("ours", **OURS), self.dtb("vendor", **VENDOR))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got, before = self.m.parse(str(out)), self.m.parse(str(self.t / "ours.dtb"))
        self.assertEqual(sorted(n for n in got if "opp@" in n), ["/cluster1-opp-table/opp@1800000000", "/cluster1-opp-table/opp@2002000000",
                                                                  "/soc@3000000/gpu-opp-table/opp@1008000000"])
        self.assertEqual(self.m.ceiling(got, "/cluster1-opp-table"), 2002)
        self.assertEqual(self.m.ceiling(got, "/soc@3000000/gpu-opp-table"), 1008)
        trips = "/thermal-zones/cpub_thermal_zone/trips/trip-point@"
        self.assertEqual([self.m.cells(got[trips + i][p])[0] for i in "01" for p in ("temperature", "hysteresis")], [60000, 2000, 60000, 2000])
        self.assertEqual(got[trips + "0"]["type"], b"passive\0")
        # what is the owner's fix and not a clock stays: the ethernet delay, the GPU's boot clock, the compatible string
        self.assertEqual(self.m.cells(got["/soc@3000000/gmac@4500000"]["tx-delay"]), (9,))
        self.assertEqual(self.m.cells(got["/soc@3000000/gpu@1800000"]["clk_rate"]), (1008000000,))
        self.assertEqual(got["/"]["compatible"], before["/"]["compatible"])
        self.assertEqual(got["/soc@3000000/gpu@1800000"]["operating-points-v2"], before["/soc@3000000/gpu@1800000"]["operating-points-v2"])
        self.assertIn("ceiling 3000 MHz -> 2002 MHz (vendor 2002)", r.stdout)
        self.assertIn("removed 3 operating points", r.stdout)

    def test_a_difference_it_does_not_understand_stops_it(self):
        vendor = self.dtb("vendor", **VENDOR)
        cases = {
            "a node that is no operating point": dict(OURS, more="\towners-own-node { x = <1>; };"),
            "a vendor operating point with another voltage": dict(OURS, uv1800="900000"),
            "an added operating point something refers to": dict(
                OURS, gpu_opp_extra='\t\t\textra: opp@1600000000 { opp-hz = /bits/ 64 <1600000000>; };', more="\tuser { ref = <&extra>; };"),
        }
        for what, values in cases.items():
            r, out = self.run_tool(self.dtb("ours", **values), vendor)
            self.assertEqual(r.returncode, 1, what + ": " + r.stdout)
            self.assertFalse(out.exists(), what)

    def test_a_tree_the_vendor_has_more_of_stops_it(self):
        r, out = self.run_tool(self.dtb("ours", **OURS), self.dtb("vendor", **dict(VENDOR, more="\tvendor-only { y = <2>; };")))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertFalse(out.exists())

    def test_a_tree_that_is_standard_already_is_said_to_be(self):
        r, out = self.run_tool(self.dtb("ours", **VENDOR), self.dtb("vendor", **VENDOR))
        self.assertEqual(r.returncode, 1)
        self.assertIn("already", r.stdout)
        self.assertFalse(out.exists())

    def test_something_that_is_not_a_device_tree(self):
        junk = self.t / "junk.dtb"
        junk.write_bytes(b"not a device tree at all")
        r, out = self.run_tool(junk, self.dtb("vendor", **VENDOR))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
