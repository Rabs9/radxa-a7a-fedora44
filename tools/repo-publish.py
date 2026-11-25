#!/usr/bin/env python3
"""repo-publish: the a7a-fedora package repository, for GitHub Pages (https://rabs9.github.io/radxa-a7a-fedora44-packages/),
a repository of its own (on a branch of the source repository its packages would be in every clone of the source).

  tools/repo-publish.py --image output/<image>.img [--out OUT]   the a7a packages of a released image (<image>.img.rpms)
  tools/repo-publish.py <package.rpm>... [--out OUT]              a fix after the release: these packages
  tools/repo-publish.py --first ...                               the very first publish (nothing is published yet)

What boards see is what was pushed, so every run starts from there (review 2026-11-24: a run that trusted its own
folder re-signed everything once that folder was gone, and published whatever lay in it):
 1. OUT (default output/public/radxa-a7a-fedora44-packages) is a git repository this script makes; it fetches the
    published branch (origin, A7A_REPO_REMOTE) and builds the next state in a scratch copy of it. A folder it did not
    make, this repository or a folder holding it is refused. Nothing published yet: only with --first.
 2. The published tree may hold only the repository's own files, each package signed with the key the boards have.
 3. The packages given: file names as rpm names them, no name twice, a7a-fedora's (kernels: --image skips them,
    naming one is refused: a kernel package lacks the modules the image adds, and installonly could make it the boot
    default), each looked through for the builder's details (tools/public-check.py on its files and its whole
    header, `rpm -qp --xml`), then signed (rpmsign; the key of A7A_GNUPGHOME, default ~/.config/a7a-public/gnupg; gpg
    asks for the passphrase; its agent is stopped at the end) and checked: rpmkeys must say "<file>: digests
    signatures OK" with tools/repo/RPM-GPG-KEY-a7a-fedora alone. One published already is kept as it is (its bytes
    are what a board's index names); the same file name with other contents is refused.
 4. The index lists the A7A_REPO_KEEP (2) highest versions of each name (rpm's own comparison). A build the published
    index lists and the new one does not is retired: it stays on the server A7A_REPO_GRACE (2, at least 1) days,
    counted from the first publish built on a pushed state that left it out (retired.txt, in the repository), for
    boards whose index still names it. The previous index's files stay beside the new ones.
 5. Before the commit: every package signed, dnf reads the repository and finds exactly the index, and the whole tree
    is looked through as the public copy of the source is.
 6. ONE commit, no history (the packages would pile up in it), authored by the public identity (A7A_PUBLIC_AUTHOR),
    dated in UTC, through an index file of its own. One run at a time (a lock in OUT/.git); a git lock there older
    than 10 minutes is a killed git's and goes. Never pushed: the push is the owner's, `git -C OUT push --force origin
    main`, right after (the grace counts from the commit).
The published tree is read from git's own listing (no archive: a .gitattributes there could hide files from it):
plain files only (no links), of the names this script writes, none over 150 MB.
Layout: <releasever>/aarch64/ (packages, noarch beside aarch64, and repodata), retired.txt, RPM-GPG-KEY-a7a-fedora,
index.html, .nojekyll. Exit 0 published (or nothing to publish), 1 a check failed, 2 refused."""

from __future__ import annotations

import argparse
import fcntl
import functools
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import rpm

P = Path(__file__).resolve().parent.parent
NAMES = {"a7a-repo", "a7a-extlinux", "a7a-board-tools", "a7a-gpu-tools", "a7a-panel", "mesa-img-a7a", "mesa-pvr-a7a"}
KERNELS = {"kernel-a7a", "kernel-a7apvr"}
OWN = {".nojekyll", "index.html", "RPM-GPG-KEY-a7a-fedora", "retired.txt"}
MARK = "a7a-packages"                    # OUT/.git/<MARK>: the folder is this script's
SOURCE = "https://github.com/Rabs9/radxa-a7a-fedora44"


class Refused(Exception):
    def __init__(self, why: str, code: int = 2):
        super().__init__(why)
        self.code = code


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def must(cmd: list[str], what: str, **kw) -> str:
    r = run(cmd, **kw)
    if r.returncode != 0:
        raise Refused(f"{what}: {(r.stderr or r.stdout).strip()[-400:]}", 1)
    return r.stdout


def query(path: Path, fmt: str) -> str:
    r = run(["rpm", "-qp", "--nosignature", "--qf", fmt, str(path)])
    if r.returncode != 0 or not r.stdout:
        raise Refused(f"{path.name} is not a package rpm can read", 2)
    return r.stdout


def nevra_file(path: Path) -> str:
    return query(path, "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}.rpm")


def evr(path: Path) -> tuple[str, str, str]:
    e, v, r = query(path, "%{EPOCHNUM} %{VERSION} %{RELEASE}").split()
    return e, v, r


def digests(path: Path) -> str:
    """What a package is, whoever signed it: its header's and its payload's digests (both must be there)."""
    d = query(path, "%{SHA256HEADER} %{PAYLOADSHA256}")
    parts = d.split()
    if len(parts) != 2 or any(len(x) != 64 for x in parts):
        raise Refused(f"cannot read the digests of {path.name}", 2)
    return d


class Publish:
    def __init__(self, a, env=os.environ):
        # gpg's passphrase prompt in this terminal
        self.env = dict(env, GPG_TTY=os.ttyname(0)) if sys.stdin.isatty() else dict(env)
        self.out = Path(a.out).absolute()
        self.first = a.first
        self.gnupg = Path(env.get("A7A_GNUPGHOME", Path.home() / ".config/a7a-public/gnupg"))
        self.pub = Path(env.get("A7A_REPO_KEY", P / "tools/repo/RPM-GPG-KEY-a7a-fedora"))
        self.private = Path(env.get("A7A_PRIVATE", Path.home() / ".config/a7a-public/private.txt"))
        self.remote = env.get("A7A_REPO_REMOTE", "https://github.com/Rabs9/radxa-a7a-fedora44-packages.git")
        self.push_url = env.get("A7A_REPO_PUSH", "git@github.com:Rabs9/radxa-a7a-fedora44-packages.git")
        author = env.get("A7A_PUBLIC_AUTHOR", "Rabs9 <241389662+Rabs9@users.noreply.github.com>")
        self.name, _, mail = author.partition(" <")
        self.mail = mail.rstrip(">")
        if not self.name or not self.mail or f"{self.name} <{self.mail}>" != author:
            raise Refused('A7A_PUBLIC_AUTHOR must be "Name <address>"')
        self.rel = env.get("A7A_RELEASEVER", "44")
        try:
            self.keep = int(env.get("A7A_REPO_KEEP", "2"))
            self.grace = int(env.get("A7A_REPO_GRACE", "2"))
            self.now = int(env.get("A7A_REPO_NOW", str(int(time.time()))))      # the tests' clock
        except ValueError:
            raise Refused("A7A_REPO_KEEP, A7A_REPO_GRACE and A7A_REPO_NOW are whole numbers")
        if self.keep < 1 or self.grace < 1:
            # 0 days would take a build out from under every board whose index was fetched before the push
            raise Refused("A7A_REPO_KEEP and A7A_REPO_GRACE must be 1 or more")
        self.inputs = [Path(x).absolute() for x in a.packages]
        self.tmp = Path(tempfile.mkdtemp())

    # ---------------------------------------------------------------- checks before anything is touched
    def check_inputs(self) -> None:
        if not self.inputs:
            raise Refused("no packages given")
        seen = {}
        for r in self.inputs:
            if not r.is_file():
                raise Refused(f"no package {r}")
            name = query(r, "%{NAME}")
            if name in KERNELS:
                raise Refused(f"{name}: kernels are not published (see this script's header)")
            if name not in NAMES:
                raise Refused(f"{name} is not an a7a-fedora package ({r.name})")
            if nevra_file(r) != r.name:
                raise Refused(f"{r.name}: a package's file name must be the one rpm gives it ({nevra_file(r)})")
            if r.name in seen:
                raise Refused(f"{r.name} is given twice ({seen[r.name]} and {r})")
            seen[r.name] = r

    def check_key(self) -> None:
        if not self.pub.is_file():
            raise Refused(f"no {self.pub}: make the key first (tools/repo-key.sh)")
        if not self.private.is_file():
            raise Refused(f"no private file {self.private} (the strings of yours the packages are looked through for)")
        genv = dict(self.env, GNUPGHOME=str(self.gnupg))
        out = run(["gpg", "--show-keys", "--with-colons", str(self.pub)], env=genv).stdout
        fpr = next((l.split(":")[9] for l in out.splitlines() if l.startswith("fpr:")), "")
        if not fpr:
            raise Refused(f"{self.pub} is not a public key gpg can read")
        if "sec:" not in run(["gpg", "--list-secret-keys", "--with-colons", fpr], env=genv).stdout:
            raise Refused(f"{self.gnupg} has no secret key for {fpr} (the key the boards trust)")
        self.fpr = fpr
        for c in ("rpmsign", "createrepo_c", "rpmkeys", "rpm2cpio", "cpio", "dnf5", "git"):
            if not shutil.which(c):
                raise Refused(f"{c} is missing (sudo dnf install rpm-sign createrepo_c)")
        self.db = self.tmp / "db"
        must(["rpmkeys", "--dbpath", str(self.db), "--import", str(self.pub)], "the key does not import")

    def signed(self, f: Path) -> bool:
        return run(["rpmkeys", "--dbpath", str(self.db), "--checksig", str(f)]).stdout.strip() == f"{f}: digests signatures OK"

    def check_out(self) -> None:
        rp, ro = P.resolve(), self.out.resolve()
        if rp == ro or ro in rp.parents:
            raise Refused(f"{self.out} is this repository or holds it")
        if self.out.exists() and any(self.out.iterdir()) and not (self.out / ".git" / MARK).is_file():
            raise Refused(f"{self.out} is not empty and was not made by this script")

    # ---------------------------------------------------------------- what is published
    def git(self, *args, **kw) -> str:
        return must(["git", "-C", str(self.out), *args], f"git {args[0]}", **kw)

    def fetch(self) -> bool:
        """The published branch into refs/remotes/origin/main; False when nothing is published yet."""
        if not (self.out / ".git").is_dir():
            # made whole beside it, then moved into place: a run killed half-way leaves no half-made OUT
            self.out.parent.mkdir(parents=True, exist_ok=True)
            new = Path(tempfile.mkdtemp(prefix=".a7a-packages-", dir=self.out.parent))
            must(["git", "-C", str(new), "init", "-q", "-b", "main"], "git init")
            (new / ".git" / MARK).write_text("made by tools/repo-publish.py\n")
            must(["git", "-C", str(new), "remote", "add", "origin", self.remote], "git remote add")
            if self.out.exists():
                self.out.rmdir()                  # empty: check_out took nothing else
            new.rename(self.out)
        # one run at a time; then a git lock in OUT older than 10 minutes is a killed git's
        self.lock = open(self.out / ".git" / "a7a-publish.lock", "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused("another repo-publish is at work in this OUT")
        for lk in ("index.lock", "HEAD.lock", "config.lock", "packed-refs.lock", "refs/heads/main.lock",
                   "refs/remotes/origin/main.lock"):
            f = self.out / ".git" / lk
            if f.exists():
                if time.time() - f.stat().st_mtime < 600:
                    raise Refused(f"a git command is at work in {self.out} ({lk}); wait, or remove the lock if none is")
                f.unlink()
                print(f"note: removed a stale {lk} in {self.out}")
        # the addresses as configured now (A7A_REPO_REMOTE, A7A_REPO_PUSH)
        self.git("remote", "set-url", "origin", self.remote)
        self.git("remote", "set-url", "--push", "origin", self.push_url)
        heads = run(["git", "-C", str(self.out), "ls-remote", "--heads", "origin", "main"])
        if heads.returncode != 0:
            raise Refused(f"cannot read what is published ({self.remote}): {heads.stderr.strip()[-300:]}", 1)
        if not heads.stdout.strip():
            if not self.first:
                raise Refused("nothing is published yet at origin: the first publish takes --first")
            return False
        if self.first:
            raise Refused("--first, but origin has a published branch already")
        self.git("fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main")
        return True

    def stage_published(self, published: bool) -> None:
        self.stage = self.tmp / "stage"
        self.stage.mkdir()
        self.d = self.stage / self.rel / "aarch64"
        self.d.mkdir(parents=True)
        if published:
            self.export_published()
        if self.d.resolve().parent.parent != self.stage.resolve():
            raise Refused("the published tree's folders are not plain folders", 1)
        for f in self.d.glob("*.rpm"):
            if not self.signed(f):
                raise Refused(f"published {f.name} does not check with {self.pub}", 1)
            if nevra_file(f) != f.name or query(f, "%{NAME}") not in NAMES:
                raise Refused(f"published {f.name} is not a package this script publishes", 1)
        self.published_index = self.index_of(self.d) if published else set()
        self.retired = {}
        rt = self.stage / "retired.txt"
        if rt.exists():
            for line in rt.read_text().splitlines():
                b, _, since = line.partition("\t")
                if b and since.isdigit():
                    # a date before this script or after now is not one it wrote: the grace starts again
                    self.retired[b] = int(since) if 1_700_000_000 <= int(since) <= self.now else self.now
        gone = sorted(b for b in self.published_index | set(self.retired) if not (self.d / b).is_file())
        if gone:
            raise Refused("the published index or retired.txt names packages that are not there (taken out by hand?): "
                          + ", ".join(gone[:10]), 1)
        md = self.d / "repodata"
        self.old_md = {}
        if (md / "repomd.xml").exists():
            # only the file names it lists (no XML parser for a file fetched from the network)
            for href in re.findall(r'href="(repodata/[A-Za-z0-9._-]+)"', (md / "repomd.xml").read_text(errors="replace")):
                if (self.d / href).is_file():
                    self.old_md[Path(href).name] = (self.d / href).read_bytes()

    def export_published(self) -> None:
        """origin/main's files into the stage, from git's own listing: plain files of the names this script writes."""
        listing = must(["git", "-C", str(self.out), "ls-tree", "-r", "-z", "-l", "--full-tree", "refs/remotes/origin/main"],
                       "git ls-tree")
        rpm_name = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~^-]*\.rpm")
        md_name = re.compile(r"repomd\.xml|[0-9a-f]{64}-[a-z_]+\.(?:xml|sqlite)(?:\.(?:zst|gz|xz|bz2))?")
        strange, entries, total = [], [], 0
        for entry in filter(None, listing.split("\0")):
            meta, _, path = entry.partition("\t")
            mode, kind, sha, size = meta.split()
            parts = path.split("/")
            ok = (mode == "100644" and kind == "blob" and size.isdigit() and int(size) <= 150 << 20 and
                  (path in OWN or (len(parts) == 3 and parts[:2] == [self.rel, "aarch64"] and rpm_name.fullmatch(parts[2]))
                   or (len(parts) == 4 and parts[:3] == [self.rel, "aarch64", "repodata"] and md_name.fullmatch(parts[3]))))
            if not ok:
                strange.append(f"{path} ({mode} {size})")
                continue
            total += int(size)
            entries.append((sha, path))
        if strange:
            raise Refused("the published tree holds what this script does not write (take it out on GitHub first): "
                          + ", ".join(strange[:10]), 1)
        if total > 3 << 30:
            raise Refused(f"the published tree is {total >> 20} MB: more than this script ever writes", 1)
        for sha, path in entries:
            dest = self.stage / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                subprocess.run(["git", "-C", str(self.out), "cat-file", "blob", sha], stdout=f, check=True)

    def index_of(self, d: Path) -> set[str]:
        """The file names dnf reads in the repository at d (it must read it)."""
        cache = self.tmp / f"dnf-{len(list(self.tmp.glob('dnf-*')))}"
        r = run(["dnf5", "-q", f"--setopt=cachedir={cache}", f"--setopt=system_cachedir={cache}", "--forcearch=aarch64",
                 f"--repofrompath=a7a-check,file://{d}", "--repo=a7a-check", "--setopt=a7a-check.gpgcheck=0",
                 "repoquery", "--qf", "%{name}-%{version}-%{release}.%{arch}.rpm\n"])
        if r.returncode != 0:
            raise Refused(f"dnf cannot read the repository in {d}: {r.stderr.strip()[-300:]}", 1)
        return set(r.stdout.split())

    # ---------------------------------------------------------------- the packages given
    def look_through(self, files: list[Path], where: Path) -> None:
        """Each in a folder of its own: its files and its whole header; then tools/public-check.py on all of them."""
        for i, r in enumerate(files):
            s = where / f"{i:03d}-{r.name}"
            (s / "files").mkdir(parents=True)
            p2c = subprocess.Popen(["rpm2cpio", str(r)], stdout=subprocess.PIPE)
            c = subprocess.run(["cpio", "-idm", "--quiet", "--no-absolute-filenames"], stdin=p2c.stdout, cwd=s / "files",
                               capture_output=True)
            p2c.wait()
            if p2c.returncode or c.returncode:
                raise Refused(f"{r.name}: cannot unpack it", 1)
            (s / "header.xml").write_text(must(["rpm", "-qp", "--nosignature", "--xml", str(r)], f"{r.name}: its header"))
        c = run([sys.executable, str(P / "tools/public-check.py"), str(where), "--private", str(self.private),
                 "--allow", str(P / "tools/public-allow.txt"), "--repo", str(P)])
        if c.returncode != 0:
            sys.stdout.write(c.stdout)
            raise Refused("something of the builder's is in it (above); nothing published", 1)

    def sign_new(self) -> dict[str, Path]:
        """The given packages not published yet, signed (in the scratch folder until every check has passed)."""
        new = {}
        for r in self.inputs:
            there = self.d / r.name
            if there.exists():
                if digests(there) != digests(r):
                    raise Refused(f"{r.name} is published already with other contents: build it again under a new release")
                print(f"kept    {r.name} (published already)")
                continue
            t = self.tmp / "signed" / r.name
            t.parent.mkdir(exist_ok=True)
            shutil.copy2(r, t)
            must(["rpmsign", "--addsign", "--define", f"_openpgp_sign_id {self.fpr}", "--define", f"_gpg_name {self.fpr}",
                  "--define", f"_gpg_path {self.gnupg}", str(t)], f"{r.name}: rpmsign", env=dict(self.env, GNUPGHOME=str(self.gnupg)))
            if not self.signed(t):
                raise Refused(f"{r.name}: its signature does not check with {self.pub}", 1)
            print(f"signed  {r.name}")
            new[r.name] = t
        return new

    # ---------------------------------------------------------------- the next state
    def plan(self, new: dict[str, Path]) -> tuple[list[str], dict[str, int], list[str], list[str]]:
        """(the index, the retired with their dates, the files to take out, the given ones left out as too low)."""
        files = {f.name: f for f in self.d.glob("*.rpm")}
        files.update(new)
        by_name: dict[str, list[str]] = {}
        for b, f in files.items():
            by_name.setdefault(query(f, "%{NAME}"), []).append(b)
        cmp = functools.cmp_to_key(lambda x, y: rpm.labelCompare(evr(files[x]), evr(files[y])))
        index, retired, out, left = [], {}, [], []
        for name, bs in sorted(by_name.items()):
            bs.sort(key=cmp, reverse=True)
            index += bs[:self.keep]
            for b in bs[self.keep:]:
                if b in new:
                    print(f"left    {b} (lower than the {self.keep} highest: not published)")
                    new.pop(b)
                    left.append(b)
                elif b in self.published_index or b not in self.retired:
                    retired[b] = self.retired.get(b, self.now) if b not in self.published_index else self.now
                    if b in self.published_index:
                        print(f"retired {b} (out of the index; stays {self.grace} days for boards whose index names it)")
                elif self.now - self.retired[b] >= self.grace * 86400:
                    out.append(b)
                    print(f"removed {b} (out of the index since {time.strftime('%Y-%m-%d', time.gmtime(self.retired[b]))})")
                else:
                    retired[b] = self.retired[b]
        return sorted(index), retired, out, left

    def build(self, new: dict[str, Path], index: list[str], retired: dict[str, int], out: list[str]) -> None:
        for b, f in new.items():
            shutil.copy2(f, self.d / b)
        for b in out:
            (self.d / b).unlink()
        (self.stage / "retired.txt").write_text("".join(f"{b}\t{t}\n" for b, t in sorted(retired.items())))
        lst = self.tmp / "pkglist"
        lst.write_text("".join(b + "\n" for b in index))
        shutil.rmtree(self.d / ".repodata", ignore_errors=True)
        # index files older than the published index's go (createrepo_c takes out only those the old repomd lists)
        md = self.d / "repodata"
        if md.is_dir():
            for f in md.iterdir():
                if f.name != "repomd.xml" and f.name not in self.old_md:
                    f.unlink()
        must(["createrepo_c", "-q", "--pkglist", str(lst), str(self.d)], "createrepo_c")
        for n, data in self.old_md.items():        # the previous index beside the new one (a board between the two)
            if n != "repomd.xml" and not (self.d / "repodata" / n).exists():
                (self.d / "repodata" / n).write_bytes(data)
        shutil.copy2(self.pub, self.stage / "RPM-GPG-KEY-a7a-fedora")
        (self.stage / ".nojekyll").write_text("")
        items = "".join(f"<li>{html.escape(b)}</li>" for b in index)
        (self.stage / "index.html").write_text(
            f'<!doctype html><meta charset=utf-8><title>radxa-a7a-fedora44 packages</title>\n'
            f'<h1>radxa-a7a-fedora44 packages</h1><p>The package repository of <a href="{SOURCE}">radxa-a7a-fedora44</a> '
            f'(Fedora 44 for the Radxa Cubie A7A). Boards running the image use it already (package a7a-repo).\n'
            f'<p>Signing key: <a href="RPM-GPG-KEY-a7a-fedora">RPM-GPG-KEY-a7a-fedora</a>, fingerprint <code>{self.fpr}</code>.\n'
            f'<h2>Fedora {self.rel}, aarch64</h2><ul>{items}</ul>\n')

    def verify(self, index: list[str], retired: dict[str, int]) -> None:
        for f in self.d.glob("*.rpm"):
            if f.name not in index and f.name not in retired:
                raise Refused(f"{f.name} would be neither in the index nor retired", 1)
            if not self.signed(f):
                raise Refused(f"{f.name} does not check with {self.pub}", 1)
        seen = self.index_of(self.d)
        if seen != set(index):
            raise Refused(f"dnf reads other packages than the index lists: {sorted(seen ^ set(index))}", 1)
        c = run([sys.executable, str(P / "tools/public-check.py"), str(self.stage), "--private", str(self.private),
                 "--allow", str(P / "tools/public-allow.txt"), "--repo", str(P)])
        if c.returncode != 0:
            sys.stdout.write(c.stdout)
            raise Refused("the repository to publish holds something of the builder's (above)", 1)

    def commit(self) -> str:
        genv = dict(self.env, GIT_INDEX_FILE=str(self.tmp / "git-index"), GIT_DIR=str(self.out / ".git"),
                    GIT_WORK_TREE=str(self.stage), TZ="UTC", GIT_AUTHOR_NAME=self.name, GIT_AUTHOR_EMAIL=self.mail,
                    GIT_COMMITTER_NAME=self.name, GIT_COMMITTER_EMAIL=self.mail)
        must(["git", "add", "-A", "--force", "."], "git add", env=genv, cwd=self.stage)
        tree = must(["git", "write-tree"], "git write-tree", env=genv, cwd=self.stage).strip()
        msg = f"radxa-a7a-fedora44 packages {time.strftime('%Y-%m-%d', time.gmtime(self.now))}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
        commit = must(["git", "commit-tree", tree], "git commit-tree", env=genv, cwd=self.stage, input=msg).strip()
        must(["git", "update-ref", "refs/heads/main", commit], "git update-ref", env=genv, cwd=self.stage)
        must(["git", "symbolic-ref", "HEAD", "refs/heads/main"], "git symbolic-ref", env=genv, cwd=self.stage)
        # OUT's own files follow, for a look before the push (a stale lock there only leaves them behind)
        r = run(["git", "-C", str(self.out), "reset", "-q", "--hard", "main"])
        if r.returncode != 0:
            print(f"note: OUT's files were not updated ({r.stderr.strip()[-200:]}); the commit is made")
        run(["git", "-C", str(self.out), "reflog", "expire", "--expire=now", "--all"])
        run(["git", "-C", str(self.out), "gc", "-q", "--prune=now"])
        return commit

    def main(self) -> int:
        self.check_inputs()
        self.check_key()
        self.check_out()
        published = self.fetch()
        self.stage_published(published)
        self.look_through(self.inputs, self.tmp / "scan")
        new = self.sign_new()
        index, retired, out, left = self.plan(new)
        if not new and not out and retired == self.retired and published and set(index) == self.published_index:
            if left:
                raise Refused(f"{', '.join(left)}: lower than the {self.keep} highest builds of its name; nothing published")
            print("repo-publish: nothing to publish (every package given is published already)")
            return 0
        self.build(new, index, retired, out)
        self.verify(index, retired)
        c = self.commit()
        print(f"repo-publish: {len(index)} packages in the index, committed {c[:7]} in {self.out}; not pushed:\n"
              f"  git -C {self.out} push --force origin main")
        return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("packages", nargs="*")
    ap.add_argument("--image", help="output/<image>.img: its packages (<image>.img.rpms), kernels left out")
    ap.add_argument("--out", default=str(P / "output/public/radxa-a7a-fedora44-packages"))
    ap.add_argument("--first", action="store_true", help="the first publish: nothing is published yet")
    a = ap.parse_args(argv)
    if a.image:
        lst = Path(a.image.removesuffix(".xz") + ".rpms")
        if not lst.is_file():
            print(f"repo-publish: no {lst} (tools/master-image.sh writes it beside the image)", file=sys.stderr)
            return 2
        for b in lst.read_text().split():
            if b.startswith("kernel-"):
                print(f"repo-publish: {b} left out (kernels are not published)")
            else:
                a.packages.append(str(P / "output/rpms" / b))
    pub = None
    try:
        pub = Publish(a)
        return pub.main()
    except Refused as e:
        print(f"repo-publish: {e}", file=sys.stderr)
        return e.code
    finally:
        if pub is not None:
            shutil.rmtree(pub.tmp, ignore_errors=True)
            run(["gpgconf", "--kill", "gpg-agent"], env=dict(os.environ, GNUPGHOME=str(pub.gnupg)))


if __name__ == "__main__":
    sys.exit(main())
