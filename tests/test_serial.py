"""Tests for the serial stage: the console watcher driven by a pty fed with a recorded transcript."""

import os
import pty
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import serial

FIXTURE = Path(__file__).parent / "fixtures" / "board-boot.txt"


class Feeder:
    """Writes a transcript into the master side of a pty like a board would, records what was typed back."""

    def __init__(self, text: str, delay: float = 0.004, stop_after: int | None = None, expect_choice: bool = False):
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.text, self.delay, self.stop_after, self.expect_choice = text, delay, stop_after, expect_choice
        self.typed = b""
        self.stop = False
        self.thread = threading.Thread(target=self._feed, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _write(self, data: bytes):
        if self.stop:
            raise StopIteration
        try:
            os.write(self.master, data)
        except OSError:
            raise StopIteration

    def _feed(self):
        try:
            self._feed_lines()
        except StopIteration:
            pass

    def _feed_lines(self):
        os.set_blocking(self.master, False)
        n = 0
        for line in self.text.splitlines():
            if self.stop or (self.stop_after is not None and n >= self.stop_after):
                return
            n += 1
            if "@@CHOICE@@" in line:
                self._write(line.replace("@@CHOICE@@", "").encode())
                if self.expect_choice:
                    want = (self.expect_choice if isinstance(self.expect_choice, str) else "2").encode()   # True = entry 2
                    deadline = time.time() + 3
                    while time.time() < deadline and want not in self.typed and not self.stop:
                        try:
                            self.typed += os.read(self.master, 64)
                        except BlockingIOError:
                            time.sleep(0.01)
                    self._write(want + b"\r\n" + want + b":\tFedora 44 KDE, Linux 6.6.98+, single user, no desktop\r\n")
                    continue
                self._write(b"\r\n")
                continue
            if "@@PROMPT@@" in line:
                self._write(line.replace("@@PROMPT@@", "").encode())
                continue
            self._write(line.encode() + b"\r\n")
            time.sleep(self.delay)

    def close(self):
        self.stop = True
        self.thread.join(timeout=5)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "serial.log"
        self.text = FIXTURE.read_text()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_boot_passes_on_the_checklist(self):
        f = Feeder(self.text).start()
        try:
            c = serial.Console(f.port, 115200, self.log, hang_seconds=5)
            r = c.watch(timeout=20)
        finally:
            f.close()
        self.assertEqual(r["verdict"], "PASS", r)
        names = [e["event"] for e in r["events"]]
        for want in ["uboot", "extlinux", "kernel", "root-mounted", "multi-user", "graphical", "checklist"]:
            self.assertIn(want, names, names)
        self.assertEqual(r["entry"], "1")
        self.assertEqual(r["checklist"]["cpu max"], "2016000 / 3000000")
        self.assertEqual(r["checklist"]["failed units"], "0")
        self.assertEqual(r["checklist"]["modules"], "41")
        log = self.log.read_text()
        self.assertRegex(log.splitlines()[1], r"^\d{2}:\d{2}:\d{2}\.\d{3} ")   # every line stamped (line 0 is the header)
        self.assertIn("Starting kernel", log)

    def test_kernel_panic_fails_before_the_timeout(self):
        text = self.text.replace("[    1.204100] VFS: Mounted root", "[    1.2] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(179,3)\n[    1.3] ---[ end Kernel panic")
        f = Feeder(text).start()
        t0 = time.time()
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=5).watch(timeout=30)
        finally:
            f.close()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("panic", r["reason"])
        self.assertLess(time.time() - t0, 10)

    def test_silence_is_a_hang(self):
        f = Feeder(self.text, stop_after=8).start()
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=1).watch(timeout=30)
        finally:
            f.close()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("no output", r["reason"])
        self.assertIn("uboot", [e["event"] for e in r["events"]])

    def test_silence_before_the_first_byte_is_not_a_hang(self):
        f = Feeder(self.text, delay=0.002)
        f.text = self.text
        threading.Timer(1.5, f.start).start()          # the board is powered on 1.5 s after the watch begins
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=1).watch(timeout=20, wait_for_output=10)
        finally:
            f.close()
        self.assertEqual(r["verdict"], "PASS", r)
        self.assertIn("first output after", self.log.read_text())

    def test_no_output_at_all_is_reported_as_such(self):
        f = Feeder(self.text, stop_after=0).start()
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=1).watch(timeout=20, wait_for_output=1)
        finally:
            f.close()
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("nothing on the console", r["reason"])

    def test_fallback_mode_picks_entry_2_at_the_menu(self):
        f = Feeder(self.text, expect_choice=True).start()
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=5).watch(timeout=20, choose_entry="2")
        finally:
            f.close()
        self.assertIn(b"2", f.typed)
        self.assertEqual(r["entry"], "2")
        self.assertEqual(r["verdict"], "PASS")

    def test_events_have_offsets_and_lines(self):
        f = Feeder(self.text).start()
        try:
            r = serial.Console(f.port, 115200, self.log, hang_seconds=5).watch(timeout=20)
        finally:
            f.close()
        e = next(x for x in r["events"] if x["event"] == "kernel")
        self.assertIn("Starting kernel", e["line"])
        self.assertGreaterEqual(e["t"], 0)

    def test_missing_port(self):
        with self.assertRaises(serial.SerialError):
            serial.Console("/dev/nonexistent-ttyACM9", 115200, self.log).watch(timeout=1)


class TestRecord(unittest.TestCase):
    def test_record_logs_stamped_lines_and_events_until_stopped(self):
        import threading
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work", tools_dir=root / "tools",
                     registry_path=root / "registry.json")
        text = FIXTURE.read_text().replace("[    1.204100] VFS: Mounted root", "[    1.2] Kernel panic - not syncing: boom")
        f = Feeder(text).start()
        cfg.serial_port = f.port
        stop = threading.Event()
        threading.Timer(1.5, stop.set).start()
        try:
            r = serial.record(cfg, stop=stop, log=root / "rec.log")
        finally:
            f.close()
            tmp.cleanup() if False else None
        self.assertEqual(r["status"], "passed")
        self.assertGreater(r["lines"], 10)
        self.assertIn("panic", [e["event"] for e in r["events"]])
        log = (root / "rec.log").read_text()
        self.assertRegex(log.splitlines()[1], r"^\d{2}:\d{2}:\d{2}\.\d{3} ")
        self.assertIn("== panic", log)
        tmp.cleanup()


class FakeRunner:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def run(self, argv, timeout=30):
        self.calls.append(argv)
        host = next((a for a in argv if "@" in a), "")
        return self.answers.get(host, (255, "connection refused"))


class TestSerialStage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.output_dir.mkdir()
        self.reg = StageRegistry(self.cfg.registry_path)
        self.img = self.cfg.output_dir / "a7a-fedora-44-kde-20260906.img"
        self.img.write_bytes(b"\0")
        self.reg.record_image(self.img.name, {})
        self.cfg.serial_hang_seconds = 5
        self.cfg.serial_boot_timeout = 20
        self.cfg.board_host = "192.168.1.50"
        self.cfg.board_tailscale = "100.101.102.103"
        self.text = FIXTURE.read_text()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_port_configured(self):
        r = serial.run(self.cfg, self.reg, image=self.img)
        self.assertEqual(r["status"], "failed")
        self.assertIn("serial_port", r["reason"])

    def test_first_boot_pass_is_recorded_with_ssh_confirmation(self):
        f = Feeder(self.text).start()
        self.cfg.serial_port = f.port
        runner = FakeRunner({"radxa@192.168.1.50": (255, "no route"), "radxa@100.101.102.103": (0, "6.6.98+\nradxa-cubie-a7a\n")})
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=runner, wait_for_power=False)
        finally:
            f.close()
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["ssh"]["host"], "radxa@100.101.102.103")
        self.assertIn("6.6.98+", r["ssh"]["output"])
        t = self.reg.images()[self.img.name]["tests"][-1]
        self.assertEqual((t["mode"], t["verdict"]), ("board", "PASS"))
        self.assertEqual(t["ssh"], "radxa@100.101.102.103")
        self.assertIn("checklist", t["milestones"])
        self.assertEqual(len(runner.calls), 2)          # LAN first, then Tailscale

    def test_failed_boot_says_what_to_do_next(self):
        text = self.text.replace("[    1.204100] VFS: Mounted root", "[    1.2] Kernel panic - not syncing: boom")
        f = Feeder(text).start()
        self.cfg.serial_port = f.port
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False)
        finally:
            f.close()
        self.assertEqual(r["status"], "failed")
        self.assertIn("panic", r["reason"])
        self.assertIn("fallback", r["next_step"])
        t = self.reg.images()[self.img.name]["tests"][-1]
        self.assertEqual(t["verdict"], "FAIL")

    def test_fallback_run_then_swap_advice(self):
        text = self.text.replace("[    1.204100] VFS: Mounted root", "[    1.2] Kernel panic - not syncing: boom")
        f = Feeder(text, expect_choice=True).start()
        self.cfg.serial_port = f.port
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False, fallback=True)
        finally:
            f.close()
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["entry"], "2")
        self.assertIn("backup card", r["next_step"])

    def test_any_entry_of_the_menu_can_be_picked(self):
        # 2026-11-15: an image with the earlier kernels as entries 3 and 4 had no helper that reaches them: --fallback
        # types 2, which there is the image's OTHER new kernel
        f = Feeder(self.text, expect_choice="3").start()
        self.cfg.serial_port = f.port
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False, entry=3)
        finally:
            f.close()
        self.assertEqual(r["entry"], "3")
        self.assertEqual(r["mode"], "entry 3")
        self.assertEqual(r["plan"]["choose_entry"], "3")
        self.assertIn(b"3", f.typed)

    def test_the_advice_after_a_failed_boot_names_the_entries_this_image_has(self):
        # "the recovery entry (2)" was true for the first image only: since the packages entry 2 is the image's other
        # kernel and recovery is the last entry; with earlier kernels aboard the way back is entry 3
        text = self.text.replace("[    1.204100] VFS: Mounted root", "[    1.2] Kernel panic - not syncing: boom")
        f = Feeder(text).start()
        self.cfg.serial_port = f.port
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False)
        finally:
            f.close()
        self.assertIn("--fallback", r["next_step"])
        self.assertIn("other kernel", r["next_step"])
        self.assertNotIn("recovery entry (entry 2", r["next_step"])
        self.reg.images()[self.img.name]["previous_kernels"] = ["6.6.98-a7a.202609091931", "6.6.98-a7apvr.202609091821"]
        f = Feeder(text).start()
        self.cfg.serial_port = f.port
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False)
        finally:
            f.close()
        self.assertIn("--entry 3", r["next_step"])
        self.assertIn("6.6.98-a7a.202609091931", r["next_step"])

    def test_no_output_is_skipped_not_a_verdict_on_the_image(self):
        f = Feeder(self.text, stop_after=0).start()
        self.cfg.serial_port = f.port
        self.cfg.serial_wait_seconds = 1
        try:
            r = serial.run(self.cfg, self.reg, image=self.img, runner=FakeRunner({}), wait_for_power=False)
        finally:
            f.close()
        self.assertEqual(r["status"], "skipped")
        self.assertIn("power the board on", r["next_step"])
        self.assertEqual(self.reg.images()[self.img.name]["tests"], [])
        self.assertEqual(self.reg.get_stage("serial")["status"], "skipped")

    def test_dry_run(self):
        self.cfg.serial_port = "/dev/ttyACM0"
        r = serial.run(self.cfg, self.reg, image=self.img, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertIn("/dev/ttyACM0", r["plan"]["port"])


if __name__ == "__main__":
    unittest.main()


class ShellFeeder:
    """A getty + bash on the master side of a pty: answers the login, then runs each line through bash locally."""

    def __init__(self, user="radxa", password="radxa", no_shell=False):
        import subprocess
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.user, self.password, self.no_shell = user, password, no_shell
        self.stop = False
        self.subprocess = subprocess
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _readline(self):
        line = b""
        while not self.stop:
            try:
                ch = os.read(self.master, 1)
            except BlockingIOError:
                time.sleep(0.005)
                continue
            except OSError:
                return None
            if not ch:
                return None
            if ch == b"\n":
                return line.decode(errors="replace")
            line += ch
        return None

    def _write(self, text: str):
        try:
            os.write(self.master, text.encode())
        except OSError:
            pass

    def _serve(self):
        os.set_blocking(self.master, False)
        logged_in = False
        while not self.stop:
            line = self._readline()
            if line is None:
                return
            if self.no_shell:
                continue
            if not logged_in:
                if line == "":
                    self._write("\r\nradxa-cubie-a7a login: ")
                elif line == self.user:
                    self._write("\r\nPassword: ")
                    pw = self._readline()
                    if pw == self.password:
                        logged_in = True
                        self._write("\r\nLast login: never\r\n[radxa@radxa-cubie-a7a ~]$ ")
                    else:
                        self._write("\r\nLogin incorrect\r\n\r\nradxa-cubie-a7a login: ")
                else:
                    self._write("\r\nPassword: ")
                    self._readline()
                    self._write("\r\nLogin incorrect\r\n\r\nradxa-cubie-a7a login: ")
                continue
            self._write(line + "\r\n")                       # the echo of what was typed
            if line.strip() == "exit":
                logged_in = False
                self._write("logout\r\n\r\nradxa-cubie-a7a login: ")
                continue
            r = self.subprocess.run(["bash", "-c", line], capture_output=True, text=True, timeout=30)
            self._write((r.stdout + r.stderr).replace("\n", "\r\n") + "[radxa@radxa-cubie-a7a ~]$ ")

    def close(self):
        self.stop = True
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass
        self.thread.join(timeout=5)


class TestConsoleShell(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "shell.log"

    def tearDown(self):
        self.tmp.cleanup()

    def test_login_and_run_returns_output_and_exit_codes(self):
        f = ShellFeeder().start()
        try:
            c = serial.Console(f.port, 115200, self.log)
            r = c.login_and_run("radxa", "radxa", ["echo hello world", "printf 'a\\nb\\n'", "false", "uname -r"], timeout=20)
        finally:
            f.close()
        self.assertEqual(r["echo hello world"], {"output": "hello world", "rc": 0})
        self.assertEqual(r["printf 'a\\nb\\n'"]["output"], "a\nb")
        self.assertEqual(r["false"]["rc"], 1)
        self.assertTrue(r["uname -r"]["output"])
        self.assertIn("console shell", self.log.read_text())

    def test_wrong_password_is_an_error(self):
        f = ShellFeeder(password="other").start()
        try:
            with self.assertRaises(serial.SerialError):
                serial.Console(f.port, 115200, self.log).login_and_run("radxa", "radxa", ["true"], timeout=5, prompt_wait=5)
        finally:
            f.close()

    def test_no_getty_is_an_error(self):
        f = ShellFeeder(no_shell=True).start()
        try:
            with self.assertRaises(serial.SerialError):
                serial.Console(f.port, 115200, self.log).login_and_run("radxa", "radxa", ["true"], timeout=5, prompt_wait=1)
        finally:
            f.close()

    def test_probe_records_facts_and_the_board(self):
        root = Path(self.tmp.name)
        cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work", tools_dir=root / "tools",
                     registry_path=root / "registry.json")
        cfg.output_dir.mkdir()
        reg = StageRegistry(cfg.registry_path)
        img = cfg.output_dir / "a7a-fedora-44-kde-20260907.img"
        img.write_bytes(b"\0")
        reg.record_image(img.name, {})
        reg.record_test(img.name, mode="board", verdict="PASS", log="")
        f = ShellFeeder().start()
        cfg.serial_port = f.port
        try:
            r = serial.probe(cfg, reg, image=img)
        finally:
            f.close()
        self.assertEqual(r["status"], "passed", r)
        self.assertTrue(r["facts"]["kernel"])
        self.assertIn("probe", reg.images()[img.name]["tests"][-1])
        self.assertEqual(reg.data["board"]["release"], r["facts"]["kernel"])
        self.assertEqual(reg.get_stage("serial")["status"], "passed")

    def test_run_commands_stage(self):
        root = Path(self.tmp.name)
        cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work", tools_dir=root / "tools",
                     registry_path=root / "registry.json")
        cfg.output_dir.mkdir()
        reg = StageRegistry(cfg.registry_path)
        f = ShellFeeder().start()
        cfg.serial_port = f.port
        try:
            r = serial.run_commands(cfg, reg, ["echo one"])
        finally:
            f.close()
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["results"]["echo one"]["output"], "one")
