"""tools/board/a7a-ac101-dtb: the AC101 of the first Cubie A7A boards and its sound card are added, the AC101B and its card are
switched off (two cards on one I2S crash this kernel), and nothing else in the tree changes. Small device trees are
compiled here with dtc, in the shape of the kernel's own."""
import glob
import importlib.machinery
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
TOOL = P / "tools" / "board" / "a7a-ac101-dtb"

TREE = """/dts-v1/;
/ {
	soc@3000000 {
		pio: pinctrl@2000000 { compatible = "allwinner,sun60iw2-pinctrl"; gpio-controller; #gpio-cells = <3>; };
		r_pio: pinctrl@7025000 { compatible = "allwinner,sun60iw2-r-pinctrl"; gpio-controller; #gpio-cells = <3>; };
		rtc { interrupt-parent = <&r_pio>; };
		pmu {
			reg_aldo5: aldo5 { regulator-name = "axp8191-aldo5"; };
			reg_aldo6: aldo6 { regulator-name = "axp8191-aldo6"; };
			bldo4 { regulator-name = "axp8191-bldo4"; };
		};
		twi@7085000 {
			#address-cells = <1>;
			#size-cells = <0>;
			ac101b: ac101b@3e {
				#sound-dai-cells = <0>;
				compatible = "allwinner,sunxi-ac101b";
				reg = <0x3e>;
				irq-gpio = <&pio %(irq_bank)s 2 0>;
				rst-gpio = <&pio 1 3 0>;
				rglt0-supply = <&%(supply)s>;
			};
%(more_i2c)s
		};
		i2s0_plat: i2s0_plat@2532000 { #sound-dai-cells = <0>; };
		i2s0_mach {
			compatible = "allwinner,sunxi-snd-mach";
			soundcard-mach,frame-master = <&i2s0_cpu>;
			i2s0_cpu: soundcard-mach,cpu { sound-dai = <&i2s0_plat>; };
			soundcard-mach,codec { sound-dai = <&ac101b>; };
		};
	};
};
"""
KERNEL = dict(irq_bank="1", supply="reg_aldo5", more_i2c="")
CODEC, CARD = "/soc@3000000/twi@7085000/ac101@1a", "/soc@3000000/i2s0_mach_ac101"
AC101B, AC101B_CARD = "/soc@3000000/twi@7085000/ac101b@3e", "/soc@3000000/i2s0_mach"


@unittest.skipUnless(shutil.which("dtc") and shutil.which("fdtput"), "dtc and fdtput needed")
class TestAc101Dtb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        loader = importlib.machinery.SourceFileLoader("a7a_ac101_dtb", str(TOOL))
        self.m = importlib.util.module_from_spec(importlib.util.spec_from_loader("a7a_ac101_dtb", loader))
        loader.exec_module(self.m)

    def tearDown(self):
        self.tmp.cleanup()

    def dtb(self, name, **values):
        src = self.t / f"{name}.dts"
        src.write_text(TREE % values)
        out = self.t / f"{name}.dtb"
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(out), str(src)], check=True, capture_output=True)
        return out

    def run_tool(self, src, name="out.dtb"):
        out = self.t / name
        r = subprocess.run([sys.executable, str(TOOL), str(src), str(out)], capture_output=True, text=True, timeout=60)
        return r, out

    def ph(self, nodes, path):
        return self.m.cells(nodes[path]["phandle"])[0]

    def test_the_ac101_and_its_card_are_added_and_nothing_else_changes(self):
        src = self.dtb("kernel", **KERNEL)
        r, out = self.run_tool(src)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        before, got = self.m.parse(str(src)), self.m.parse(str(out))
        c = got[CODEC]
        self.assertEqual(c["compatible"], b"allwinner,sunxi-ac101\0")
        self.assertEqual(self.m.cells(c["reg"]), (0x1a,))
        pio, r_pio = self.ph(got, "/soc@3000000/pinctrl@2000000"), self.ph(got, "/soc@3000000/pinctrl@7025000")
        self.assertEqual(self.m.cells(c["irq-gpio"]), (pio, 1, 2, 0))          # PB2, CODEC-IRQ
        self.assertEqual(self.m.cells(c["jack-det-gpio"]), (r_pio, 0, 11, 0))   # PL11, HP-DET-PL11 (r_pio's banks start at PL = 0)
        self.assertEqual(self.m.cells(c["jack-det-gpio-level"]), (0,))
        # ALDO5 3.3 V as the AC101B has it, and BLDO4 1.8 V, which gets its first phandle for this
        self.assertEqual(self.m.cells(c["rglt0-supply"]), (self.ph(got, "/soc@3000000/pmu/aldo5"),))
        self.assertEqual(self.m.cells(c["rglt0-voltage"]), (3300000,))
        self.assertNotIn("phandle", before["/soc@3000000/pmu/bldo4"])
        self.assertEqual(self.m.cells(c["rglt1-supply"]), (self.ph(got, "/soc@3000000/pmu/bldo4"),))
        self.assertEqual(self.m.cells(c["rglt1-voltage"]), (1800000,))
        self.assertEqual(self.m.cells(c["rglt-max"]), (2,))
        self.assertEqual(c["status"], b"okay\0")
        # its own card on the AC101B's I2S, as Radxa's tree had it before the AC101B
        k = got[CARD]
        self.assertEqual(k["compatible"], b"allwinner,sunxi-snd-mach\0")
        self.assertEqual(k["soundcard-mach,name"], b"allwinner-ac101\0")
        self.assertEqual(self.m.cells(k["soundcard-mach,slot-width"]), (16,))
        self.assertIn(b"HPOUTL\0HPOUTL_PIN\0HPOUTR\0HPOUTR_PIN\0", k["soundcard-mach,routing"])
        cpu = self.ph(got, CARD + "/soundcard-mach,cpu")
        self.assertEqual(self.m.cells(k["soundcard-mach,frame-master"]), (cpu,))
        self.assertEqual(self.m.cells(k["soundcard-mach,bitclock-master"]), (cpu,))
        self.assertEqual(self.m.cells(got[CARD + "/soundcard-mach,cpu"]["sound-dai"]),
                         self.m.cells(got["/soc@3000000/i2s0_mach/soundcard-mach,cpu"]["sound-dai"]))
        self.assertEqual(self.m.cells(got[CARD + "/soundcard-mach,codec"]["sound-dai"]), (self.ph(got, CODEC),))
        # the AC101B and its card are off: a second card on the same I2S that fails takes the I2S down with it
        self.assertEqual(got[AC101B]["status"], b"disabled\0")
        self.assertEqual(got[AC101B_CARD]["status"], b"disabled\0")
        # everything else that was there is as it was, apart from BLDO4's new phandle
        added = {"/soc@3000000/pmu/bldo4": {"phandle"}, AC101B: {"status"}, AC101B_CARD: {"status"}}
        for n, props in before.items():
            self.assertEqual(set(got[n]) - set(props), added.get(n, set()), n)
            for p, v in props.items():
                if p not in added.get(n, set()):
                    self.assertEqual(got[n][p], v, f"{n}: {p}")
        self.assertEqual(set(got) - set(before), {CODEC, CARD, CARD + "/soundcard-mach,cpu", CARD + "/soundcard-mach,codec"})
        phs = [self.m.cells(p["phandle"])[0] for p in got.values() if "phandle" in p]
        self.assertEqual(len(phs), len(set(phs)))

    def test_undo_puts_the_packaged_state_back(self):
        src = self.dtb("kernel", **KERNEL)
        r, ac101 = self.run_tool(src)
        self.assertEqual(r.returncode, 0, r.stdout)
        undo = self.t / "undo.dtb"
        r = subprocess.run([sys.executable, str(TOOL), "--undo", str(ac101), str(undo)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        before, got = self.m.parse(str(src)), self.m.parse(str(undo))
        self.assertEqual(set(got), set(before))                                  # the AC101 and its card are gone
        for n, props in before.items():                                          # and the rest is as packaged:
            extra = {"phandle"} if n == "/soc@3000000/pmu/bldo4" else set()      # BLDO4 keeps its (unused) phandle
            self.assertEqual(set(got[n]) - set(props) - {"status"}, extra, n)
            for p, v in props.items():
                self.assertEqual(got[n][p], v, f"{n}: {p}")
        for n in (AC101B, AC101B_CARD):
            self.assertEqual(got[n]["status"], b"okay\0")
        # undo of a packaged tree changes nothing
        r = subprocess.run([sys.executable, str(TOOL), "--undo", str(src), str(self.t / "same.dtb")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.t / "same.dtb").read_bytes(), src.read_bytes())

    def test_the_two_card_tree_and_a_switched_off_ac101_are_refused(self):
        r, ac101 = self.run_tool(self.dtb("kernel", **KERNEL))
        self.assertEqual(r.returncode, 0, r.stdout)
        cases = {"both cards on (the tree that froze the board)": [(AC101B, "okay"), (AC101B_CARD, "okay")],
                 "the AC101 off": [(CODEC, "disabled")], "the AC101's card off": [(CARD, "disabled")]}
        for what, changes in cases.items():
            bad = self.t / "bad.dtb"
            bad.write_bytes(ac101.read_bytes())
            for node, status in changes:
                subprocess.run(["fdtput", "-t", "s", str(bad), node, "status", status], check=True)
            for args in ([], ["--undo"]):
                out = self.t / "refused.dtb"
                r = subprocess.run([sys.executable, str(TOOL), *args, str(bad), str(out)], capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 1, f"{what} {args}: {r.stdout}")
                self.assertFalse(out.exists(), what)
                self.assertFalse(Path(str(out) + ".tmp").exists(), what)

    def test_a_tree_with_an_ac101_is_copied_unchanged(self):
        r, once = self.run_tool(self.dtb("kernel", **KERNEL))
        self.assertEqual(r.returncode, 0, r.stdout)
        r, twice = self.run_tool(once, "twice.dtb")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("already", r.stdout)
        self.assertEqual(once.read_bytes(), twice.read_bytes())

    def test_a_tree_it_does_not_know_stops_it(self):
        cases = {
            "an AC101B whose interrupt is not PB2": dict(KERNEL, irq_bank="2"),
            "an AC101B fed by another regulator": dict(KERNEL, supply="reg_aldo6"),
            "two AC101Bs": dict(KERNEL, more_i2c='\t\t\tsecond@3f { compatible = "allwinner,sunxi-ac101b"; reg = <0x3f>; };'),
            "a node where the AC101 goes": dict(KERNEL, more_i2c='\t\t\tac101@1a { reg = <0x1a>; };'),
        }
        for what, values in cases.items():
            r, out = self.run_tool(self.dtb("kernel", **values))
            self.assertEqual(r.returncode, 1, what + ": " + r.stdout)
            self.assertFalse(out.exists(), what)
            self.assertFalse(Path(str(out) + ".tmp").exists(), what)

    def test_something_that_is_not_a_device_tree(self):
        junk = self.t / "junk.dtb"
        junk.write_bytes(b"not a device tree at all")
        r, out = self.run_tool(junk)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(out.exists())

    def test_the_kernel_packages_tree(self):
        """The real device tree of the kernel packages in output/rpms, when there are any."""
        rpms = sorted(glob.glob(str(P / "output" / "rpms" / "kernel-a7a*.aarch64.rpm")))
        if not rpms or not shutil.which("rpm2cpio") or not shutil.which("cpio"):
            self.skipTest("no kernel package in output/rpms")
        for rpm in rpms:
            d = self.t / Path(rpm).name
            d.mkdir()
            subprocess.run(f"rpm2cpio '{rpm}' | cpio -id --quiet './usr/lib/linux-image-*/allwinner/sun60i-a733-cubie-a7a.dtb'",
                           shell=True, cwd=d, check=True)
            src = next(d.rglob("sun60i-a733-cubie-a7a.dtb"))
            r, out = self.run_tool(src, Path(rpm).name + ".dtb")
            self.assertEqual(r.returncode, 0, rpm + ": " + r.stdout)
            got = self.m.parse(str(out))
            self.assertIn(CODEC, got, rpm)
            self.assertIn(CARD, got, rpm)
            # PL11 in the bank numbering the tree's own r_pio references use (PL2 of the packaged tree: <r_pio 0 2 0>)
            r_pio = self.m.cells(got["/soc@3000000/pinctrl@7025000"]["phandle"])[0]
            banks = {self.m.cells(v)[1] for p in got.values() for k, v in p.items()
                     if k.endswith("gpio") and self.m.cells(v) and len(self.m.cells(v)) == 4 and self.m.cells(v)[0] == r_pio}
            self.assertEqual(self.m.cells(got[CODEC]["jack-det-gpio"])[1:3], (0, 11), rpm)
            self.assertIn(0, banks, rpm)


if __name__ == "__main__":
    unittest.main()
