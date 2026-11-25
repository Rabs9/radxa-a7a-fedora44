#!/usr/bin/env python3
"""vnc-shot: one screenshot of a VNC display, and what is on it in numbers.

  tools/vnc-shot.py [--host 127.0.0.1] [--display 5] [--out shot.png]

For the emulator (tools/emulate.sh gives QEMU `-vnc 127.0.0.1:5`): a login prompt on the serial console and a greeter
session in logind do not say that anything is DRAWN. 2026-11-16: with KWin 6.8 the greeter's compositor exited at
once (its software painting mode was gone), every check passed, and the screen was black.
Prints one line, `WxH colours=N dark=P% top=#rrggbb:Q%`: the number of distinct colours and the share of near-black
pixels among 1 in 16 pixels, and the most frequent colour with its share. A login screen has thousands of colours; a
dead compositor leaves one colour or a text console (a handful). Exit 0 when a frame was read, 1 otherwise.
Python 3 standard library only (RFB 3.8, no authentication, raw encoding)."""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import zlib
from collections import Counter


def read(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ConnectionError(f"the server closed the connection after {len(buf)} of {n} bytes")
        buf += chunk
    return bytes(buf)


def grab(host: str, port: int, timeout: float = 20.0):
    """(width, height, pixels as BGRX bytes) of the whole framebuffer."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        banner = read(s, 12)
        if not banner.startswith(b"RFB "):
            raise ConnectionError(f"not a VNC server: {banner!r}")
        s.sendall(b"RFB 003.008\n")
        n = read(s, 1)[0]
        if n == 0:
            raise ConnectionError("the server refused: " + read(s, struct.unpack(">I", read(s, 4))[0]).decode(errors="replace"))
        if 1 not in read(s, n):
            raise ConnectionError("the server wants a password (security type None is not offered)")
        s.sendall(b"\x01")
        if struct.unpack(">I", read(s, 4))[0] != 0:
            raise ConnectionError("the security handshake failed")
        s.sendall(b"\x01")                                    # shared: do not throw another viewer out
        width, height = struct.unpack(">HH", read(s, 4))
        read(s, 16)
        read(s, struct.unpack(">I", read(s, 4))[0])           # the name
        # 32 bits per pixel, depth 24, little endian, true colour, 8 bits each, red at 16: bytes B G R X
        s.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        s.sendall(struct.pack(">BxHi", 2, 1, 0))               # encodings: raw only
        s.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, width, height))
        frame = bytearray(width * height * 4)
        covered = 0
        while covered < width * height:
            kind = read(s, 1)[0]
            if kind == 0:                                     # FramebufferUpdate
                (rects,) = struct.unpack(">xH", read(s, 3))
                for _ in range(rects):
                    x, y, w, h, enc = struct.unpack(">HHHHi", read(s, 12))
                    if enc != 0:
                        raise ConnectionError(f"the server sent encoding {enc}, only raw was asked for")
                    data = read(s, w * h * 4)
                    for row in range(h):
                        at = ((y + row) * width + x) * 4
                        frame[at:at + w * 4] = data[row * w * 4:(row + 1) * w * 4]
                    covered += w * h
            elif kind == 1:                                   # SetColourMapEntries
                (count,) = struct.unpack(">xHH", read(s, 5))[1:]
                read(s, count * 6)
            elif kind == 2:                                   # Bell
                pass
            elif kind == 3:                                   # ServerCutText
                read(s, struct.unpack(">xxxI", read(s, 7))[0])
            else:
                raise ConnectionError(f"unexpected message type {kind} from the server")
        return width, height, bytes(frame)


def png(width: int, height: int, bgrx: bytes) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
    rows = bytearray()
    for y in range(height):
        line = bgrx[y * width * 4:(y + 1) * width * 4]
        rows.append(0)
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = line[2::4], line[1::4], line[0::4]
        rows += rgb
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


def describe(width: int, height: int, bgrx: bytes) -> str:
    counts: Counter = Counter()
    dark = total = 0
    for at in range(0, len(bgrx), 4 * 16):                    # 1 pixel in 16
        b, g, r = bgrx[at], bgrx[at + 1], bgrx[at + 2]
        counts[(r, g, b)] += 1
        total += 1
        dark += r < 16 and g < 16 and b < 16
    (r, g, b), n = counts.most_common(1)[0]
    return f"{width}x{height} colours={len(counts)} dark={100 * dark // total}% top=#{r:02x}{g:02x}{b:02x}:{100 * n // total}%"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--display", type=int, default=5, help="VNC display number (port 5900 + N); the emulator uses 5")
    ap.add_argument("--out", default=None, help="write the frame as a PNG")
    args = ap.parse_args(argv)
    try:
        width, height, frame = grab(args.host, 5900 + args.display)
    except (OSError, ConnectionError, struct.error) as e:
        print(f"no frame: {e}")
        return 1
    if args.out:
        with open(args.out, "wb") as f:
            f.write(png(width, height, frame))
    print(describe(width, height, frame))
    return 0


if __name__ == "__main__":
    sys.exit(main())
