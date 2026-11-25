"""tools/repo/a7a-fedora.repo, tools/repo-key.sh, tools/repo-publish.py: where a board's updates come from, signed."""

import configparser
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
HAVE_PUBLISH = all(shutil.which(c) for c in ("rpmsign", "createrepo_c", "rpmkeys", "rpmbuild", "gpg", "dnf5", "rpm2cpio", "cpio"))
DAY = 86400
T0 = 1_800_000_000                                  # the tests' clock (A7A_REPO_NOW)


class RepoFileTest(unittest.TestCase):
    def test_the_repo_file(self):
        c = configparser.ConfigParser(interpolation=None)
        c.read(P / "tools/repo/a7a-fedora.repo")
        r = c["a7a-fedora"]
        # a repository of its own: on a branch of the source one, its packages would be in every clone of the source
        self.assertEqual(r["baseurl"], "https://rabs9.github.io/radxa-a7a-fedora44-packages/$releasever/$basearch/")
        self.assertEqual(r["enabled"], "1")
        self.assertEqual(r["gpgcheck"], "1")                       # every package signed with the image's key
        self.assertEqual(r["gpgkey"], "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora")
        # offline, or a Fedora release without packages here: Fedora's own updates go on
        self.assertEqual(r["skip_if_unavailable"], "True")

    def test_publish_layout_matches_the_repo_file(self):
        text = (P / "tools/repo-publish.py").read_text()
        self.assertIn('self.d = self.stage / self.rel / "aarch64"', text)
        self.assertIn("https://rabs9.github.io/radxa-a7a-fedora44-packages/", text)
        self.assertIn("https://github.com/Rabs9/radxa-a7a-fedora44-packages.git", text)


def make_key(home: Path) -> Path:
    """A throwaway signing key without a passphrase in a GnuPG home of its own; its public half, armored."""
    home.mkdir(mode=0o700, parents=True)
    env = dict(os.environ, GNUPGHOME=str(home))
    subprocess.run(["gpg", "--batch", "--quiet", "--passphrase", "", "--quick-generate-key", "a7a test <t@example.org>",
                    "ed25519", "sign", "never"], env=env, check=True, capture_output=True)
    pub = home.parent / f"{home.name}-KEY.asc"
    pub.write_bytes(subprocess.run(["gpg", "--armor", "--export"], env=env, check=True, capture_output=True).stdout)
    return pub


def make_rpm(out: Path, name: str, release: str, files: dict, when: int, version: str = "1") -> Path:
    """A small noarch package built here (rpmbuild): its files {path: text}, built "at" `when` (SOURCE_DATE_EPOCH)."""
    top = out / f"build-{name}-{version}-{release}-{when}"
    (top / "SPECS").mkdir(parents=True)
    install = "\n".join(f"install -D -m 644 /dev/stdin %{{buildroot}}{p} <<'EOF'\n{t}\nEOF" for p, t in files.items())
    (top / "SPECS/t.spec").write_text(f"""Name: {name}
Version: {version}
Release: {release}
Summary: test
License: MIT
BuildArch: noarch
%description
test
%install
{install}
%files
{chr(10).join(files)}
""")
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(when))
    subprocess.run(["rpmbuild", "--define", f"_topdir {top}", "--define", "_buildhost a7a-fedora",
                    "--define", "use_source_date_epoch_as_buildtime 1", "-bb", str(top / "SPECS/t.spec")],
                   env=env, check=True, capture_output=True)
    rpm = next((top / "RPMS/noarch").glob("*.rpm"))
    out.mkdir(parents=True, exist_ok=True)
    return Path(shutil.copy2(rpm, out / rpm.name))


def git(*args, check=True) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check,
                          env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_AUTHOR_NAME="x", GIT_AUTHOR_EMAIL="x@example.org",
                                   GIT_COMMITTER_NAME="x", GIT_COMMITTER_EMAIL="x@example.org")).stdout


@unittest.skipUnless(HAVE_PUBLISH, "rpm-sign/createrepo_c/dnf5 not installed (sudo dnf install rpm-sign createrepo_c)")
class PublishTest(unittest.TestCase):
    """The whole publish against a bare repository standing in for GitHub; packages built here, a throwaway key."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.home = self.t / "gnupg"
        self.pub = make_key(self.home)
        self.private = self.t / "private.txt"
        self.private.write_text("# test\nzorblax\n")
        self.remote = self.t / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(self.remote))
        self.out = self.t / "out"
        self.b = [make_rpm(self.t / "in", "a7a-extlinux", str(i), {"/usr/local/bin/a7a-extlinux": f"build {i}"}, T0 + i * 60)
                  for i in (1, 2, 3)]

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, *rpms, first=False, now=T0, out=None, **env):
        e = {**os.environ, "A7A_GNUPGHOME": str(self.home), "A7A_REPO_KEY": str(self.pub), "A7A_PRIVATE": str(self.private),
             "A7A_REPO_REMOTE": str(self.remote), "A7A_REPO_PUSH": str(self.remote), "A7A_REPO_NOW": str(now),
             "GIT_CONFIG_GLOBAL": "/dev/null", **env}
        args = [str(P / "tools/repo-publish.py"), *map(str, rpms), "--out", str(out or self.out)] + (["--first"] if first else [])
        return subprocess.run(["python3", *args], capture_output=True, text=True, env=e, stdin=subprocess.DEVNULL)

    def ok(self, *rpms, **kw):
        r = self.publish(*rpms, **kw)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def push(self, out=None):
        git("-C", str(out or self.out), "push", "-q", "--force", "origin", "main")

    def published(self, path: str) -> bytes:
        return subprocess.run(["git", "-C", str(self.remote), "show", f"main:{path}"], capture_output=True, check=True).stdout

    def published_files(self) -> list:
        return sorted(git("-C", str(self.remote), "ls-tree", "-r", "--name-only", "main").split())

    def index(self, d=None):
        d = d or self.out / "44/aarch64"
        q = subprocess.run(["dnf5", "-q", f"--setopt=cachedir={self.t}/c{len(list(self.t.glob('c*')))}", "--forcearch=aarch64",
                            f"--setopt=system_cachedir={self.t}/cs", f"--repofrompath=t,file://{d}", "--repo=t",
                            "--setopt=t.gpgcheck=0", "repoquery", "--qf", "%{name}-%{version}-%{release}\n"],
                           capture_output=True, text=True)
        return sorted(q.stdout.split())

    def test_signed_two_in_the_index_one_commit(self):
        self.ok(self.b[0], first=True)
        self.push()
        for b in self.b[1:]:
            self.ok(b)
            self.push()
        self.assertEqual(self.index(), ["a7a-extlinux-1-2", "a7a-extlinux-1-3"])
        db = self.t / "keys"
        subprocess.run(["rpmkeys", "--dbpath", str(db), "--import", str(self.pub)], check=True)
        for f in (self.out / "44/aarch64").glob("*.rpm"):
            out = subprocess.run(["rpmkeys", "--dbpath", str(db), "--checksig", str(f)], capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), f"{f}: digests signatures OK")
        log = git("-C", str(self.remote), "log", "--format=%an <%ae>|%ad", "--date=iso", "main").splitlines()
        self.assertEqual(len(log), 1)                                      # no history: the packages would pile up
        self.assertTrue(log[0].startswith("Rabs9 <241389662+Rabs9@users.noreply.github.com>|") and log[0].endswith("+0000"), log[0])
        self.assertEqual(self.published("RPM-GPG-KEY-a7a-fedora"), self.pub.read_bytes())
        self.assertIn("retired.txt", self.published_files())

    def test_the_grace_counts_from_what_was_pushed(self):
        """Review 2026-11-24: three local runs over three days, one push: the build left out was gone at once."""
        self.ok(*self.b[:2], first=True)
        self.push()
        self.ok(self.b[2], now=T0)                                         # b1 leaves the index ...
        self.ok(self.b[2], now=T0 + 3 * DAY)                               # ... not pushed: still listed where boards look
        self.assertTrue((self.out / "44/aarch64" / self.b[0].name).exists())
        self.push()                                                        # now boards stop seeing it: day 3
        r = self.ok(self.b[2], now=T0 + 4 * DAY)
        self.assertIn("nothing to publish", r.stdout)
        r = self.ok(self.b[2], now=T0 + 5 * DAY)                           # two days after the push
        self.assertIn("removed " + self.b[0].name, r.stdout)
        self.push()
        self.assertNotIn("44/aarch64/" + self.b[0].name, self.published_files())

    def test_never_replaced_even_with_out_lost(self):
        """Review 2026-11-24: OUT deleted, the next run signed everything again and the push replaced it."""
        self.ok(self.b[0], first=True)
        self.push()
        before = self.published("44/aarch64/" + self.b[0].name)
        shutil.rmtree(self.out)
        r = self.ok(self.b[0], self.b[1])
        self.assertIn("kept    " + self.b[0].name, r.stdout)
        self.push()
        self.assertEqual(self.published("44/aarch64/" + self.b[0].name), before)
        other = make_rpm(self.t / "other", "a7a-extlinux", "1", {"/usr/local/bin/a7a-extlinux": "else"}, T0 + 60)
        r = self.publish(other)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("published already with other contents", r.stderr)

    def test_files_nobody_published_this_way_are_refused_or_ignored(self):
        """Review 2026-11-24: files put by hand were published unchecked."""
        self.ok(self.b[0], first=True)
        (self.out / "44/aarch64/stray.noarch.rpm").write_bytes(self.b[1].read_bytes())    # in OUT, never pushed
        (self.out / "NOTES.txt").write_text("cd /home/" + "zorblax\n")
        self.push()
        self.ok(self.b[1])
        self.push()
        self.assertEqual([f for f in self.published_files() if "stray" in f or "NOTES" in f], [])
        clone = self.t / "clone"
        git("clone", "-q", str(self.remote), str(clone))
        (clone / "key.asc").write_text("x\n")
        git("-C", str(clone), "add", "key.asc")
        git("-C", str(clone), "commit", "-q", "-m", "by hand")
        git("-C", str(clone), "push", "-q", "origin", "main")
        r = self.publish(self.b[2])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("does not write", r.stderr)

    def test_the_index_by_version_not_build_time(self):
        """Review 2026-11-24: a version 3 built before versions 1 and 2 was retired."""
        v = [make_rpm(self.t / "v", "a7a-board-tools", "1", {"/x": f"v{n}"}, T0 + (10 - n) * 60, version=str(n)) for n in (3, 1, 2)]
        self.ok(*v, first=True)
        self.assertEqual(self.index(), ["a7a-board-tools-2-1", "a7a-board-tools-3-1"])
        self.assertFalse((self.out / "44/aarch64" / v[1].name).exists())   # lower than the two: not published at all

    def test_an_interrupted_run_does_not_block_the_next(self):
        self.ok(self.b[0], first=True)
        self.push()
        (self.out / ".git/index.lock").write_text("")                     # a git killed in OUT, an hour ago
        os.utime(self.out / ".git/index.lock", (time.time() - 3600, time.time() - 3600))
        git("-C", str(self.out), "branch", "a7a-new")                     # the old script killed
        self.ok(self.b[1])
        self.push()
        self.assertIn("44/aarch64/" + self.b[1].name, self.published_files())

    def test_refusals(self):
        self.assertIn("--first", self.publish(self.b[0]).stderr)          # nothing published, no --first
        self.ok(self.b[0], first=True)
        self.push()
        renamed = self.t / "renamed" / "extlinux-fix.rpm"
        renamed.parent.mkdir()
        shutil.copy2(self.b[1], renamed)
        other = self.t / "other"
        other.mkdir()
        (other / "keep.txt").write_text("mine\n")
        cases = [
            (self.publish(self.b[1], first=True), "already"),
            (self.publish(self.b[1], self.b[1]), "given twice"),
            (self.publish(self.b[1], A7A_REPO_GRACE="0"), "1 or more"),
            (self.publish("/etc/os-release"), "is not a package"),
            (self.publish(make_rpm(self.t / "k", "kernel-a7a", "1", {"/boot/x": "k"}, T0)), "kernels are not published"),
            (self.publish(self.b[1], A7A_REPO_KEY=str(self.t / "none.asc")), "tools/repo-key.sh"),
            (self.publish(self.b[1], A7A_GNUPGHOME=str(make_key(self.t / "g2" / "gnupg").parent / "gnupg")), "no secret key"),
            (self.publish(self.b[1], out=P), "this repository"),
            (self.publish(renamed), "the one rpm gives it"),
            (self.publish(self.b[1], out=other), "not made by this script"),
        ]
        for r, why in cases:
            self.assertEqual(r.returncode, 2, why + ": " + r.stdout + r.stderr)
            self.assertIn(why, r.stderr)
        self.assertEqual(os.listdir(other), ["keep.txt"])

    def by_hand(self, change):
        """A commit on the "GitHub" side made by hand: change(clone folder) edits it."""
        clone = self.t / f"hand{len(list(self.t.glob('hand*')))}"
        git("clone", "-q", str(self.remote), str(clone))
        change(clone)
        git("-C", str(clone), "add", "-A", "-f")
        git("-C", str(clone), "commit", "-q", "-m", "by hand")
        git("-C", str(clone), "push", "-q", "--force", "origin", "main")

    def test_links_and_attributes_on_the_published_side_are_refused(self):
        """Review 2026-11-24: a linked folder was written through into OUT; a .gitattributes hid files from the check."""
        self.ok(self.b[0], first=True)
        self.push()
        def link(c):
            shutil.rmtree(c / "44/aarch64")
            os.symlink(str(self.out / "44/aarch64"), c / "44/aarch64")
        self.by_hand(link)
        r = self.publish(self.b[1])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("does not write", r.stderr)
        self.assertEqual(sorted(f.name for f in (self.out / "44/aarch64").glob("*.rpm")), [self.b[0].name])

    def test_a_gitattributes_is_refused(self):
        self.ok(self.b[0], first=True)
        self.push()
        self.by_hand(lambda c: (c / ".gitattributes").write_text("* export-ignore\n"))
        r = self.publish(self.b[1])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(".gitattributes", r.stderr)

    def test_an_index_naming_a_missing_package_is_refused(self):
        """Review 2026-11-24: a package taken out by hand, its index kept: "nothing to publish", boards got a 404."""
        self.ok(*self.b[:2], first=True)
        self.push()
        self.by_hand(lambda c: (c / "44/aarch64" / self.b[1].name).unlink())
        r = self.publish(self.b[0])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not there", r.stderr)

    def test_old_index_files_go_but_the_last_ones_stay(self):
        """Review 2026-11-24: every publish left three more index files behind."""
        builds = [make_rpm(self.t / "many", "a7a-panel", str(i), {"/x": str(i)}, T0 + i, version="1") for i in range(1, 6)]
        self.ok(builds[0], first=True)
        self.push()
        for b in builds[1:]:
            self.ok(b)
            self.push()
        primaries = [f for f in self.published_files() if "-primary.xml" in f]
        self.assertEqual(len(primaries), 2, primaries)                     # this index and the one before it

    def test_a_lower_version_is_said_not_published(self):
        v2 = make_rpm(self.t / "v", "a7a-board-tools", "1", {"/x": "2"}, T0, version="2")
        v3 = make_rpm(self.t / "v", "a7a-board-tools", "1", {"/x": "3"}, T0, version="3")
        v1 = make_rpm(self.t / "v", "a7a-board-tools", "1", {"/x": "1"}, T0, version="1")
        self.ok(v2, v3, first=True)
        self.push()
        r = self.publish(v1)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("lower than", r.stderr)

    def test_stale_locks_go_fresh_ones_stop_it(self):
        self.ok(self.b[0], first=True)
        self.push()
        lock = self.out / ".git/refs/heads/main.lock"
        lock.write_text("")
        r = self.publish(self.b[1])                                        # a git at work right now
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("at work", r.stderr)
        os.utime(lock, (time.time() - 3600, time.time() - 3600))           # a killed one's, an hour old (real time)
        r = self.ok(self.b[1])
        self.assertIn("removed a stale refs/heads/main.lock", r.stdout)

    def test_a_retirement_date_it_did_not_write_starts_the_grace_again(self):
        self.ok(*self.b[:2], first=True)
        self.push()
        self.ok(self.b[2], now=T0)
        self.push()
        self.by_hand(lambda c: (c / "retired.txt").write_text(f"{self.b[0].name}\t0\n"))
        r = self.ok(self.b[2], now=T0 + DAY)
        self.assertNotIn("removed", r.stdout)
        self.assertIn("44/aarch64/" + self.b[0].name, self.published_files())

    def test_a_package_with_something_of_the_builders_is_refused(self):
        self.ok(self.b[0], first=True)
        self.push()
        home = "/home/" + "zorblax"
        leak = make_rpm(self.t / "leak", "a7a-board-tools", "9", {"/usr/local/bin/x": f"cd {home}/src"}, T0)
        r = self.publish(leak)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("home-path", r.stdout)
        self.assertNotIn("a7a-board-tools", " ".join(self.published_files()))


if __name__ == "__main__":
    unittest.main()
