"""tools/board/a7a-clock-keep: the time the next boot starts its clock from, kept only while chronyd is in sync."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools/board/a7a-clock-keep"


class ClockKeepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        (self.t / "bin").mkdir()
        self.clock = self.t / "var/lib/systemd/timesync/clock"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, leap: str | None) -> subprocess.CompletedProcess:
        fake = self.t / "bin/chronyc"
        if leap is None:
            fake.write_text("#!/bin/sh\necho '506 Cannot talk to daemon' >&2\nexit 1\n")
        else:
            fake.write_text(f"#!/bin/sh\nprintf 'Reference ID    : C0A80201 (router)\\nLeap status     : {leap}\\n'\n")
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{self.t / 'bin'}:/usr/bin:/bin", A7A_CLOCK_FILE=str(self.clock))
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_kept_when_in_sync(self):
        r = self.run_with("Normal")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.clock.exists())
        self.assertLess(abs(self.clock.stat().st_mtime - time.time()), 5)

    def test_left_alone_when_not(self):
        for leap in ("Not synchronised", None):
            r = self.run_with(leap)
            self.assertEqual(r.returncode, 0, r.stderr)                  # never a failed unit: there is nothing wrong
            self.assertFalse(self.clock.exists(), leap)
        # and an older time is not written over by a clock nobody vouches for
        self.clock.parent.mkdir(parents=True)
        self.clock.touch()
        os.utime(self.clock, (1700000000, 1700000000))
        self.run_with("Not synchronised")
        self.assertEqual(self.clock.stat().st_mtime, 1700000000)

    def test_the_units(self):
        unit = (P / "tools/board/systemd/a7a-clock-keep.service").read_text()
        for line in ("Type=oneshot", "RemainAfterExit=yes", "ExecStop=/usr/local/bin/a7a-clock-keep", "After=chronyd.service",
                     "WantedBy=multi-user.target"):
            self.assertIn(line + "\n", unit)
        timer = (P / "tools/board/systemd/a7a-clock-keep.timer").read_text()
        self.assertIn("Unit=a7a-clock-keep-tick.service\n", timer)       # not the service that stays active
        self.assertIn("OnUnitActiveSec=1h\n", timer)


if __name__ == "__main__":
    unittest.main()
