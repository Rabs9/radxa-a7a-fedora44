"""tools/clock-test.sh, the part that touches the boot menu (gpu-set, restart, gpu-verify), against the fake board of
test_clock.py. On the real board this runs over ssh with the owner standing by: a boot menu that is not put back, or
is put back empty, is a card that boots into the wrong GPU clock or not at all. Here: the menu comes back byte for
byte on every path (verified, failed soak, a7a-clock failing half-way), nothing is touched when the step cannot run
(nobody logged in, the test labels taken, an unfinished earlier run), and the immutable flag ends as it began."""

import hashlib
import unittest
from pathlib import Path

from test_clock import FakeBoard, P, RELEASE

SCRIPT = P / "tools/clock-test.sh"


class Board(FakeBoard):
    def __init__(self, immutable=True):
        super().__init__()
        self.vartmp = self.tmp / "vartmp"
        self.vartmp.mkdir()
        self.flag = self.tmp / "immutable"
        if immutable:
            self.flag.write_text("")
        self.put(self.root / "sys/kernel/debug/pvr/status", "HWR Event Count: 0")
        # the real a7a-clock (rewritten to the fake tree) behind the name the test calls; its soak is a stand-in
        self.stub("a7a-clock", f'''if [ "$1" = soak ]; then
  [ -e "{self.tmp}/soak-fails" ] && {{ echo "VERDICT: ERRORS"; exit 0; }}
  echo "SOAK: 2 runs, new errors 0, new hardware recoveries 0, max gpu 50°C"; echo "VERDICT: STABLE"; exit 0
fi
exec bash "{self.script}" "$@"''')
        # fdtput keeps what it was told beside the device tree, fdtget reads it back
        self.stub("fdtput", f'[ -e "{self.tmp}/fdtput-fails" ] && exit 1; echo "$5 $6" >> "$3.props"; echo "fdtput $*" >> "{self.tmp}/calls"')
        self.stub("fdtget", 'awk -v p="$5" \'$1 == p {v = $2} END {print v}\' "$3.props" 2>/dev/null')
        self.stub("chattr", f'case "$1" in +i) : > "{self.flag}" ;; -i) rm -f "{self.flag}" ;; esac; exit 0')
        self.stub("lsattr", f'[ -e "{self.flag}" ] && echo "----i---------e------- $1" || echo "--------------e------- $1"')
        self.stub("pgrep", f'[ -e "{self.tmp}/session" ] && {{ echo 4242; exit 0; }}; exit 1')
        self.stub("journalctl", "exit 0")
        text = SCRIPT.read_text()
        for old, new in (("/sys/", f"{self.root}/sys/"), ("/boot/extlinux/extlinux.conf", str(self.ext)),
                         ("/var/tmp/clock-test", f"{self.vartmp}/clock-test"),
                         ("/usr/lib/linux-image-gpu", f"{self.root}/usr/lib/linux-image-gpu")):
            text = text.replace(old, new)
        self.test = self.tmp / "clock-test.sh"
        self.test.write_text(text)

    def clock_test(self, *args):
        import os
        import subprocess
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin", NM_STATE=str(self.nm))
        return subprocess.run(["bash", str(self.test), *args], capture_output=True, text=True, env=env)

    def menu(self):
        return hashlib.sha256(self.ext.read_bytes()).hexdigest()

    def left_behind(self):
        return sorted(p.name for p in self.vartmp.iterdir()) + sorted(p.name for p in (self.root / "usr/lib").glob("linux-image-gpu*"))


class GpuEntryAcrossARestart(unittest.TestCase):
    def board(self, **kw):
        b = Board(**kw)
        self.addCleanup(b.close)
        return b

    def test_set_then_verify_puts_the_menu_back_byte_for_byte(self):
        for immutable in (True, False):
            with self.subTest(immutable=immutable):
                b = self.board(immutable=immutable)
                before = b.menu()
                r = b.clock_test("gpu-set")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue(b.ext.read_text().startswith("default g_840_800\n"))
                self.assertIn(f"linux /boot/vmlinuz-{RELEASE}", b.ext.read_text().split("label g_840_800")[1])
                self.assertEqual(b.left_behind(), ["clock-test.extlinux.conf", "clock-test.state", "linux-image-gpu840v800"])
                # "after the restart": the GPU runs at the entry's clock and somebody is logged in
                b.clk(gpu=840000000, dsu=780000000)
                (b.tmp / "session").write_text("")
                r = b.clock_test("gpu-verify")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("PASS  boot menu put back byte for byte", r.stdout)
                self.assertEqual(b.menu(), before)
                self.assertEqual(b.left_behind(), [])
                self.assertEqual(b.flag.exists(), immutable)

    def test_verify_without_a_session_changes_nothing_and_keeps_the_entry_waiting(self):
        b = self.board()
        self.assertEqual(b.clock_test("gpu-set").returncode, 0)
        waiting = b.menu()
        r = b.clock_test("gpu-verify")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("log in at the board", r.stdout)
        self.assertEqual(b.menu(), waiting)
        self.assertIn("clock-test.state", b.left_behind())

    def test_a_failed_soak_fails_the_step_and_still_puts_the_menu_back(self):
        b = self.board()
        before = b.menu()
        self.assertEqual(b.clock_test("gpu-set").returncode, 0)
        b.clk(gpu=840000000, dsu=780000000)
        (b.tmp / "session").write_text("")
        (b.tmp / "soak-fails").write_text("")
        r = b.clock_test("gpu-verify")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL  the soak at 840 MHz did not end with VERDICT: STABLE", r.stdout)
        self.assertEqual(b.menu(), before)
        self.assertEqual(b.left_behind(), [])

    def test_a_gpu_that_is_not_at_the_entrys_clock_fails_the_step(self):
        b = self.board()
        self.assertEqual(b.clock_test("gpu-set").returncode, 0)
        (b.tmp / "session").write_text("")       # the fake's GPU is still at 1008 MHz: the entry did not take
        r = b.clock_test("gpu-verify")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FAIL  GPU clock (MHz): read back '1008', expected '840'", r.stdout)

    def test_set_is_refused_while_an_earlier_run_is_unfinished(self):
        b = self.board()
        self.assertEqual(b.clock_test("gpu-set").returncode, 0)
        waiting = b.menu()
        r = b.clock_test("gpu-set")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(b.menu(), waiting)

    def test_set_takes_a_label_nobody_uses(self):
        b = self.board()
        b.ext.write_text(b.ext.read_text() + "\nlabel g_840_800\n\tlinux /boot/vmlinuz-x\n")
        r = b.clock_test("gpu-set")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(b.ext.read_text().startswith("default g_864_800\n"))

    def test_set_does_nothing_when_every_test_label_is_taken(self):
        b = self.board()
        b.ext.write_text(b.ext.read_text() + "".join(f"\nlabel g_{c}_800\n\tlinux /boot/vmlinuz-x\n" for c in (840, 864, 816)))
        before = b.menu()
        r = b.clock_test("gpu-set")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(b.menu(), before)
        self.assertEqual(b.left_behind(), [])

    def test_when_a7a_clock_fails_half_way_the_menu_is_put_back_and_nothing_asks_for_a_restart(self):
        b = self.board()
        before = b.menu()
        (b.tmp / "fdtput-fails").write_text("")
        r = b.clock_test("gpu-set")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("Reboot, LOG IN", r.stdout)
        self.assertEqual(b.menu(), before)
        self.assertEqual(b.left_behind(), [])
        self.assertTrue(b.flag.exists())


if __name__ == "__main__":
    unittest.main()
