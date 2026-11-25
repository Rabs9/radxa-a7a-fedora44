#!/usr/bin/env python3
"""master-audit: everything in an image that no package accounts for must be on the list of what is let in.

  tools/master-audit.py --allow tools/master-allow.rules --owned FILE --present FILE [--verify FILE] [--lists DIR]
                        [--min-paths N] [--complete]

tools/master-filter.rules says what of a used card stays behind; whatever it does not name is copied. This is the
other half. From three listings made inside the image (tools/master-image.sh):
  --owned    `rpm -qa --qf '[%{FILEFLAGS} %{FILEVERIFYFLAGS} %{FILENAMES}\\n]'`, paths as they are on disk
  --present  `find / -xdev -printf '%y %n %p\\n'` (type, number of links, path), every type of file
  --verify   `rpm -Va`
it derives, and compares path by path with the patterns of the same section of the allow file:
  [unowned]     files, links, directories, sockets, pipes and device nodes no package owns (a directory that merely
                holds package files is fine: its name is the package's)
  [ghost]       %ghost files and links that exist: a package names them, their content is the machine's own
  [unverified]  package files whose content rpm does not checksum (%verify(not md5), most %config of that kind:
                /etc/passwd, /etc/hosts, cups' configuration...): they can hold anything and `rpm -V` stays silent
  [changed]     package files whose content or link target is no longer the package's
A path nothing matches fails the run and is printed: somebody then decides whether it stays behind (a filter rule)
or is known and wanted (a pattern). A card with something new on it stops the build instead of being published.
A package file whose owner or group is not the package's fails without a pattern to excuse it.
--lists DIR: the derived lists are written there (unowned-files.txt, unowned-dirs.txt, ghost-files.txt,
unverified-files.txt, changed-files.txt, and pristine-files.txt: regular package files that are checksummed and
unchanged, the only places where a string is certainly the package's and not the owner's).
--complete: each listing must end with the line "#done" (the command that made it ran to its end).

The allow file: sections as above; one pattern per line; # comments.
  *  anything within one path component     **  anything, any depth     a pattern ending in /** also matches the
  directory itself.
  d:/path  l:/path  f:/path   the pattern is for directories, links or regular files only (letters combine: dl:).
  A name with an "@" in it (the instance of a unit: wg-quick@office.service) is only matched by a pattern that
  spells an "@" itself: what stands behind the @ is somebody's own name for something.
Exit 0: every path is accounted for; 1: some are not; 2: usage, unreadable or implausible listings."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

SECTIONS = ("unowned", "ghost", "unverified", "changed")
SHOWN = 60
GHOST = 64                                      # RPMFILE_GHOST
DIGEST = 1                                      # RPMVERIFY_FILEDIGEST
# where tools/priv.sh's sandbox mounts its own filesystems over the image's directories: what rpm -V says about
# their modes and owners is about the sandbox
SANDBOX = ("/", "/proc", "/sys", "/dev", "/run", "/tmp")


def to_regex(pattern: str) -> re.Pattern:
    """The pattern as a regular expression over a whole path."""
    tail = ""
    if pattern.endswith("/**"):
        pattern, tail = pattern[:-3], "(/.*)?"
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + tail, re.DOTALL)


def load_allow(path: str):
    """{section: [(pattern text, types or None, regex)]}; a pattern outside a known section is an error."""
    allow, section = defaultdict(list), None
    with open(path) as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            m = re.fullmatch(r"\[([a-z]+)\]", line)
            if m:
                section = m.group(1)
                if section not in SECTIONS:
                    raise ValueError(f"{path}:{n}: no such section [{section}] (have: {', '.join(SECTIONS)})")
                continue
            m = re.fullmatch(r"(?:([dlf]+):)?(/.*)", line)
            if section is None or not m:
                raise ValueError(f"{path}:{n}: {line!r} is not a pattern inside a section (patterns are absolute paths)")
            allow[section].append((line, m.group(1), to_regex(m.group(2))))
    return allow


def audit(entries, patterns):
    """(paths nothing matches, patterns that matched nothing) for [(type, path)]."""
    used, loose = set(), []
    for kind, p in entries:
        at = "@" in os.path.basename(p)
        for text, types, rx in patterns:
            if types and kind not in types:
                continue
            if at and "@" not in text:
                continue
            if rx.fullmatch(p):
                used.add(text)
                break
        else:
            loose.append(p)
    return loose, [text for text, _, _ in patterns if text not in used]


def lines_of(path: str):
    """(the lines of a listing, whether its last line is the "#done" of a command that ran to its end)."""
    with open(path, errors="replace") as f:
        lines = f.read().split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    done = bool(lines) and lines[-1] == "#done"
    return (lines[:-1] if done else lines), done


def read_owned(path: str):
    """({path: (flags, verify flags)} of everything packages name, the directories above those paths, done)."""
    owned, above = {}, set()
    lines, done = lines_of(path)
    for line in lines:
        p = line.split(" ", 2)
        if len(p) != 3 or not p[0].isdigit() or not p[1].isdigit() or not p[2].startswith("/"):
            continue
        flags, verify, name = int(p[0]), int(p[1]), p[2]
        # a path several packages name is a ghost only if all of them say so, and unverified only if none verifies it
        old = owned.get(name)
        owned[name] = (flags, verify) if old is None else (old[0] & flags | (old[0] | flags) & ~GHOST, old[1] | verify)
        d = os.path.dirname(name)
        while d not in above:
            above.add(d)
            if d == "/":
                break
            d = os.path.dirname(d)
    return owned, above, done


def read_present(path: str):
    """([(type, links, path)] of what is in the image, done)."""
    out = []
    lines, done = lines_of(path)
    for line in lines:
        p = line.split(" ", 2)
        if len(p) == 3 and p[1].isdigit() and p[2].startswith("/"):
            out.append((p[0], int(p[1]), p[2]))
    return out, done


def read_verify(path: str):
    """(paths whose content or link target changed, 'flags path' lines whose owner or group changed, done) of an
    `rpm -Va`."""
    changed, owner = [], []
    lines, done = lines_of(path)
    for line in lines:
        m = re.match(r"^([S.?][M.?][5.?][D.?][L.?][U.?][G.?][T.?][P.?])\s+(?:[a-z]\s+)?(/.*)$", line)
        if not m:
            continue                      # "missing ..." lines: a file that is not there carries nothing
        flags, p = m.group(1), m.group(2)
        if flags[2] in "5?" or flags[4] in "L?":      # "?": rpm could not read it, so nothing says it is unchanged
            changed.append(p)
        if (flags[5] == "U" or flags[6] == "G") and p not in SANDBOX:
            owner.append(f"{flags} {p}")
    return sorted(set(changed)), owner, done


def derive(owned, above, present, changed):
    """The lists the sections are about, as [(type, path)], and the regular package files that are checksummed and
    unchanged."""
    files, dirs, ghosts, unverified, pristine = [], [], [], [], []
    changed = set(changed)
    for kind, _, p in present:
        if p in owned:
            flags, verify = owned[p]
            if kind == "d":
                continue
            if flags & GHOST:
                ghosts.append((kind, p))
            elif kind == "f" and not verify & DIGEST:
                unverified.append((kind, p))
            elif kind == "f" and p not in changed:
                pristine.append(p)
            continue
        if kind == "d":
            if p not in above and p not in SANDBOX:
                dirs.append((kind, p))
        else:
            files.append((kind, p))               # links, regular files, and sockets, pipes, device nodes alike
    return sorted(files, key=lambda e: e[1]), sorted(dirs, key=lambda e: e[1]), sorted(ghosts, key=lambda e: e[1]), \
        sorted(unverified, key=lambda e: e[1]), sorted(pristine)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--allow", required=True)
    ap.add_argument("--owned", required=True)
    ap.add_argument("--present", required=True)
    ap.add_argument("--verify", default=None)
    ap.add_argument("--lists", default=None, help="a directory for the derived lists")
    ap.add_argument("--min-paths", type=int, default=1000, help="fewer paths than this in a listing: nothing is judged")
    ap.add_argument("--complete", action="store_true", help='every listing must end with the line "#done"')
    args = ap.parse_args(argv)
    try:
        allow = load_allow(args.allow)
        owned, above, done_o = read_owned(args.owned)
        present, done_p = read_present(args.present)
        changed, owner, done_v = read_verify(args.verify) if args.verify else ([], [], True)
    except (OSError, ValueError) as e:
        print(f"master-audit: {e}")
        return 2
    if len(owned) < args.min_paths or len(present) < args.min_paths:
        print(f"master-audit: implausible listings ({len(owned)} package paths, {len(present)} paths in the image, "
              f"at least {args.min_paths} expected of each): nothing was judged")
        return 2
    if args.complete and not (done_o and done_p and done_v):
        cut = [n for n, d in (("--owned", done_o), ("--present", done_p), ("--verify", done_v)) if not d]
        print(f"master-audit: {', '.join(cut)} did not end with #done: the command that made it did not finish; nothing was judged")
        return 2
    files, dirs, ghosts, unverified, pristine = derive(owned, above, present, changed)
    # a changed file that is a ghost or unverified one is judged in that section; here: what rpm -V itself reports
    lists = {"unowned": files + dirs, "ghost": ghosts, "unverified": unverified, "changed": [("f", p) for p in changed]}
    if args.lists:
        for name, content in (("unowned-files.txt", [p for _, p in files]), ("unowned-dirs.txt", [p for _, p in dirs]),
                              ("ghost-files.txt", [p for _, p in ghosts]), ("unverified-files.txt", [p for _, p in unverified]),
                              ("changed-files.txt", changed), ("pristine-files.txt", pristine)):
            with open(os.path.join(args.lists, name), "w") as f:
                f.write("".join(p + "\n" for p in content))
    bad = 0
    for s in SECTIONS:
        if s == "changed" and not args.verify:
            continue
        entries = lists[s]
        loose, unused = audit(entries, allow[s])
        print(f"{s}: {len(entries)} paths, {len(entries) - len(loose)} accounted for by {len(allow[s]) - len(unused)} of {len(allow[s])} patterns")
        if loose:
            bad += len(loose)
            print(f"  NOT ACCOUNTED FOR ({len(loose)}): leave them behind (tools/master-filter.rules) or let them in ([{s}] in {args.allow})")
            for p in loose[:SHOWN]:
                print(f"    {p!r}" if "\n" in p or "\r" in p else f"    {p}")
            if len(loose) > SHOWN:
                print(f"    ... and {len(loose) - SHOWN} more")
        # a pattern nothing needed is a door left open: say so, do not fail (another card, another package set)
        for text in unused:
            print(f"  note: [{s}] {text} matched nothing")
    if owner:
        bad += len(owner)
        print(f"  PACKAGE FILES WHOSE OWNER OR GROUP IS NOT THE PACKAGE'S ({len(owner)}):")
        for p in owner[:SHOWN]:
            print(f"    {p}")
    if bad:
        print(f"{bad} path(s) in the image that nothing accounts for")
        return 1
    print("every path that no package accounts for is on the list")
    return 0


if __name__ == "__main__":
    sys.exit(main())
