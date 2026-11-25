"""tools/public-check.py and tools/public-export.sh: the public copy holds nothing of the person who builds it."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(P / "tools"))
import importlib.util

spec = importlib.util.spec_from_file_location("public_check", P / "tools/public-check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


# Strings that look personal are put together at run time, so that this file itself passes the check it tests.
AT = "@"
MAILBOX = "jo.smith" + AT + "proton.me"
MAC_REAL = ":".join(["C2", "10", "20", "30", "40", "50"])
TAILNET_IP = ".".join(["100", "110", "7", "8"])
HOME_JO = "/home/" + "jo"


def git(repo, *args, **kw):
    env = dict(os.environ, GIT_AUTHOR_NAME="Dev Person", GIT_AUTHOR_EMAIL="dev@build.example",
               GIT_COMMITTER_NAME="Dev Person", GIT_COMMITTER_EMAIL="dev@build.example", GIT_CONFIG_GLOBAL="/dev/null")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=kw.get("check", True))


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def hits(self, text, private=(), allow=(), machine=()):
        (self.t / "f.txt").write_text(text)
        return [(k, s) for _, _, k, s in pc.check(self.t, list(private), set(allow), list(machine))]

    def test_what_counts(self):
        self.assertEqual(self.hits(f"mail me: {MAILBOX}\n"), [("email", MAILBOX)])
        self.assertEqual(self.hits(f"Connected to {MAC_REAL}\n"), [("network-address", MAC_REAL.lower())])
        self.assertEqual(self.hits(f"ssh radxa{AT}{TAILNET_IP}\n"), [("tailnet", TAILNET_IP)])
        self.assertEqual(self.hits(f"cd {HOME_JO}/Projects\n"), [("home-path", HOME_JO)])

    def test_what_does_not(self):
        text = ("broadcast ff:ff:ff:ff:ff:ff from 00:00:00:00:00:00; 100.64.0.0/10; /home/radxa/.config; /home/radxa.\n"
                "serial-getty@ttyAS0.service, owner@mail.example, us@example.com, x@y.test, radxa@radxa-cubie-a7a.local,\n"
                "241389662+Rabs9@users.noreply.github.com; 12:34:56 is a time; 2001:db8::1\n")
        self.assertEqual(self.hits(text), [])
        self.assertEqual(self.hits("a made-up 02:11:22:33:44:55\n", allow={"02:11:22:33:44:55"}), [])

    def test_what_the_review_found_through(self):
        """Each way past the first version of the check (review 2026-11-24)."""
        local_mail = "jo" + AT + "workbox.local"                             # a machine on someone's network
        self.assertEqual(self.hits(f"ssh {local_mail}\n"), [("email", local_mail)])
        v6 = ":".join(["2a02", "8071", "1234", "5678", ""]) + ":1"              # a global address, or its prefix
        self.assertEqual(self.hits(f"addr {v6}\n"), [("ipv6", v6)])
        self.assertEqual(self.hits(f"prefix {v6[:-2]}/64\n"), [("ipv6", v6[:-2])])
        self.assertEqual(self.hits("2001:db8:12:f901::1 and fd7a:115c:a1e0::/48, 12:34:56\n"), [])
        ts6 = ":".join(["fd7a", "115c", "a1e0", "ab12", "4843", "cd96", "6257", "1234"])
        self.assertEqual(self.hits(f"{ts6}\n"), [("tailnet", ts6)])
        dash = MAC_REAL.replace(":", "-")
        self.assertEqual(self.hits(f"Wi-Fi {dash}\n"), [("network-address", MAC_REAL.lower())])
        # a file's name; a file in UTF-16 and one in gzip
        (self.t / ("notes-for-" + "jo" + ".txt")).write_text("x\n")
        got = [(k, s) for _, _, k, s in pc.check(self.t, [], set(), [("this-machine", "jo", True)])]
        self.assertIn(("this-machine", "jo"), got)
        for f in self.t.iterdir():
            f.unlink()
        (self.t / "w.reg").write_bytes(("\ufeffpath=" + HOME_JO + "\\x\n").encode("utf-16-le"))
        import gzip
        (self.t / "log.gz").write_bytes(gzip.compress(f"mail {MAILBOX}\n".encode()))
        got = sorted((k, s) for _, _, k, s in pc.check(self.t, [], set(), []))
        self.assertEqual(got, [("email", MAILBOX + " (inside its gzip)"), ("home-path", HOME_JO + " (utf-16-le)")])
        # a private word in a compound, and a folder named target
        for f in self.t.iterdir():
            f.unlink()
        self.assertEqual(len(self.hits("zorblax2 here\n", private=["zorblax"])), 1)
        (self.t / "target").mkdir()
        (self.t / "target/x").write_text(f"{MAILBOX}\n")
        self.assertIn(("email", MAILBOX), [(k, s) for _, _, k, s in pc.check(self.t, [], set(), [])])

    def test_a_private_key_in_the_tree(self):
        head = "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----"
        self.assertEqual(self.hits(f"{head}\nlQdGBGc...\n"), [("secret-key", head)])
        self.assertEqual(len(self.hits("-----BEGIN OPENSSH " + "PRIVATE KEY-----\n")), 1)
        self.assertEqual(self.hits("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"), [])     # the a7a-repo key: public
        self.assertEqual(len(self.hits("Key: (" + "private-key (rsa (n #00#))\n")), 1)          # gpg-agent's
        self.assertEqual(len(self.hits("PuTTY-User-Key-" + "File-3: ssh-ed25519\n")), 1)
        for f in self.t.iterdir():
            f.unlink()
        (self.t / "backup.gpg").write_bytes(bytes([0x95, 0x01, 0xd8, 0x04]) + b"\x00" * 40)    # gpg --export-secret-keys
        (self.t / ("signing" + ".key")).write_text("x\n")
        got = sorted((k, s) for _, _, k, s in pc.check(self.t, [], set(), []))
        self.assertEqual(got, [("secret-key", "a file named like a private key"), ("secret-key", "an OpenPGP secret key (binary)")])

    def test_a_private_string_is_never_shown(self):
        got = self.hits("the prefix 77aa:f00d and ZorbLax here\n", private=["", "77aa:f00d", "zorblax"])
        self.assertEqual(got, [("private", "line 2 of the private file"), ("private", "line 3 of the private file")])
        self.assertNotIn("77aa", repr(got))

    def test_a_private_word_is_a_whole_word(self):
        self.assertEqual(self.hits("two zorblaxes of firmware\n", private=["zorblax"]), [])
        self.assertEqual(len(self.hits("one zorblax.\n", private=["zorblax"])), 1)
        # a part of an address is matched anywhere
        self.assertEqual(len(self.hits(f"bssid {MAC_REAL}\n", private=[MAC_REAL[:8]], allow={MAC_REAL.lower()})), 1)

    def test_a_link_by_where_it_points(self):
        os.symlink(HOME_JO + "/secret", self.t / "link")
        os.symlink("nowhere", self.t / "dangling")
        self.assertEqual([(k, s) for _, _, k, s in pc.check(self.t, [], set(), [])], [("home-path", HOME_JO)])

    def test_this_machines_names(self):
        machine = [("this-machine", "jo", True), ("this-machine", "192.168.7.", False)]
        self.assertEqual(self.hits("Jo's voice, board at 192.168.7.20\n", machine=machine),
                         [("this-machine", "Jo"), ("this-machine", "192.168.7.")])
        self.assertEqual(self.hits("a joint, 192.168.70.1\n", machine=machine), [])

    def test_the_command(self):
        (self.t / "a.txt").write_text("nothing here\n")
        r = subprocess.run([sys.executable, str(P / "tools/public-check.py"), str(self.t), "--no-live",
                            "--private", "/nonexistent"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (self.t / "b.txt").write_text(f"cd {HOME_JO}\n")
        r = subprocess.run([sys.executable, str(P / "tools/public-check.py"), str(self.t), "--no-live",
                            "--private", "/nonexistent"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"b.txt:1: home-path: {HOME_JO}", r.stdout)

    def test_this_repository_as_committed(self):
        """The committed tree, minus what the export leaves out, passes the checks that need no private file."""
        if not (P / ".git").exists():
            self.skipTest("not a git checkout")
        stage = self.t / "stage"
        stage.mkdir()
        arc = subprocess.run(["git", "-C", str(P), "archive", "HEAD"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(stage)], input=arc, check=True)
        for line in (P / "tools/public-exclude.list").read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                for hit in stage.glob(line.strip()):
                    shutil.rmtree(hit) if hit.is_dir() else hit.unlink()
        allow = set(s for s in pc.strings_of(P / "tools/public-allow.txt") if s)
        found = pc.check(stage, [], allow, [])
        self.assertEqual(found, [], "\n".join(f"{f}:{n}: {k}: {s}" for f, n, k, s in found))


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.repo = self.t / "dev"
        (self.repo / "tools").mkdir(parents=True)
        for f in ("public-export.sh", "public-check.py", "public-allow.txt"):
            shutil.copy2(P / "tools" / f, self.repo / "tools" / f)
        (self.repo / "tools/public-exclude.list").write_text("# left out\nSESSION.md\nnotes\n")
        (self.repo / "README.md").write_text("a7a-fedora\n")
        (self.repo / "SESSION.md").write_text(f"cd {HOME_JO}/x\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes/mail.txt").write_text(f"from {MAILBOX}\n")
        git(self.repo, "init", "-q", "-b", "master")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dev")
        self.private = self.t / "private.txt"
        self.private.write_text("# mine\nsecretword\n")
        self.out = self.t / "public"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, *extra, private=None):
        env = dict(os.environ, A7A_PRIVATE=str(private or self.private), GIT_CONFIG_GLOBAL="/dev/null",
                   A7A_PUBLIC_AUTHOR="Pub Lic <1+pub@users.noreply.github.com>", HOME=str(self.t))
        return subprocess.run(["bash", str(self.repo / "tools/public-export.sh"), str(self.out), *extra],
                              capture_output=True, text=True, env=env)

    def test_one_commit_by_the_public_name_without_the_left_out_files(self):
        r = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.out / "README.md").exists())
        self.assertFalse((self.out / "SESSION.md").exists())
        self.assertFalse((self.out / "notes").exists())
        log = git(self.out, "log", "--format=%an <%ae>|%cn <%ce>|%ad", "--date=iso").stdout.strip().splitlines()
        self.assertEqual(len(log), 1)
        self.assertTrue(log[0].startswith("Pub Lic <1+pub@users.noreply.github.com>|Pub Lic <1+pub@users.noreply.github.com>|"), log[0])
        self.assertTrue(log[0].endswith("+0000"), log[0])               # the date in UTC: no time zone of the builder
        self.assertNotIn("Dev Person", git(self.out, "log", "--format=%B%an%cn").stdout)
        # the same tree again: no new commit
        r = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no commit", r.stdout)
        self.assertEqual(len(git(self.out, "log", "--format=%h").stdout.split()), 1)

    def test_a_leak_stops_it_and_leaves_the_copy_alone(self):
        self.assertEqual(self.export().returncode, 0)
        (self.repo / "README.md").write_text("a7a-fedora, by SecretWord\n")
        git(self.repo, "commit", "-q", "-am", "leak")
        r = self.export()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("line 2 of the private file", r.stdout)
        self.assertNotIn("SecretWord", r.stdout + r.stderr)
        self.assertEqual((self.out / "README.md").read_text(), "a7a-fedora\n")

    def test_only_committed_files(self):
        (self.repo / "README.md").write_text(f"cd {HOME_JO}\n")         # not committed
        r = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.out / "README.md").read_text(), "a7a-fedora\n")

    def test_never_into_this_repository_or_a_clone_of_it(self):
        """Review 2026-11-24: a folder with a git repository and the script in it was taken, the development repository
        itself among them, and its ignored files (the card backups under output/) deleted."""
        keep = self.repo / "output/backups/card.img"
        keep.parent.mkdir(parents=True)
        keep.write_text("a card\n")
        for out in (self.repo, self.repo.parent):
            self.out = out
            r = self.export()
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("this repository or holds it", r.stdout)
        clone = self.t / "clone"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(clone)], check=True, capture_output=True)
        (clone / "output").mkdir()
        (clone / "output/card.img").write_text("another card\n")
        self.out = clone
        r = self.export()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(keep.exists() and (clone / "output/card.img").exists())
        self.assertEqual(git(self.repo, "log", "--format=%an").stdout.split(), ["Dev", "Person"])

    def test_the_list_as_committed_and_a_glob_that_finds_nothing_is_said(self):
        (self.repo / "tools/public-exclude.list").write_text("README.md\n")      # changed, not committed: not used
        (self.repo / "my notes.md").write_text("notes\n")
        git(self.repo, "add", "my notes.md")
        git(self.repo, "commit", "-q", "-m", "a file with a space")
        git(self.repo, "checkout", "-q", "--", "tools/public-exclude.list")
        (self.repo / "tools/public-exclude.list").write_text("# left out\nSESSION.md\nnotes\nmy notes.md\nOLD-LOG.md\r\n")
        git(self.repo, "commit", "-q", "-am", "list")
        r = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.out / "README.md").exists())
        self.assertFalse((self.out / "my notes.md").exists())
        self.assertIn("'OLD-LOG.md' of public-exclude.list matches nothing", r.stdout)

    def test_a_tracked_file_that_gitignore_names_goes_out(self):
        (self.repo / ".gitignore").write_text("registry.json\n")
        (self.repo / "registry.json").write_text("{}\n")
        git(self.repo, "add", "-f", ".gitignore", "registry.json")
        git(self.repo, "commit", "-q", "-m", "ignored but tracked")
        r = self.export()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("registry.json", git(self.out, "ls-files").stdout.split())

    def test_refusals(self):
        r = self.export(private=self.t / "missing.txt")
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.out.exists())
        other = self.t / "other"
        other.mkdir()
        (other / "keep.txt").write_text("mine\n")
        self.out = other
        r = self.export()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertTrue((other / "keep.txt").exists())


if __name__ == "__main__":
    unittest.main()
