#!/usr/bin/env python3
"""standard-dtb: the device tree of the "standard" image, made from the one the kernel package ships.

  tools/standard-dtb.py <kernel's.dtb> <vendor.dtb> <out.dtb>

The kernel packages carry the owner's overclocked device tree: operating points above the vendor's ceiling (CPU to
2800/3000 MHz where the vendor's tables end at 1800/2002, GPU points above 1008 MHz) and thermal trip points moved
to suit them. The standard image runs the same kernels at the vendor's clocks. Its device tree is the package's own,
proven one with exactly two kinds of change, both taken from a device tree compiled from the vendor's unmodified
sources (tools/standard-dtb.sh makes that reference):
  - every operating point (a node opp@<Hz>) that the vendor's table does not have is removed;
  - `temperature` and `hysteresis` of the thermal trip points are set to the vendor's values.
Everything else stays as the package has it: the RGMII delay that makes gigabit ethernet work on this board, the GPU's
boot clock (1008 MHz, the vendor's own top operating point), the bindings the kernel of that release expects.

The result is checked before it is written: no node may be left that the vendor's tree lacks, every operating point
that remains must be the vendor's property for property, every trip point must have the vendor's limits, and the
removed nodes must not be referred to from anywhere. What still differs from the vendor's tree is printed.
Needs fdtput (package dtc). Exit 0: written; 1: the trees differ in a way this tool does not understand; 2: usage."""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys

OPP = re.compile(r"/opp@\d+$")
TRIP_PROPS = ("temperature", "hysteresis")


def parse(path: str):
    """{node path: {property: bytes}} of a flattened device tree."""
    with open(path, "rb") as f:
        d = f.read()
    magic, _, off_struct, off_strings = struct.unpack(">IIII", d[:16].ljust(16, b"\0"))
    if magic != 0xD00DFEED:
        raise ValueError(f"{path} is not a device tree blob")
    nodes, stack, p = {}, [], off_struct

    def name_at(o):
        return d[off_strings + o:d.index(b"\0", off_strings + o)].decode()

    while True:
        tok = struct.unpack(">I", d[p:p + 4])[0]
        p += 4
        if tok == 1:
            e = d.index(b"\0", p)
            stack.append(d[p:e].decode())
            p = (e + 4) & ~3
            nodes["/" + "/".join(stack[1:])] = {}
        elif tok == 2:
            stack.pop()
        elif tok == 3:
            ln, no = struct.unpack(">II", d[p:p + 8])
            p += 8
            nodes["/" + "/".join(stack[1:])][name_at(no)] = d[p:p + ln]
            p = (p + ln + 3) & ~3
        elif tok == 9:
            return nodes
        elif tok != 4:
            raise ValueError(f"{path}: unknown token {tok} in the structure block")


def cells(v: bytes):
    return struct.unpack(f">{len(v) // 4}I", v) if v is not None and len(v) % 4 == 0 else None


def plan(ours, vendor):
    """(operating points to remove, [(node, property, value)] to set), or ValueError when the trees differ in a way
    that is not an added operating point."""
    extra = sorted(set(ours) - set(vendor))
    remove = [n for n in extra if OPP.search(n) and n.rsplit("/", 1)[0] in vendor]
    other = [n for n in extra if n not in remove and not any(n.startswith(r + "/") for r in remove)]
    if other:
        raise ValueError("nodes the vendor's tree does not have and that are no operating points: " + ", ".join(other[:8]))
    missing = sorted(n for n in set(vendor) - set(ours))
    if missing:
        raise ValueError("nodes of the vendor's tree that the kernel's tree lacks: " + ", ".join(missing[:8]))
    for n in remove:
        if "phandle" in ours[n] or "linux,phandle" in ours[n]:
            raise ValueError(f"{n} is referred to from elsewhere in the tree (it has a phandle): not removed")
    sets = []
    for n in sorted(ours):
        if n.startswith("/thermal-zones/") and "/trips/" in n:
            for prop in TRIP_PROPS:
                a, b = ours[n].get(prop), vendor[n].get(prop)
                if a != b:
                    if b is None or len(b) != 4:
                        raise ValueError(f"{n}: the vendor's tree has no single-cell {prop}")
                    sets.append((n, prop, cells(b)[0]))
    return remove, sets


def check(out, vendor):
    """Problems of the finished tree (an empty list is a pass)."""
    bad = [f"node {n} is not in the vendor's tree" for n in sorted(set(out) - set(vendor))]
    for n in sorted(set(out) & set(vendor)):
        if OPP.search(n):
            for prop in sorted(set(out[n]) | set(vendor[n])):
                if prop not in ("phandle", "linux,phandle") and out[n].get(prop) != vendor[n].get(prop):
                    bad.append(f"{n}: {prop} is not the vendor's")
        if n.startswith("/thermal-zones/") and "/trips/" in n:
            for prop in TRIP_PROPS:
                if out[n].get(prop) != vendor[n].get(prop):
                    bad.append(f"{n}: {prop} is not the vendor's")
    return bad


def ceiling(nodes, table: str):
    """The highest opp-hz of an operating point table, in MHz (0 when the table is not there)."""
    top = 0
    for n, props in nodes.items():
        if n.startswith(table + "/") and OPP.search(n) and "opp-hz" in props:
            c = cells(props["opp-hz"])
            top = max(top, (c[0] << 32 | c[1]) // 1000000)
    return top


def tables(nodes):
    return sorted({n.rsplit("/", 1)[0] for n in nodes if OPP.search(n)})


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, ref, dst = argv
    if not shutil.which("fdtput"):
        print("standard-dtb: fdtput is not installed (package dtc)")
        return 2
    try:
        ours, vendor = parse(src), parse(ref)
        remove, sets = plan(ours, vendor)
    except (OSError, ValueError) as e:
        print(f"standard-dtb: {e}")
        return 1
    if not remove and not sets:
        print(f"standard-dtb: {src} has the vendor's operating points and trip points already: nothing to make standard")
        return 1
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    try:
        # labels of removed nodes, when the tree was compiled with them
        symbols = [k for k, v in ours.get("/__symbols__", {}).items() if v.rstrip(b"\0").decode(errors="replace") in remove]
        for k in symbols:
            subprocess.run(["fdtput", "-d", tmp, "/__symbols__", k], check=True)
        for k in range(0, len(remove), 20):
            subprocess.run(["fdtput", "-r", tmp, *remove[k:k + 20]], check=True)
        for node, prop, value in sets:
            subprocess.run(["fdtput", "-t", "u", tmp, node, prop, str(value)], check=True)
        out = parse(tmp)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        print(f"standard-dtb: {e}")
        return 1
    bad = check(out, vendor)
    # nothing but the planned changes happened to the tree
    for n in out:
        for prop in set(out[n]) | set(ours[n]):
            if out[n].get(prop) != ours[n].get(prop) and (n, prop) not in {(a, b) for a, b, _ in sets} and n != "/__symbols__":
                bad.append(f"{n}: {prop} changed although nothing asked for it")
    if set(ours) - set(out) != set(remove):
        bad.append("the nodes that are gone are not exactly the operating points that were to be removed")
    if bad:
        print("standard-dtb: the result is not what was intended:")
        for b in bad[:20]:
            print("  " + b)
        return 1
    shutil.move(tmp, dst)
    print(f"removed {len(remove)} operating points the vendor's tables do not have, set {len(sets)} trip point values to the vendor's")
    for t in tables(ours):
        print(f"  {t}: ceiling {ceiling(ours, t)} MHz -> {ceiling(out, t)} MHz (vendor {ceiling(vendor, t)})")
    for node, prop, value in sets:
        print(f"  {node}: {prop} {cells(ours[node][prop])[0]} -> {value}")
    left = [(n, p) for n in sorted(set(out) & set(vendor)) for p in sorted(set(out[n]) | set(vendor[n]))
            if p not in ("phandle", "linux,phandle") and out[n].get(p) != vendor[n].get(p)]
    # cells that differ only in small numbers are references to other nodes, numbered differently by each compilation
    left = [(n, p) for n, p in left if not (cells(out[n].get(p)) and cells(vendor[n].get(p)) and len(out[n][p]) == len(vendor[n][p])
                                              and all(a < 4096 and b < 4096 for a, b in zip(cells(out[n][p]), cells(vendor[n][p])) if a != b))]
    print(f"still different from the vendor's tree, as in the kernel's own ({len(left)}): " + "; ".join(f"{n} {p}" for n, p in left[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
