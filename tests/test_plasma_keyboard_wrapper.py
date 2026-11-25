"""tools/board/plasma-keyboard: the login screen's on-screen keyboard on Fedora's Mesa (the greeter's KWin hands it the
DDK's libraries on a DDK boot, and it aborted without EGL). Run with `id` faked, the greeter's DDK file in a scratch
directory, and the real keyboard replaced by a program that prints what it got."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
WRAPPER = P / "tools" / "board" / "plasma-keyboard"
TARGET = "exec /usr/bin/plasma-keyboard \"$@\""
ENV_FILE = "ENV_FILE=/run/a7a/greeter-kwin.env"
# what a7a-gpu-session wrote on the board's DDK boot (2026-11-18), plus a name added later to show the file is read
DDK = {"LD_LIBRARY_PATH": "/opt/pvr/lib", "LIBGL_DRIVERS_PATH": "/opt/pvr/lib/dri", "KWIN_COMPOSE": "O2ES",
       "VK_ICD_FILENAMES": "/opt/pvr/icd/powervr_icd.json", "MESA_LOADER_DRIVER_OVERRIDE": "sunxi-drm",
       "KWIN_FORCE_SW_CURSOR": "1", "KWIN_RENDER_NODES": "/dev/dri/none", "GBM_BACKENDS_PATH": "/opt/pvr/lib/gbm"}
SHOWN = list(DDK) + ["HOME"]


class PlasmaKeyboardWrapperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "bin").mkdir()
        fake_id = d / "bin" / "id"
        fake_id.write_text('#!/bin/sh\n[ "$1" = -un ] && echo "$FAKE_USER"\n')
        self.env_file = d / "greeter-kwin.env"
        self.env_file.write_text("# written by a7a-gpu-session\n" + "".join(f"{k}={v}\n" for k, v in DDK.items()))
        keyboard = d / "keyboard"
        keyboard.write_text("#!/bin/sh\nfor v in " + " ".join(SHOWN) + '; do eval "echo $v=\\${$v-unset}"; done\n'
                            'echo "argc=$#"\nfor a in "$@"; do echo "arg=$a"; done\n')
        text = WRAPPER.read_text()
        self.assertEqual(text.count(TARGET), 1)            # else the test would start the real keyboard
        self.assertEqual(text.count(ENV_FILE), 1)
        self.wrapper = d / "plasma-keyboard"
        self.wrapper.write_text(text.replace(TARGET, f'exec {keyboard} "$@"')
                                    .replace(ENV_FILE, f"ENV_FILE={self.env_file}"))
        for f in (fake_id, keyboard, self.wrapper):
            f.chmod(0o755)
        self.path = f"{d / 'bin'}:{os.environ['PATH']}"

    def run_as(self, user):
        env = dict(os.environ, PATH=self.path, FAKE_USER=user, **DDK)
        out = subprocess.run([str(self.wrapper), "--a", "b c", ""], env=env, capture_output=True, text=True,
                             check=True, timeout=10).stdout.splitlines()
        got = dict(line.split("=", 1) for line in out if not line.startswith("arg="))
        got["args"] = [line[4:] for line in out if line.startswith("arg=")]
        return got

    def test_the_login_screen_gets_fedoras_mesa(self):
        got = self.run_as("plasmalogin")
        self.assertEqual(got["LD_LIBRARY_PATH"], "/usr/lib64")
        for v in ("LIBGL_DRIVERS_PATH", "MESA_LOADER_DRIVER_OVERRIDE", "VK_ICD_FILENAMES", "GBM_BACKENDS_PATH"):
            self.assertEqual(got[v], "unset", v)
        for v in ("KWIN_COMPOSE", "KWIN_FORCE_SW_CURSOR", "KWIN_RENDER_NODES"):   # KWin's own: left alone
            self.assertEqual(got[v], DDK[v], v)
        self.assertEqual(got["HOME"], os.environ["HOME"])
        self.assertEqual((got["argc"], got["args"]), ("3", ["--a", "b c", ""]))

    def test_without_the_ddk_file_the_known_names_still_go(self):
        self.env_file.unlink()                               # the open-stack boot: no file
        got = self.run_as("plasmalogin")
        self.assertEqual(got["LD_LIBRARY_PATH"], "/usr/lib64")
        for v in ("LIBGL_DRIVERS_PATH", "MESA_LOADER_DRIVER_OVERRIDE", "VK_ICD_FILENAMES"):
            self.assertEqual(got[v], "unset", v)

    def test_everyone_else_gets_it_unchanged(self):
        got = self.run_as("radxa")
        for v, value in DDK.items():
            self.assertEqual(got[v], value, v)
        self.assertEqual((got["argc"], got["args"]), ("3", ["--a", "b c", ""]))


if __name__ == "__main__":
    unittest.main()
