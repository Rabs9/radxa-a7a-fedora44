"""tools/board/llm/a7a-llm: the context the model server starts with, from the Assistant's settings (a7a-panel's
assistant.json) unless A7A_LLM_CTX says otherwise."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "board" / "llm" / "a7a-llm"


class ContextTest(unittest.TestCase):
    def context(self, settings=None, **env):
        with tempfile.TemporaryDirectory() as d:
            if settings is not None:
                f = Path(d) / "a7a-panel" / "assistant.json"
                f.parent.mkdir()
                f.write_text(settings if isinstance(settings, str) else json.dumps(settings))
            r = subprocess.run(["bash", str(SCRIPT), "status"], capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, XDG_CONFIG_HOME=d, XDG_RUNTIME_DIR=d, **env))
        line = next(l for l in r.stdout.splitlines() if l.startswith("context: "))
        return int(line.split()[1])

    def test_the_settings_choose_it(self):
        self.assertEqual(self.context(), 8192)                       # no settings: as before
        self.assertEqual(self.context({"context": 16384}), 16384)
        self.assertEqual(self.context({"context": 4096}), 4096)

    def test_odd_settings_mean_the_default(self):
        for odd in ({"context": 12345}, {"context": "16384"}, {"context": 16384.0}, {"context": True}, {}, [16384], "{broken"):
            self.assertEqual(self.context(odd), 8192, odd)

    def test_the_environment_wins(self):
        self.assertEqual(self.context({"context": 16384}, A7A_LLM_CTX="2048"), 2048)

    def test_the_memory_check_counts_a_larger_context(self):
        text = SCRIPT.read_text()
        self.assertIn("NEED=$(( MSZ + 700 + (CTX > 8192 ? (CTX - 8192) * 147 / 1000 : 0) ))", text)
        self.assertEqual((16384 - 8192) * 147 // 1000, 1204)         # about 1.2 GB more for 16384


if __name__ == "__main__":
    unittest.main()


class GetTest(unittest.TestCase):
    """`a7a-llm get` on a first run (2026-11-24): no internet, a cut download, a small card. curl and df are faked;
    the panel shows the last line of what it printed (stdout, then stderr), so that line says what happened."""
    SIZE = 2375773280

    # curl's stand-in: writes $WRITE (printf) to the -o file, says $SAY on stderr, exits $RC
    CURL = 'o=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && { shift; o=$1; }; shift; done\n' \
           '[ -n "$WRITE" ] && printf "$WRITE" >> "$o"; [ -n "$SAY" ] && echo "$SAY" >&2; exit ${RC:-0}'

    def get(self, curl="exit 0", free=10 << 30, part=None, **env):
        """Runs it; the lines it printed (stdout, then stderr, as the panel joins them), and what is left in the
        models folder (name: first bytes)."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            b = d / "bin"
            b.mkdir()
            (b / "curl").write_text(f"#!/bin/bash\n{curl}\n")
            (b / "df").write_text(f"#!/bin/bash\necho '      Avail'\necho {free}\n")
            for f in b.iterdir():
                f.chmod(0o755)
            models = d / "models"
            if part is not None:
                models.mkdir()
                (models / "Qwen3-4B-Instruct-2507-Q4_0.gguf.part").write_bytes(part)
            r = subprocess.run(["bash", str(SCRIPT), "get"], capture_output=True, text=True, timeout=30,
                               env=dict(os.environ, PATH=f"{b}:{os.environ['PATH']}", A7A_LLM_MODELS=str(models),
                                        XDG_CONFIG_HOME=str(d), XDG_RUNTIME_DIR=str(d), **env))
            left = {f.name: f.read_bytes()[:8] for f in models.iterdir() if not f.name.endswith(".lock")} if models.exists() else {}
            argf = d / "curl-args"
            self.args = argf.read_text() if argf.exists() else ""
        return r, [l for l in (r.stdout + r.stderr).splitlines() if l.strip()], left

    def test_no_progress_bar_without_a_terminal(self):
        # the panel runs it with its output captured: curl's bar there is tens of thousands of carriage-returned states
        # in one line, which filled the Voice tab and froze the panel (2026-11-24); it shows the progress itself
        r, lines, _ = self.get('echo "curl $*" > "$A7A_LLM_MODELS/../curl-args"; exit 6')
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("--progress-bar", self.args)
        self.assertIn("-sS", self.args)

    def test_after_curl_recovered_the_last_line_is_still_ours(self):
        # curl retried (its warning on stderr) and exited 0 with a short file: the panel shows our line, not curl's
        # (review 2026-11-24)
        r, lines, left = self.get(self.CURL, WRITE="GGUF1234", RC="0",
                                  SAY="Warning: Problem : HTTP error. Will retry in 1 second. 5 retries left.")
        self.assertEqual(r.returncode, 1)
        self.assertIn("the download is incomplete", lines[-1])
        self.assertEqual(left, {"Qwen3-4B-Instruct-2507-Q4_0.gguf.part": b"GGUF1234"})     # kept for the next run

    def test_a_login_page_instead_of_the_model_is_not_kept(self):
        # a cafe's network: curl -fL follows it to its login page and saves that, exit 0 (review 2026-11-24: the
        # next run resumed after the page and threw the whole 2.4 GB away at the checksum)
        r, lines, left = self.get(self.CURL, WRITE="<html>Log in</html>", RC="0")
        self.assertEqual(r.returncode, 1)
        self.assertRegex(lines[-1], r"not the model.*login page")
        self.assertEqual(left, {})
        # a page kept by an older run goes before the next download, which starts from nothing
        r, lines, left = self.get(self.CURL, WRITE="GGUF", RC="0", part=b"<html>Log in</html>")
        self.assertEqual(left, {"Qwen3-4B-Instruct-2507-Q4_0.gguf.part": b"GGUF"})

    def test_no_connection_says_so(self):
        for code, said in ((6, "curl: (6) Could not resolve host: huggingface.co"),
                           (7, "curl: (7) Failed to connect to huggingface.co port 443 after 3 ms: Could not connect"),
                           (28, "curl: (28) Connection timed out after 30001 milliseconds")):
            with self.subTest(code=code):
                r, lines, _ = self.get(f'echo "{said}" >&2; exit {code}')
                self.assertEqual(r.returncode, 1)
                self.assertRegex(lines[-1], r"no connection to huggingface\.co.*internet.*carries on where it stopped")

    def test_a_cut_download_says_the_next_try_carries_on(self):
        r, lines, _ = self.get('echo "curl: (56) Recv failure: Connection reset by peer" >&2; exit 56')
        self.assertEqual(r.returncode, 1)
        self.assertRegex(lines[-1], r"the download stopped \(curl 56\).*carries on where it stopped")

    def test_too_little_room_names_what_it_asks_for(self):
        # the model fits, the 500 MB kept spare does not: the message gives the figure it checks against
        free = self.SIZE + 100_000_000
        r, lines, _ = self.get(free=free)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{free // 1048576} MB free", lines[-1])
        self.assertIn(f"needs {(self.SIZE + 500_000_000) // 1048576} MB", lines[-1])
