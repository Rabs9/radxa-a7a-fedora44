"""Tests for tools/board/a7a-var-dirs: the directories under /var that packages own and that a system installed from
an image without Fedora's /var still lacks. rpm is faked (it prints the file list of a model system), install is
faked to make the directory and log the owner and mode it was asked for (only root can set them)."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "board" / "a7a-var-dirs"

# mode(octal) flags user group path, as `rpm -qa --qf '[%{FILEMODES:octal} %{FILEFLAGS} ...]'` REALLY prints them: a
# directory is 40755, not 040755 (2026-11-16: the first version of the tool matched the second form, its test fed it that
# form, and on the real system it created nothing)
RPM_LIST = """\
40755 0 root root /var/lib/logrotate
40755 0 root root /var/lib/rpm-state
40755 0 root root /var/lib/plocate
100644 0 root root /var/lib/plocate/CACHEDIR.TAG
40700 0 gssproxy gssproxy /var/lib/gssproxy
40700 0 gssproxy gssproxy /var/lib/gssproxy/rcache
41777 0 root root /var/tmp
42755 0 root systemd-journal /var/log/journal
40755 64 root root /var/cache/dnf
40755 0 root root /var/run/faillock
40755 0 root root /var/lock/subsys
40755 0 root root /usr/lib/modules
040755 0 root root /var/lib/color
40755 0 root root /var/lib/color
40755 0 root root /var/lib/exists
40755 0 root root /var/lib/is-a-file
120777 0 root root /var/mail
"""


# mode flags path target, as the second query prints them: a path that is not a link has no fourth field
RPM_LINKS = """\
120777 0 /var/mail spool/mail
120777 0 /var/run ../run
120777 0 /var/lock ../run/lock
120777 64 /var/lib/ghost-link elsewhere
120777 0 /var/lib/exists-link target
120777 0 /usr/lib/somewhere/link target
40755 0 /var/lib/logrotate
100644 0 /var/lib/plocate/CACHEDIR.TAG
"""


class TestVarDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        (self.root / "var/lib/exists").mkdir(parents=True)
        (self.root / "var/lib/exists/keep").write_text("x")
        (self.root / "var/lib/is-a-file").write_text("not a directory, and not ours to change")
        (self.root / "var/lib/exists-link").symlink_to("what-the-admin-chose")
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "install.log"
        fakes = {
            "rpm": f"case \"$*\" in *FILELINKTOS*) cat <<'EOF'\n{RPM_LINKS}EOF\n;; *) cat <<'EOF'\n{RPM_LIST}EOF\n;; esac\n",
            # install -d -m MODE -o USER -g GROUP PATH
            "install": f'echo "$3 $5 $7 ${{8#{self.root}}}" >> "{self.log}"; [ -e "$FAIL_INSTALL" ] && exit 1; mkdir -p "$8"\n',
        }
        for name, body in fakes.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, **env):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", A7A_VAR_DIRS_ROOT=str(self.root), FAIL_INSTALL="/nonexistent")
        e.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=60)

    def made(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_creates_what_is_missing_with_the_packages_owner_and_mode(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.made(), ["0755 root root /var/lib/color", "0700 gssproxy gssproxy /var/lib/gssproxy",
                                       "0700 gssproxy gssproxy /var/lib/gssproxy/rcache", "0755 root root /var/lib/logrotate",
                                       "0755 root root /var/lib/plocate", "0755 root root /var/lib/rpm-state",
                                       "2755 root systemd-journal /var/log/journal", "1777 root root /var/tmp"])
        self.assertIn("a7a-var-dirs: 9 created", r.stdout)                 # eight directories and one link
        self.assertTrue((self.root / "var/lib/gssproxy/rcache").is_dir())

    def test_links_that_packages_own_are_made_too(self):
        # /var/mail -> spool/mail is payload of the filesystem package: missing on the working card for the same reason
        # as the directories (rpm -Va, 2026-11-16)
        r = self.run_tool()
        self.assertEqual(os.readlink(self.root / "var/mail"), "spool/mail")
        self.assertIn("linked /var/mail -> spool/mail", r.stdout)
        self.assertEqual(os.readlink(self.root / "var/lib/exists-link"), "what-the-admin-chose")     # never changed
        for never in ("var/run", "var/lock", "var/lib/ghost-link", "usr/lib/somewhere/link"):
            self.assertFalse(os.path.lexists(self.root / never), never)

    def test_leaves_alone_what_exists_ghosts_files_links_and_what_lives_in_run(self):
        self.run_tool()
        paths = [line.split()[-1] for line in self.made()]
        for never in ("/var/lib/exists", "/var/lib/is-a-file", "/var/cache/dnf", "/var/run/faillock", "/var/lock/subsys",
                      "/usr/lib/modules", "/var/mail", "/var/lib/plocate/CACHEDIR.TAG"):
            self.assertNotIn(never, paths)
        self.assertEqual((self.root / "var/lib/exists/keep").read_text(), "x")
        self.assertTrue((self.root / "var/lib/is-a-file").is_file())

    def test_second_run_has_nothing_to_do(self):
        self.run_tool()
        self.log.unlink()
        r = self.run_tool()
        self.assertEqual(self.made(), [])
        self.assertIn("a7a-var-dirs: 0 created", r.stdout)
        self.assertEqual(os.readlink(self.root / "var/mail"), "spool/mail")

    def test_dry_run_creates_nothing(self):
        r = self.run_tool("--dry-run")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.made(), [])
        self.assertIn("would create /var/lib/logrotate (0755 root:root)", r.stdout)
        self.assertIn("would link /var/mail -> spool/mail", r.stdout)
        self.assertIn("9 would be created", r.stdout)
        self.assertFalse((self.root / "var/lib/logrotate").exists())
        self.assertFalse(os.path.lexists(self.root / "var/mail"))

    def test_a_directory_that_cannot_be_made_is_an_error(self):
        flag = Path(self.tmp.name) / "fail"
        flag.touch()
        r = self.run_tool(FAIL_INSTALL=str(flag))
        self.assertEqual(r.returncode, 1)
        self.assertIn("could NOT create", r.stderr)
        self.assertIn("8 failed", r.stdout)

    def test_no_word_about_failures_when_there_are_none(self):
        self.assertNotIn("failed", self.run_tool().stdout)


if __name__ == "__main__":
    unittest.main()
