"""Tests for tools/vnc-shot.py: one frame from a VNC server (the emulator's display) and what is on it. A small RFB
3.8 server in a thread plays QEMU: no authentication, raw encoding, the frame sent in two rectangles."""

import importlib.machinery
import importlib.util
import socket
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "vnc-shot.py"
_loader = importlib.machinery.SourceFileLoader("vnc_shot", str(SCRIPT))
shot = importlib.util.module_from_spec(importlib.util.spec_from_file_location("vnc_shot", str(SCRIPT), loader=_loader))
sys.modules["vnc_shot"] = shot
_loader.exec_module(shot)


def pixels(width, height, colour_of):
    """BGRX bytes for a frame whose pixel (x, y) has colour_of(x, y) = (r, g, b)."""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b = colour_of(x, y)
            out += bytes((b, g, r, 0))
    return bytes(out)


class Server:
    def __init__(self, width, height, frame, security=(1,), extra=b""):
        self.width, self.height, self.frame, self.security, self.extra = width, height, frame, security, extra
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.seen = {}
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, c, n):
        buf = b""
        while len(buf) < n:
            part = c.recv(n - len(buf))
            if not part:
                raise ConnectionError
            buf += part
        return buf

    def _serve(self):
        try:
            c, _ = self.sock.accept()
            with c:
                c.sendall(b"RFB 003.008\n")
                self.seen["version"] = self._read(c, 12)
                c.sendall(bytes((len(self.security),)) + bytes(self.security))
                if 1 not in self.security:
                    return
                self._read(c, 1)
                c.sendall(struct.pack(">I", 0))
                self.seen["shared"] = self._read(c, 1)
                c.sendall(struct.pack(">HH", self.width, self.height) + bytes(16) + struct.pack(">I", 4) + b"QEMU")
                self.seen["pixel_format"] = self._read(c, 20)
                self.seen["encodings"] = self._read(c, 8)
                self.seen["request"] = self._read(c, 10)
                c.sendall(self.extra)                              # a bell, a clipboard text: things a server may say first
                half = self.height // 2
                row = self.width * 4
                c.sendall(struct.pack(">BxH", 0, 2))
                c.sendall(struct.pack(">HHHHi", 0, half, self.width, self.height - half, 0) + self.frame[half * row:])
                c.sendall(struct.pack(">HHHHi", 0, 0, self.width, half, 0) + self.frame[:half * row])
        except (ConnectionError, OSError):
            pass
        finally:
            self.sock.close()


class TestVncShot(unittest.TestCase):
    def test_reads_the_whole_frame_whatever_order_the_rectangles_come_in(self):
        frame = pixels(8, 6, lambda x, y: (x * 30, y * 40, 200))
        s = Server(8, 6, frame, extra=b"\x02" + struct.pack(">BxxxI", 3, 5) + b"hello")
        width, height, got = shot.grab("127.0.0.1", s.port, timeout=5)
        self.assertEqual((width, height), (8, 6))
        self.assertEqual(got, frame)
        self.assertEqual(s.seen["version"], b"RFB 003.008\n")
        self.assertEqual(s.seen["shared"], b"\x01")                                       # another viewer stays connected
        self.assertEqual(s.seen["pixel_format"][4:8], bytes((32, 24, 0, 1)))                # 32 bpp, little endian, true colour
        self.assertEqual(struct.unpack(">BBHHHH", s.seen["request"]), (3, 0, 0, 0, 8, 6))   # the whole screen, not incremental

    def test_a_login_screen_and_a_black_screen_read_differently(self):
        # the numbers the emulator stage judges: 2026-11-16 the greeter's compositor was gone and the frame had 2 colours
        picture = pixels(64, 64, lambda x, y: (x * 4, y * 4, (x * y) % 256))
        self.assertRegex(shot.describe(64, 64, picture), r"^64x64 colours=\d{3,} dark=\d+% top=#[0-9a-f]{6}:\d+%$")
        self.assertGreater(int(shot.describe(64, 64, picture).split("colours=")[1].split()[0]), 200)
        black = pixels(64, 64, lambda x, y: (255, 255, 255) if (x, y) == (0, 0) else (0, 0, 0))
        self.assertEqual(shot.describe(64, 64, black), "64x64 colours=2 dark=99% top=#000000:99%")

    def test_png_holds_the_pixels(self):
        frame = pixels(3, 2, lambda x, y: (10 * x, 20 * y, 7))
        data = shot.png(3, 2, frame)
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", data[16:24]), (3, 2))
        idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
        rows = zlib.decompress(idat)
        self.assertEqual(rows, b"\x00" + bytes((0, 0, 7, 10, 0, 7, 20, 0, 7)) + b"\x00" + bytes((0, 20, 7, 10, 20, 7, 20, 20, 7)))

    def test_cli_writes_the_file_and_one_line_and_says_when_there_is_no_frame(self):
        import contextlib
        import io
        frame = pixels(16, 16, lambda x, y: (x * 16, y * 16, 0))
        s = Server(16, 16, frame)
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = shot.main(["--display", str(s.port - 5900), "--out", f"{d}/s.png"])
            self.assertEqual(rc, 0, out.getvalue())
            self.assertTrue(out.getvalue().startswith("16x16 colours="))
            self.assertGreater(Path(f"{d}/s.png").stat().st_size, 50)
        # a server that wants a password, and no server at all: one line that says so, exit 1
        s = Server(4, 4, bytes(64), security=(2,))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = shot.main(["--display", str(s.port - 5900)])
        self.assertEqual(rc, 1)
        self.assertIn("password", out.getvalue())
        free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = shot.main(["--display", str(port - 5900)])
        self.assertEqual(rc, 1)
        self.assertTrue(out.getvalue().startswith("no frame:"))


if __name__ == "__main__":
    unittest.main()
