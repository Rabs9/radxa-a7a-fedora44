#!/usr/bin/env python3
"""master-canary: look in a finished image for strings that belong to the owner of the card it was made from.

  tools/master-canary.py <image> --canaries output/master-work/canaries.txt [--fs-offset BYTES]
        [--pristine FILE] [--not-pristine FILE ...] [--report FILE] [--note STRING ...] [--also STRING ...]

The canary file (tools/master-canaries.sh wrote it from the card, "<kind><TAB><string>" per line) holds secrets, so a
hit is reported by kind, by the number of the line in that file and by where it is, never by its text.
  - a kind without prefix is the owner's alone: found, the exit status is 1;
  - a kind "soft:..." may occur by chance (a file name, a word of a command line): a hit never changes the exit
    status; where it was seen is listed for a reader.
A hit inside a package's own file that rpm has just verified (--pristine) is that package's text, whoever else
uses the same string: it is left out for the soft kinds and shown without counting for the others (the sample
address 00:11:22:33:44:55 in a manual is not the owner's network card).
--also STRING is not secret and is shown; found, it counts like a canary of the owner's. --note STRING is shown with
the places it is in and never changes the exit status (traces that are known and accepted).
The whole image file is searched, free space included, with `grep -a -F -o -b`: once for the strings that count and
once for the others (grep reports one match per place, the longest: a passphrase inside a command line that is
itself a soft string would otherwise go unreported). With --fs-offset (the byte offset of the root filesystem) each
hit is mapped to the file its block belongs to (debugfs -c, read-only); --pristine is the list of regular package
files that are checksummed and unchanged (tools/master-audit.py writes it), --not-pristine takes paths out of it.
Where a string was found can itself be the owner's (a directory named by an address, a profile named after the
network): the places go into --report (mode 600) only, the terminal gets kinds and counts.
Exit 0: nothing of the owner's alone was found; 1: something was; 2: the search could not run."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict

BATCH = 2000


def load(path: str):
    """[(kind, string, line number)] of the canary file; what cannot be a grep -F pattern line is left out."""
    out = []
    with open(path, "rb") as f:
        for n, raw in enumerate(f, 1):
            line = raw.rstrip(b"\r\n")
            kind, sep, value = line.partition(b"\t")
            if not sep or len(value) < 8 or b"\0" in value:
                continue
            out.append((kind.decode("ascii", "replace"), value, n))
    return out


def search(image: str, patterns):
    """{pattern bytes: [offsets]} for every pattern that occurs in the image."""
    hits = defaultdict(list)
    if not patterns:
        return hits
    with tempfile.NamedTemporaryFile(prefix="canary-", suffix=".pat") as pf:
        os.chmod(pf.name, 0o600)
        pf.write(b"\n".join(sorted(set(patterns))) + b"\n")
        pf.flush()
        env = dict(os.environ, LC_ALL="C")
        p = subprocess.run(["/usr/bin/grep", "-a", "-F", "-o", "-b", "-f", pf.name, image], capture_output=True, env=env)
    if p.returncode not in (0, 1):
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or f"grep exit {p.returncode}")
    for line in p.stdout.split(b"\n"):
        offset, sep, text = line.partition(b":")
        if sep and offset.isdigit():
            hits[text].append(int(offset))
    return hits


def _debugfs(image: str, fs_offset: int, request: str) -> str:
    p = subprocess.run(["debugfs", "-c", "-R", request, f"{image}?offset={fs_offset}"], capture_output=True, text=True, errors="replace")
    return p.stdout


def block_size(image: str, fs_offset: int) -> int:
    """The filesystem's block size, from its superblock (s_log_block_size, and the ext magic beside it)."""
    with open(image, "rb") as f:
        f.seek(fs_offset + 1024)
        sb = f.read(64)
    if len(sb) < 64 or sb[56:58] != b"\x53\xef":
        raise RuntimeError(f"no ext filesystem at byte {fs_offset} of {image}")
    return 1024 << int.from_bytes(sb[24:28], "little")


def locate(image: str, fs_offset: int, offsets):
    """{offset: where}: the path of the file whose block holds the byte, or a description in brackets for what is not
    a file's data (before the filesystem, its journal, metadata or unallocated space)."""
    BLOCK = block_size(image, fs_offset)
    blocks = sorted({(o - fs_offset) // BLOCK for o in offsets if o >= fs_offset})
    block_inode = {}
    for k in range(0, len(blocks), BATCH):
        for line in _debugfs(image, fs_offset, "icheck " + " ".join(map(str, blocks[k:k + BATCH]))).splitlines():
            p = line.split()
            if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
                block_inode[int(p[0])] = int(p[1])
    inodes = sorted(set(block_inode.values()))
    inode_path = {}
    for k in range(0, len(inodes), BATCH):
        for line in _debugfs(image, fs_offset, "ncheck " + " ".join(map(str, inodes[k:k + BATCH]))).splitlines():
            p = line.split(None, 1)
            if len(p) == 2 and p[0].isdigit():
                inode_path.setdefault(int(p[0]), p[1])
    where = {}
    for o in offsets:
        if o < fs_offset:
            where[o] = "[before the root filesystem: boot firmware, partitions 1 and 2]"
            continue
        inode = block_inode.get((o - fs_offset) // BLOCK)
        if inode is None:
            where[o] = "[filesystem metadata or unallocated space]"
        elif inode == 8:
            where[o] = "[the filesystem's journal]"
        else:
            where[o] = inode_path.get(inode, f"[inode {inode}: no name]")
    return where


def read_paths(path: str):
    with open(path, errors="replace") as f:
        return {line.rstrip("\n") for line in f if line.strip()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--canaries", default=None)
    ap.add_argument("--also", action="append", default=[], help="a string that is not secret; found, it counts; may be repeated")
    ap.add_argument("--note", action="append", default=[], help="a string that is not secret; shown with its places, never counts")
    ap.add_argument("--fs-offset", type=int, default=None, help="byte offset of the root filesystem: hits are mapped to files")
    ap.add_argument("--pristine", default=None, help="paths that packages own, one per line")
    ap.add_argument("--not-pristine", action="append", default=[], help="paths among them whose content is the machine's own")
    ap.add_argument("--report", default=None, help="write the lines here too (mode 600)")
    args = ap.parse_args(argv)
    try:
        canaries = load(args.canaries) if args.canaries else []
        also = [("also", s.encode(), 0) for s in args.also if len(s) >= 6]
        notes = [("note", s.encode(), 0) for s in args.note if len(s) >= 4]
        # the pattern file holds every secret: it must not outlive a kill of this process
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, lambda n, f: sys.exit(128 + n))
        hard = search(args.image, [v for k, v, _ in canaries + also if not k.startswith("soft:")])
        soft = search(args.image, [v for k, v, _ in canaries + notes if k.startswith("soft:") or k == "note"])
        hits = {**soft, **hard}
        where = locate(args.image, args.fs_offset, sorted({o for offs in hits.values() for o in offs})) if args.fs_offset is not None else {}
        pristine = read_paths(args.pristine) if args.pristine else set()
        for p in args.not_pristine:
            pristine -= read_paths(p)
    except (OSError, RuntimeError) as e:
        print(f"the search could not run: {e}")
        return 2

    lines = []
    kinds = Counter(kind for kind, _, _ in canaries)
    hard = sum(n for k, n in kinds.items() if not k.startswith("soft:"))     # (the dict of hits above is not needed any more)
    lines.append((f"{hard} strings of the owner's alone and {len(canaries) - hard} that may occur by chance, of {len(kinds)} kinds, "
                  f"searched in {args.image} ({os.path.getsize(args.image)} bytes)",) * 2)

    def places(offs):
        """'N in <where>; ...' for the offsets, or just the first offsets when nothing maps them."""
        if not where:
            return f"{len(offs)} time(s), first at byte {offs[0]}"
        c = Counter(where[o] for o in offs)
        return "; ".join(f"{n} in {w}" for w, n in c.most_common(6)) + (f"; and {len(c) - 6} more places" if len(c) > 6 else "")

    def outside(offs):
        """The offsets that are not inside a package's own verified file (all of them when nothing can tell)."""
        return [o for o in offs if where.get(o) not in pristine] if where and pristine else offs

    # each line: (what the report gets, what the terminal gets: no place, a place can be the owner's string itself)
    found = soft_seen = 0
    for kind, value, n in canaries:
        offs = hits.get(value)
        if not offs:
            continue
        out = outside(offs)
        if kind.startswith("soft:"):
            if out:
                soft_seen += 1
                lines.append((f"seen {kind}: canary line {n}: {places(out)}", None))
        elif out:
            found += 1
            lines.append((f"FOUND {kind}: canary line {n}: {places(out)}", f"FOUND {kind}: canary line {n}: {len(out)} time(s)"))
        else:
            lines.append((f"package text {kind}: canary line {n}: {places(offs)}",
                          f"package text {kind}: canary line {n}: {len(offs)} time(s), in packages' own verified files only (not counted)"))
    for _, value, _ in also:
        if value in hits:
            found += 1
            lines.append((f"FOUND {value.decode(errors='replace')!r}: {places(hits[value])}",) * 2)
    for _, value, _ in notes:
        if value in hits:
            lines.append((f"known {value.decode(errors='replace')!r}: {places(hits[value])}",) * 2)
    if soft_seen:
        lines.append((f"{soft_seen} string(s) that may occur by chance were seen outside the packages' own files: for a reader to judge",) * 2)
    lines.append(("nothing of the owner's alone is in the image" if not found else f"{found} string(s) of the owner's are in the image",) * 2)
    if args.report:
        fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(full for full, _ in lines) + "\n")
    # without a report the places are all there is: they go to the terminal
    print("\n".join(short if args.report else full for full, short in lines if args.report is None or short is not None))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
