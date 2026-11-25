"""tools/board/tmpfiles.d/a7a-x11.conf: the X11 socket directories stay out of the /tmp clean-up. Run with this
system's systemd-tmpfiles and its own x11.conf on a scratch root (--root), the directories backdated a month, as they
are on the board at the first clean-up (created at systemd's build date). The /tmp line here ages directories by
modification time only (M:): a birth time cannot be backdated, and Fedora's line also reads it, which on the board is
as old as the rest. The checks look at the scratch root, not at systemd's messages."""
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
CONF = P / "tools" / "board" / "tmpfiles.d" / "a7a-x11.conf"
X11 = Path("/usr/lib/tmpfiles.d/x11.conf")
DIRS = [".X11-unix", ".ICE-unix", ".XIM-unix", ".font-unix"]


def tmpfiles():
    return shutil.which("systemd-tmpfiles") if X11.is_file() else None


def userns():
    """the boot run sets owners: as root inside a user namespace of our own"""
    try:
        return subprocess.run(["unshare", "-r", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


@unittest.skipUnless(tmpfiles(), "needs systemd-tmpfiles and systemd's x11.conf")
class X11TmpfilesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.root = self.d / "root"
        month = time.time() - 31 * 86400
        for n in DIRS:
            (self.root / "tmp" / n).mkdir(parents=True)
            os.utime(self.root / "tmp" / n, (month, month))
        self.q = self.d / "tmp.conf"
        self.q.write_text("q /tmp 1777 root root M:10d\n")

    def clean(self, *confs):
        r = subprocess.run([tmpfiles(), f"--root={self.root}", "--clean", str(self.q), str(X11), *map(str, confs)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [n for n in DIRS if (self.root / "tmp" / n).is_dir()]

    def test_without_it_the_clean_up_removes_the_empty_directories(self):
        self.assertEqual(self.clean(), [])

    def test_with_it_the_clean_up_keeps_them(self):
        self.assertEqual(self.clean(CONF), DIRS)

    @unittest.skipUnless(userns(), "needs unprivileged user namespaces (unshare -r)")
    def test_the_boot_still_empties_and_creates_them(self):
        root = self.d / "boot"
        (root / "tmp" / ".X11-unix").mkdir(parents=True)
        (root / "tmp" / ".X11-unix" / "X0").touch()       # left from the last session
        (root / "tmp" / ".X0-lock").touch()
        r = subprocess.run(["unshare", "-r", tmpfiles(), f"--root={root}", "--create", "--remove", "--boot",
                            str(X11), str(CONF)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        for n in DIRS:
            self.assertEqual(stat.S_IMODE((root / "tmp" / n).stat().st_mode), 0o1777, n)
        self.assertEqual(list((root / "tmp" / ".X11-unix").iterdir()), [])
        self.assertFalse((root / "tmp" / ".X0-lock").exists())

    def test_it_names_only_the_directories_of_x11_conf(self):
        lines = [l.split() for l in CONF.read_text().splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(lines, [["X", f"/tmp/{n}"] for n in DIRS])
        boot = [l.split()[1] for l in X11.read_text().splitlines() if l.startswith("D! ")]
        self.assertEqual(sorted(boot), sorted(f"/tmp/{n}" for n in DIRS))


if __name__ == "__main__":
    unittest.main()
