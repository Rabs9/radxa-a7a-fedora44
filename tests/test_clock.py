"""tools/board/a7a-clock against a fake board: the script's /sys, /proc/device-tree, /boot/extlinux, /etc, /home and
module paths are rewritten to a temporary tree; sudo, tee (standing in for the kernel's answer to a sysfs write),
nmcli, lsmod, modinfo, uname, fdtput, chattr and glmark2 are stubs.

What these cover was found the hard way on 2026-11-16, most of it by a reviewer reading for controls that cannot
work: memory rates that are not round numbers of MHz (`a7a-clock ram 854` and the panel's buttons answered "not
available"); a memory limit left by `profile eco` that silently clamped the next request; the NPU, whose cur_freq is
its module clock and never moves; `a7a-clock gpu abc` writing a clock of 0 into the default boot entry; DSU modules
built for another kernel; a WiFi band switch that left a board off the network; and `a7a-clock check`, the read-only
answer to "does this image run at the clocks it promises"."""

import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
CLOCK = P / "tools/board/a7a-clock"
RELEASE = "6.6.98-test"

TEE = r'''#!/bin/bash
# the kernel's side of a sysfs write: devfreq clamps a request to max_freq, a "stuck" device ignores it, and the
# NPU's cur_freq (its module clock) does not follow its target
mode=w; [ "$1" = -a ] && { mode=a; shift; }
f=$1; v=$(cat)
if [ $mode = a ]; then printf '%s\n' "$v" >> "$f"; else printf '%s\n' "$v" > "$f"; fi
case "$f" in
  */userspace/set_freq)
    d=${f%/userspace/set_freq}
    [ -e "$d/refuse" ] && exit 1        # the memory driver's -ETIMEDOUT: the write itself fails
    [ -e "$d/stuck" ] && exit 0
    m=$(cat "$d/max_freq" 2>/dev/null || echo 0)
    [ "$m" -gt 0 ] && [ "$v" -gt "$m" ] && v=$m
    echo "$v" > "$d/target_freq"
    case "$d" in *npu) ;; *) echo "$v" > "$d/cur_freq" ;; esac ;;
esac
exit 0
'''

NMCLI = r'''#!/bin/bash
# NetworkManager with one or more saved profiles, kept in $NM_STATE/<uuid>/{name,type,band,active}; a band listed
# in $NM_STATE/no-signal cannot be connected on
S=$NM_STATE; echo "nmcli $*" >> "$S/calls"
args=(); for a in "$@"; do case "$a" in -t|-w|-e) ;; 30|no) ;; *) args+=("$a") ;; esac; done
set -- "${args[@]}"
reach() { ! grep -qxF "$(cat "$S/$1/band")" "$S/no-signal" 2>/dev/null; }
case "$*" in
  "-f UUID,TYPE con show") for d in "$S"/*/; do u=$(basename "$d"); echo "$u:$(cat "$d/type")"; done ;;
  "-f UUID con show --active") for d in "$S"/*/; do [ "$(cat "$d/active")" = 1 ] && basename "$d"; done; true ;;
  "-g 802-11-wireless.band con show "*) cat "$S/${*: -1}/band" ;;
  "-g connection.id con show "*) cat "$S/${*: -1}/name" ;;
  "con modify "*) echo "${5-}" > "$S/$3/band" ;;
  "con up "*) u=$3; if reach "$u"; then echo 1 > "$S/$u/active"; else echo 0 > "$S/$u/active"; exit 4; fi ;;
  "-f DEVICE,TYPE,STATE dev status") st=disconnected; for d in "$S"/*/; do [ "$(cat "$d/active")" = 1 ] && st=connected; done; echo "wlan0:wifi:$st" ;;
  "-g GENERAL.AUTOCONNECT device show wlan0") [ -e "$S/user-disconnected" ] && echo no || echo yes ;;
  "device connect wlan0") for d in "$S"/*/; do u=$(basename "$d"); reach "$u" && { echo 1 > "$d/active"; exit 0; }; done; exit 4 ;;
  *) echo "nmcli stub: unexpected call: $*" >&2; exit 99 ;;
esac
'''


class FakeBoard:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a7a-clock-test."))
        self.root = self.tmp / "root"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.nm = self.tmp / "nm"
        self.nm.mkdir()
        self.cpu = self.root / "sys/devices/system/cpu/cpufreq"
        for pol, top, cap, opps in (("policy0", 2800000, 2800000, "416000 1196000 2800000"),
                                    ("policy6", 3200000, 3000000, "408000 1512000 3000000 3200000")):
            self.put(self.cpu / pol / "cpuinfo_max_freq", top)
            self.put(self.cpu / pol / "scaling_max_freq", cap)
            self.put(self.cpu / pol / "scaling_cur_freq", cap)
            self.put(self.cpu / pol / "scaling_governor", "performance")
            self.put(self.cpu / pol / "scaling_available_governors", "performance schedutil ")
            self.put(self.cpu / pol / "scaling_available_frequencies", opps)
        self.dmc = self.root / "sys/class/devfreq/a020000.dmcfreq"
        self.npu = self.root / "sys/class/devfreq/3600000.npu"
        for node, rates in ((self.dmc, "427200000 854400000 1220571428 2136000000"),
                            (self.npu, "492000000 852000000 1008000000")):
            top = rates.split()[-1]
            self.put(node / "available_frequencies", rates)
            self.put(node / "cur_freq", top)
            self.put(node / "target_freq", top)
            self.put(node / "max_freq", top)
            self.put(node / "governor", "performance")
            (node / "userspace").mkdir()
        self.clk(gpu=1008000000, dsu=780000000)
        self.put(self.root / "sys/class/regulator/regulator.5/name", "axp8191-dcdc4")
        self.put(self.root / "sys/class/regulator/regulator.5/microvolts", 800000)
        dt = self.root / "proc/device-tree/soc@3000000/gpu@1800000"
        dt.mkdir(parents=True)
        (dt / "clk_rate").write_bytes(struct.pack(">I", 1008000000))
        self.variant("oc", 2136)
        self.ext = self.root / "boot/extlinux/extlinux.conf"
        self.put(self.ext, f"default fedora-{RELEASE}\nlabel fedora-{RELEASE}\n\tlinux /boot/vmlinuz-{RELEASE}\n"
                           f"\tfdtdir /usr/lib/linux-image-{RELEASE}/\n\tappend root=/dev/mmcblk0p3")
        self.put(self.root / f"usr/lib/linux-image-{RELEASE}/allwinner/sun60i-a733-cubie-a7a.dtb", f"tree of {RELEASE}")
        for m in ("dsu_oc", "dsu_dyn"):
            self.put(self.root / f"usr/local/lib/a7a/dsu/{RELEASE}/{m}.ko", "")
        self.stub("sudo", '[ "$1" = -n ] && shift; exec "$@"')
        self.stub("tee", TEE, raw=True)
        self.stub("nmcli", NMCLI, raw=True)
        self.stub("lsmod", 'echo "Module Size Used by"')
        self.stub("modinfo", f'echo "{RELEASE} SMP preempt mod_unload aarch64"')
        self.stub("uname", f'[ "$1" = -r ] && echo {RELEASE} || /usr/bin/uname "$@"')
        for name in ("fdtput", "chattr", "sync", "insmod", "rmmod", "glmark2-es2-wayland"):
            self.stub(name, f'echo "{name} $*" >> "{self.tmp}/calls"; exit 0')
        text = CLOCK.read_text()
        for path in ("/sys/", "/proc/device-tree", "/proc/asound/", "/etc/a7a-image-variant", "/etc/a7a/", "/boot/extlinux",
                     "/home/radxa/", "/var/lib/a7a/", "/usr/lib/linux-image-", "/usr/lib/modules/", "/usr/local/lib/a7a/",
                     "/usr/local/bin/a7a-gpu-stat", "/usr/local/bin/a7a-ac101-dtb"):
            text = text.replace(path, f"{self.root}{path}")
        self.script = self.tmp / "a7a-clock"
        self.script.write_text(text)
        # the GPU state reader a7a-clock json runs, reading the fake board's debugfs
        stat = self.root / "usr/local/bin/a7a-gpu-stat"
        stat.parent.mkdir(parents=True, exist_ok=True)
        stat.write_text((P / "tools/board/a7a-gpu-stat").read_text().replace("/sys/", f"{self.root}/sys/"))
        stat.chmod(0o755)

    def put(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{value}\n" if value != "" else "")

    def stub(self, name, body, raw=False):
        f = self.bin / name
        f.write_text(body if raw else f"#!/bin/bash\n{body}\n")
        f.chmod(0o755)

    def clk(self, gpu, dsu):
        self.put(self.root / "sys/kernel/debug/clk/clk_summary",
                 f"    pll-gpu   1 1 0 {gpu} 0 0 50000 Y\n       gpu0   1 1 0 {gpu} 0 0 50000 Y\n"
                 f"    cpu_dsu   1 1 0 {dsu} 0 0 50000 Y")

    def variant(self, name, ram):
        self.put(self.root / "etc/a7a-image-variant",
                 f"variant={name}\ncpu=A76 3000 MHz, A55 2800 MHz\nram={ram} MHz\ngpu=1008 MHz\nethernet=RGMII tx-delay 9")

    def wifi(self, uuid, name, band="", active=1):
        for key, value in (("name", name), ("type", "802-11-wireless"), ("band", band), ("active", active)):
            self.put(self.nm / uuid / key, value)

    def read(self, path):
        return Path(path).read_text().strip()

    def calls(self):
        f = self.tmp / "calls"
        return f.read_text() if f.exists() else ""

    def run(self, *args):
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", NM_STATE=str(self.nm))
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=env)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.b = FakeBoard()
        self.addCleanup(self.b.close)


class RatesTest(Base):
    def test_memory_rates_are_asked_for_in_mhz_and_set_in_the_devices_hz(self):
        for mhz, hz in ((427, "427200000"), (854, "854400000"), (1220, "1220571428"), (1221, "1220571428"),
                        (2136, "2136000000")):
            with self.subTest(mhz=mhz):
                r = self.b.run("ram", str(mhz))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertEqual(self.b.read(self.b.dmc / "cur_freq"), hz)
                self.assertEqual(self.b.read(self.b.dmc / "governor"), "userspace")
                self.assertIn(f"set to {int(hz) // 1000000} MHz", r.stdout)

    def test_a_rate_the_device_does_not_offer_is_refused_and_nothing_is_written(self):
        for bad in ("2000", "abc", ""):
            with self.subTest(arg=bad):
                r = self.b.run("ram", bad)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("427 854 1220 2136", r.stdout)
                self.assertFalse((self.b.dmc / "userspace/set_freq").exists())
                self.assertEqual(self.b.read(self.b.dmc / "governor"), "performance")

    def test_the_limit_profile_eco_leaves_does_not_clamp_the_next_request(self):
        self.b.put(self.b.dmc / "max_freq", 854400000)
        self.b.put(self.b.dmc / "cur_freq", 854400000)
        r = self.b.run("ram", "2136")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.dmc / "cur_freq"), "2136000000")
        self.assertEqual(self.b.read(self.b.dmc / "max_freq"), "2136000000")

    def test_a_device_that_does_not_follow_is_reported_and_fails(self):
        (self.b.dmc / "stuck").write_text("")
        r = self.b.run("ram", "854")
        self.assertEqual(r.returncode, 1)
        self.assertIn("asked for 854 MHz, the device is at 2136 MHz", r.stdout)

    def test_a_rate_the_hardware_refuses_is_a_write_error_and_is_reported(self):
        (self.b.dmc / "refuse").write_text("")
        r = self.b.run("ram", "854")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the device refused 854 MHz (the write failed); it is at 2136 MHz", r.stdout)

    def test_the_npu_is_read_at_its_target_because_its_cur_freq_is_the_module_clock(self):
        r = self.b.run("npu", "492")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("set to 492 MHz", r.stdout)
        self.assertEqual(self.b.read(self.b.npu / "cur_freq"), "1008000000")     # the fake's module clock stays
        self.assertIn("-- NPU: 492 MHz  (available: 492 852 1008)", self.b.run("status").stdout)

    def test_status_shows_memory_rates_in_mhz(self):
        self.assertIn("-- RAM (DMC): 2136 MHz  (available: 427 854 1220 2136)", self.b.run("status").stdout)

    def test_cpu_governor_must_be_one_the_kernel_has(self):
        for bad in ("", "turbo"):
            with self.subTest(arg=bad):
                r = self.b.run("cpu", bad) if bad else self.b.run("cpu")
                self.assertEqual(r.returncode, 1)
                self.assertIn("usage: a7a-clock cpu <performance|schedutil>", r.stdout)
                self.assertEqual(self.b.read(self.b.cpu / "policy6/scaling_governor"), "performance")
        self.assertEqual(self.b.run("cpu", "schedutil").returncode, 0)
        self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_governor"), "schedutil")

    def test_a_governor_is_a_word_not_a_pattern(self):
        for bad in (".*", "performance schedutil", "perf"):
            with self.subTest(arg=bad):
                self.assertEqual(self.b.run("cpu", bad).returncode, 1)
                self.assertEqual(self.b.read(self.b.cpu / "policy6/scaling_governor"), "performance")

    def test_cpu_caps_are_numbers_or_max(self):
        r = self.b.run("cpumax", "01196", "max")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_max_freq"), "1196000")
        self.assertEqual(self.b.read(self.b.cpu / "policy6/scaling_max_freq"), "3000000")
        for bad in (("abc", "1512"), ("1196",), ("1196", "1e3")):
            with self.subTest(args=bad):
                r = self.b.run("cpumax", *bad)
                self.assertEqual(r.returncode, 1)
                self.assertIn("usage: a7a-clock cpumax", r.stdout)

    def test_a_soak_needs_minutes_before_it_can_say_stable(self):
        for bad in ("0", "abc", "-1"):
            with self.subTest(arg=bad):
                r = self.b.run("soak", bad)
                self.assertEqual(r.returncode, 1)
                self.assertIn("usage: a7a-clock soak", r.stdout)
                self.assertNotIn("STABLE", r.stdout)


class GpuEntryTest(Base):
    def digest(self):
        return hashlib.sha256(self.b.ext.read_bytes()).hexdigest()

    def test_what_is_not_a_clock_or_a_voltage_changes_nothing(self):
        before = self.digest()
        for args in (("abc",), ("0",), ("500",), ("700",), ("1500",), ("1008", "99"), ("1008", "5000"), ("1008", "x")):
            with self.subTest(args=args):
                r = self.b.run("gpu", *args)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(self.digest(), before)
                self.assertEqual(list((self.b.root / "usr/lib").glob("linux-image-gpu*")), [])

    def test_a_valid_clock_becomes_an_entry_of_the_default_entrys_kernel(self):
        r = self.b.run("gpu", "840")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = self.b.ext.read_text()
        self.assertTrue(text.startswith("default g_840_800\n"), text)
        self.assertIn(f"label g_840_800\n\tmenu label Fedora, GPU 840 MHz @ 800 mV\n\tlinux /boot/vmlinuz-{RELEASE}\n", text)
        self.assertIn("append root=/dev/mmcblk0p3", text.split("label g_840_800")[1])
        self.assertIn("clk_rate 840000000", self.b.calls())
        self.assertIn("regulator-min-microvolt 800000", self.b.calls())

    def test_a_clock_between_two_validated_pairs_gets_the_voltage_of_the_higher_pair(self):
        for clk, mv in ((840, 800), (1104, 920), (1250, 1040), (1399, 1150)):
            with self.subTest(clk=clk):
                r = self.b.run("gpu", str(clk))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"label g_{clk}_{mv}\n", self.b.ext.read_text())
                self.assertIn(f"regulator-min-microvolt {mv * 1000}", self.b.calls())

    def test_numbers_are_decimal_whatever_their_leading_zeros(self):
        r = self.b.run("gpu", "01300", "01040")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("label g_1300_1040\n", self.b.ext.read_text())
        self.assertIn("clk_rate 1300000000", self.b.calls())
        self.assertEqual(self.b.run("gpu", "0900").returncode, 0)
        self.assertIn("label g_900_800\n", self.b.ext.read_text())

    def test_a_second_entry_is_made_from_the_tree_the_first_one_was_made_from(self):
        # a system whose own tree is the "-standard" one: the default entry names it
        std = self.b.root / f"usr/lib/linux-image-{RELEASE}-standard"
        self.b.put(std / "allwinner/sun60i-a733-cubie-a7a.dtb", "the standard tree")
        self.b.put(self.b.ext, f"default fedora-{RELEASE}\nlabel fedora-{RELEASE}\n\tlinux /boot/vmlinuz-{RELEASE}\n"
                               f"\tfdtdir {std}/\n\tappend root=/dev/mmcblk0p3")
        for clk in ("840", "864"):      # the second one starts from a default that is the first g_* entry
            r = self.b.run("gpu", clk)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            copied = self.b.root / f"usr/lib/linux-image-gpu{clk}v800/allwinner/sun60i-a733-cubie-a7a.dtb"
            self.assertEqual(copied.read_text().strip(), "the standard tree")
            self.assertEqual((self.b.root / f"usr/lib/linux-image-gpu{clk}v800/base").read_text().strip(), f"{std}/")

    def test_the_base_tree_is_the_default_entrys_kernels_not_the_running_kernels(self):
        # the other flavour booted from the menu: there is no tree for it here, so using it would fail
        self.b.stub("uname", '[ "$1" = -r ] && echo 6.6.98-other || /usr/bin/uname "$@"')
        r = self.b.run("gpu", "840")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        copied = self.b.root / "usr/lib/linux-image-gpu840v800/allwinner/sun60i-a733-cubie-a7a.dtb"
        self.assertEqual(copied.read_text().strip(), f"tree of {RELEASE}")


class DsuTest(Base):
    def test_the_module_of_the_running_kernel_is_loaded_from_the_per_release_place(self):
        r = self.b.run("dsu", "1352")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"insmod {self.b.root}/usr/local/lib/a7a/dsu/{RELEASE}/dsu_oc.ko freq=1352000000 uv=1000000", self.b.calls())

    def test_a_module_for_another_kernel_is_refused_before_a_profile_changes_anything(self):
        self.b.stub("modinfo", 'echo "6.6.98+ SMP preempt mod_unload aarch64"')
        for args in (("profile", "eco"), ("profile", "oc"), ("dsu", "dyn"), ("dsu", "1352")):
            with self.subTest(args=args):
                r = self.b.run(*args)
                self.assertEqual(r.returncode, 1)
                self.assertIn(f"is built for kernel '6.6.98+', this one is {RELEASE}: it cannot load", r.stdout)
                self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_max_freq"), "2800000")
                self.assertEqual(self.b.read(self.b.dmc / "max_freq"), "2136000000")
                self.assertNotIn("insmod", self.b.calls())
                self.assertNotIn("rmmod", self.b.calls())

    def test_a_dsu_clock_the_table_does_not_reach_is_refused_before_anything_is_unloaded(self):
        for bad in ("0", "99999", "08", "1353", "779"):
            with self.subTest(arg=bad):
                r = self.b.run("dsu", bad)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("from 780 (stock) to 1352", r.stdout)
                self.assertNotIn("insmod", self.b.calls())
                self.assertNotIn("rmmod", self.b.calls())
        self.assertEqual(self.b.run("dsu", "01274").returncode, 0)
        self.assertIn("dsu_oc.ko freq=1274000000 uv=1000000", self.b.calls())

    def test_profile_eco_caps_the_clocks_and_loads_the_dynamic_module(self):
        r = self.b.run("profile", "eco")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_max_freq"), "1196000")
        self.assertEqual(self.b.read(self.b.cpu / "policy6/scaling_max_freq"), "1512000")
        self.assertEqual(self.b.read(self.b.dmc / "max_freq"), "854400000")
        self.assertIn("dsu_dyn.ko", self.b.calls())

    def test_profile_oc_asks_for_the_validated_voltage(self):
        self.assertEqual(self.b.run("profile", "oc").returncode, 0)
        self.assertIn("dsu_oc.ko freq=1352000000 uv=1000000", self.b.calls())

    def test_status_names_the_rung_of_the_dynamic_module(self):
        self.b.stub("lsmod", 'echo "Module Size Used by"; echo "dsu_dyn 20480 0"')
        f = self.b.root / "sys/module/dsu_dyn/parameters/oc_hz"
        for value, shown in ((1352000000, "(module: dsu_dyn + 1352 MHz rung)"), (0, "(module: dsu_dyn)"),
                             (None, "(module: dsu_dyn)"), ("", "(module: dsu_dyn)")):
            with self.subTest(oc_hz=value):
                f.unlink(missing_ok=True)
                if value is not None:
                    self.b.put(f, value)
                r = self.b.run("status")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(shown, r.stdout)
                self.assertIn("-- USB stick", r.stdout)      # status went on past the L3 line
        # the module gone between lsmod and the read of its parameter: no rung without the module
        self.b.stub("lsmod", 'echo "Module Size Used by"')
        self.b.put(f, 1352000000)
        r = self.b.run("status")
        self.assertIn("(module: none -> stock 780)", r.stdout)
        self.assertNotIn("rung", r.stdout)


class PowerTest(Base):
    """`a7a-clock power`: the three positions of KDE's power slider (tuned runs it)."""

    def test_each_position_sets_the_governor_the_cpu_caps_and_the_memory_ceiling(self):
        for mode, gov, little, big, ram in (("power-saver", "schedutil", "1196000", "1512000", "854400000"),
                                            ("balanced", "schedutil", "2800000", "3000000", "2136000000"),
                                            ("performance", "performance", "2800000", "3000000", "2136000000")):
            with self.subTest(mode=mode):
                # values no position sets: a step that does nothing cannot pass on what the one before left
                for pol in ("policy0", "policy6"):
                    self.b.put(self.b.cpu / pol / "scaling_governor", "ondemand")
                    self.b.put(self.b.cpu / pol / "scaling_max_freq", 1)
                self.b.put(self.b.dmc / "max_freq", 1)
                r = self.b.run("power", mode)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                for pol in ("policy0", "policy6"):
                    self.assertEqual(self.b.read(self.b.cpu / pol / "scaling_governor"), gov)
                self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_max_freq"), little)
                self.assertEqual(self.b.read(self.b.cpu / "policy6/scaling_max_freq"), big)
                self.assertEqual(self.b.read(self.b.dmc / "max_freq"), ram)

    def test_a_change_of_profile_takes_the_l3_off_its_over_bin_rung(self):
        # on the board 2026-11-17: the dynoc preset, then the slider's Balanced, left the L3 with its 1352 MHz rung
        for loaded, params in (("dsu_dyn", {"oc_hz": 1352000000}), ("dsu_oc", {"freq": 1352000000})):
            with self.subTest(loaded=loaded):
                (self.b.tmp / "calls").unlink(missing_ok=True)
                self.b.stub("lsmod", f'echo "Module Size Used by"; echo "{loaded} 20480 0"')
                for k, v in params.items():
                    self.b.put(self.b.root / f"sys/module/{loaded}/parameters/{k}", v)
                r = self.b.run("power", "balanced")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"rmmod {loaded}", self.b.calls())
                self.assertIn(f"insmod {self.b.root}/usr/local/lib/a7a/dsu/{RELEASE}/dsu_dyn.ko\n", self.b.calls())

    def test_a_plain_dynamic_l3_module_stays_loaded(self):
        self.b.stub("lsmod", 'echo "Module Size Used by"; echo "dsu_dyn 20480 0"')
        for oc_hz in (0, None):    # None: a dsu_dyn built before the parameter existed
            with self.subTest(oc_hz=oc_hz):
                (self.b.tmp / "calls").unlink(missing_ok=True)
                f = self.b.root / "sys/module/dsu_dyn/parameters/oc_hz"
                f.unlink(missing_ok=True)
                if oc_hz is not None:
                    self.b.put(f, oc_hz)
                r = self.b.run("power", "power-saver")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("rmmod", self.b.calls())
                self.assertNotIn("insmod", self.b.calls())

    def test_both_l3_modules_at_once_are_replaced_by_the_plain_dynamic_one(self):
        self.b.stub("lsmod", 'echo "Module Size Used by"; echo "dsu_dyn 20480 0"; echo "dsu_oc 16384 0"')
        self.b.put(self.b.root / "sys/module/dsu_dyn/parameters/oc_hz", 0)
        r = self.b.run("power", "balanced")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("rmmod dsu_dyn", self.b.calls())
        self.assertIn("rmmod dsu_oc", self.b.calls())
        self.assertIn(f"insmod {self.b.root}/usr/local/lib/a7a/dsu/{RELEASE}/dsu_dyn.ko\n", self.b.calls())

    def test_a_module_for_another_kernel_leaves_the_l3_stock_and_the_profile_still_switches(self):
        # unlike `profile` and `dsu` (DsuTest): tuned runs this, and the slider must still move the CPU and memory
        self.b.stub("modinfo", 'echo "6.6.98+ SMP preempt mod_unload aarch64"')
        self.b.stub("lsmod", 'echo "Module Size Used by"; echo "dsu_dyn 20480 0"')
        r = self.b.run("power", "power-saver")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.cpu / "policy0/scaling_max_freq"), "1196000")
        self.assertIn("rmmod dsu_dyn", self.b.calls())
        self.assertNotIn("insmod", self.b.calls())


class WifiTest(Base):
    U = "11111111-2222-3333-4444-555555555555"

    def test_a_band_that_connects_is_kept(self):
        self.b.wifi(self.U, "home")
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.nm / self.U / "band"), "a")
        self.assertEqual(self.b.read(self.b.nm / self.U / "active"), "1")
        self.assertIn("home: band -> 5", r.stdout)

    def test_a_band_without_signal_is_put_back_and_the_board_stays_on_the_network(self):
        self.b.wifi(self.U, "home", band="")
        (self.b.nm / "no-signal").write_text("bg\n")
        r = self.b.run("wifi", "2.4")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("no connection on 2.4: band put back, reconnected", r.stdout)
        self.assertEqual(self.b.read(self.b.nm / self.U / "band"), "")
        self.assertEqual(self.b.read(self.b.nm / self.U / "active"), "1")

    def test_a_profile_name_is_free_text(self):
        self.b.wifi(self.U, "Wi-Fi [5G]: up*stairs")
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Wi-Fi [5G]: up*stairs: band -> 5", r.stdout)
        self.assertIn(f"nmcli -w 30 con up {self.U}", (self.b.nm / "calls").read_text())     # it was seen as active

    def test_a_wifi_the_user_switched_off_is_not_connected_by_a_band_change(self):
        self.b.wifi(self.U, "home", active=0)
        (self.b.nm / "user-disconnected").write_text("")
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.b.read(self.b.nm / self.U / "band"), "a")
        self.assertEqual(self.b.read(self.b.nm / self.U / "active"), "0")
        self.assertNotIn("device connect", (self.b.nm / "calls").read_text())

    def test_a_wifi_that_dropped_is_connected_on_the_band_chosen(self):
        # 2026-11-17: the board was off its WiFi, the panel's 5 GHz button changed the band and nothing else
        self.b.wifi(self.U, "home", active=0)
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("connected on 5", r.stdout)
        self.assertEqual(self.b.read(self.b.nm / self.U / "band"), "a")
        self.assertEqual(self.b.read(self.b.nm / self.U / "active"), "1")

    def test_a_wifi_that_dropped_and_cannot_connect_on_the_band_gets_its_band_back(self):
        self.b.wifi(self.U, "home", band="", active=0)
        (self.b.nm / "no-signal").write_text("a\n")
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("no connection on 5: band put back, connected", r.stdout)
        self.assertEqual(self.b.read(self.b.nm / self.U / "band"), "")
        self.assertEqual(self.b.read(self.b.nm / self.U / "active"), "1")

    def test_without_a_saved_profile_there_is_nothing_to_lock(self):
        r = self.b.run("wifi", "5")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no saved wifi connection yet", r.stdout)


class JsonTest(Base):
    # a7a-panel asks this for what only root can read (a7a-gpu-stat: the PowerVR driver's debugfs), every 2 s while
    # it is open. The status file as the DDK writes it on the board (2026-11-17), with a load and a recovery added.
    STATUS = ("Driver Status:   OK\n\nDevice ID: 0:128\nFirmware Status: OK\nServer Errors:   0\nHWR Event Count: 3\n"
              "CRR Event Count: 0\nAPM Event Count: 2084\nGPU Utilisation: 37%\nDM Utilisation:  VM0\n           2D:   0% \n"
              "         GEOM:   12% \n           3D:   35% \n          CDM:   4% \n          RAY:   0% \n        GEOM2:   0% ")
    STATS = "MemoryUsageKMalloc                     1623184\nMemoryUsageAllocGPUMemUMA            346980352\nMemoryUsageAllocGPUMemUMAMax         374538240"

    def test_json_gives_the_gpu_state(self):
        self.b.put(self.b.root / "sys/kernel/debug/pvr/status", self.STATUS)
        self.b.put(self.b.root / "sys/kernel/debug/pvr/driver_stats", self.STATS)
        r = self.b.run("json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(dict(l.split("=") for l in r.stdout.split()),
                         {"hwr": "3", "gpu_load": "37", "gpu_geom": "12", "gpu_3d": "35", "gpu_cdm": "4", "gpu_mem_mb": "330"})

    def test_json_without_the_memory_statistics_still_gives_the_load(self):
        self.b.put(self.b.root / "sys/kernel/debug/pvr/status", self.STATUS)
        r = self.b.run("json")
        self.assertIn("gpu_load=37", r.stdout)
        self.assertNotIn("gpu_mem_mb", r.stdout)

    def test_json_without_the_gpu_driver_says_nothing(self):
        r = self.b.run("json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


class CheckTest(Base):
    def test_check_passes_on_the_image_as_promised(self):
        r = self.b.run("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("== summary: 0 fail, 0 warn", r.stdout)
        # the big cluster's table lists 3.2 GHz, the PLL gives 3.0: 3000 is what counts
        self.assertIn("A76 (big): operating points up to 3000 MHz", r.stdout)
        self.assertIn("GPU at 1008 MHz @ 800 mV", r.stdout)
        self.assertIn("memory controller: runs at 2136 MHz", r.stdout)
        self.assertIn("dsu_oc.ko is built for this kernel", r.stdout)

    def test_check_names_the_wrong_boot_firmware_for_the_variant(self):
        self.b.variant("standard", 2040)
        r = self.b.run("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  memory controller: the boot firmware offers 2136 MHz at most, the image says 2040", r.stdout)

    def test_check_fails_when_the_gpu_is_not_at_its_boot_entrys_clock(self):
        self.b.clk(gpu=600000000, dsu=780000000)
        r = self.b.run("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL  GPU at 600 MHz", r.stdout)

    def test_check_allows_the_pll_its_step(self):
        (self.b.root / "proc/device-tree/soc@3000000/gpu@1800000/clk_rate").write_bytes(struct.pack(">I", 1300000000))
        self.b.clk(gpu=1296000000, dsu=780000000)
        self.b.put(self.b.ext, self.b.ext.read_text().replace(f"default fedora-{RELEASE}", "default g_1300_1040"))
        r = self.b.run("check")
        self.assertIn("PASS  GPU at 1296 MHz", r.stdout)

    def test_check_warns_about_caps_and_a_lowered_memory_clock_without_failing(self):
        self.b.put(self.b.cpu / "policy0/scaling_max_freq", 1800000)
        self.b.put(self.b.dmc / "cur_freq", 854400000)
        r = self.b.run("check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("WARN  A55 (little): capped at 1800 MHz", r.stdout)
        self.assertIn("WARN  memory controller: at 854 MHz of 2136", r.stdout)

    def test_check_fails_on_dsu_modules_built_for_another_kernel(self):
        self.b.stub("modinfo", 'echo "6.6.98+ SMP preempt mod_unload aarch64"')
        r = self.b.run("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("dsu_oc.ko is built for kernel '6.6.98+'", r.stdout)
        self.assertIn("the panel's DSU buttons cannot work", r.stdout)

    def test_check_fails_when_there_is_no_module_for_this_kernel(self):
        shutil.rmtree(self.b.root / "usr/local/lib/a7a")
        r = self.b.run("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"DSU module missing: {self.b.root}/usr/local/lib/a7a/dsu/{RELEASE}/dsu_oc.ko", r.stdout)

    def test_check_without_the_variant_file_compares_with_the_hardware(self):
        (self.b.root / "etc/a7a-image-variant").unlink()
        r = self.b.run("check")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("not a published image", r.stdout)


@unittest.skipUnless(shutil.which("dtc") and shutil.which("fdtput") and shutil.which("fdtget"), "dtc, fdtput and fdtget needed")
class CodecTest(Base):
    """`a7a-clock codec`: the 3.5 mm jack of a board built with the AC101 (2026-11-18), with the real a7a-ac101-dtb and
    fdtput over device trees compiled here in the kernel's shape (tests/test_ac101_dtb.py's), one of the kernel package
    and one copy `a7a-clock gpu` made. The review of that day found writes that failed unnoticed, a GPU copy that
    `ac101b` left with the AC101, and no guard on a board built with the AC101B: each has a test here."""
    CARDS_AC101 = " 0 [allwinnerac101 ]: allwinner-ac101 - allwinner-ac101\n                      allwinner-ac101\n" \
                  " 1 [allwinnerhdmi  ]: allwinner-hdmi - allwinner-hdmi\n                      allwinner-hdmi\n"
    CARDS_AC101B = " 0 [sunxiac101b    ]: sunxi-ac101b - sunxi-ac101b\n                      sunxi-ac101b\n" \
                   " 1 [allwinnerhdmi  ]: allwinner-hdmi - allwinner-hdmi\n                      allwinner-hdmi\n"
    CARDS_HDMI = " 0 [allwinnerhdmi  ]: allwinner-hdmi - allwinner-hdmi\n                      allwinner-hdmi\n"

    def setUp(self):
        super().setUp()
        loader = importlib.machinery.SourceFileLoader("test_ac101_dtb_trees", str(P / "tests/test_ac101_dtb.py"))
        trees = importlib.util.module_from_spec(importlib.util.spec_from_loader("test_ac101_dtb_trees", loader))
        loader.exec_module(trees)
        self.tree_src, self.kernel_values = trees.TREE, trees.KERNEL
        (self.b.bin / "fdtput").unlink()                      # the real one: the tool writes real trees
        tool = self.b.root / "usr/local/bin/a7a-ac101-dtb"
        tool.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(P / "tools/board/a7a-ac101-dtb", tool)
        for name in ("amixer", "systemctl"):
            self.b.stub(name, f'echo "{name} $*" >> "{self.b.tmp}/calls"; exit 0')
        for name in ("dmesg", "journalctl"):                  # this boot's kernel log, as the fake board has it
            self.b.stub(name, f'cat "{self.b.tmp}/klog" 2>/dev/null; exit 0')
        self.package = self.dtb(f"linux-image-{RELEASE}")
        self.gpu_copy = self.dtb("linux-image-gpu1008v800")
        self.original = {p: p.read_bytes() for p in (self.package, self.gpu_copy)}
        self.cards(self.CARDS_HDMI)

    def dtb(self, directory, **values):
        out = self.b.root / "usr/lib" / directory / "allwinner/sun60i-a733-cubie-a7a.dtb"
        out.parent.mkdir(parents=True, exist_ok=True)
        src = self.b.tmp / f"{directory}.dts"
        src.write_text(self.tree_src % dict(self.kernel_values, **values))
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(out), str(src)], check=True, capture_output=True)
        return out

    def cards(self, text):
        self.b.put(self.b.root / "proc/asound/cards", text.rstrip("\n"))

    def running(self, codec):
        """The running device tree: the AC101 switched on, or not there."""
        node = self.b.root / "proc/device-tree/soc@3000000/twi@7085000/ac101@1a"
        if codec == "AC101":
            node.mkdir(parents=True, exist_ok=True)
            (node / "status").write_bytes(b"okay\0")
        elif node.exists():
            shutil.rmtree(node)

    def has_ac101(self, path):
        r = subprocess.run(["fdtget", str(path), "/soc@3000000/twi@7085000/ac101@1a", "status"], capture_output=True, text=True)
        return r.returncode == 0 and r.stdout.strip() == "okay"

    def kept(self, path):
        return self.b.root / "var/lib/a7a/codec" / (path.parent.parent.name + ".dtb")

    def test_ac101_gives_every_tree_the_ac101_and_keeps_each_as_it_was(self):
        r = self.b.run("codec", "ac101")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for tree in (self.package, self.gpu_copy):
            self.assertTrue(self.has_ac101(tree), tree)
            self.assertEqual(self.kept(tree).read_bytes(), self.original[tree], tree)
            self.assertFalse(Path(str(tree) + ".a7a-new").exists(), tree)        # written beside, renamed over
        self.assertEqual(self.b.read(self.b.root / "etc/a7a/codec"), "ac101")
        self.assertIn("systemctl enable a7a-codec.service", self.b.calls())
        self.assertIn("REBOOT", r.stdout)
        # a second run changes nothing, and what is kept stays the tree as the package had it
        converted = self.package.read_bytes()
        r = self.b.run("codec", "ac101")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.package.read_bytes(), converted)
        self.assertEqual(self.kept(self.package).read_bytes(), self.original[self.package])

    def test_ac101b_puts_every_tree_back(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        r = self.b.run("codec", "ac101b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for tree in (self.package, self.gpu_copy):
            self.assertEqual(tree.read_bytes(), self.original[tree], tree)
            self.assertFalse(self.kept(tree).exists(), tree)
        self.assertFalse((self.b.root / "etc/a7a/codec").exists())
        self.assertIn("systemctl disable a7a-codec.service", self.b.calls())

    def test_a_gpu_copy_made_after_the_switch_is_put_back_too(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        late = self.b.root / "usr/lib/linux-image-gpu1200v920/allwinner/sun60i-a733-cubie-a7a.dtb"
        late.parent.mkdir(parents=True)
        shutil.copy(self.package, late)                       # what `gpu 1200` copies now: a tree with the AC101
        r = self.b.run("codec", "ac101b")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.has_ac101(late))
        self.assertIn("nothing was kept for it", r.stdout)
        st = subprocess.run(["fdtget", str(late), "/soc@3000000/twi@7085000/ac101b@3e", "status"], capture_output=True, text=True)
        self.assertEqual(st.stdout.strip(), "okay")

    def test_a_tree_the_tool_does_not_know_leaves_every_tree_as_it_was(self):
        self.dtb("linux-image-gpu1008v800", irq_bank="2")           # its AC101B interrupt is not PB2
        unknown = self.gpu_copy.read_bytes()
        r = self.b.run("codec", "ac101")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.package.read_bytes(), self.original[self.package])
        self.assertEqual(self.gpu_copy.read_bytes(), unknown)
        self.assertFalse((self.b.root / "etc/a7a/codec").exists())
        self.assertFalse(self.kept(self.package).exists())

    def test_nothing_is_kept_nothing_changes(self):
        keep = self.b.root / "var/lib/a7a/codec"
        keep.parent.mkdir(parents=True)
        keep.write_text("not a directory")                    # the place of the kept copies cannot be made
        r = self.b.run("codec", "ac101")
        self.assertEqual(r.returncode, 1, r.stdout)
        for tree in (self.package, self.gpu_copy):
            self.assertEqual(tree.read_bytes(), self.original[tree], tree)
        self.assertFalse((self.b.root / "etc/a7a/codec").exists())

    def test_a_tree_that_cannot_be_put_back_keeps_the_choice(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        self.kept(self.package).chmod(0)                      # its kept copy cannot be read
        try:
            r = self.b.run("codec", "ac101b")
        finally:
            self.kept(self.package).chmod(0o644)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertTrue(self.has_ac101(self.package))
        self.assertTrue(self.kept(self.package).exists())                     # still there to try again
        self.assertEqual(self.b.read(self.b.root / "etc/a7a/codec"), "ac101")
        self.assertNotIn("systemctl disable", self.b.calls())

    def test_a_board_whose_ac101b_answers_is_not_switched_without_force(self):
        self.cards(self.CARDS_AC101B)
        r = self.b.run("codec", "ac101")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("built with the AC101B", r.stdout)
        self.assertEqual(self.package.read_bytes(), self.original[self.package])
        r = self.b.run("codec", "ac101", "--force")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(self.has_ac101(self.package))

    def test_reapply_converts_a_new_tree_refreshes_what_is_kept_and_sets_the_playback_path(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        new = self.dtb("linux-image-6.6.99-test")                   # a kernel package installed after the switch
        new_original = new.read_bytes()
        rebuilt = self.dtb(f"linux-image-{RELEASE}", more_i2c='\t\t\trtc@51 { reg = <0x51>; };')   # the same release again, another tree
        rebuilt_original = rebuilt.read_bytes()
        self.cards(self.CARDS_AC101)
        self.running("AC101")
        r = self.b.run("codec", "reapply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.has_ac101(new))
        self.assertEqual(self.kept(new).read_bytes(), new_original)
        self.assertEqual(self.kept(rebuilt).read_bytes(), rebuilt_original)     # what is kept: the tree it replaced
        mixer = [line for line in self.b.calls().splitlines() if line.startswith("amixer")]
        self.assertEqual(mixer, [f"amixer -q -c allwinnerac101 sset {c}" for c in (
            "HPOUTL Source Mux OMIXL", "HPOUTR Source Mux OMIXR", "OutputL Mixer DACL on", "OutputR Mixer DACR on",
            "HPOUTL on", "HPOUTR on")])

    def test_reapply_trees_does_the_trees_only(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        new = self.dtb("linux-image-6.6.99-test")
        self.cards(self.CARDS_AC101)
        r = self.b.run("codec", "reapply-trees")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.has_ac101(new))
        self.assertNotIn("amixer", self.b.calls())

    def test_reapply_says_so_when_the_ac101_does_not_answer(self):
        self.assertEqual(self.b.run("codec", "ac101", "--force").returncode, 0)
        self.running("AC101")
        self.b.put(self.b.tmp / "klog", "[ 6.5] sunxi:sound-ac101:[ERR]: 1724 ac101_probe(): try read ac101 5 times but failed, ac101 probe failed")
        r = self.b.run("codec", "reapply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("'a7a-clock codec ac101b'", r.stdout)
        r = self.b.run("codec", "status")
        self.assertIn("built with the AC101B. 'a7a-clock codec ac101b', then reboot", r.stdout)

    def test_reapply_without_the_ac101_card_or_without_the_choice_does_nothing(self):
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        r = self.b.run("codec", "reapply")                           # only the HDMI card: no mixer to set
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("amixer", self.b.calls())
        self.assertEqual(self.b.run("codec", "ac101b").returncode, 0)
        before = self.package.read_bytes()
        self.cards(self.CARDS_AC101)
        r = self.b.run("codec", "reapply")                           # the choice is gone: nothing at all
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.package.read_bytes(), before)
        self.assertNotIn("amixer", self.b.calls())

    def test_status_names_the_codec_of_each_tree_and_points_at_the_switch(self):
        self.b.put(self.b.tmp / "klog", "[    9.3] sunxi:sound-ac101b:[ERR]: 1707 ac101b_probe(): try read ac101b 5 times but failed, ac101b probe failed")
        r = self.b.run("codec", "status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("running device tree describes the AC101B", r.stdout)
        self.assertIn(f"{self.package}: AC101B", r.stdout)
        self.assertIn("the jack has no sound card", r.stdout)
        self.assertIn("'a7a-clock codec ac101', then reboot", r.stdout)
        # switched, not yet rebooted
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        r = self.b.run("codec", "status")
        self.assertIn("REBOOT to apply", r.stdout)
        self.assertNotIn("'a7a-clock codec ac101', then reboot", r.stdout)
        # after the reboot: the running tree has the AC101, its card is there, no hint
        self.running("AC101")
        self.cards(self.CARDS_AC101)
        r = self.b.run("codec", "status")
        self.assertIn("running device tree describes the AC101;", r.stdout)
        self.assertIn(f"{self.gpu_copy}: AC101", r.stdout)
        self.assertIn("  the jack's sound card: allwinner-ac101\n", r.stdout)
        self.assertEqual(r.stdout.count("allwinner-ac101"), 1, r.stdout)    # the card's line names it twice
        self.assertNotIn("then reboot", r.stdout)

    AC101B_FAILED = "[    9.3] sunxi:sound-ac101b:[ERR]: 1707 ac101b_probe(): try read ac101b 5 times but failed, ac101b probe failed"
    AC101_FAILED = "[    6.5] sunxi:sound-ac101:[ERR]: 1724 ac101_probe(): try read ac101 5 times but failed, ac101 probe failed"

    def state(self):
        """`codec state`: the one line a7a-panel reads (2026-11-24: the panel offers the switch)."""
        r = self.b.run("codec", "state")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("\n"), 1, r.stdout)
        return r.stdout.strip()

    def test_state_on_a_board_built_with_the_ac101_asks_for_the_switch_then_the_restart(self):
        self.b.put(self.b.tmp / "klog", self.AC101B_FAILED)
        self.assertEqual(self.state(), "switch AC101")
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        self.assertEqual(self.state(), "reboot AC101")
        # restarted: the AC101 in the running tree, its card up
        self.running("AC101")
        self.cards(self.CARDS_AC101)
        self.b.put(self.b.tmp / "klog", "")
        self.assertEqual(self.state(), "ok AC101")
        # switched back, not yet restarted, the AC101's card still up: a restart would silence a jack that works
        # (review 2026-11-24: the panel urged that restart): `keep AC101`, whose button switches to it again
        self.assertEqual(self.b.run("codec", "ac101b").returncode, 0)
        self.assertEqual(self.state(), "keep AC101")
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        self.assertEqual(self.state(), "ok AC101")

    def test_state_asks_for_the_restart_only_once_every_tree_has_the_switch(self):
        # the choice made, but a tree a kernel update brought did not take the AC101 (review 2026-11-24: "restart"
        # at every boot, forever): the switch is offered again, and its button says why it stops
        self.b.put(self.b.tmp / "klog", self.AC101B_FAILED)
        self.assertEqual(self.b.run("codec", "ac101").returncode, 0)
        self.assertEqual(self.state(), "reboot AC101")
        self.package.write_bytes(self.original[self.package])
        self.assertEqual(self.state(), "switch AC101")

    def test_state_on_a_board_built_with_the_ac101b_switched_by_force(self):
        # its card up, the trees switched to the AC101: keep the AC101B (its button puts the trees back)
        self.cards(self.CARDS_AC101B)
        self.assertEqual(self.b.run("codec", "ac101", "--force").returncode, 0)
        self.assertEqual(self.state(), "keep AC101B")
        self.assertEqual(self.b.run("codec", "ac101b").returncode, 0)
        self.assertEqual(self.state(), "ok AC101B")

    def test_state_on_a_board_built_with_the_ac101b_is_ok_whatever_the_log_says(self):
        self.cards(self.CARDS_AC101B)
        self.b.put(self.b.tmp / "klog", self.AC101_FAILED)
        self.assertEqual(self.state(), "ok AC101B")

    def test_state_after_a_switch_on_a_board_built_with_the_ac101b_asks_for_the_way_back(self):
        self.assertEqual(self.b.run("codec", "ac101", "--force").returncode, 0)
        self.running("AC101")
        self.b.put(self.b.tmp / "klog", self.AC101_FAILED)
        self.assertEqual(self.state(), "switch AC101B")

    def test_state_without_a_card_or_a_word_in_the_log_is_none(self):
        self.assertEqual(self.state(), "none")
        self.b.put(self.b.tmp / "klog", "[    9.3] some other line")
        self.assertEqual(self.state(), "none")

    def test_state_reads_the_log_through_sudo_only_without_a_password(self):
        # the user cannot read the kernel's journal; sudo works only as `sudo -n` (the panel has no terminal)
        tmp = self.b.tmp
        self.b.stub("journalctl", "exit 0")
        self.b.stub("sudo", f'[ "$1" = -n ] || {{ echo "sudo without -n: $*" >> "{tmp}/calls"; exit 1; }}; shift; '
                            f'[ "$1" = journalctl ] && {{ cat "{tmp}/klog"; exit 0; }}; exec "$@"')
        self.b.put(tmp / "klog", self.AC101B_FAILED)
        self.assertEqual(self.state(), "switch AC101")
        self.assertNotIn("sudo without -n", self.b.calls())
        # sudo refused too: no hint, no wait
        self.b.stub("sudo", "exit 1")
        self.assertEqual(self.state(), "none")

    def test_an_unknown_word_is_refused(self):
        r = self.b.run("codec", "ac102")
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage: a7a-clock codec", r.stdout)


if __name__ == "__main__":
    unittest.main()
