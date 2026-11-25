#!/usr/bin/env python3
"""boot0-check: is the boot0 in a boot firmware region whole, and does it set the memory clock an image promises?

  tools/boot0-check.py <firmware region, 16 MiB> [expected memory clock in MHz]

An Allwinner boot0 ("eGON.BT0") sits at sector 256 with a copy at sector 2064; it carries its length and a checksum
over that length (the sum of its 32-bit words with the checksum field set to 0x5F0A6C39), and the memory clock in
MHz as a 16-bit number at byte 116 of its third sector. The images of this project differ in exactly that boot0
(tools/master-image.sh: 2136 MHz on the overclocked image, 2040 on the standard one; the vendor's is 1800): a file
with a damaged boot0 or the other variant's must not be written into an image.
Prints what it found; exit 0 when both copies are valid (and have the expected clock, when one is given), else 1."""
import struct
import sys

MAGIC, STAMP, COPIES = b"eGON.BT0", 0x5F0A6C39, (256, 2064)


def boot0(data: bytes, sector: int):
    """(problem or None, memory clock in MHz) of the boot0 at that sector."""
    b = data[sector * 512: sector * 512 + 0x100000]
    if b[4:12] != MAGIC:
        return "no eGON.BT0 header", None
    stored, length = struct.unpack("<II", b[12:20])
    if length == 0 or length % 4 or length > len(b):
        return f"implausible length {length:#x}", None
    body = bytearray(b[:length])
    body[12:16] = struct.pack("<I", STAMP)
    if sum(struct.unpack(f"<{length // 4}I", bytes(body))) & 0xFFFFFFFF != stored:
        return "checksum does not match", None
    return None, struct.unpack("<H", b[2 * 512 + 116: 2 * 512 + 118])[0]


def main(argv) -> int:
    if len(argv) not in (1, 2) or (len(argv) == 2 and not argv[1].isdigit()):
        print(__doc__)
        return 1
    try:
        with open(argv[0], "rb") as f:
            data = f.read(16 * 1024 * 1024)
    except OSError as e:
        print(f"boot0-check: {e}")
        return 1
    want = int(argv[1]) if len(argv) == 2 else None
    bad = False
    for sector in COPIES:
        problem, clock = boot0(data, sector)
        if problem is None and want is not None and clock != want:
            problem = f"memory clock {clock} MHz, not {want}"
        print(f"boot0 at sector {sector}: " + (problem or f"valid, memory clock {clock} MHz"))
        bad = bad or problem is not None
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
