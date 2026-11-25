#!/usr/bin/env python3
"""public-check: look in a tree that is about to be made public for what belongs to the person who builds it.

  tools/public-check.py <tree> [--private FILE] [--allow FILE] [--repo DIR] [--no-live]

What counts (each hit is reported as file:line and its kind; the exit status is 1 if there is any). Every file is
read as bytes, and also as the text inside it where that is UTF-16 or gzip; a file's path is checked as line 0.
  email          an e-mail address, except made-up ones (made_up()) and those in the allow file
  network-address a hardware address (aa:bb:cc:dd:ee:ff, also with dashes), except the all-zero and broadcast ones
                 and those in the allow file (made-up ones in tests)
  tailnet        an address of Tailscale's ranges: IPv4 100.64.0.0/10 (also carrier NAT), IPv6 fd7a:115c:a1e0::/48
  ipv6           a global IPv6 address or prefix (2000::/3), except the documentation range 2001:db8::/32
  home-path      /home/<name>/ for a name other than radxa (the image's account) or a placeholder
  secret-key     a private key: armored (PGP, OpenSSH, PEM), gpg-agent's key files (their S-expression), PuTTY's, a binary OpenPGP
                 secret key at a file's start, or a file named like one (secring.gpg, private-keys-v1.d/, *.key, *.ppk,
                 id_rsa ...): a key backup in the tree (reviews 2026-11-24)
  this-machine   something of the machine this runs on (--no-live leaves these out, for tests): its host name, its
                 user's login and real names (as words), its hardware addresses, the first three numbers of its
                 private IPv4 addresses, its global IPv6 prefixes, its Tailscale addresses and its peers' names, its
                 saved WiFi networks' names, the hosts of ~/.ssh/config, git's user.name/user.email (global, and
                 --repo's own)
  private        a line of the --private file (one string per line, compared without case; a single word as a
                 whole word, letters around it not; # starts a comment). That file is the builder's own and lives
                 outside the repository (default ~/.config/a7a-public/private.txt): its strings are never printed,
                 only their line numbers.
The allow file (default tools/public-allow.txt) lists exact strings that are fine to publish, one per line: a hit
whose text is one of them is not one.
Exit 0: nothing found; 1: something was; 2: the check could not run."""

from __future__ import annotations

import argparse
import gzip
import ipaddress
import json
import os
import pwd
import re
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MAC = re.compile(rb"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}(?![0-9A-Fa-f:-])")
IP4 = re.compile(rb"(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])")
# an IPv6 address or prefix as written: groups of hex digits with at least two colons ("2001:db8:1:2::", "fd7a:115c:a1e0:")
IP6 = re.compile(rb"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{1,4}:){2,7}(?::?[0-9A-Fa-f]{1,4}|:)?(?:(?::[0-9A-Fa-f]{1,4}){1,6})?(?![0-9A-Za-z.])")
HOME = re.compile(rb"/home/([A-Za-z0-9._-]+)")
SECRET = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
                    rb"|\((?:\d+:)?(?:protected-)?private-key\b|PuTTY-User-Key-File-\d")
SECRET_NAME = re.compile(r"(?:^|/)(?:secring\.(?:gpg|kbx)|private-keys-v1\.d/.*|[^/]+\.(?:key|ppk)|id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?)$")


def binary_secret_key(data: bytes) -> bool:
    """A file that begins with an OpenPGP secret-key packet (`gpg --export-secret-keys` without --armor): tag 5 in
    the old format (0x94-0x97) or the new (0xc5), its length, then key version 3 to 6."""
    if len(data) < 8:
        return False
    b = data[0]
    if 0x94 <= b <= 0x97:
        at = {0x94: 2, 0x95: 3, 0x96: 5, 0x97: 1}[b]
    elif b == 0xC5:
        at = 2 if data[1] < 192 else 3 if data[1] < 224 else 6
    else:
        return False
    return data[at] in (3, 4, 5, 6)
HOME_OK = {b"radxa", b"user", b"you", b"yourname", b"USER", b"$USER", b"me", b"someone", b"owner", b"name", b"plasmalogin",
           b"linuxbrew", b"builder", b"other", b"sam"}
MAC_OK = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}
TAILNET = ipaddress.ip_network("100.64.0.0/10")
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
GLOBAL6 = ipaddress.ip_network("2000::/3")
DOC6 = ipaddress.ip_network("2001:db8::/32")
SKIP_DIRS = {".git", "__pycache__"}
UNIT_SUFFIXES = (".service", ".socket", ".target", ".timer", ".mount", ".path", ".slice", ".scope", ".device")
MADE_UP = (".example", ".test", ".invalid", ".localhost")
# names a machine or login of anyone's may have (root owns every packaged file): taken from this machine's login,
# peers or ssh hosts they would stop every export
COMMON = {"root", "user", "radxa", "admin", "builder", "localhost", "fedora", "ubuntu", "debian", "raspberrypi", "pop-os", "parrot", "android", "phone", "laptop",
          "desktop", "server", "radxa", "radxa-cubie-a7a", "github.com", "gitlab.com", "codeberg.org", "ubuntu-phablet",
          "device-of-shared-to-user", "orangepi", "armbian"}
WORD = (rb"(?<![A-Za-z0-9])", rb"(?![A-Za-z0-9])")        # a name of this machine's: not inside a longer word or number
PRIVATE_WORD = (rb"(?<![A-Za-z])", rb"(?![A-Za-z])")      # a private word: "word2" counts, "words" does not


def made_up(e: str) -> bool:
    """An address that is nobody's: GitHub's no-reply ones, the reserved example names (example.com, x.example,
    .test, .invalid), systemd's instance units ("serial-getty@ttyAS0.service") and the image's own board
    (radxa@radxa-cubie-a7a.local). Not .local otherwise: that is a real machine's name on someone's network."""
    e = e.lower()
    domain = e.rsplit("@", 1)[-1]
    return (domain in ("users.noreply.github.com", "radxa-cubie-a7a.local") or domain.endswith(UNIT_SUFFIXES)
            or domain.endswith(MADE_UP) or domain.split(".")[0] == "example")


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def prefix6(a: ipaddress.IPv6Address) -> list[str]:
    """How a /64 of `a` is written: its four groups without their leading zeros, and its last two (the part that is
    the owner's, after the provider's)."""
    groups = [g.lstrip("0") or "0" for g in a.exploded.split(":")[:4]]
    return [":".join(groups) + ":", ":".join(groups[2:4])]


def live(repo: Path | None = None) -> list[tuple[str, str, bool]]:
    """(kind, string, whole word?) for the machine this runs on."""
    out: list[tuple[str, str, bool]] = []

    def add(s: str, word: bool, shortest: int = 4):
        s = s.strip()
        if len(s) >= shortest and s.lower() not in COMMON:
            out.append(("this-machine", s, word))

    add(socket.gethostname().split(".")[0], True)
    try:
        me = pwd.getpwuid(os.getuid())
        add(me.pw_name, True, 3)
        for part in re.split(r"[\s,]+", me.pw_gecos):         # the real name, word by word
            add(part, True, 3)
    except KeyError:
        pass
    add(os.environ.get("USER") or "", True, 3)
    for p in Path("/sys/class/net").glob("*/address"):
        try:
            m = p.read_text().strip().lower()
        except OSError:
            continue
        if m not in MAC_OK:
            add(m, False)
    for line in run(["ip", "-o", "addr"]).splitlines():
        f = line.split()
        try:
            a = ipaddress.ip_interface(f[3]).ip
        except (IndexError, ValueError):
            continue
        if a.version == 4 and a.is_private and not a.is_loopback and not a.is_link_local:
            add(".".join(str(a).split(".")[:3]) + ".", False)
        elif a.version == 4 and a in TAILNET:
            add(str(a), False)
        elif a.version == 6 and a in GLOBAL6:
            for s in prefix6(a):
                add(s, False)
    for line in run(["tailscale", "ip"]).splitlines():
        add(line, False)
    try:
        peers = json.loads(run(["tailscale", "status", "--json"]) or "{}").get("Peer", {}) or {}
        for p in peers.values():
            add(p.get("HostName", ""), True)
            add((p.get("DNSName", "") or "").split(".")[0], True)
    except (ValueError, AttributeError):
        pass
    for line in run(["nmcli", "-g", "NAME,TYPE", "connection", "show"]).splitlines():
        name, _, kind = line.rpartition(":")
        if "wireless" in kind:
            add(name.replace("\\:", ":"), True)
    try:
        for line in (Path.home() / ".ssh/config").read_text(errors="replace").splitlines():
            f = line.split()
            if len(f) >= 2 and f[0].lower() in ("host", "hostname") and not any(c in f[1] for c in "*?!"):
                add(f[1], True, 6)
    except OSError:
        pass
    for key in ("user.name", "user.email"):
        add(run(["git", "config", "--global", key]), key == "user.name")
        if repo:
            add(run(["git", "-C", str(repo), "config", "--local", key]), key == "user.name")
    seen, uniq = set(), []
    for k, s, w in out:
        if s.lower() not in seen:
            seen.add(s.lower())
            uniq.append((k, s, w))
    return uniq


def strings_of(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    lines = []
    for raw in path.read_text(errors="replace").splitlines():
        s = raw.strip()
        lines.append("" if not s or s.startswith("#") else s)
    return lines


def files(tree: Path):
    for root, dirs, names in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            p = Path(root) / n
            if p.is_symlink() or p.is_file():
                yield p


def texts(data: bytes) -> list[tuple[str, bytes]]:
    """The bytes, and the text inside them: decompressed when gzip, decoded when UTF-16 (a NUL in every other byte)."""
    out = [("", data)]
    if data[:2] == b"\x1f\x8b":
        try:
            out.append((" (inside its gzip)", gzip.decompress(data)[:64 << 20]))
        except (OSError, EOFError):
            pass
    head = data[:4096]
    if len(head) >= 4 and (head.count(b"\x00") * 3 > len(head) or head[:2] in (b"\xff\xfe", b"\xfe\xff")):
        for enc in ("utf-16-le", "utf-16-be"):
            out.append((f" ({enc})", data.decode(enc, errors="ignore").encode("utf-8", errors="ignore")))
    return out


def check(tree: Path, private: list[str], allow: set[str], machine: list[tuple[str, str, bool]]):
    """[(relative path, line number, kind, shown text)]; a private string is shown by its line in the file only."""
    allow_l = {a.lower() for a in allow}

    def rx(s: str, around) -> re.Pattern:
        return re.compile(around[0] + re.escape(s.encode()) + around[1], re.IGNORECASE)
    fixed = [(k, rx(s, WORD) if w else rx(s, (b"", b"")), None) for k, s, w in machine]
    fixed += [("private", rx(s, PRIVATE_WORD) if re.fullmatch(r"[A-Za-z0-9_-]+", s) else rx(s, (b"", b"")),
               f"line {i + 1} of the private file") for i, s in enumerate(private) if s]
    hits = []

    def scan(rel: str, no: int, line: bytes, note: str = ""):
        def hit(kind: str, text: str):
            if text.lower() not in allow_l:
                hits.append((rel, no, kind, text + note))
        for m in EMAIL.finditer(line):
            e = m.group().decode(errors="replace")
            if not made_up(e):
                hit("email", e)
        for m in MAC.finditer(line):
            v = m.group().decode().lower().replace("-", ":")
            if v not in MAC_OK:
                hit("network-address", v)
        for m in IP4.finditer(line):
            try:
                a = ipaddress.ip_address(m.group().decode())
            except ValueError:
                continue
            if a in TAILNET and str(a) != "100.64.0.0":
                hit("tailnet", str(a))
        for m in IP6.finditer(line):
            s = m.group().decode()
            a = None
            for cand in (s, s.rstrip(":") + "::", s + ":"):
                try:
                    a = ipaddress.IPv6Address(cand)
                    break
                except ValueError:
                    continue
            if a is None or s.count(":") < 2:
                continue
            if a in TAILNET6 and a != TAILNET6.network_address:      # the range itself is Tailscale's, public
                hit("tailnet", s)
            elif a in GLOBAL6 and a not in DOC6 and len(s.replace(":", "")) >= 8:
                hit("ipv6", s)
        if SECRET.search(line):
            hit("secret-key", SECRET.search(line).group().decode())
        for m in HOME.finditer(line):
            name = m.group(1).rstrip(b".")                  # "/home/radxa." at the end of a sentence
            if name and name not in HOME_OK:
                hit("home-path", m.group().decode(errors="replace"))
        for kind, pattern, shown in fixed:
            f = pattern.search(line)
            if f:
                hit(kind, shown or f.group().decode(errors="replace"))

    for p in files(tree):
        rel = str(p.relative_to(tree))
        scan(rel, 0, rel.encode(errors="replace"))
        if SECRET_NAME.search(rel) and rel.lower() not in allow_l:
            hits.append((rel, 0, "secret-key", "a file named like a private key"))
        try:
            # a link is judged by where it points ("../../home/<name>/x"), never followed
            data = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
        except OSError as e:
            hits.append((rel, 0, "unreadable", str(e)))
            continue
        if not p.is_symlink() and binary_secret_key(data):
            hits.append((rel, 0, "secret-key", "an OpenPGP secret key (binary)"))
        for note, text in texts(data):
            for no, line in enumerate(text.split(b"\n"), 1):
                scan(rel, no, line, note)
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tree")
    ap.add_argument("--private", default=os.path.expanduser("~/.config/a7a-public/private.txt"))
    ap.add_argument("--allow", default=str(HERE / "public-allow.txt"))
    ap.add_argument("--repo", help="the repository the tree comes from: its own git identity counts too")
    ap.add_argument("--no-live", action="store_true", help="leave out this machine's own names and addresses")
    a = ap.parse_args(argv)
    tree = Path(a.tree)
    if not tree.is_dir():
        print(f"public-check: {tree} is not a directory", file=sys.stderr)
        return 2
    allow = set(s for s in strings_of(Path(a.allow)) if s)
    hits = check(tree, strings_of(Path(a.private)), allow, [] if a.no_live else live(Path(a.repo) if a.repo else None))
    for rel, no, kind, text in hits:
        print(f"{rel}:{no}: {kind}: {text}")
    print(f"public-check: {len(hits)} found in {tree}" if hits else f"public-check: nothing found in {tree}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
