"""tools/board/a7a-voice: voice commands (a wake phrase, then a command typed in the panel's Voice tab). The matching,
the settings, the model download, setup and the listener's steps, with the speech models and the microphone replaced
by scripted fakes; the command-line side (save, test, status) run as a program with systemd-run, setsid and systemctl
faked on PATH."""
import bz2
import contextlib
import fcntl
import hashlib
import http.server
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "board" / "a7a-voice"
UNIT = P / "tools" / "board" / "systemd" / "a7a-voice.service"


def load():
    loader = SourceFileLoader("a7a_voice", str(SCRIPT))
    m = module_from_spec(spec_from_loader("a7a_voice", loader))
    loader.exec_module(m)
    return m


V = load()
# The image ships "hey cubie" (short, the owner's choice 2026-11-24); these tests were written with "hey computer"
# in their transcripts and test the listener, not the default: they keep it. ShippedDefaultTest checks the default.
SHIPPED_WAKE = V.DEFAULTS["wake"]
V.DEFAULTS["wake"] = "hey computer"
R = V.PREROLL                                         # the tests' timings below take 2 s of pre-roll
assert R == 20
# the runtime directory's files in one of the tests' own: a test must never stop (hush) a reading on this machine, nor
# touch its listener's status (review 2026-11-19: ListenTest's wake phrases did)
RUN = tempfile.TemporaryDirectory()
for _name in ("STATUS", "PROGRESS", "KEYWORDS", "SPEECH_PROGRESS", "SPEAKING", "HUSHED", "SPEECH", "PANEL"):
    setattr(V, _name, Path(RUN.name) / getattr(V, _name).name)
CMDS = V.DEFAULTS["commands"] + [{"say": "lights on", "run": "on"}, {"say": "lights off", "run": "off"},
                                 {"say": "volume 50", "run": "fifty"}]


def said(text, cmds=CMDS):
    m, _ = V.best_match(V.words(text), cmds, 0.75)
    return m["say"] if m else None


class MatchTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(V.words("Set volume to 55%, it's 2 o’clock!"),
                         ["set", "volume", "to", "fifty", "five", "it's", "two", "o'clock"])
        self.assertEqual(V.words("Wi-Fi off"), ["wifi", "off"])
        self.assertEqual(V.words("channel 1000, 2010 and 12345678"),
                         ["channel", "one", "thousand", "two", "thousand", "ten", "and", "12345678"])
        self.assertEqual(V.words("x" + "9" * 5000), ["x", "9" * 5000])        # no int() of a huge number
        self.assertEqual([V.number_words(n) for n in (0, 13, 20, 55, 100, 105, 999, 1000, 21500)],
                         ["zero", "thirteen", "twenty", "fifty five", "one hundred", "one hundred five",
                          "nine hundred ninety nine", "one thousand", "twenty one thousand five hundred"])

    def test_the_phrase_inside_a_sentence(self):
        self.assertEqual(said("OPEN FIREFOX"), "open firefox")
        self.assertEqual(said("Open Firefox."), "open firefox")                       # Kroko writes like this
        self.assertEqual(said("turn the volume up please"), "volume up")
        self.assertEqual(said("turn the lights off"), "lights off")
        self.assertEqual(said("mute"), "mute")

    def test_close_enough(self):
        self.assertEqual(said("open fire fox"), "open firefox")                       # a word written in two
        self.assertEqual(said("open fire folks"), "open firefox")
        self.assertEqual(said("lock screen"), "lock the screen")                      # a filler word left out
        self.assertEqual(said("set volume to 50"), "volume 50")
        self.assertEqual(said("volume fifty"), "volume 50")
        self.assertEqual(said("it's muted"), "mute")
        # typed apart, heard together (and the other way round)
        cmds = [{"say": "wi-fi off", "run": "a"}, {"say": "open you tube", "run": "b"}, {"say": "open fire fox", "run": "c"}]
        self.assertEqual(said("wifi off", cmds), "wi-fi off")
        self.assertEqual(said("open youtube", cmds), "open you tube")
        self.assertEqual(said("open firefox", cmds), "open fire fox")

    def test_not_close_enough(self):
        for text in ("WHAT'S THE TEMPERATURE", "HEY CUBY", "open the pedal", "", "give me a minute", "that's cute",
                     "mutt", "look at the screen", "volume town", "lights of"):
            self.assertIsNone(said(text), text)
        # a word of a two-word phrase is not the phrase
        self.assertIsNone(said("set volume to 50", V.DEFAULTS["commands"]))

    def test_negated(self):
        self.assertIsNone(said("don't lock the screen"))
        self.assertIsNone(said("do not open firefox"))
        self.assertEqual(said("never mind, mute"), "mute")
        self.assertEqual(said("not now, open firefox"), "open firefox")
        self.assertEqual(said("do not disturb", [{"say": "do not disturb", "run": "x"}]), "do not disturb")

    def test_the_longer_phrase_on_a_tie_and_incomplete_commands_never(self):
        cmds = [{"say": "open", "run": "a"}, {"say": "open firefox", "run": "b"}, {"say": "open firefox now", "run": ""},
                {"say": "", "run": "c"}]
        m, s = V.best_match(V.words("open firefox now"), cmds, 0.75)
        self.assertEqual((m["run"], s), ("b", 1.0))

    def test_the_floor(self):
        m, s = V.best_match(V.words("lock screen"), CMDS, 0.9)
        self.assertIsNone(m)
        self.assertAlmostEqual(V.best_match(V.words("lock screen"), CMDS, 0.75)[1], 0.87, places=2)

    def test_a_long_sentence_against_many_commands_is_quick(self):
        import time
        cmds = [{"say": f"command number {n} please now", "run": "x"} for n in range(30)]
        text = " ".join(["and then we were talking about the weather and other things for a while"] * 3)
        t = time.process_time()
        self.assertIsNone(V.best_match(V.words(text), cmds, 0.75)[0])
        self.assertLess(time.process_time() - t, 0.5)

    def test_after_the_wake_phrase(self):
        w = "hey computer"
        self.assertEqual(V.after_wake(V.words("Hey computer, open Firefox."), w), ["open", "firefox"])
        self.assertEqual(V.after_wake(V.words("Mute. Hey computer, open Firefox."), w), ["open", "firefox"])
        self.assertEqual(V.after_wake(V.words("A computer open firefox"), w), ["open", "firefox"])   # misheard
        self.assertEqual(V.after_wake(V.words("hey computer hey computer volume up"), w), ["volume", "up"])
        self.assertEqual(V.after_wake(V.words("hey computer open computer settings"), w), ["open", "computer", "settings"])
        self.assertEqual(V.after_wake(V.words("Hey computer."), w), [])
        self.assertEqual(V.after_wake(V.words("Okay, so, hey computer."), w), [])
        self.assertEqual(V.after_wake(V.words("volume up hey computer volume down"), w), ["volume", "down"])
        # a stretch like the phrase inside the command is not taken for it (review 2026-11-18)
        self.assertEqual(V.after_wake(V.words("A computer, shut down the computer."), w), ["shut", "down", "the", "computer"])
        self.assertEqual(V.after_wake(V.words("Hi computer, open the computer settings."), w),
                         ["open", "the", "computer", "settings"])
        self.assertEqual(V.after_wake(V.words("Jarvis, open Jarvis notes"), "jarvis"), ["open", "jarvis", "notes"])
        self.assertEqual(V.after_wake(V.words("Hey, compute her, open Firefox."), w), ["open", "firefox"])
        # the pre-roll caught only the phrase's end
        self.assertEqual(V.after_wake(V.words("uter, open Firefox."), w), ["open", "firefox"])
        self.assertEqual(V.after_wake(V.words("ter open firefox"), w), ["open", "firefox"])
        self.assertIsNone(V.after_wake(V.words("okay so"), w))
        self.assertIsNone(V.after_wake([], w))

    def test_a_wake_like_phrase_in_the_pre_roll(self):
        w = "hey computer"
        self.assertEqual(V.after_wake(V.words("My computer, lock the screen. Hey computer, mute."), w), ["mute"])
        self.assertEqual(V.cut_at_wake(V.words("Hey computer, open Firefox."), w), (2, True))
        self.assertEqual(V.cut_at_wake(V.words("A computer, open Firefox."), w), (2, False))
        # the owner's phrase only close ("commuter"), the pre-roll's "my computer" closer by its letters: the stretch
        # with every word of the phrase in it is the phrase (2026-11-24, open since 11-23); the cut is not sure
        said = V.words("My computer is slow. Hey commuter, open the pod bay doors.")
        self.assertEqual(V.after_wake(said, w), ["open", "the", "pod", "bay", "doors"])
        self.assertEqual(V.cut_at_wake(said, w), (6, False))
        self.assertEqual(V.after_wake(V.words("My computer is slow, hey commuter, mute."), w), ["mute"])
        # a stretch the run-together letters alone make like the phrase is not word for word ("the computer")
        self.assertEqual(V.after_wake(V.words("The computer is slow. Hey commuter, mute."), w), ["mute"])
        # the phrase written loosely ("a computer", "hey cuby": could be it), then a command that holds something like
        # it word for word: the command is not cut (review 2026-11-24)
        self.assertEqual(V.after_wake(V.words("A computer, type they computed it"), w), ["type", "they", "computed", "it"])
        self.assertEqual(V.after_wake(V.words("A computer, go to how they compute pi"), w),
                         ["go", "to", "how", "they", "compute", "pi"])
        self.assertEqual(V.after_wake(V.words("Hey cuby, type hey cubi"), "hey cubie"), ["type", "hey", "cubi"])
        self.assertEqual(V.after_wake(V.words("A computer, type hey commuter"), w), ["type", "hey", "commuter"])
        # without "hey" in either (the recogniser wrote "a computer"), as before: the first close one (needs timings)
        self.assertEqual(V.after_wake(V.words("Hey commuter, mute."), w), ["mute"])

    def test_a_later_sentence_loses_only_a_wake_phrase_it_begins_with(self):
        w = "hey computer"
        self.assertEqual(V.lead_wake(V.words("Computer status."), w), ["computer", "status"])
        self.assertEqual(V.lead_wake(V.words("A computer, mute."), w), ["mute"])
        self.assertEqual(V.lead_wake(V.words("Jarvis notes."), "jarvis"), ["jarvis", "notes"])
        self.assertEqual(V.lead_wake(V.words("Hey computer, volume up."), w), ["volume", "up"])
        self.assertEqual(V.lead_wake(V.words("Shut down the computer."), w), ["shut", "down", "the", "computer"])
        self.assertEqual(V.lead_wake(V.words("Volume up."), w), ["volume", "up"])


class OnlineTest(unittest.TestCase):
    """The online assistants of the panel's settings: their names (never their keys) become wake phrases."""
    def test_their_names_from_the_panels_settings(self):
        # named as the panel names them (the same entries in prefs.rs's online_names_are_the_voices_too), or "hey
        # second google" would ask the panel for a name it does not have
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "assistant.json"
            f.write_text(json.dumps({"online": [
                {"name": "google", "key": "k1", "on": True}, {"name": "brave", "key": "k2"},
                {"name": "mistral", "key": "", "on": True}, {"name": "groq", "key": "k3", "on": False},
                {"name": "Google", "key": "k4"}, {"name": "", "key": "k5"}, 5, {"key": "k6"},
                {"name": "", "provider": "Brave", "key": "k7"}, {"name": "GPT-4o Mini Turbo Max", "key": "k8", "on": "yes"},
                {"name": "a very long name for an assistant", "key": " k9 "},
                {"name": "supercalifragilistic expialidocious", "key": "k10"}, {"name": "x", "key": 5},
                {"name": "", "provider": "Cloudflare", "key": "k11"}]}))
            self.assertEqual(V.online_names(f), ["google", "brave", "second google", "other", "second other", "second brave",
                                                 "gpt o mini", "a very long", "supercalifragilistic exp", "cloudflare"])
            # trimmed as Rust trims: "groq\x1f" is no provider the panel knows ("other"), "\x1f" a key (prefs.rs keeps it)
            f.write_text(json.dumps({"online": [{"name": "", "provider": "groq\x1f", "key": "\x1f"}, {"name": "", "provider": " groq ", "key": "k"}]}))
            self.assertEqual(V.online_names(f), ["other", "groq"])
            f.write_text(json.dumps({"online": [{"name": "google", "key": "k"}] * 7}))
            self.assertEqual(V.online_names(f), ["google", "second google", "third google", "fourth google", "fifth google",
                                                 "sixth google"])             # a seventh: dropped, as the panel drops it
            f.write_text("{broken")
            self.assertEqual(V.online_names(f), [])
            self.assertEqual(V.online_names(Path(d) / "none.json"), [])

    def test_the_phrases_the_spotter_can_tell_apart(self):
        # pieces as the spotter's model cuts words (here: in fours), so one name can begin another below the words
        def pieces_of(phrase):
            return [("\u2581" if i == 0 else "") + w[i:i + 4] for w in phrase.upper().split() for i in range(0, len(w), 4)]
        known = {x for p in ("hey computer", "hey assistant", "hey google", "hey brave search", "hey bravery", "hey comp",
                             "hey ai", "hey brave") for x in pieces_of(p)}
        cfg = dict(V.clean(None), online=["google", "brave", "brave search", "bravery", "computer", "comp", "ai", "zorp",
                                          "assistant"])
        lines, online, left = V.keywords(cfg, pieces_of, known)
        self.assertEqual(online, ["google", "brave", "bravery"])            # "bravery" is not "brave" and more
        self.assertEqual(lines[:2], ["\u2581HEY \u2581COMP UTER", "\u2581HEY \u2581ASSI STAN T"])
        self.assertEqual(len(lines), 5)
        self.assertEqual([why.split("\u201d")[0].strip("\u201c") for why in left],
                         ["hey brave search", "hey computer", "hey comp", "hey ai", "hey zorp", "hey assistant"])
        self.assertIn("begins like another", left[0])                       # "hey brave" would be named first
        self.assertIn("another wake phrase already", left[1])
        self.assertIn("begins like another", left[2])                       # it would take "hey computer"
        self.assertIn("too short", left[3])
        self.assertIn("does not know", left[4])
        with self.assertRaisesRegex(ValueError, "pieces the spotter does not know"):
            V.keywords(dict(cfg, wake="hey zorp"), pieces_of, known)        # the owner's own phrase: an error

    def test_the_npu_setting_and_the_spotter_it_falls_back_to(self):
        self.assertFalse(V.clean(None)["npu"])
        self.assertTrue(V.clean({"npu": True})["npu"])
        self.assertFalse(V.clean({"npu": "yes"})["npu"])                   # only a real true switches it on
        cpu, npu = object(), object()
        sherpa = mock.MagicMock()
        sherpa.KeywordSpotter.return_value = cpu
        sp = mock.MagicMock()
        sp.SentencePieceProcessor.return_value.encode = lambda text, out_type: text.split()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(sys.modules, {"sherpa_onnx": sherpa, "sentencepiece": sp}), \
                mock.patch.object(V, "MODELS", Path(d)), mock.patch.object(V, "KEYWORDS", Path(d) / "kw.txt"):
            m = Path(d) / V.KWS[0]
            m.mkdir()
            (m / "tokens.txt").write_text("HEY 1\nCOMPUTER 2\nASSISTANT 3\n")
            for part in ("encoder", "decoder", "joiner"):
                (m / f"{part}.int8.onnx").write_text("")
            cfg = dict(V.clean(None), online=[])
            self.assertIs(V.spotter(cfg)[0], cpu)                          # off: the CPU's, nothing asked of the NPU
            self.assertEqual(V.ENGINE, {"engine": "cpu", "npu_error": ""})
            with mock.patch.object(V, "npu_spotter", return_value=npu) as made:
                self.assertIs(V.spotter(dict(cfg, npu=True))[0].k, npu)    # behind the guard (next test)
            self.assertEqual(made.call_args[0], (m, cfg["sensitivity"]))
            self.assertEqual(V.ENGINE, {"engine": "npu", "npu_error": ""})
            with mock.patch.object(V, "npu_spotter", side_effect=OSError("vip_init failed")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertIs(V.spotter(dict(cfg, npu=True))[0], cpu)       # it cannot start: the CPU's listens
            self.assertEqual(V.ENGINE["engine"], "cpu")
            self.assertIn("vip_init failed", V.ENGINE["npu_error"])
            self.assertIs(V.spotter(cfg)[0], cpu)
            self.assertEqual(V.ENGINE["npu_error"], "")                     # switched off again: no old reason
            try:
                import kaldi_native_fbank  # noqa: F401
            except ImportError:                                             # the real files, a venv without its package
                with mock.patch.object(V, "NPU", P / "tools" / "board" / "npu"), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertIs(V.spotter(dict(cfg, npu=True))[0], cpu)
                self.assertIn("kaldi_native_fbank", V.ENGINE["npu_error"])

    def test_a_failing_npu_hands_the_listening_to_the_cpu_for_good(self):
        class Spot:                       # a spotter that counts what it decoded; `fail` makes its runs raise
            def __init__(self, name, fail=False):
                self.name, self.fail, self.decoded = name, fail, 0

            def create_stream(self):
                return types.SimpleNamespace(owner=self.name, fed=0, accept_waveform=lambda r, x: None)

            def is_ready(self, s):
                assert s.owner == self.name
                return True

            def decode_stream(self, s):
                if self.fail:
                    raise OSError("vip_run_network failed")
                self.decoded += 1

            def get_result(self, s):
                return ""

            def reset_stream(self, s):
                pass
        npu, cpu = Spot("npu"), Spot("cpu")
        made = []
        g = V.Guarded(npu, lambda: made.append(1) or cpu)
        V.ENGINE.update(engine="npu", npu_error="")
        st = g.create_stream()
        self.assertTrue(g.is_ready(st))
        g.decode_stream(st)
        self.assertEqual((npu.decoded, made), (1, []))
        npu.fail = True
        with contextlib.redirect_stdout(io.StringIO()) as out:
            g.decode_stream(st)                                             # fails: the CPU's from now on
        self.assertEqual(made, [1])
        self.assertEqual(V.ENGINE["engine"], "cpu")
        self.assertIn("vip_run_network failed", V.ENGINE["npu_error"])
        self.assertIn("the CPU's is listening", out.getvalue())
        self.assertTrue(g.is_ready(st))                                     # the old stream follows (a CPU one now)
        g.decode_stream(st)
        st.accept_waveform(16000, [0.0])
        self.assertEqual((cpu.decoded, made), (1, [1]))
        cpu.fail = True
        with self.assertRaisesRegex(OSError, "vip_run"):                    # the CPU's own error is not swallowed
            g.decode_stream(st)

    def test_their_wake_phrases_and_what_they_wake(self):
        cfg = V.clean(None)
        cfg["online"] = ["google", "brave", "computer"]        # "hey computer" is the commands' phrase already
        self.assertEqual(V.wake_phrases(cfg), ["hey computer", "hey assistant", "hey google", "hey brave"])
        act = V.Actions(cfg, launch=lambda l: None, notify=lambda *a: None, chime=lambda s: None, hush=lambda: False)
        self.assertEqual([act.kind_of(x) for x in ("HEY GOOGLE", "HEY ASSISTANT", "HEY COMPUTER", "HEY BRAVE")],
                         ["ask:google", "ask", "command", "ask:brave"])
        act.kind = "ask:brave"
        self.assertEqual(act.phrase(), "hey brave")

    def test_ask_routes_to_it_and_starts_no_model(self):
        asked = []
        with mock.patch.object(V, "ask", lambda q, wait=300.0, to="": asked.append((q, to)) or 0):
            self.assertEqual(V.main(["ask", "--to", "google", "what time is it"]), 0)
            self.assertEqual(V.main(["ask", "how hot is it"]), 0)
        self.assertEqual(asked, [("what time is it", "google"), ("how hot is it", "")])


class SettingsTest(unittest.TestCase):
    def test_defaults_under_what_is_set(self):
        cfg = V.clean({"wake": "  okay   board ", "match": 2, "sensitivity": -1, "window": "8", "mic": " usb ",
                       "commands": [{"say": "x", "run": 3}, "junk", {"say": "y"}], "other": 1})
        self.assertEqual(cfg["wake"], "okay board")
        self.assertEqual((cfg["match"], cfg["sensitivity"], cfg["window"], cfg["mic"]), (1.0, 0.05, 8.0, "usb"))
        self.assertEqual(cfg["commands"], [{"say": "x", "run": "3"}, {"say": "y", "run": ""}])
        self.assertNotIn("other", cfg)

    def test_a_wake_phrase_without_words_and_odd_input(self):
        self.assertEqual(V.clean({"wake": "!!", "match": True})["wake"], "hey computer")
        self.assertEqual(V.clean({"match": True})["match"], 0.75)
        self.assertEqual(V.clean(None), V.clean([]))
        self.assertEqual(V.clean(None)["commands"], V.DEFAULTS["commands"])
        V.clean(None)["commands"].clear()                 # the defaults are a copy
        self.assertTrue(V.clean(None)["commands"])

    def test_the_assistants_phrase(self):
        self.assertEqual(V.clean(None)["assistant"], "hey assistant")
        self.assertEqual(V.clean({"assistant": "  hey   brain "})["assistant"], "hey brain")
        self.assertEqual(V.clean({"assistant": ""})["assistant"], "")                    # none
        self.assertEqual(V.clean({"assistant": "?!"})["assistant"], "")
        self.assertEqual(V.clean({"wake": "hey board", "assistant": "Hey, board!"})["assistant"], "")   # not both
        self.assertEqual(V.wake_phrases(V.clean(None)), ["hey computer", "hey assistant"])
        self.assertEqual(V.wake_phrases(V.clean({"assistant": ""})), ["hey computer"])
        act = V.Actions(V.clean(None))
        self.assertEqual((act.kind_of("HEY ASSISTANT"), act.kind_of("HEY COMPUTER")), ("ask", "command"))
        self.assertEqual(V.Actions(V.clean({"assistant": ""})).kind_of("HEY ASSISTANT"), "command")

    def test_load_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            self.assertEqual(V.load_config(p), (V.clean(None), ""))
            for bad, why in (('{"wake": "hello board"', "Expecting"), ("null", "not a JSON object"),
                             ("[1]", "not a JSON object"), ('{"commands": "x"}', "not a list")):
                p.write_text(bad)
                cfg, problem = V.load_config(p)
                self.assertEqual(cfg, V.clean(None), bad)
                self.assertIn(why, problem, bad)
            V.write_json(p, {"wake": "hello board"})
            self.assertEqual(V.load_config(p)[0]["wake"], "hello board")
            self.assertEqual(sorted(os.listdir(d)), ["config.json"])                 # no .tmp left

    def test_keyword_line(self):
        self.assertEqual(V.keyword_line(["▁HE", "Y", "▁COMP", "U", "TER"]), "▁HE Y ▁COMP U TER")


class Stream:
    def __init__(self, clock=None):
        self.chunks, self.todo, self.hit, self.reset_at = 0, 0, False, -1
        self.clock, self.fed = clock, None

    def accept_waveform(self, rate, samples):
        assert rate == V.RATE
        self.chunks += 1
        self.todo += 1
        if self.clock:
            self.fed = self.clock()


class Spotter:
    """Spots the wake phrase on the chunks listed (counted from the stream's start); `said` names it, by chunk."""
    def __init__(self, at, said=None):
        self.at, self.streams, self.said = set(at), 0, said or {}

    def create_stream(self):
        self.streams += 1
        return Stream()

    def is_ready(self, s):
        return s.todo > 0

    def decode_stream(self, s):
        s.todo -= 1
        s.hit = s.chunks - s.todo in self.at and (s.chunks - s.todo)

    def get_result(self, s):
        return self.said.get(s.hit, "HEY COMPUTER") if s.hit else ""

    def reset_stream(self, s):
        s.hit = False


class Recogniser:
    """Ends a sentence when a stream has had the chunks listed, with the text given (a key (n, chunks): in the n-th
    stream only, counted from 1), and says so until the stream is reset or writes something new (a chunk in
    `partials` after it), as sherpa-onnx's does; otherwise, from the chunks in `partials`, what it has written so far.
    What it writes runs on from what it wrote before since the last reset, as sherpa-onnx's does (a text given whole
    is taken as it is; one given on its own follows the earlier ones). Each stream's `resets` are the chunks it was
    reset on. A fresh stream made in a session (the listener's, after a first sentence without a command: fed the
    stream before it this chunk) goes on with its chunks' count, and writes nothing of what that one wrote."""
    def __init__(self, sentences, partials=None, tick=lambda: 0):
        self.sentences, self.partials, self.streams, self.tick = sentences, partials or {}, [], tick

    def create_stream(self):
        st, prev = Stream(self.tick), (self.streams[-1] if self.streams else None)
        st.n, st.resets, st.origin = len(self.streams) + 1, [], None
        if prev is not None and prev.fed == self.tick():
            st.origin = self.tick() - self.pos(prev)
            st.reset_at = self.pos(prev)
        self.streams.append(st)
        return st

    def pos(self, s):
        """The stream's chunk count, as the session's first stream counts (`sentences` and `partials` are in it)."""
        return s.chunks if s.origin is None else self.tick() - s.origin

    def ends(self, s, upto):
        at = lambda c: c[1] if isinstance(c, tuple) else c
        mine = [c for c in self.sentences if s.reset_at < at(c) <= upto and (not isinstance(c, tuple) or c[0] == s.n)]
        return sorted(mine, key=lambda c: (at(c), isinstance(c, tuple))), at

    def said(self, s):
        ends, at = self.ends(s, self.pos(s))
        if not ends:
            return None
        return None if any(at(ends[-1]) < c <= self.pos(s) for c in self.partials) else self.sentences[ends[-1]]

    def whole(self, s, text, key):
        """`text` (written by chunk `key`) after what the stream wrote before it since the last reset."""
        ends, at = self.ends(s, key - 1)
        done = ""
        for t in [self.sentences[c] for c in ends] + [text]:
            done = t if t.startswith(done) else f"{done} {t}".strip()
        return done

    def is_ready(self, s):
        return False

    def decode_stream(self, s):
        raise AssertionError("not ready")

    def get_result(self, s):
        if s.reset_at == self.pos(s):
            return ""
        ends, at = self.ends(s, self.pos(s))
        seen = [c for c in self.partials if s.reset_at < c <= self.pos(s)]
        if self.said(s) is not None:
            return self.whole(s, self.said(s), at(ends[-1]))
        if seen and (not ends or max(seen) > at(ends[-1])):
            return self.whole(s, self.partials[max(seen)], max(seen))
        return self.whole(s, "", self.pos(s) + 1)

    def is_endpoint(self, s):
        return self.said(s) is not None and s.reset_at != self.pos(s)

    def reset(self, s):
        s.reset_at = self.pos(s)
        s.resets.append(self.pos(s))


class ListenerTest(unittest.TestCase):
    """The wake phrase is spotted at chunk 20 (2.0 s) in most of these: the recogniser's stream then has the 15
    chunks of pre-roll, and its chunk R + k comes at 2.0 + k/10 s."""

    def run_listener(self, wake_at, sentences, chunks, cfg=None, partials=None, said=None, loud=(), reading=(),
                     read_from=None):
        """`reading`: whether an answer was being read aloud at each wake phrase (hush's answers), none by default;
        `read_from`: the second from which an answer is being read (Actions.reading)."""
        self.done, self.notes, self.chimes, self.states, self.at, self.keyed, self.keyed_at = [], [], [], [], [], [], []
        clock = [0.0]
        hushes = list(reading)
        act = V.Actions(cfg or V.clean({"commands": CMDS}), launch=lambda l: self.done.append(l) or self.at.append(round(clock[0], 1)),
                        notify=lambda t, b="": self.notes.append((t, b)), chime=self.chimes.append,
                        status=lambda **kw: self.states.append(kw), hush=lambda: hushes.pop(0) if hushes else False,
                        reading=lambda: read_from is not None and clock[0] >= read_from,
                        keys=lambda lines: self.keyed.append(lines) or self.keyed_at.append(round(clock[0], 1)) or "")
        step = [0]
        self.asr = Recogniser(sentences, partials, tick=lambda: step[0])
        self.kws = Spotter(wake_at, said)
        listener = V.Listener(self.kws, self.asr, act, now=lambda: clock[0])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            for n in range(1, chunks + 1):
                clock[0] += 0.1
                step[0] = n
                listener.feed([0.3 if n in loud else 0.0] * V.CHUNK)
        self.printed = out.getvalue()
        return listener

    def test_wake_and_command_in_one_breath(self):
        listener = self.run_listener([20], {R + 12: "Hey computer, open Firefox."}, 60)
        self.assertEqual((self.done, self.at), (["firefox"], [3.2]))
        self.assertEqual(len(self.asr.streams), 1)
        self.assertEqual(self.notes, [("▶ open firefox", "firefox")])
        self.assertEqual(len(self.chimes), 1)
        self.assertIsNone(listener.rs)                    # the recogniser stops with the command
        self.assertEqual([s.get("state") for s in self.states], ["awake", "listening"])
        self.assertEqual(self.states[-1]["heard"], "open firefox")

    def test_a_sentence_does_not_end_while_the_voice_goes_on(self):
        # the recogniser ends "type double-u double-u double-u …", said in one go, at "type www" (the owner's voice, board
        # 2026-11-22): the end waits for half a second without voice, and it is all there then
        said = {R + 8: "Hey computer, type www", R + 14: "Hey computer, type www dot google dot com"}
        written = {R + 7: "Hey computer, type ww", R + 10: "Hey computer, type www dot google"}
        self.run_listener([20], said, 50, partials=written, loud=set(range(12, 34)))     # to 3.3 s, in one breath
        self.assertEqual((self.keyed, self.keyed_at), ([["type www.google.com"]], [3.8]))
        self.assertEqual((len(self.asr.streams), self.asr.streams[0].resets), (1, [R + 18]))
        self.run_listener([20], said, 50, partials=written)        # a pause there: it ends where the recogniser says
        self.assertEqual(self.keyed, [["type www"]])
        # the rest said, the voice stopped, and the rest written 0.9 s after: the end waits for it (review 2026-11-22)
        self.run_listener([20], {R + 8: "Hey computer, type www", R + 24: "Hey computer, type www dot google dot com"},
                          60, partials={R + 7: "Hey computer, type ww", R + 22: "Hey computer, type www dot google dot com"},
                          loud=set(range(12, 34)))
        self.assertEqual((self.keyed, self.keyed_at), ([["type www.google.com"]], [4.4]))
        # a pause after the wake phrase is not between sentences: "hey computer" [0.6 s] "type double-u …"
        self.run_listener([20], {R + 5: "Hey computer", R + 12: "Hey computer, type ww",
                                 R + 22: "Hey computer, type www dot google dot com"}, 60,
                          partials={R + 7: "Hey computer, type", R + 9: "Hey computer, type ww",
                                    R + 18: "Hey computer, type www dot google dot com"},
                          loud=set(range(12, 18)) | set(range(24, 41)))
        self.assertEqual(self.keyed, [["type www.google.com"]])
        # the same with the wake phrase said again, in a session open since "hey computer" (review 2026-11-22)
        self.run_listener([20], {R + 5: "Hey computer", R + 30: "Hey computer, type ww",
                                 R + 40: "Hey computer, type www dot google dot com"}, 70,
                          partials={R + 16: "Hey computer", R + 24: "Hey computer, type", R + 27: "Hey computer, type ww",
                                    R + 36: "Hey computer, type www dot google dot com"},
                          loud=set(range(12, 18)) | set(range(30, 36)) | set(range(42, 59)))
        self.assertEqual(self.keyed, [["type www.google.com"]])
        # a voice that never stops (a television talking): the end waits 2 s at most; a steady noise is the room's
        # own level, quiet, and the end does not wait at all
        self.run_listener([20], {R + 8: "Hey computer, type hello"}, 80, loud=set(range(12, 81)))
        self.assertEqual((self.keyed, self.keyed_at), ([["type hello"]], [5.3]))
        # in all, across the ends a new word clears: a television talking typed its words for 30 s (review 2026-11-22)
        self.run_listener([20], {R + 8: "Hey computer, type hello", R + 20: "Hey computer, type hello I told you",
                                 R + 32: "Hey computer, type hello I told you not to"}, 80,
                          partials={R + 18: "Hey computer, type hello I", R + 30: "Hey computer, type hello I told you not",
                                    R + 45: "Hey computer, type hello I told you not to go there"},
                          loud=set(range(12, 81)))
        self.assertEqual((self.keyed, self.keyed_at), ([["type hello I told you not to"]], [5.7]))
        self.run_listener([20], {R + 8: "Hey computer, type hello"}, 80, loud=set(range(1, 81)))
        self.assertEqual((self.keyed, self.keyed_at), ([["type hello"]], [2.8]))

    def test_a_fresh_stream_for_the_command(self):
        # the recogniser wrote the phrase past knowing ("Hey, Kyivi"), its end came while "address bar" was said, and
        # the reset there left "re bar"; run on unreset, it wrote nothing more at times (the owner's voice, board
        # 2026-11-23): the command gets a fresh stream, from just before the voice that came after the phrase
        self.run_listener([20], {R + 5: "Hey, Kyivi", R + 14: "open Firefox."}, 50,
                          loud=set(range(12, 18)) | set(range(24, 34)))
        self.assertEqual((self.done, self.asr.streams[0].resets, len(self.asr.streams)), (["firefox"], [], 2))
        # fed from chunk 21, 0.3 s before the voice that came back at 24 (never the phrase, at 12-17), to 34 (the
        # command ran early there)
        self.assertEqual(self.asr.streams[1].chunks, (R + 5 - 21 + 1) + (34 - (R + 5)))
        # the wake phrase alone, then nothing: the session ends as before, told so
        self.run_listener([20], {R + 5: "Hey computer."}, 150, loud=set(range(12, 18)))
        self.assertEqual((self.done, self.keyed), ([], []))
        self.assertIn("no command after the wake phrase", self.printed)

    def test_what_a_fresh_stream_is_fed(self):
        # each chunk tagged with its number (its last sample: the loudness stays), the fresh stream's first chunk read
        # back: from 0.3 s before the voice after the pause after the phrase; the last 0.3 s while that pause lasts;
        # from the spot in one breath; and no fresh stream for a question's words (they were taken: review 2026-11-23)
        def first_fed(wake_at, sentences, chunks, loud, said=None):
            fed = []
            asr = Recogniser(sentences, tick=lambda: step[0])
            make = asr.create_stream

            def create_stream():
                st = make()
                st.accept_waveform = lambda rate, samples, st=st, take=st.accept_waveform: (
                    take(rate, samples), fed.append((len(asr.streams), round(samples[-1] * 1e6))))
                return st
            asr.create_stream = create_stream
            act = V.Actions(V.clean({"commands": CMDS}), launch=lambda l: None, notify=lambda t, b="": None,
                            chime=lambda s: None, status=lambda **kw: None, hush=lambda: False,
                            reading=lambda: False, keys=lambda lines: "")
            listener, step = V.Listener(Spotter(wake_at, said), asr, act, now=lambda: step[0] / 10), [0]
            with contextlib.redirect_stdout(io.StringIO()):
                for n in range(1, chunks + 1):
                    step[0] = n
                    listener.feed([0.3 if n in loud else 0.0] * (V.CHUNK - 1) + [n * 1e-6])
            return [c for k, c in fed if k == 2][:1], len(asr.streams)
        phrase = set(range(12, 18))                              # "hey computer" at 12-17, spotted at 20
        # a pause after it (18-23), the command from 24, its end at R + 5 = 25 while it is said: from 21 (0.3 s before)
        self.assertEqual(first_fed([20], {R + 5: "Hey, Kyivi", R + 14: "open Firefox."}, 50, phrase | set(range(24, 34))),
                         ([21], 2))
        # the command said and over before the end came ("mute" at 24-27, quiet since): from 21 still, not the last 0.3 s
        self.assertEqual(first_fed([20], {R + 12: "Hey, Kyivi", R + 20: "mute"}, 50, phrase | set(range(24, 28))),
                         ([21], 2))
        # the same after a gap under 0.5 s (no pause: 18-21), "mute" at 22-25: from the spot, not the last 0.3 s
        self.assertEqual(first_fed([20], {R + 12: "Hey, Kyivi", R + 20: "mute"}, 50, phrase | set(range(22, 26))),
                         ([20], 2))
        # still in the pause after the phrase: the last 0.3 s
        self.assertEqual(first_fed([20], {R + 9: "Hey, Kyivi", R + 20: "mute"}, 50, phrase), ([27], 2))
        # one breath (no pause after the phrase): from the spot
        self.assertEqual(first_fed([20], {R + 5: "Hey, Kyivi", R + 14: "open Firefox."}, 50, set(range(12, 34))),
                         ([20], 2))
        # a question's first sentence with its words: reset, no fresh stream
        self.assertEqual(first_fed([20], {R + 5: "Hey assistant, what is the weather", R + 30: "in Tokyo"}, 60,
                                   phrase | set(range(24, 34)), said={20: "HEY ASSISTANT"}), ([], 1))

    def test_the_wait_on_the_wake_phrase_does_not_spend_the_commands_hold(self):
        # "hey cubie, type double-u double-u double-u dot google dot com", said slowly: the recogniser wrote nothing
        # new for 2.4 s, so the wake phrase's end was held all its 2.5 s; the command's own end came while the voice
        # went on, and was taken at "type ww" (the owner's voice, board 2026-11-23)
        self.run_listener([20], {R + 5: "Hey computer", R + 32: "Hey computer, type ww",
                                 R + 40: "Hey computer, type www dot google dot com"}, 80,
                          partials={R + 30: "Hey computer, type", R + 36: "Hey computer, type www dot google dot com"},
                          loud=set(range(12, 57)))
        self.assertEqual(self.keyed, [["type www.google.com"]])

    def test_the_command_said_as_the_wake_phrase_ends_keeps_its_start(self):
        # "hey computer" [0.6 s] "type hello": the recogniser ended "Hey computer" while "type" was said, and the
        # reset cut it to "pe" (board 2026-11-22): a sentence of the wake phrase alone waits for what comes after it
        self.run_listener([20], {R + 5: "Hey computer", R + 14: "Hey computer, type hello"}, 50,
                          partials={R + 8: "Hey computer, type"}, loud=set(range(12, 18)) | set(range(24, 34)))
        self.assertEqual(self.keyed, [["type hello"]])
        self.assertEqual(self.asr.streams[0].resets, [R + 18])      # not at R + 5, in "type"
        # the wake phrase and a hesitation, the same (review 2026-11-22)
        self.run_listener([20], {R + 5: "Hey computer, uh", R + 14: "Hey computer, uh, type hello"}, 50,
                          partials={R + 8: "Hey computer, uh, type"}, loud=set(range(12, 18)) | set(range(24, 34)))
        self.assertEqual((self.keyed, self.asr.streams[0].resets), ([["type hello"]], [R + 18]))
        # nothing of the pre-roll written: what comes is all command, in the same stream, unreset (board 2026-11-22)
        self.run_listener([20], {R + 5: "", R + 14: "type hello"}, 50,
                          partials={R + 8: "type"}, loud=set(range(12, 18)) | set(range(24, 34)))
        self.assertEqual((self.keyed, self.asr.streams[0].resets), ([["type hello"]], [R + 18]))
        # the command written, the wake phrase not (the first sentence is the pre-roll's): its end is taken, and the
        # command gets a fresh stream (no reset in it); held, the command went into it and was dropped with it
        self.run_listener([20], {R + 5: "and so", R + 14: "type hello"}, 50,
                          partials={R + 8: "type"}, loud=set(range(12, 18)) | set(range(24, 34)))
        self.assertEqual((self.keyed, self.asr.streams[0].resets, len(self.asr.streams)), ([["type hello"]], [], 2))

    def test_a_dot_where_www_was_said(self):
        # the recogniser wrote "type dot Google dot com" for "type www dot google dot com" (the owner's voice, board
        # 2026-11-23): "dot google.com" was typed; Firefox takes google.com as well as www.google.com
        self.run_listener([20], {R + 12: "Hey computer, type dot Google dot com"}, 60)
        self.assertEqual(self.keyed, [["type google.com"]])
        self.run_listener([20], {R + 12: "Hey computer, type dot net is a framework"}, 60)   # no address after it
        self.assertEqual(self.keyed, [["type dot net is a framework"]])

    def test_pe_is_type(self):
        # the recogniser wrote "pe" for "type" after the wake phrase 5 times of 8 (owner, board 2026-11-22/23); the
        # last one, as written: nothing was typed
        self.run_listener([20], {R + 12: "Hey cubie, pe ww dotgoogle dot com"}, 60, cfg=V.clean({"wake": "hey cubie"}))
        self.assertEqual(self.keyed, [["type www.google.com"]])
        self.run_listener([20], {R + 12: "Hey computer, pe hello world"}, 60)
        self.assertEqual(self.keyed, [["type hello world"]])
        # taken into the phrase when that is written close ("heykubipe" comes closer to "heycubie" than "heykubi"):
        # given back, as the command's first word (review 2026-11-23)
        cubie = V.clean({"wake": "hey cubie"})
        for said in ("Hey Kubi, pe hello world", "Hey cubi pe hello world", "Hey Kubity pe hello world"):
            self.run_listener([20], {R + 12: said}, 60, cfg=cubie)
            self.assertEqual((self.keyed, self.done), ([["type hello world"]], []), said)
        self.run_listener([20], {R + 12: "Hey Kubi, pe mute"}, 60, cfg=cubie)
        self.assertEqual((self.keyed, self.done), ([["type mute"]], []))          # typed, not muted
        # never the phrase's own end: a phrase ending so keeps it, as before ("hey ho pe" for "hey hope", "penelo pe",
        # a long one, "proto type", and the end of "prototype" when only that was caught: reviews 2026-11-23)
        mute = "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"
        for wake, said, ran in (("hey hope", "Hey ho pe, open Firefox.", "firefox"),
                                ("hey penelope", "Hey Penelo pe mute.", mute),
                                ("okay google my telescope", "Okay Google my telesco pe, open Firefox.", "firefox"),
                                ("hey prototype", "Hey proto type, open Firefox.", "firefox"),
                                ("hey prototype", "type open Firefox.", "firefox"),
                                ("peep", "Pe, open Firefox.", "firefox")):
            self.run_listener([20], {R + 12: said}, 60, cfg=V.clean({"wake": wake}))
            self.assertEqual((self.keyed, self.done[:1]), ([], [ran]), said)
        # a whole "type" after a phrase ending in "pe" is the command's (review 2026-11-23)
        for wake, said in (("hope", "Ho type mute."), ("skype", "Sky type mute.")):
            self.run_listener([20], {R + 12: said}, 60, cfg=V.clean({"wake": wake}))
            self.assertEqual((self.keyed, self.done), ([["type mute"]], []), said)
        self.assertEqual(V.builtin_words(["pe"]), "")

    def test_a_miss_says_whether_it_came_with_the_wake_phrase(self):
        # in one breath with the phrase (a "pe" there is the recogniser's) or in a later sentence (after a reset);
        # nothing said before the command is printed, the pre-roll least of all (reviews 2026-11-23)
        for said in ("My PIN is four two one six. Hey commuter, open the pod bay doors.",
                     "It is on my computer. Hey commuter, open the pod bay doors.",
                     "My computer is slow. Hey commuter, open the pod bay doors.",       # open since 11-23
                     "Call me at noon, a. Hey computer, open the pod bay doors."):
            self.run_listener([20], {R + 12: said}, 60)
            self.assertIn("heard: 'open the pod bay doors' (with the wake phrase)\nno command matched", self.printed)
            for before in ("pin", "six", "my computer", "noon", "'a "):
                self.assertNotIn(before, self.printed.lower(), said)
        self.run_listener([20], {R + 5: "Hey computer.", R + 30: "Open the pod bay doors."}, 80)
        self.assertIn("heard: 'open the pod bay doors'\nno command matched", self.printed)

    def test_a_sound_before_a_command(self):
        # "uh, type mute" types; it does not mute at once for its last word (review 2026-11-22)
        self.run_listener([20], {R + 12: "Hey computer, uh, type mute"}, 60, partials={R + 8: "Hey computer, uh, type mute"})
        self.assertEqual((self.keyed, self.done), ([["type mute"]], []))
        # "okay, go to sleep": the owner's command, not a search, the wake phrase written close only (no early run)
        cfg = V.clean({"commands": CMDS + [{"say": "go to sleep", "run": "systemctl suspend"}]})
        self.run_listener([20], {R + 12: "Hey commuter, okay go to sleep"}, 60, cfg=cfg)
        self.assertEqual(self.done, ["systemctl suspend"])
        # "and" is talk, not a sound: no search for "bed kids"
        with mock.patch.object(V.shutil, "which", return_value="/usr/bin/firefox"):
            self.run_listener([20], {R + 12: "Hey computer, and go to bed kids"}, 60, cfg=cfg)
        self.assertEqual((self.done, self.keyed), ([], []))
        self.assertEqual(self.notes[-1][0], "No command matched")

    def test_after_a_pause_the_voice_is_a_new_sentence(self):
        # the recogniser's end comes while the next sentence is being said: after a pause, it is a new one, as it
        # was (review 2026-11-22: held, "hey computer, mute" went into the question, and "stop dictation" was typed)
        self.run_listener([20, 47], {R + 24: "Hey assistant, what time is it", R + 40: "Hey computer, mute."}, 100,
                          partials={R + 8: "Hey assistant, what time is it", R + 26: "Hey computer"},
                          said={20: "HEY ASSISTANT", 47: "HEY COMPUTER"}, loud=set(range(12, 28)) | set(range(43, 53)))
        self.assertEqual(len(self.done), 2, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        self.assertEqual(self.done[1], "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
        listener = self.run_listener([20], {R + 10: "Hey computer, start dictation.", R + 47: "i am fine period see you",
                                            R + 58: "stop dictation"}, 100,
                                     partials={R + 33: "i am fine period see you", R + 50: "stop"},
                                     loud=set(range(40, 56)) | set(range(66, 76)))
        self.assertEqual(self.keyed, [["type I am fine. See you"]])
        self.assertEqual([n[0] for n in self.notes], ["Dictation on", "Dictation off"])
        self.assertIsNone(listener.rs)
        # a sentence of a hesitation's word alone is typed, and a pause after it ends it ("Okay." [0.7 s] "delete
        # that" was typed as "okay delete that": review 2026-11-22)
        self.run_listener([20], {R + 10: "Hey computer, start dictation.", R + 34: "Okay.", R + 44: "delete that"}, 100,
                          partials={R + 23: "Okay.", R + 37: "delete"}, loud=set(range(40, 46)) | set(range(53, 63)))
        self.assertEqual(self.keyed, [["type Okay"], ["key backspace backspace backspace backspace"]])

    def test_the_recogniser_starts_with_what_came_just_before(self):
        self.run_listener([5], {}, 6)
        self.assertEqual(self.asr.streams[0].chunks, 6)   # the 5 chunks so far, then the next one
        self.run_listener([40], {}, 40)
        self.assertEqual(self.asr.streams[0].chunks, V.PREROLL)

    def test_type_and_go_to_by_voice(self):
        self.run_listener([20], {R + 12: "Hey computer, type www dot google dot com"}, 60)
        self.assertEqual((self.keyed, self.done), ([["type www.google.com"]], []))
        self.run_listener([20], {R + 12: "Hey computer, type hello comma world"}, 60)
        self.assertEqual(self.keyed, [["type hello, world"]])
        self.run_listener([20], {R + 12: "Hey computer, go to lemonde dot fr"}, 60)
        self.assertEqual((self.keyed, self.done), ([], ["xdg-open https://lemonde.fr"]))
        # no address said: searched for, never guessed into a site ("my bank" is not mybank.com)
        with mock.patch.object(V.shutil, "which", return_value="/usr/bin/firefox"):
            self.run_listener([20], {R + 12: "Hey computer, go to my bank"}, 60)
        self.assertEqual(self.done, ["firefox --search 'my bank'"])
        with mock.patch.object(V.shutil, "which", return_value=None):
            self.run_listener([20], {R + 12: "Hey computer, go to my bank"}, 60)
        self.assertEqual(self.done, ["xdg-open 'https://duckduckgo.com/?q=my%20bank'"])
        # a command said word for word comes first: the owner's "go to sleep", as the panel's Test would run it
        cfg = V.clean({"commands": CMDS + [{"say": "go to sleep", "run": "systemctl suspend"}]})
        self.run_listener([20], {R + 12: "Hey computer, go to sleep"}, 60, cfg=cfg)
        self.assertEqual(self.done, ["systemctl suspend"])
        # "type …" never runs early, on the words said so far: the sentence's end decides what is typed
        self.run_listener([20], {R + 20: "Hey computer, type open firefox please"}, 60,
                          partials={R + 10: "Hey computer, type open firefox"})
        self.assertEqual((self.done, self.keyed), ([], [["type open firefox please"]]))

    def test_dictation_types_each_sentence_until_stopped(self):
        found = [{"id": "system:press-enter", "say": "press enter", "run": "a7a-voice key enter", "on": True, "kind": "system"},
                 {"id": "system:pause", "say": "pause", "run": "a7a-voice key playpause", "on": True, "kind": "system"}]
        cfg = V.clean({"commands": CMDS, "found": found})
        listener = self.run_listener([20], {R + 10: "Hey computer, start dictation.", R + 30: "hello there",
                                            R + 50: "how are you question mark", R + 70: "press enter",
                                            R + 90: "i am fine period see you", R + 110: "stop dictation",
                                            R + 130: "this is not typed"}, 200, cfg=cfg)
        self.assertEqual(self.keyed, [["type Hello there"], ["type  how are you?"], ["type  I am fine. See you"]])
        self.assertEqual(self.done, ["a7a-voice key enter"])     # a command's words exactly: the command
        self.assertEqual([n[0] for n in self.notes], ["Dictation on", "▶ press enter", "Dictation off"])
        self.assertIsNone(listener.rs)
        self.assertEqual(len(self.asr.streams), 1)                 # one stream throughout: no wake phrase between
        # punctuation said alone is typed (and spaces what follows right); a delete puts the tail back; a command's
        # words other than a key's are typed
        listener = self.run_listener([20], {R + 10: "Hey computer, start dictation.", R + 30: "hello there",
                                            R + 50: "new line", R + 70: "how are you", R + 90: "delete that",
                                            R + 110: "fine thanks", R + 130: "mute", R + 150: "new paragraph dear sir",
                                            R + 170: "pause"},
                                     200, cfg=cfg)
        self.assertEqual(self.keyed, [["type Hello there"], ["key shift+enter"], ["type How are you"],
                                      ["key " + " ".join(["backspace"] * 11)], ["type Fine thanks"], ["type  mute"],
                                      ["key shift+enter", "key shift+enter", "type Dear sir"], ["type  pause"]])
        self.assertEqual(self.done, [])

    def test_dictation_ends_after_a_minute_of_silence_or_at_a_wake_phrase(self):
        listener = self.run_listener([20], {R + 10: "Hey computer, start dictation."}, 20 + R + 10 + 610)
        self.assertEqual(self.notes[-1], ("Dictation off", "A minute without a word"))
        self.assertFalse(listener.act.dictating)
        self.run_listener([20, 60], {(1, R + 10): "Hey computer, start dictation.", (2, R + 10): "Hey computer, mute."},
                          120)
        self.assertEqual(self.done, ["wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"])
        self.assertEqual(self.keyed, [])                           # "hey computer, mute" was not typed

    def test_delete_that_takes_back_what_was_typed_last(self):
        self.run_listener([20, 60], {(1, R + 10): "Hey computer, type hi there", (2, R + 10): "Hey computer, delete that"},
                          120)
        self.assertEqual(self.keyed, [["type hi there"], ["key " + " ".join(["backspace"] * 8)]])

    def test_wake_phrase_then_a_pause_then_the_command(self):
        self.run_listener([20], {R + 5: "Hey computer.", R + 30: "Open Firefox."}, 80)
        self.assertEqual((self.done, self.at), (["firefox"], [5.0]))

    def test_nothing_is_written_down_without_the_wake_phrase(self):
        self.run_listener([], {5: "open firefox", 30: "volume up"}, 100)
        self.assertEqual((self.asr.streams, self.done, self.notes, self.chimes, self.printed), ([], [], [], [], ""))

    def test_what_came_before_the_wake_phrase_is_not_a_command_and_not_kept(self):
        # "mute" said just before the phrase, caught by the pre-roll: the partials hold it from the start
        self.run_listener([20], {R + 25: "Mute. Hey computer, open Firefox."}, 80,
                          partials={R + 3: "Mute.", R + 6: "Mute. Hey computer", R + 10: "Mute. Hey computer, open Firefox"})
        self.assertEqual(self.done, ["firefox"])
        self.assertNotIn("mute", self.printed.lower())
        self.assertNotIn("mute", json.dumps(self.states + self.notes).lower())
        # at the sentence's end too: a stale command of the same length does not win the tie
        self.run_listener([20], {R + 25: "volume up hey computer volume down"}, 80)
        self.assertEqual(self.done, ["wpctl set-volume @DEFAULT_AUDIO_SINK@ 10%-"])

    def test_a_command_inside_the_wake_phrase_does_not_run_on_the_phrase(self):
        cfg = V.clean({"commands": [{"say": "computer", "run": "x"}, {"say": "volume up", "run": "up"}]})
        self.run_listener([20], {R + 8: "Hey computer.", R + 30: "Volume up."}, 80, cfg=cfg, partials={R + 4: "Hey computer"})
        self.assertEqual(self.done, ["up"])

    def test_talk_before_the_wake_phrase_then_a_pause(self):
        self.run_listener([20], {R + 8: "Okay, so, hey computer.", R + 30: "Open Firefox."}, 80)
        self.assertEqual((self.done, [n[0] for n in self.notes]), (["firefox"], ["▶ open firefox"]))

    def test_a_stray_spot_in_a_later_sentence_cuts_nothing(self):
        cfg = V.clean({"commands": CMDS + [{"say": "shut down the computer", "run": "poweroff"}]})
        self.run_listener([20, 20 + 15], {R + 5: "Hey computer.", R + 30: "Shut down the computer."}, 80, cfg=cfg)
        self.assertEqual(self.done, ["poweroff"])

    def test_a_hesitation_after_the_wake_phrase_waits_for_the_command(self):
        self.run_listener([20], {R + 8: "Hey computer, um, okay.", R + 30: "Open Firefox."}, 80)
        self.assertEqual((self.done, [n[0] for n in self.notes]), (["firefox"], ["\u25b6 open firefox"]))

    def test_a_command_in_the_pre_roll_does_not_run_before_the_owners_phrase_is_written(self):
        # "my computer, mute" just before the owner's "hey computer, open firefox": the early partial holds only "my
        # computer" (close, not as written), so nothing runs from it; the end of the sentence finds the owner's phrase
        self.run_listener([20], {R + 25: "My computer, mute. Hey computer, open Firefox."}, 80,
                          partials={R + 3: "My computer, mute.", R + 10: "My computer, mute. Hey computer, open Firefox"})
        self.assertEqual(self.done, ["firefox"])

    def test_a_wake_phrase_the_pre_roll_caught_the_end_of(self):
        self.run_listener([20], {R + 12: "uter, open Firefox."}, 60)
        self.assertEqual(self.done, ["firefox"])

    def test_a_word_after_the_wake_phrase_that_is_no_command(self):
        self.run_listener([20], {R + 10: "Hey computer, stop."}, 40)
        self.assertEqual((self.done, self.notes), ([], [("No command matched", "Heard: “stop”")]))

    def test_no_command_within_the_window(self):
        listener = self.run_listener([10], {}, 10 + 81)
        self.assertEqual(self.done, [])
        self.assertEqual([n[0] for n in self.notes], ["No command heard"])
        self.assertIsNone(listener.rs)

    def test_no_giving_up_while_words_come(self):
        # spotted at 1.0 s; the command's words from 8.0 s on, its sentence ends at 10.5 s: past the 8 s window, taken
        self.run_listener([10], {10 + 8: "Hey computer.", 10 + 95: "Open Firefox."}, 120, partials={10 + 70: "Open"})
        self.assertEqual((self.done, [n[0] for n in self.notes]), (["firefox"], ["▶ open firefox"]))

    def test_a_command_nobody_typed(self):
        self.run_listener([10], {10 + 20: "Hey computer, open Chrome."}, 60)
        self.assertEqual(self.done, [])
        self.assertEqual(self.notes, [("No command matched", "Heard: “open chrome”")])

    def test_the_wake_phrase_twice_then_a_command(self):
        self.run_listener([10, 40], {10 + 8: "Hey computer.", 10 + 38: "Hey computer.", 10 + 60: "volume up"}, 120)
        self.assertEqual(self.done, ["wpctl set-volume -l 1.0 @DEFAULT_AUDIO_SINK@ 10%+"])
        self.assertEqual(len(self.chimes), 2)
        # the phrase said again opens no stream of its own; the one after the first (a sentence without a command)
        # is the fresh one for the command (Listener.fresh)
        self.assertEqual(len(self.asr.streams), 2)

    def test_a_command_heard_whole_runs_before_the_sentence_ends(self):
        self.run_listener([20], {R + 30: "Hey computer, open firefox."}, 80,
                          partials={R + 10: "Hey computer", R + 15: "Hey computer, open firefox"})
        self.assertEqual((self.done, self.at), (["firefox"], [3.5]))
        self.assertEqual(len(self.notes), 1)              # not run again at the sentence's end

    def test_once_per_sentence(self):
        # a match at the sentence's end releases the recogniser: a later sentence of that stream is never read
        listener = self.run_listener([20], {R + 20: "Hey computer, open fire fox.", R + 40: "volume up"}, 100,
                                     partials={R + 15: "Hey computer, open fire fox"})
        self.assertEqual(self.done, ["firefox"])
        self.assertIsNone(listener.rs)

    def test_a_close_partial_waits_for_the_end(self):
        # "fire fox" is close (score 1.0 by its letters), not word for word: it runs when the sentence ends
        self.run_listener([20], {R + 30: "Hey computer, open fire fox."}, 80, partials={R + 15: "Hey computer, open fire fox"})
        self.assertEqual((self.done, self.at), (["firefox"], [5.0]))

    def test_a_phrase_inside_a_longer_one_waits_for_the_end(self):
        cfg = V.clean({"commands": [{"say": "volume up", "run": "up"}, {"say": "turn volume up to max", "run": "max"}]})
        self.run_listener([20], {R + 30: "Hey computer, turn volume up to max"}, 80, cfg=cfg,
                          partials={R + 10: "Hey computer, turn volume up", R + 20: "Hey computer, turn volume up to"})
        self.assertEqual((self.done, self.at), (["max"], [5.0]))
        self.run_listener([20], {R + 30: "Hey computer, turn volume up"}, 80, cfg=cfg, partials={R + 10: "Hey computer, turn volume up"})
        self.assertEqual((self.done, self.at), (["up"], [5.0]))

    def test_a_question_for_the_assistant(self):
        # "hey assistant" spotted: what follows goes to `a7a-voice ask`, whole, even when it holds a command's phrase
        self.run_listener([20], {R + 20: "Hey assistant, open Firefox and tell me how hot it is."}, 80,
                          said={20: "HEY ASSISTANT"}, partials={R + 10: "Hey assistant, open Firefox"})
        self.assertEqual(len(self.done), 1)
        self.assertTrue(self.done[0].endswith(" ask 'open firefox and tell me how hot it is'"), self.done[0])
        self.assertEqual(self.states[-1]["ran"], "the Assistant")
        self.assertEqual(self.notes, [])                  # `ask` tells the owner itself
        # "hey assistant" alone: the question may come after a pause
        self.run_listener([20], {R + 5: "Hey assistant.", R + 30: "What time is it?"}, 100, said={20: "HEY ASSISTANT"})
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        # and nothing at all: told so, as a question
        self.run_listener([10], {}, 100, said={10: "HEY ASSISTANT"})
        self.assertEqual([n[0] for n in self.notes], ["No question heard"])

    def test_a_question_with_pauses_in_it_is_asked_whole(self):
        # the owner's "what is the situation … with the memory" went out at its first pause (2026-11-19): the end of
        # a sentence no longer ends a question, ASK_PAUSE without a new word does
        self.run_listener([20], {R + 12: "Hey assistant, what is the situation", R + 30: "with the memory",
                                 R + 47: "on this board"}, 120, said={20: "HEY ASSISTANT"})
        self.assertEqual(len(self.done), 1, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what is the situation with the memory on this board'"), self.done)
        self.assertTrue(8.7 <= self.at[0] <= 8.8, self.at)  # 2 s after its last words (6.7 s), not at its first pause
        self.assertEqual(self.notes, [])                  # past the 8 s window, but a question was under way
        # one that runs on and on goes out at ASK_LONGEST, with what was being said then
        talk = {R + 10 + 10 * k: f"part {k}" for k in range(1, 39)}             # a sentence a second, to 41.0 s
        self.run_listener([20], {**talk, R + 10: "Hey assistant, first"}, 450, said={20: "HEY ASSISTANT"},
                          partials={R + 395: "and the last words"})             # 41.5 s: being said at 42.0 s
        self.assertEqual(len(self.done), 1)
        self.assertTrue(self.done[0].endswith("part thirty eight and the last words'"), self.done[0])
        self.assertTrue(42.0 <= self.at[0] <= 42.1, self.at)                  # 40 s after the wake phrase

    def test_hey_assistant_stop_during_a_reading_asks_nothing(self):
        # its "answer" would be read aloud in turn (review 2026-11-19)
        for stop in ("stop", "that's enough thank you"):
            self.run_listener([20], {13: stop}, 100, said={20: "HEY ASSISTANT"}, reading=[True])
            self.assertEqual((self.done, self.notes), ([], []), stop)
        self.run_listener([20], {13: "and tomorrow"}, 100, said={20: "HEY ASSISTANT"}, reading=[True])
        self.assertEqual(len(self.done), 1)                         # a real question still goes

    def test_a_question_goes_at_once_when_an_answer_begins_to_be_read(self):
        # the earlier question's answer starting to be read must not join this question, nor hold it open
        self.run_listener([20], {R + 12: "Hey assistant, what time is it", R + 25: "the board is at fifty degrees"},
                          100, said={20: "HEY ASSISTANT"}, read_from=4.0)
        self.assertEqual(self.at, [4.0])
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)

    def test_a_command_after_a_question(self):
        # "hey assistant, what time is it" [1.5 s] "hey computer, mute": the question goes, then the command runs
        self.run_listener([20, 47], {R + 12: "Hey assistant, what time is it", R + 30: "Hey computer, mute."}, 100,
                          said={20: "HEY ASSISTANT", 47: "HEY COMPUTER"})
        self.assertEqual(len(self.done), 2, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        self.assertEqual(self.done[1], "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")

    def test_the_commands_phrase_right_after_the_assistants(self):
        # "hey assistant" [1 s] "hey computer, mute", the two in one sentence (its end held for the voice): a command,
        # not the question "hey computer mute" (review 2026-11-23)
        self.run_listener([20], {R + 20: "Hey assistant. Hey computer, mute."}, 80, said={20: "HEY ASSISTANT"})
        self.assertEqual(self.done, ["wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"])
        self.run_listener([20], {R + 20: "Hey assistant, um, hey computer, mute."}, 80, said={20: "HEY ASSISTANT"})
        self.assertEqual(self.done, ["wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"])
        # a question that only holds the phrase's words further on stays a question
        self.run_listener([20], {R + 20: "Hey assistant, is hey computer a good name?"}, 80, said={20: "HEY ASSISTANT"})
        self.assertTrue(self.done[0].endswith(" ask 'is hey computer a good name'"), self.done)

    def test_a_question_then_the_commands_phrase_in_the_same_sentence(self):
        # "hey assistant, what time is it" [0.6 s] "hey computer, mute": too short a pause for the recogniser to end
        # the sentence, so it writes the two as one; the spotter heard the commands' phrase inside the session. The
        # question goes, the rest is a command (2026-11-24, open since 11-23)
        self.run_listener([20, 38], {R + 26: "Hey assistant, what time is it. Hey computer, mute."}, 120,
                          said={20: "HEY ASSISTANT", 38: "HEY COMPUTER"}, partials={R + 16: "Hey assistant, what time is it"})
        self.assertEqual(len(self.done), 2, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        self.assertEqual(self.done[1], "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
        # the phrase written close, word for word ("hey commuter"): the same
        self.run_listener([20, 38], {R + 26: "Hey assistant, what time is it. Hey commuter, mute."}, 120,
                          said={20: "HEY ASSISTANT", 38: "HEY COMPUTER"})
        self.assertEqual(len(self.done), 2, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        # the command in the next sentence: the session waits for it, from the second phrase on
        self.run_listener([20, 38], {R + 26: "Hey assistant, what time is it. Hey computer.", R + 50: "Mute."}, 120,
                          said={20: "HEY ASSISTANT", 38: "HEY COMPUTER"})
        self.assertEqual(len(self.done), 2, self.done)
        self.assertEqual(self.done[1], "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
        # a sound alone before the commands' phrase is no question (review 2026-11-24: "uh" was asked)
        for said in ("Hey assistant, uh, hey computer, mute.", "Hey assistant. Okay, hey computer, mute."):
            self.run_listener([20, 30], {R + 26: said}, 120, said={20: "HEY ASSISTANT", 30: "HEY COMPUTER"})
            self.assertEqual(self.done, ["wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle"], said)
        # the phrase's words in a question, the spotter not hearing it: a question still
        self.run_listener([20], {R + 26: "Hey assistant, what time is it hey computer mute"}, 120, said={20: "HEY ASSISTANT"})
        self.assertEqual(len(self.done), 1, self.done)
        self.assertTrue(self.done[0].endswith(" ask 'what time is it hey computer mute'"), self.done)

    def test_a_command_the_recogniser_writes_late_after_a_question(self):
        # the question written by 3.2 s; "hey computer" spotted at 5.0 s, its words written from 5.3 s: the question's
        # pause ran out before them. The question goes, and the session waits for the command (review 2026-11-24:
        # it was dropped without a word)
        self.run_listener([20, 50], {R + 12: "Hey assistant, what time is it", R + 40: "Hey computer, mute."}, 140,
                          said={20: "HEY ASSISTANT", 50: "HEY COMPUTER"}, partials={R + 36: "Hey"})
        self.assertEqual(len(self.done), 2, (self.done, self.printed))
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"), self.done)
        self.assertEqual(self.done[1], "wpctl set-mute @DEFAULT_AUDIO_SINK@ toggle")
        # a stray spot inside the question ("my computer", words after it): asked, and nothing waits after it
        self.run_listener([20, 30], {R + 30: "Hey assistant, why does my computer get hot when I open Firefox?"}, 140,
                          said={20: "HEY ASSISTANT", 30: "HEY COMPUTER"},
                          partials={R + 20: "Hey assistant, why does my computer get hot when I open Firefox"})
        self.assertEqual((len(self.done), self.notes), (1, []), self.printed)

    def test_the_wake_phrase_said_again_does_not_stretch_a_long_question(self):
        talk = {R + 10 + 10 * k: f"part {k}" for k in range(1, 60)}
        self.run_listener([20, 320], {**talk, R + 10: "Hey assistant, first"}, 600, said={20: "HEY ASSISTANT",
                                                                                         320: "HEY ASSISTANT"})
        self.assertTrue(42.0 <= self.at[0] <= 42.1, self.at)       # 40 s from the session's start, not the repeat

    def test_a_session_ends_when_the_microphone_goes_quiet(self):
        listener = self.run_listener([20], {R + 12: "Hey assistant, what time is it"}, 35, said={20: "HEY ASSISTANT"})
        self.assertEqual(self.done, [])                             # the pause not over yet
        listener.gap()                                              # unplugged: no more chunks
        self.assertTrue(self.done[0].endswith(" ask 'what time is it'"))
        self.assertIsNone(listener.rs)
        listener = self.run_listener([20], {}, 30)                  # a command session: dropped, nothing shown
        listener.gap()
        self.assertEqual((self.done, self.notes, listener.rs), ([], [], None))
        listener.gap()                                              # and nothing to end: nothing happens

    def test_a_question_for_an_online_assistant(self):
        # "hey google, what time is it": the question goes to the panel's online assistant called google
        cfg = V.clean({"commands": CMDS})
        cfg["online"] = ["google"]
        self.run_listener([20], {R + 12: "Hey Google, what time is it"}, 100, cfg=cfg, said={20: "HEY GOOGLE"})
        self.assertEqual(len(self.done), 1, self.done)
        self.assertTrue(self.done[0].endswith(" ask --to google 'what time is it'"), self.done[0])
        self.assertEqual(self.states[-1]["ran"], "google")
        # nothing said after it: the miss is told as a question's
        self.run_listener([20], {}, 120, cfg=cfg, said={20: "HEY GOOGLE"})
        self.assertEqual([n[0] for n in self.notes], ["No question heard"])
        self.assertIn("hey google", self.notes[0][1])

    def test_the_phrase_that_opens_a_session_decides_it(self):
        # "hey assistant, why does my computer get hot when I open firefox": a stray "hey computer" on "my computer"
        self.run_listener([20, 30], {R + 30: "Hey assistant, why does my computer get hot when I open Firefox?"}, 100,
                          said={20: "HEY ASSISTANT", 30: "HEY COMPUTER"},
                          partials={R + 20: "Hey assistant, why does my computer get hot when I open Firefox"})
        self.assertEqual(len(self.done), 1)
        self.assertIn(" ask 'why does my computer get hot when i open firefox'", self.done[0])
        self.assertEqual(len(self.chimes), 1)

    def test_a_wake_phrase_stops_an_answer_being_read(self):
        # said for that alone: nothing after it, and nothing shown about it
        self.run_listener([20], {}, 120, reading=[True])
        self.assertEqual((self.done, self.notes), ([], []))
        self.assertIn("(the answer being read stopped)", self.printed)
        self.assertEqual(self.states[-1], {"state": "listening"})
        # "hey computer, stop": no such command, and no notification about it either
        self.run_listener([20], {R + 12: "Hey computer, stop."}, 60, reading=[True])
        self.assertEqual((self.done, self.notes), ([], []))
        # a command after it runs as ever, and a question goes to the Assistant
        self.run_listener([20], {R + 12: "Hey computer, volume up."}, 60, reading=[True])
        self.assertEqual(self.done, [V.DEFAULTS["commands"][0]["run"]])
        self.run_listener([20], {R + 12: "Hey assistant, and tomorrow?"}, 100, said={20: "HEY ASSISTANT"}, reading=[True])
        self.assertIn("ask 'and tomorrow'", self.done[0])

    def test_after_a_stopped_answer_a_command_that_lost_its_start_is_shown(self):
        # "hey computer volume down" in one breath during a reading: the recogniser starts at the phrase, after the
        # spotter named it, and may hear only "down": that is shown, so the owner says it again (review 2026-11-19)
        self.run_listener([20], {13: "down"}, 60, reading=[True])
        self.assertEqual((self.done, self.notes), ([], [("No command matched", "Heard: \u201cdown\u201d")]))
        for stop in ("stop", "that's enough", "be quiet please", "shut up", "okay thank you", "stop speaking",
                     "stop reading", "pause", "never mind", "that's fine"):
            self.run_listener([20], {13: stop}, 60, reading=[True])
            self.assertEqual(self.notes, [], stop)

    def test_after_a_stopped_answer_only_what_follows_the_phrase_counts(self):
        # the pre-roll held the speaker's words (an answer saying "… hey computer, mute"): the recogniser starts at the
        # phrase, without it, and its first sentence is all command, with no phrase to cut it at
        self.run_listener([20], {13: "volume up"}, 60, reading=[True])
        self.assertEqual(self.done, [V.DEFAULTS["commands"][0]["run"]])
        self.assertEqual(self.asr.streams[0].chunks, 13)                 # from the phrase on, not 2 s before it
        self.run_listener([20], {R + 13: "volume up"}, 60)                 # the same with no reading: the phrase is
        self.assertEqual(self.done, [])                                    # not in it, so it is no command (yet)

    def test_the_next_session_after_a_stopped_answer_reports_as_ever(self):
        # the phrase said again inside the session keeps it a stop; the next session is an ordinary one
        self.run_listener([20, 40, 150], {}, 300, reading=[True, False, False])
        self.assertEqual([t for t, _ in self.notes], ["No command heard"])
        self.assertEqual(self.printed.count("(the answer being read stopped)"), 2)

    def test_a_fresh_spotter_stream_in_every_pause_but_not_while_awake(self):
        self.run_listener([], {}, 80, loud=set(range(5, 15)) | set(range(40, 50)))
        self.assertEqual(self.kws.streams, 1 + 2)         # one after each stretch of sound
        self.run_listener([12], {}, 60, loud=set(range(5, 15)))
        self.assertEqual(self.kws.streams, 1)             # spotted at 12: awake through the pause that followed
        self.run_listener([], {}, 60, loud=set(range(5, 15)) | {19, 20} | set(range(23, 26)))
        self.assertEqual(self.kws.streams, 1 + 1)         # short gaps inside speech (a breath) are no pause

    def test_a_phrase_spotted_after_it_ends_survives_the_pause_that_follows(self):
        # sound to chunk 14, then quiet; the spotter names the phrase 0.4, 0.5, 0.6 s after it ended
        for late in (4, 5, 6):
            self.run_listener([14 + late], {R + 30: "Hey computer, open Firefox."}, 80, loud=set(range(5, 15)))
            self.assertEqual(self.done, ["firefox"], late)

    def test_a_new_spotter_keeps_listening(self):
        act = V.Actions(V.clean({"commands": CMDS}), launch=lambda l: None, notify=lambda *a: None, chime=lambda p: None)
        listener = V.Listener(Spotter([]), Recogniser({}), act, now=lambda: 0.0)
        listener.spotter(Spotter([1]))
        with contextlib.redirect_stdout(io.StringIO()):
            listener.feed([0.0] * V.CHUNK)
        self.assertIsNotNone(listener.rs)

    def test_a_spotter_stream_that_heard_nothing_is_renewed(self):
        with mock.patch.object(V, "REFRESH", 50):
            self.run_listener([], {}, 160)
            self.assertEqual(self.kws.streams, 1 + 3)
            self.run_listener([40], {}, 100)              # the count starts again at a spot, and waits while awake
            self.assertEqual(self.kws.streams, 1)


def desktop(d, name, **fields):
    p = Path(d) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[Desktop Entry]\n" + "".join(f"{k}={v}\n" for k, v in {"Type": "Application", "Exec": "x", **fields}.items())
                 + "[Desktop Action new]\nName=Not this one\n")


class FoundTest(unittest.TestCase):
    def test_the_menus_applications_are_found(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as system:
            desktop(system, "org.kde.kwrite.desktop", Name="KWrite", GenericName="Text Editor", StartupWMClass="kwrite")
            desktop(system, "firefox.desktop", Name="Firefox")
            desktop(home, "firefox.desktop", Name="My Firefox")                     # the owner's copy wins
            desktop(system, "kde4/old.desktop", Name="Old")                         # an id from its folder
            desktop(system, "hidden.desktop", Name="Hidden", NoDisplay="true")
            desktop(system, "gnome.desktop", Name="Gnome only", OnlyShowIn="GNOME;")
            desktop(system, "notkde.desktop", Name="Not KDE", NotShowIn="KDE;")
            desktop(system, "term.desktop", Name="Top", Terminal="true")
            desktop(system, "gone.desktop", Name="Gone", TryExec="/nonexistent/program")
            desktop(system, "link.desktop", Name="Link", Type="Link")
            got = V.desktop_entries([Path(home), Path(system)], desktop="KDE")
        self.assertEqual([(e["id"], e["name"]) for e in got],
                         [("org.kde.kwrite", "KWrite"), ("firefox", "My Firefox"), ("kde4-old", "Old")])
        self.assertEqual(got[0]["wmclass"], "kwrite")

    def test_how_an_application_is_said(self):
        e = lambda name, generic="": {"name": name, "generic": generic}
        self.assertEqual(V.app_words(e("KCalc", "Scientific Calculator")), "scientific calculator")
        self.assertEqual(V.app_words(e("KRDC", "Remote Desktop Client")), "remote desktop client")
        self.assertEqual(V.app_words(e("Konsole", "Terminal")), "konsole")             # K and a vowel: a word
        self.assertEqual(V.app_words(e("KWalletManager", "Password Manager for KDE Wallets")), "kwalletmanager")
        self.assertEqual(V.app_words(e("SuperTux 2", "Platform Game")), "supertux two")

    def test_a_scan_keeps_what_the_owner_changed(self):
        apps = [{"id": "org.kde.kwrite", "name": "KWrite", "generic": "Text Editor", "wmclass": "", "path": "/k"},
                {"id": "firefox", "name": "Firefox", "generic": "", "wmclass": "", "path": "/f"}]
        cfg = V.clean({"commands": CMDS})
        found, rep = V.scanned(cfg, apps)
        by = {f["id"]: f for f in found}
        self.assertEqual(by["open:org.kde.kwrite"], {"id": "open:org.kde.kwrite", "say": "open text editor",
                                                     "run": "a7a-voice app open org.kde.kwrite", "on": True, "kind": "app",
                                                     "made": "open text editor"})
        self.assertEqual(by["close:firefox"]["run"], "a7a-voice app close firefox")
        self.assertFalse(by["open:firefox"]["on"])            # the built-in "open firefox" has those words
        self.assertFalse(by["system:shut-down"]["on"])        # off until the owner switches it on
        self.assertTrue(by["system:press-enter"]["on"])
        # SYSTEM's group is its kind, the panel's heading for it
        self.assertEqual([by[f"system:{k}"]["kind"] for k in ("press-enter", "address-bar", "play", "show-desktop", "time")],
                         ["keys", "browser", "music", "window", "board"])
        self.assertEqual((len(rep["added"]), rep["removed"], rep["same"]), (len(found), [], []))
        # one found before the groups (kind "system") takes its group at the next scan, the owner's words kept
        old = dict(by["system:play"], kind="system", say="play music")
        again = {f["id"]: f for f in V.scanned(V.clean({"commands": CMDS, "found": [old]}), apps)[0]}
        self.assertEqual((again["system:play"]["kind"], again["system:play"]["say"]), ("music", "play music"))
        # the owner renames one and switches one off; then KWrite is uninstalled and KMines comes
        by["open:firefox"]["on"] = True
        by["system:play"]["say"] = "play music"
        by["close:firefox"]["on"] = False
        cfg["found"] = found
        apps2 = [apps[1], {"id": "org.kde.kmines", "name": "KMines", "generic": "Minesweeper", "wmclass": "", "path": "/m"}]
        found2, rep2 = V.scanned(cfg, apps2)
        by2 = {f["id"]: f for f in found2}
        self.assertEqual((by2["system:play"]["say"], by2["close:firefox"]["on"], by2["open:firefox"]["on"]),
                         ("play music", False, True))
        self.assertNotIn("open:org.kde.kwrite", by2)
        self.assertEqual(rep2["added"], ["open minesweeper", "close minesweeper"])
        self.assertEqual(rep2["removed"], ["open text editor", "close text editor"])
        self.assertEqual(rep2["same"], ["open firefox"])     # now on, beside the built-in one: said twice
        # an application renamed: words the scan made follow its name, the owner's stay
        cfg["found"] = found2
        apps3 = [dict(apps[1], name="Firefox Nightly"), dict(apps2[1], generic="Mines")]
        by3 = {f["id"]: f for f in V.scanned(cfg, apps3)[0]}
        self.assertEqual((by3["open:firefox"]["say"], by3["open:org.kde.kmines"]["say"]), ("open firefox nightly", "open mines"))
        by2["open:org.kde.kmines"]["say"] = "open the mines game"
        by3 = {f["id"]: f for f in V.scanned(cfg, apps3)[0]}
        self.assertEqual(by3["open:org.kde.kmines"]["say"], "open the mines game")

    def test_a_dry_test_says_the_match_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "config.json"
            f.write_text(json.dumps({"found": [{"id": "s", "say": "close this window", "run": "a7a-voice shortcut 'Window Close'"}]}))
            with mock.patch.object(V, "CONFIG", f), mock.patch.object(V, "launch") as launch, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(V.main(["test", "--dry", "close this window"]), 0)
                launch.assert_not_called()
                self.assertIn("Window Close", out.getvalue())
                self.assertEqual(V.main(["test", "--panel", "close this window"]), 0)    # the panel's Test: said only
                launch.assert_not_called()
                self.assertEqual(V.main(["test", "close this window"]), 0)
                launch.assert_called_once_with("a7a-voice shortcut 'Window Close'")

    def test_found_commands_in_the_settings_and_the_matching(self):
        cfg = V.clean({"commands": [{"say": "lights on", "run": "on"}], "found": [
            {"id": "a", "say": "open dolphin", "run": "a7a-voice app open org.kde.dolphin"},
            {"id": "b", "say": "close dolphin", "run": "x", "on": False},
            {"say": "no id", "run": "x"}, "not an object", {"id": 3, "say": "y", "run": "y"}]})
        self.assertEqual([(f["id"], f["on"]) for f in cfg["found"]], [("a", True), ("b", False)])
        self.assertEqual([c["say"] for c in V.active(cfg)], ["lights on", "open dolphin"])
        self.assertEqual(V.best_match(V.words("open dolphin please"), V.active(cfg), 0.75)[0]["id"], "a")
        self.assertEqual(V.clean(None)["found"], [])
        self.assertEqual(V.same_words([{"say": "A b", "run": "1"}, {"say": "a B!", "run": "2"}, {"say": "a b", "run": " "}]), ["a b"])


class TypingTest(unittest.TestCase):
    def test_an_address_said(self):
        a = lambda s: V.address(s.split())
        self.assertEqual(a("www dot google dot com"), "www.google.com")
        self.assertEqual(a("w w w dot lemonde dot fr"), "www.lemonde.fr")
        self.assertEqual(a("double u double u double u dot example dot org"), "www.example.org")
        self.assertEqual(a("sam at example dot fr"), "sam@example.fr")
        self.assertEqual(a("github dot com slash rabs nine"), "github.com/rabs")
        self.assertEqual(a("go to lemonde dot fr now"), "lemonde.fr")
        self.assertIsNone(a("meet me at the dot of noon"))                # no domain after a dot: not an address
        self.assertIsNone(a("hello world"))
        # the recogniser drops one w of three at times ("Type WW.google.com"): two or three before a dot are www
        self.assertEqual(a("ww dot google dot com"), "www.google.com")
        self.assertEqual(a("w ww dot google dot com"), "www.google.com")
        self.assertEqual(V.www("ww is fine".split()), ["ww", "is", "fine"])
        self.assertEqual(V.www("w dot com".split()), ["w", "dot", "com"])
        # whole words only: "show w w w dot …" is not "showww w" (review 2026-11-22)
        self.assertEqual(V.www("show w w w dot google dot com".split()), "show www dot google dot com".split())
        self.assertEqual(V.www("new w w w".split()), ["new", "www"])
        # its dot not written ("Type www google.com", the owner's voice, board 2026-11-22): www is the address's still
        self.assertEqual(a("www google dot com"), "www.google.com")
        self.assertEqual(a("ww google dot com"), "www.google.com")             # a w and the dot dropped
        # "dot" glued to the word after it ("pe ww dotgoogle dot com": owner, board 2026-11-23)
        self.assertEqual(a("ww dotgoogle dot com"), "www.google.com")
        self.assertEqual(V.spoken_text("a dotted line and www dotted".split(), False), "a dotted line and www dotted")
        self.assertEqual(V.spoken_text(V.words("Type WWW google.com")[1:], False), "www.google.com")
        self.assertEqual(V.spoken_text("www is how it starts".split(), False), "www is how it starts")

    def test_an_address_the_recogniser_writes_as_one(self):
        # it writes "google dot com" as google.com (board 2026-11-22): its words are the ones said, and typed as one
        w = V.words
        self.assertEqual(w("Type www.google.com"), ["type", "www", "dot", "google", "dot", "com"])
        self.assertEqual(w("Go to LeMonde.fr."), ["go", "to", "lemonde", "dot", "fr"])
        self.assertEqual(w("mail sam@example.fr"), ["mail", "sam", "at", "example", "dot", "fr"])
        self.assertEqual(w("github.com/rabs"), ["github", "dot", "com", "slash", "rabs"])
        self.assertEqual(w("my-site.com"), ["my", "dash", "site", "dot", "com"])
        self.assertEqual(w("e.g. the wi-fi"), ["e", "g", "the", "wifi"])          # no domain: as before
        self.assertEqual(w("at 3.30, 2 cats"), ["at", "three", "thirty", "two", "cats"])
        # an address keeps its digits: spelled out, they fell out of it ("1password.com" opened password.com: review)
        self.assertEqual(V.address(w("Go to 1password.com")[2:]), "1password.com")
        self.assertEqual(V.address(w("a7a-fedora.dev")), "a7a-fedora.dev")
        self.assertEqual(V.spoken_text(w("Type github.com/rabs9")[1:], False), "github.com/rabs9")
        self.assertEqual(V.spoken_text(w("Type WW.google.com")[1:], False), "www.google.com")
        self.assertEqual(V.address(w("Go to LeMonde.fr")[2:]), "lemonde.fr")

    def test_text_said_with_its_punctuation(self):
        t = lambda s, cap=True: V.spoken_text(s.split(), cap)
        self.assertEqual(t("hello comma how are you question mark"), "Hello, how are you?")
        self.assertEqual(t("i think so period new line i'm sure exclamation mark"), "I think so.\nI'm sure!")
        self.assertEqual(t("see you full stop bye", cap=False), "see you. Bye")
        self.assertEqual(t("new paragraph dear sir colon"), "\n\nDear sir:")
        # an address inside a sentence: only its words are written as one, and it gets no capital
        self.assertEqual(t("my email is sam at example dot com period"), "My email is sam@example.com.")
        self.assertEqual(t("www dot google dot com is it", cap=True), "www.google.com is it")
        self.assertEqual(t("meet me at noon"), "Meet me at noon")
        # one "at" at most, after a name (not a common word)
        self.assertEqual(t("email me at sam at example dot net"), "Email me at sam@example.net")
        self.assertEqual(t("visit us at example dot com"), "Visit us at example.com")
        self.assertEqual(t("i am at google dot com"), "I am at google.com")
        self.assertEqual(t("at dot com"), "At dot com")
        self.assertEqual(V.type_lines("one\ntwo"), ["type one", "key shift+enter", "type two"])
        self.assertEqual(V.type_lines("x" * (V.TYPE_CHUNK + 5)), ["type " + "x" * V.TYPE_CHUNK, "type xxxxx"])
        self.assertEqual(V.type_lines("\n"), ["key shift+enter"])

    def test_typing_by_voices_own_phrases(self):
        b = lambda s: V.builtin_words(V.words(s))
        self.assertEqual([b(s) for s in ("type hello", "type", "go to lemonde dot fr", "go to", "delete that",
                                         "start dictation", "start dictating", "stop dictation", "stop dictating",
                                         "open firefox", "stop the dictation of all my letters")],
                         ["type", "", "go", "", "delete", "start", "start", "stop", "stop", "", ""])

    def test_keys_go_through_the_listeners_keyboard_or_a_dotool_of_their_own(self):
        with tempfile.TemporaryDirectory() as d:
            pipe = Path(d) / "keys"
            os.mkfifo(pipe)
            with mock.patch.object(V, "KEYS_PIPE", pipe), mock.patch.object(V.subprocess, "run") as run:
                reader = os.open(pipe, os.O_RDONLY | os.O_NONBLOCK)          # a dotoold reading it
                try:
                    self.assertEqual(V.keys(["key ctrl+l", "type a\nb"]), "")
                    self.assertEqual(os.read(reader, 100), b"key ctrl+l\ntype a b\n")
                finally:
                    os.close(reader)
                run.assert_not_called()
                run.return_value = mock.Mock(returncode=0, stderr=b"")
                self.assertEqual(V.keys(["key enter"]), "")                 # nobody reads it: dotool itself
                self.assertEqual(run.call_args[0][0], ["dotool"])
                self.assertEqual(run.call_args[1]["input"], b"key enter\n")
                run.side_effect = FileNotFoundError
                self.assertIn("dnf install dotool", V.keys(["key enter"]))

    def test_the_listeners_keyboard(self):
        with tempfile.TemporaryDirectory() as d:
            out, pipe = Path(d) / "typed", Path(d) / "keys"
            t = V.Typist(pipe, ["sh", "-c", f"cat > {out}"])
            try:
                self.assertEqual(t.send(["key ctrl+l", "type a\nb"]), "")
                with mock.patch.object(V, "KEYS_PIPE", pipe):
                    self.assertEqual(V.keys(["key enter"]), "")       # a key command, through the pipe
                for _ in range(50):
                    if out.exists() and out.read_text().count("\n") >= 3:
                        break
                    time.sleep(0.05)
                self.assertEqual(out.read_text(), "key ctrl+l\ntype a b\nkey enter\n")
            finally:
                t.close()
            self.assertFalse(pipe.exists())
            # a dotool that stops at once (no /dev/uinput) is said, and not started again within 5 s
            t = V.Typist(Path(d) / "keys2", ["sh", "-c", "echo no uinput >&2; exit 3"])
            try:
                self.assertEqual(t.send(["key enter"]), "dotool stopped (exit 3): no uinput")
                self.assertEqual(t.send(["key enter"]), "dotool stopped")
            finally:
                t.close()

    def test_the_keyboard_layout_is_the_desktops(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
            self.assertEqual(V.keyboard_env(), {})
            (Path(d) / "kxkbrc").write_text("[Layout]\nLayoutList=fr,us\nVariantList=oss,\nUse=true\n")
            self.assertEqual(V.keyboard_env(), {"DOTOOL_XKB_LAYOUT": "fr", "DOTOOL_XKB_VARIANT": "oss"})


class Known:
    """The spotter's pieces, for Engine: every word but "BAD"."""
    def __contains__(self, piece):
        return piece != "BAD"


class Engine:
    """listen() with its heavy parts faked: the spotter factory (records the wake phrases it was asked for, refuses
    "bad phrase"), the recogniser, the microphone (n chunks, with hooks at chunk numbers), detached commands."""
    def __init__(self, test, d: Path, spots=(), sentences=None, chunks=30, hooks=None):
        self.wakes, self.started, self.d = [], [], d
        self.spots, self.sentences, self.chunks, self.hooks = spots, sentences or {}, chunks, hooks or {}
        for name, value in (("ready", lambda: True), ("into_venv", lambda argv: None), ("spotter", self.spotter),
                            ("recogniser", lambda: Recogniser(self.sentences)), ("microphone", self.microphone),
                            ("detached", self.started.append), ("STATUS", d / "status.json"), ("CONFIG", d / "config.json"),
                            ("KEYWORDS", d / "keywords.txt")):
            p = mock.patch.object(V, name, value)
            p.start()
            test.addCleanup(p.stop)
        old = signal.getsignal(signal.SIGTERM)
        test.addCleanup(signal.signal, signal.SIGTERM, old)

    def spotter(self, cfg):
        self.wakes.append(cfg["wake"])
        if self.hooks.get("load"):
            self.hooks.pop("load")()
        _, online, left = V.keywords(cfg, lambda phrase: phrase.upper().split(), Known())
        return Spotter(self.spots), online, left

    def microphone(self, mic, quiet=V.QUIET, silent=lambda gone: None):
        self.silent = silent
        for n in range(self.chunks):
            if n in self.hooks:
                self.hooks[n]()
            yield [0.0] * V.CHUNK
        self.last = self.status()                         # before the end of the stream is reported over it

    def config(self, cfg):
        p = self.d / "config.json"
        p.write_text(json.dumps(cfg) if not isinstance(cfg, str) else cfg)
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9 * (1 + len(self.wakes))))

    def status(self):
        return json.loads((self.d / "status.json").read_text())

    def listen(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return V.listen()


class ListenTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)

    def test_a_save_while_the_models_load_is_taken(self):
        e = Engine(self, self.d, chunks=25)
        e.config({"wake": "hey computer"})
        e.hooks["load"] = lambda: e.config({"wake": "okay board"})
        self.assertEqual(e.listen(), 1)                   # the fake microphone ends
        self.assertEqual(e.wakes, ["hey computer", "okay board"])
        self.assertEqual(e.status()["wake"], "okay board")

    def test_unreadable_settings_at_the_start_mean_no_commands(self):
        e = Engine(self, self.d, spots=[3], sentences={3 + 5: "hey computer volume up"}, chunks=20)
        e.config("{broken")
        e.listen()
        self.assertFalse([a for a in e.started if a[0] == "systemd-run"])
        self.assertIn("no commands until the settings are saved again", e.last["error"])

    def test_settings_that_stop_reading_keep_the_last_good_ones(self):
        e = Engine(self, self.d, spots=[25], sentences={15 + 5: "hey computer volume up"}, chunks=40)
        e.config({"wake": "hey computer"})
        e.hooks[5] = lambda: e.config("{broken")
        e.listen()
        self.assertTrue([a for a in e.started if a[0] == "systemd-run"])
        self.assertIn("Expecting", e.last["error"])

    def test_a_wake_phrase_the_spotter_cannot_take_leaves_the_old_one(self):
        e = Engine(self, self.d, chunks=30)
        e.config({"wake": "hey computer"})
        e.hooks[5] = lambda: e.config({"wake": "bad phrase"})
        self.assertEqual(e.listen(), 1)                   # it went on to the end of the microphone
        self.assertEqual(e.wakes, ["hey computer", "bad phrase"])
        st = e.last
        self.assertEqual(st["wake"], "hey computer")
        self.assertIn("still listening for \u201chey computer\u201d", st["error"])

    def test_the_phrases_it_really_listens_for_are_reported(self):
        # a refused phrase was shown as working in the panel's "Who answers" (2026-11-24): the listener says which
        # two it wakes for, the old ones when it could not take the new
        e = Engine(self, self.d, chunks=30)
        e.config({"wake": "hey computer", "assistant": "hey helper"})
        seen = {}
        e.hooks[5] = lambda: seen.update(e.status())
        e.hooks[8] = lambda: e.config({"wake": "hey computer", "assistant": "bad phrase"})     # read at chunk 10
        e.listen()
        self.assertEqual((seen["wake"], seen["assistant"]), ("hey computer", "hey helper"))
        self.assertEqual((e.last["wake"], e.last["assistant"]), ("hey computer", "hey helper"))
        self.assertIn("still listening for", e.last["error"])
        self.assertEqual(e.last["read"], V.mtime(V.CONFIG))          # the settings it has read, refused phrase and all

    def test_a_new_microphone_restarts_the_listener(self):
        e = Engine(self, self.d, chunks=30)
        e.config({"mic": ""})
        e.hooks[5] = lambda: e.config({"mic": "usb"})
        self.assertEqual(e.listen(), 1)
        self.assertEqual(e.status()["state"], "listening")

    def test_the_microphone_ending_and_a_stop(self):
        e = Engine(self, self.d, chunks=5)
        self.assertEqual(e.listen(), 1)
        self.assertEqual((e.status()["state"], e.status()["error"]), ("error", "the microphone stream ended"))
        e.hooks[2] = lambda: (_ for _ in ()).throw(SystemExit(0))      # what the SIGTERM handler does
        self.assertEqual(e.listen(), 0)
        self.assertEqual(e.status()["state"], "off")

    def test_silence_from_the_microphone_is_reported_and_waited_out(self):
        e = Engine(self, self.d, chunks=20)
        e.config({"wake": "hey computer"})
        seen = {}
        e.hooks[5] = lambda: e.silent(True)
        e.hooks[7] = lambda: seen.update(e.status())
        e.hooks[10] = lambda: e.silent(False)
        self.assertEqual(e.listen(), 1)                   # on to the end of the fake microphone
        self.assertIn("no sound from the microphone", seen["error"])
        self.assertEqual(e.last["error"], "")

    def test_a_save_while_the_microphone_is_silent_keeps_its_error_up(self):
        e = Engine(self, self.d, chunks=40)
        e.config({"wake": "hey computer"})
        seen = {}
        e.hooks[5] = lambda: e.silent(True)
        e.hooks[8] = lambda: e.config({"wake": "okay board"})          # read at chunk 10
        e.hooks[15] = lambda: seen.update(e.status())
        e.hooks[18] = lambda: e.config("{broken")                       # read at chunk 20: shown once the sound is back
        e.hooks[25] = lambda: seen.update(after=e.status())
        e.hooks[28] = lambda: e.silent(False)
        e.listen()
        self.assertEqual(seen["wake"], "okay board")
        self.assertIn("no sound from the microphone", seen["error"])
        self.assertIn("no sound from the microphone", seen["after"]["error"])
        self.assertIn("Expecting", e.last["error"])

    def test_the_online_assistants_it_listens_for_and_why_not_the_others(self):
        e = Engine(self, self.d, chunks=30)
        e.config({"wake": "hey computer"})
        panel = self.d / "assistant.json"
        p = mock.patch.object(V, "PANEL", panel)
        p.start()
        self.addCleanup(p.stop)

        def online(*names):
            V.write_json(panel, {"online": [{"name": n, "key": "k"} for n in names]})
            st = panel.stat()
            os.utime(panel, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9 * (1 + len(e.wakes))))
        online("google", "ai")
        seen = {}
        e.hooks[5] = lambda: seen.update(e.status())
        e.hooks[12] = lambda: online("google", "ai", "brave")              # read at chunk 20
        e.listen()
        self.assertEqual((seen["online"], len(seen["left_out"])), (["google"], 1))
        self.assertIn("too short", seen["left_out"][0])
        self.assertEqual(e.last["online"], ["google", "brave"])
        self.assertEqual(len(e.wakes), 2)                                   # a new spotter for the new name

    def test_settings_are_read_while_the_microphone_is_silent(self):
        e = Engine(self, self.d, chunks=0)
        e.config({"mic": ""})

        def mic(m, quiet=V.QUIET, silent=lambda gone: None):
            silent(True)
            yield None
            e.config({"mic": "usb"})
            yield None
            raise AssertionError("the new microphone was not taken")
        with mock.patch.object(V, "microphone", mic):
            self.assertEqual(e.listen(), 1)              # restarting on the new source

    def test_a_failing_engine_is_reported(self):
        e = Engine(self, self.d)
        with mock.patch.object(V, "recogniser", mock.Mock(side_effect=FileNotFoundError("no encoder"))):
            self.assertEqual(e.listen(), 1)
        self.assertEqual((e.status()["state"], e.status()["error"]), ("error", "no encoder"))


class AskTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.d = Path(tmp.name)
        self.log = d / "log"
        progs = {"notify-send": f'echo "notify $*" >> {self.log}; echo 7',
                 "a7a-llm": f'echo "llm $*" >> {self.log}; echo "$FAKE_SERVE"; exit "${{FAKE_SERVE_EXIT:-0}}"',
                 "a7a-panel": f'echo "panel $*" >> {self.log}; [ -n "$FAKE_ANSWER" ] && echo "$FAKE_ANSWER" && exit 0; '
                              'echo "no model server" >&2; exit 1'}
        for name, body in progs.items():
            (d / name).write_text(f"#!/bin/sh\n{body}\n")
            (d / name).chmod(0o755)
        self.env = {"PATH": f"{d}:/usr/bin:/bin", "FAKE_SERVE": "up: http://127.0.0.1:8080", "FAKE_ANSWER": "**49 \u00b0C**, all fine."}
        # answers not read aloud (no settings), and a7a-voice itself a stand-in that writes down what it is to read
        (d / "a7a-voice").write_text(f'#!/bin/sh\necho "$* $(cat)" >> {self.log}\n')
        (d / "a7a-voice").chmod(0o755)
        for name, value in (("SPEECH", d / "speech.json"), ("__file__", str(d / "a7a-voice"))):
            p = mock.patch.object(V, name, value)
            p.start()
            self.addCleanup(p.stop)

    def ask(self, ready, to="", **env):
        with mock.patch.dict(os.environ, dict(self.env, **env)), mock.patch.object(V, "llm_ready", mock.Mock(side_effect=ready)), \
                mock.patch.object(V.time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            r = V.ask("how hot is the board", wait=60, to=to)
        return r, self.log.read_text().splitlines()

    def test_an_online_assistant_answers_without_the_model_server(self):
        r, log = self.ask([False], to="google")                 # the board's model is down: not started for google
        self.assertEqual(r, 0)
        self.assertFalse([l for l in log if l.startswith("llm")])
        self.assertIn("panel --answer how hot is the board --assistant google", log)
        self.assertIn("Google 49 \u00b0C, all fine.", log[-1])        # its own name on the notification

    def test_the_answer_with_the_server_up(self):
        r, log = self.ask([True])
        self.assertEqual(r, 0)
        self.assertIn("panel --answer how hot is the board", log)
        self.assertIn("--replace-id=7 Assistant 49 \u00b0C, all fine.", log[-1])     # in place of "Thinking...", no markdown
        self.assertFalse([l for l in log if l.startswith("llm")])
        self.assertFalse([l for l in log if l.startswith("say")])                     # not read aloud

    def test_the_answer_read_aloud_when_answers_are(self):
        V.write_json(V.SPEECH, {"speak": True, "voice": "M2"})
        r, log = self.ask([True])
        self.assertEqual(r, 0)
        self.assertIn("--replace-id=7 Assistant 49 \u00b0C, all fine.", log[-2])     # shown first
        self.assertEqual(log[-1], "say **49 \u00b0C**, all fine.")                     # the whole answer, to be read

    def test_the_server_started_first(self):
        r, log = self.ask([False, False, True])
        self.assertEqual(r, 0)
        self.assertIn("llm serve", log)
        self.assertIn("Starting the model first", " ".join(log))

    def test_the_server_refused(self):
        r, log = self.ask([False], FAKE_SERVE="REFUSING: Qwen needs ~3000MB, only 2000MB available", FAKE_SERVE_EXIT="1")
        self.assertEqual(r, 1)
        self.assertIn("The model did not start: REFUSING: Qwen needs ~3000MB", log[-1])
        self.assertFalse([l for l in log if l.startswith("panel")])

    def test_an_answer_with_markup_shows_as_it_is(self):
        r, log = self.ask([True], FAKE_ANSWER='if x < 2 & y > 3 see <a href="http://x">this</a>')
        self.assertEqual(r, 0)
        self.assertIn("if x &lt; 2 &amp; y &gt; 3 see &lt;a href=\"http://x\"&gt;this&lt;/a&gt;", log[-1])
        started = []
        with mock.patch.object(V, "detached", started.append):
            V.notify("No command matched", "Heard: <b>x</b> & y")
        self.assertEqual(started[0][-2:], ["No command matched", "Heard: &lt;b&gt;x&lt;/b&gt; &amp; y"])

    def test_no_answer(self):
        r, log = self.ask([True], FAKE_ANSWER="")
        self.assertEqual(r, 1)
        self.assertIn("No answer: no model server", log[-1])


class SpeakableTest(unittest.TestCase):
    def test_marks_and_units_as_said(self):
        self.assertEqual(V.speakable("The board is at **52 °C**, and 6 GB of 12 GB are in use (50%)."),
                         ["The board is at 52 degrees Celsius, and 6 gigabytes of 12 gigabytes are in use (50 percent)."])
        self.assertEqual(V.speakable("It reads `thermal_zone0` & _sensors_: 61.5°F at 3 GHz, 17 tok/s"),
                         ["It reads thermal zone0 and sensors: 61.5 degrees Fahrenheit at 3 gigahertz, 17 tokens a second."])

    def test_lists_tables_code_and_links(self):
        text = ("## Top processes\n\n- `llama-server`: 5.9 GiB\n- *firefox*: 800 MB\n\n| CPU | temp |\n|---|:---:|\n"
                "| big | 61 °C |\n\n```sh\nps aux --sort=-rss\n```\n> See [the docs](https://docs.radxa.com/x) or "
                "https://example.org/y. Then reboot 🙂\n\n---\n1. One\n2) Two")
        self.assertEqual(V.speakable(text), ["Top processes.", "llama-server: 5.9 gigabytes.", "firefox: 800 megabytes.",
                                             "CPU, temp.", "big, 61 degrees Celsius.", "See the docs or the link.",
                                             "Then reboot.", "One.", "Two."])

    def test_long_sentences_in_pieces(self):
        words = " ".join(["word"] * 70) + "."                                   # 350 characters, no commas
        pieces = V.speakable(words)
        self.assertLessEqual(len(pieces[0]), V.FIRST_PIECE)
        self.assertTrue(all(len(p) <= V.PIECE for p in pieces), pieces)
        self.assertEqual(" ".join(pieces), words)
        t = ("The big cores run at 3 GHz, the little ones at 2.8 GHz, and the GPU at 1008 MHz, which is the clock "
             "this image starts it at; nothing was changed.")
        self.assertEqual(V.speakable(t), ["The big cores run at 3 gigahertz, the little ones at 2.8 gigahertz, and the "
                                          "GPU at 1008 megahertz,",                     # cut after the last comma in it
                                          "which is the clock this image starts it at; nothing was changed."])

    def test_nothing_to_say(self):
        for text in ("", "```\ncode only\n```", "---\n| - |", "🙂 👍", "**  **", "!!! ??? ... ;;;", "?" * 500):
            self.assertEqual(V.speakable(text), [], text)

    def test_what_cannot_be_said_and_words_too_long_for_a_piece(self):
        key = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        self.assertEqual(V.speakable(f"Its sha256 is {key}, as listed."), ["Its sha256 is, as listed."])
        word = "a" * 39                                        # just short enough to stay, and no space to cut at
        pieces = V.speakable(" ".join([word] * 2) + word * 3 + ".")
        self.assertTrue(all(len(p) <= V.PIECE for p in pieces), [len(p) for p in pieces])


class SpeechTest(unittest.TestCase):
    """speak, say and hush, in this process: the settings, the lock that hands the speaker from one reading to the
    next, the voice made and played with the engine and the player replaced."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.d = Path(tmp.name)
        self.started, self.out = [], d / "played"
        for name, value in (("SPEECH", d / "speech.json"), ("SPEAKING", d / "speaking"), ("DATA", d / "data"),
                            ("SPEECH_PROGRESS", d / "speech-progress.json"), ("READY", d / "data" / "setup.json"),
                            ("HUSHED", d / "hushed"), ("detached", self.started.append)):
            p = mock.patch.object(V, name, value)
            p.start()
            self.addCleanup(p.stop)

    def reader(self):
        """A stand-in for a7a-voice say at work (its command line says so): the lock held, its number in it, a process
        group of its own."""
        code = ("import fcntl, os, sys, time\nos.setpgrp()\nf = open(sys.argv[1], 'a+')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
                "f.truncate(0); f.write(str(os.getpid())); f.flush()\nprint('reading', flush=True)\ntime.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", code, str(V.SPEAKING), "/usr/local/bin/a7a-voice", "say"],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertEqual(p.stdout.readline().strip(), "reading")
        return p

    def speak(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            r = V.speak(list(args))
        return r, out.getvalue()

    def test_hush_ends_what_is_being_read(self):
        self.assertFalse(V.hush())                                  # nothing has been read yet
        p = self.reader()
        self.assertTrue(V.held(V.SPEAKING))
        self.assertEqual(V.reader(), p.pid)
        self.assertTrue(V.hush())
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(V.held(V.SPEAKING))
        self.assertEqual(V.SPEAKING.read_text(), "")               # its number gone with it
        self.assertFalse(V.hush())

    def test_a_number_left_behind_is_not_a_reader(self):
        # a reader's number in the file while someone only looks (the lock held for a moment): the process it names
        # now is another program, which is left alone (review 2026-11-19)
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        V.SPEAKING.write_text(str(other.pid))
        with open(V.SPEAKING) as looking:
            fcntl.flock(looking, fcntl.LOCK_EX)                     # as hush looks, or a reader before its number
            self.assertEqual(V.reader(), 0)
            self.assertFalse(V.hush(wait=0.2))
        self.assertIsNone(other.poll())

    def test_a_hush_before_the_reader_takes_the_lock(self):
        # Stop speaking pressed while `say` was still starting (two Pythons): it gives up when it gets there
        with mock.patch.object(V, "started", lambda: V.boot_seconds() - 1):
            V.hush()
            r, made, _ = self.say("One. Two.", f"sh -c 'cat > {self.out}'")
        self.assertEqual((r, made), (0, []))
        # one started after the hush reads as ever
        with mock.patch.object(V, "started", lambda: V.boot_seconds() + 1):
            self.assertEqual(len(self.say("One.", f"sh -c 'cat > {self.out}'")[1]), 1)

    def test_a_hush_between_pieces_ends_the_reading_well(self):
        # a hush that came after the reader started, seen between two pieces (it had not ended the reader itself):
        # the player is stopped, and that is no failure (review 2026-11-19)
        def generate_then_hush(piece):
            if piece == "Two.":
                V.hush()                                            # (this process holds the lock: not ended by it)
        with mock.patch.object(V, "started", lambda: V.boot_seconds() - 1):
            r, made, out = self.say("One. Two. Three.", f"sh -c 'cat > {self.out}'", after=generate_then_hush)
        self.assertEqual((r, out), (0, ""))
        self.assertEqual([m[0] for m in made], ["One.", "Two."])

    def test_a_hush_time_that_cannot_be_written(self):
        (self.d / "file").write_text("")
        with mock.patch.object(V, "HUSHED", self.d / "file" / "hushed"):
            self.assertFalse(V.hush())

    def test_say_reads_its_text_from_stdin(self):
        # (in the venv: the answer is not in the program's arguments, where anyone could read it)
        stdin = io.TextIOWrapper(io.BytesIO("The board is at 52 °C.".encode()))
        with mock.patch.object(V.sys, "stdin", stdin):
            r, made, _ = self.say(None, f"sh -c 'cat > {self.out}'")
        self.assertEqual((r, [m[0] for m in made]), (0, ["The board is at 52 degrees Celsius."]))

    def test_the_switch_and_the_voice(self):
        r, out = self.speak()
        self.assertEqual(json.loads(out), {"speak": False, "voice": "F1", "voices": V.VOICES, "ready": False,
                                           "speaking": False, "setup": None, "busy": False})
        self.assertEqual(self.speak("voice", "M2")[0], 0)
        self.assertEqual(V.speech_settings(), {"speak": False, "voice": "M2"})
        self.assertEqual(self.started, [])                          # no sample: the voice is not set up
        self.assertEqual(self.speak("voice", "M9")[0], 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.speak("loud")[0], 2)
        # on: the voice set up first; a failed setup leaves it off
        with mock.patch.object(V, "setup", lambda part: 1):
            self.assertEqual(self.speak("on")[0], 1)
        self.assertFalse(V.speech_settings()["speak"])
        made = []
        with mock.patch.object(V, "setup", lambda part: made.append(part) or 0), \
                mock.patch.object(V, "ready", lambda models=None: bool(made)):
            r, out = self.speak("on")
            self.assertEqual((r, made), (0, ["speak"]))
            self.assertEqual(V.speech_settings(), {"speak": True, "voice": "M2"})
            self.assertEqual(self.started, [[str(Path(V.__file__).resolve()), "say", V.SAMPLE]])
            self.speak("voice", "F3")                               # a new voice: a sample in it
            self.assertEqual(len(self.started), 2)
            self.speak("on")                                        # set up already: no setup again
            self.assertEqual(made, ["speak"])
        p = self.reader()
        self.assertEqual(self.speak("off"), (0, "answers are not read aloud\n"))
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)       # and what was being read stops
        self.assertEqual(V.speech_settings(), {"speak": False, "voice": "F3"})

    def test_settings_that_do_not_read(self):
        for raw in ("{broken", "[1]", '{"speak": "yes", "voice": "Z1"}'):
            V.SPEECH.write_text(raw)
            self.assertEqual(V.speech_settings(), {"speak": False, "voice": "F1"}, raw)

    def test_a_download_that_ended_without_finishing(self):
        V.write_json(V.SPEECH_PROGRESS, {"fraction": 0.3, "step": "downloading", "pid": 1})
        self.assertIn("stopped before it finished", V.speech_status()["setup"]["error"])
        V.DATA.mkdir(parents=True)
        with open(V.DATA / "setup.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            held.write("1 speak\n")
            held.flush()
            self.assertEqual(V.speech_status()["setup"]["step"], "downloading")

    def say(self, text, player, after=lambda piece: None):
        """say_aloud with a voice that makes 0.5 s of quiet, 1 s at half scale and 0.7 s of quiet (at 1 kHz) for each
        piece (`after` called with it), and `player` in place of pw-cat."""
        import numpy as np
        made = []

        class Voice:
            def generate(self, piece, gen):
                made.append((piece, gen))
                after(piece)
                x = np.concatenate([np.zeros(500), np.full(1000, 0.5), np.zeros(700)]).astype(np.float32)
                return type("Audio", (), {"samples": x, "sample_rate": 1000})()

        with mock.patch.dict(os.environ, {"A7A_VOICE_PLAYER": player}), \
                mock.patch.multiple(V, ready=lambda models=None: True, into_venv=lambda argv: None,
                                    voice_engine=Voice, generation=lambda voice: voice), \
                mock.patch.object(V.os, "setpgrp", lambda: None), contextlib.redirect_stdout(io.StringIO()) as out:
            r = V.say_aloud(text)
        return r, made, out.getvalue()

    def test_say_reads_each_piece_into_one_stream(self):
        V.write_json(V.SPEECH, {"speak": True, "voice": "F2"})
        p = self.reader()                                           # something is being read: it stops first
        r, made, out = self.say("One. Two.", f"sh -c 'cat > {self.out}; echo {{rate}} >> {self.out}.rate'")
        self.assertEqual((r, out), (0, ""))
        self.assertEqual(p.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(made, [("One.", "F2"), ("Two.", "F2")])
        # each piece: 0.3 s of its quiet before the voice, the voice, 0.15 s after it
        self.assertEqual(len(self.out.read_bytes()), 2 * (300 + 1000 + 150) * 4)
        self.assertEqual((self.d / "played.rate").read_text(), "1000\n")
        self.assertFalse(V.held(V.SPEAKING))                        # let go when done, its number with it
        self.assertEqual(V.SPEAKING.read_text(), "")

    def test_a_player_that_fails(self):
        r, made, out = self.say("One. Two. Three.", "sh -c 'echo no sound card >&2; exit 3'")
        self.assertEqual(r, 1)
        self.assertIn("not read aloud: the player stopped (exit 3): no sound card", out)

    def test_say_before_setup_and_with_nothing_to_say(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(V.say_aloud("hello"), 1)
        self.assertIn("not set up", out.getvalue())
        self.assertEqual(self.say("```\nls\n```", "false")[:2], (0, []))


class MicrophoneTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)

    def test_silence_is_told_and_waited_out(self):
        fake = self.d / "pw-record"
        # 0.1 s of sound, 0.6 s of nothing (the microphone unplugged), 0.2 s of sound, then the stream ends
        fake.write_text("#!/bin/sh\nhead -c 3200 /dev/zero\nsleep 0.6\nhead -c 6400 /dev/zero\n")
        fake.chmod(0o755)
        told = []
        with mock.patch.dict(os.environ, {"PATH": f"{self.d}:{os.environ['PATH']}"}):
            got = list(V.microphone("", quiet=0.3, silent=told.append))
        chunks = [c for c in got if c is not None]
        self.assertEqual((len(chunks), len(chunks[0]), told), (3, V.CHUNK, [True, False]))
        self.assertGreaterEqual(sum(c is None for c in got), 1)    # the listener reads new settings meanwhile

    def test_without_pw_record(self):
        with mock.patch.dict(os.environ, {"PATH": str(self.d)}):
            with self.assertRaisesRegex(RuntimeError, "pipewire-utils"):
                list(V.microphone(""))


def tarball(files: dict) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        for f, data in files.items():
            i = tarfile.TarInfo(f)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
    return bz2.compress(raw.getvalue())


class Server:
    """A web server with the one file, that honours Range (as GitHub's does) or not; `status`: it answers that
    instead; `cut`: it sends that many bytes of the file and hangs up."""
    def __init__(self, test, data: bytes, ranges=True, status=None, cut=None):
        self.asked = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                r = self.headers.get("Range")
                outer.asked.append(r)
                if status:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(r.split("=")[1].split("-")[0]) if r and ranges else 0
                body = data[start:]
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body if cut is None else body[:cut])

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        test.addCleanup(self.httpd.server_close)
        test.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


class DownloadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.data = tarball({"m/tokens.txt": b"a 0\n", "m/encoder.int8.onnx": b"x" * 5000, "m/encoder.onnx": b"big",
                             "m/test_wavs/0.wav": b"w", "m/../evil": b"e", "other/tokens.txt": b"o"})
        for name, value in (("MODELS", self.d / "models"), ("PROGRESS", self.d / "progress.json")):
            p = mock.patch.object(V, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.model = ("m", "tag", len(self.data), hashlib.sha256(self.data).hexdigest(), r"^(tokens\.txt|.*\.int8\.onnx)$")

    def serve(self, ranges=True, status=None, cut=None):
        s = Server(self, self.data, ranges, status, cut)
        p = mock.patch.object(V, "RELEASES", s.url)
        p.start()
        self.addCleanup(p.stop)
        return s

    def test_only_the_files_it_needs_and_flat(self):
        s = self.serve()
        V.download(self.model, 0.0, 1.0)
        self.assertEqual(sorted(os.listdir(V.MODELS)), ["m"])                         # the tarball part removed
        self.assertEqual(sorted(os.listdir(V.MODELS / "m")), ["encoder.int8.onnx", "tokens.txt"])
        self.assertEqual((V.MODELS / "m" / "tokens.txt").read_bytes(), b"a 0\n")
        self.assertFalse((self.d / "evil").exists())
        self.assertEqual(s.asked, [None])
        V.download(self.model, 0.0, 1.0)                                              # there already: nothing to do
        self.assertEqual(s.asked, [None])

    def test_a_part_left_over_is_continued(self):
        s = self.serve()
        V.MODELS.mkdir()
        (V.MODELS / "m.tar.bz2.part").write_bytes(self.data[:100])
        V.download(self.model, 0.0, 1.0)
        self.assertEqual(s.asked, ["bytes=100-"])
        self.assertTrue((V.MODELS / "m" / "tokens.txt").exists())

    def test_a_server_that_sends_it_all_again(self):
        s = self.serve(ranges=False)
        V.MODELS.mkdir()
        (V.MODELS / "m.tar.bz2.part").write_bytes(self.data[:100])
        V.download(self.model, 0.0, 1.0)
        self.assertEqual(s.asked, ["bytes=100-"])
        self.assertTrue((V.MODELS / "m" / "tokens.txt").exists())

    def test_a_damaged_download_is_removed(self):
        self.serve()
        bad = self.model[:3] + ("0" * 64,) + self.model[4:]
        with self.assertRaisesRegex(RuntimeError, "damaged"):
            V.download(bad, 0.0, 1.0)
        self.assertFalse((V.MODELS / "m.tar.bz2.part").exists())
        self.assertFalse((V.MODELS / "m").exists())

    def test_a_wrong_size_is_refused(self):
        self.serve()
        with self.assertRaisesRegex(RuntimeError, "expected"):
            V.download(self.model[:2] + (self.model[2] + 1,) + self.model[3:], 0.0, 1.0)

    def test_the_voice_has_no_tokens_file(self):
        # Supertonic's files: what it needs kept, its README not; the directory says it is there
        self.data = tarball({f"{V.TTS[0]}/{f}": b"x" for f in ("tts.json", "voice.bin", "unicode_indexer.bin", "LICENSE",
                                                                 "vocoder.int8.onnx", "README.md")})
        s = self.serve()
        model = (V.TTS[0], "tts-models", len(self.data), hashlib.sha256(self.data).hexdigest(), V.TTS[4])
        V.download(model, 0.0, 1.0, self.d / "speech-progress.json")
        self.assertEqual(sorted(os.listdir(V.MODELS / V.TTS[0])),
                         ["LICENSE", "tts.json", "unicode_indexer.bin", "vocoder.int8.onnx", "voice.bin"])
        V.download(model, 0.0, 1.0)
        self.assertEqual(len(s.asked), 1)

    def test_a_download_without_what_it_needs_is_refused(self):
        self.serve()
        with self.assertRaisesRegex(RuntimeError, "none of the files it needs"):
            V.download(self.model[:4] + (r"^nothing$",), 0.0, 1.0)
        self.assertFalse((V.MODELS / "m").exists())

    # the first run (2026-11-24): a board not yet online, a server that refuses, a connection that drops. Each says
    # so in words, and what had come is kept for the next try

    def test_no_connection_says_so_and_keeps_what_came(self):
        with socket.socket() as s:                    # a port nothing listens on: the board offline
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        V.MODELS.mkdir()
        (V.MODELS / "m.tar.bz2.part").write_bytes(self.data[:100])
        with mock.patch.object(V, "RELEASES", f"http://127.0.0.1:{port}"):
            with self.assertRaisesRegex(RuntimeError, r"no connection to the download server \(github\.com\).*internet"):
                V.download(self.model, 0.0, 1.0)
        self.assertEqual((V.MODELS / "m.tar.bz2.part").read_bytes(), self.data[:100])

    def test_a_server_that_refuses_says_what_it_answered(self):
        self.serve(status=503)
        with self.assertRaisesRegex(RuntimeError, r"the download server answered 503"):
            V.download(self.model, 0.0, 1.0)
        self.assertFalse((V.MODELS / "m").exists())

    def test_a_login_page_instead_of_the_file_keeps_what_came(self):
        # a cafe's network answers every address with its login page, as a 200 (review 2026-11-24: the .part was
        # truncated, then removed as "not the file this setup knows")
        V.MODELS.mkdir()
        (V.MODELS / "m.tar.bz2.part").write_bytes(self.data[:100])
        page = Server(self, b"<html><body>Log in to the cafe's network</body></html>", ranges=False)
        with mock.patch.object(V, "RELEASES", page.url):
            with self.assertRaisesRegex(RuntimeError, r"not the model's archive.*login page.*internet"):
                V.download(self.model, 0.0, 1.0)
            self.assertEqual((V.MODELS / "m.tar.bz2.part").read_bytes(), self.data[:100])
            (V.MODELS / "m.tar.bz2.part").unlink()
            with self.assertRaisesRegex(RuntimeError, r"not the model's archive"):
                V.download(self.model, 0.0, 1.0)
            self.assertFalse((V.MODELS / "m.tar.bz2.part").exists())

    def test_a_part_longer_than_the_servers_file_goes(self):
        # the server answers 416 (the part asks from past its end): every try would; the part goes
        V.MODELS.mkdir()
        (V.MODELS / "m.tar.bz2.part").write_bytes(self.data[:100])
        self.serve(status=416)
        with self.assertRaisesRegex(RuntimeError, r"416"):
            V.download(self.model, 0.0, 1.0)
        self.assertFalse((V.MODELS / "m.tar.bz2.part").exists())

    def test_a_connection_cut_in_the_middle_says_the_next_try_carries_on(self):
        half = len(self.data) // 2
        s = self.serve(cut=half)
        with self.assertRaisesRegex(RuntimeError, r"the download was cut.*trying again carries on where it stopped"):
            V.download(self.model, 0.0, 1.0)
        self.assertFalse((V.MODELS / "m").exists())
        kept = (V.MODELS / "m.tar.bz2.part").stat().st_size
        self.assertLessEqual(kept, half)
        # and the next try asks for the rest only
        s.httpd.shutdown()
        s2 = self.serve()
        V.download(self.model, 0.0, 1.0)
        self.assertEqual(s2.asked, [f"bytes={kept}-"] if kept else [None])
        self.assertTrue((V.MODELS / "m" / "tokens.txt").exists())


class SetupTest(unittest.TestCase):
    """setup() with the venv, pip, the load check and systemctl faked (subprocess.run), and the downloads too."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = self.d = Path(tmp.name)
        self.calls, self.enabled, self.fail, self.check_fails, self.where, self.failing = [], True, None, False, [], None
        self.import_fails = 0
        for name, value in (("DATA", d / "data"), ("VENV", d / "data" / "venv"), ("MODELS", d / "data" / "models"),
                            ("READY", d / "data" / "setup.json"), ("PROGRESS", d / "progress.json"),
                            ("SPEECH_PROGRESS", d / "speech-progress.json"), ("STATUS", d / "status.json"),
                            ("download", self.download)):
            p = mock.patch.object(V, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(V.subprocess, "run", self.run_)
        p.start()
        self.addCleanup(p.stop)

    def run_(self, argv, **kw):
        """`check_fails`: the check's exit status (3: the engine does not load; -9: killed), for the parts named in
        `failing` (all when None); `import_fails`: the engine's import probe's."""
        self.calls.append([str(a) for a in argv])
        if argv[1:2] == ["-c"] and self.import_fails:
            raise subprocess.CalledProcessError(self.import_fails, argv)
        if argv[1:3] == ["-m", "venv"]:
            (Path(argv[3]) / "bin").mkdir(parents=True)
            (Path(argv[3]) / "bin" / "python").write_text("")
        part = "speak" if argv[-1] == "speak" else "listen"
        if "check" in argv[-2:] and self.check_fails and (self.failing is None or part in self.failing):
            raise subprocess.CalledProcessError(1 if self.check_fails is True else self.check_fails, argv)
        out = "enabled\n" if argv[0] == "systemctl" and "is-enabled" in argv and self.enabled else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    def download(self, model, lo, hi, where=None):
        self.where.append(where)
        if self.fail:
            raise RuntimeError(self.fail)
        (V.MODELS / model[0]).mkdir(parents=True, exist_ok=True)
        (V.MODELS / model[0] / "tokens.txt").write_text("a 0\n")

    def setup(self, part="listen"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            r = V.setup(part)
        return r, out.getvalue()

    def test_set_up_from_nothing(self):
        V.write_json(V.STATUS, {"state": "error", "error": "not set up"})
        r, out = self.setup()
        self.assertEqual(r, 0, out)
        self.assertTrue(V.ready())
        self.assertEqual(V.read_json(V.READY)["python"], V.python_version())
        self.assertTrue((V.VENV / "bin" / "python").exists())
        self.assertFalse(V.VENV.with_name("venv.new").exists())
        self.assertFalse(V.PROGRESS.exists())
        self.assertFalse(V.STATUS.exists())               # the listener's "not set up" is gone
        self.assertIn(["systemctl", "--user", "restart", V.UNIT], self.calls)
        self.assertTrue(any(c[-1] == "check" for c in self.calls))
        self.assertTrue((V.DATA / "setup.log").exists())

    def test_a_venv_of_this_version_is_kept_and_a_listener_off_stays_off(self):
        self.setup()
        self.calls.clear()
        self.enabled = False
        r, _ = self.setup()
        self.assertEqual(r, 0)
        self.assertFalse([c for c in self.calls if c[1:3] == ["-m", "venv"]])
        self.assertNotIn(["systemctl", "--user", "restart", V.UNIT], self.calls)

    def test_another_python_makes_a_new_venv(self):
        self.setup()
        V.write_json(V.READY, dict(V.read_json(V.READY), python="3.13"))
        self.assertFalse(V.ready())
        self.calls.clear()
        self.setup()
        self.assertTrue([c for c in self.calls if c[1:3] == ["-m", "venv"]])
        self.assertTrue(V.ready())

    def test_a_failure_is_left_for_the_panel(self):
        self.fail = "no network"
        r, out = self.setup()
        self.assertEqual(r, 1)
        self.assertIn("setup failed: no network", out)
        st = V.read_json(V.PROGRESS)
        self.assertEqual((st["error"], st["pid"]), ("no network", os.getpid()))
        self.assertFalse(V.READY.exists())

    def test_a_card_without_room_stops_before_anything_is_made(self):
        with mock.patch.object(V, "free_bytes", lambda path: 100 << 20):
            r, out = self.setup()
        self.assertEqual(r, 1)
        self.assertRegex(out, r"not enough space on the card: 100 MB free, setting up needs about \d+ MB")
        self.assertIn("not enough space", V.read_json(V.PROGRESS)["error"])
        self.assertEqual(self.calls, [])                  # no venv, no pip
        self.assertEqual(self.where, [])                  # no download

    def test_the_room_asked_for_is_what_is_still_missing(self):
        fresh = V.space_needed(V.PARTS["listen"])
        # the engine, and each model's archive beside what it unpacks
        self.assertGreater(fresh, V.VENV_BYTES + 2 * (V.KWS[2] + V.ASR[2]))
        self.setup()
        self.assertLess(V.space_needed(V.PARTS["listen"]), 100 << 20)      # all there: the margin only
        speak = V.space_needed(V.PARTS["speak"])                            # the venv is there: the voice only
        self.assertGreater(speak, 2 * V.TTS[2])
        self.assertLess(speak, V.VENV_BYTES + 2 * V.TTS[2])
        # a part already downloaded counts as come
        V.MODELS.mkdir(parents=True, exist_ok=True)
        with open(V.MODELS / f"{V.TTS[0]}.tar.bz2.part", "wb") as f:
            f.truncate(100 << 20)
        self.assertEqual(V.space_needed(V.PARTS["speak"]), speak - (100 << 20))

    def pip_fails(self, said):
        def run(argv, **kw):
            if argv[1:3] == ["-m", "pip"]:
                kw["stdout"].write(said)
                kw["stdout"].flush()
                raise subprocess.CalledProcessError(1, argv)
            return self.run_(argv, **kw)
        with mock.patch.object(V.subprocess, "run", run):
            return self.setup()

    def test_pip_without_internet_says_so(self):
        r, out = self.pip_fails("WARNING: Retrying (Retry(total=4, connect=None, read=None)) after connection broken by "
                                "'NewConnectionError(\"Failed to establish a new connection: [Errno -3] Temporary failure "
                                "in name resolution\")': /simple/sherpa-onnx/\nERROR: Could not find a version that "
                                "satisfies the requirement sherpa-onnx==2.0.1 (from versions: none)\n")
        self.assertEqual(r, 1)
        err = V.read_json(V.PROGRESS)["error"]
        self.assertIn("could not download the speech engine", err)
        self.assertIn("internet", err)
        self.assertNotIn("non-zero exit status", err)
        self.assertFalse(V.VENV.exists())

    def test_pip_failing_otherwise_points_at_its_log(self):
        r, out = self.pip_fails("ERROR: sherpa_onnx-2.0.1-cp314-cp314-manylinux_2_28_aarch64.whl is not a supported wheel on this platform.\n")
        self.assertEqual(r, 1)
        err = V.read_json(V.PROGRESS)["error"]
        self.assertIn("installing the speech engine failed", err)
        self.assertIn("is not a supported wheel", err)        # pip's own last word
        self.assertNotIn("internet", err)

    def test_one_setup_at_a_time(self):
        V.DATA.mkdir(parents=True)
        with open(V.DATA / "setup.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r, out = self.setup()
        self.assertEqual((r, out.strip()), (1, "setup is already running"))
        self.assertEqual(self.calls, [])

    def test_a_setup_that_ended_without_finishing_is_shown_as_stopped(self):
        V.write_json(V.PROGRESS, {"fraction": 0.4, "step": "downloading", "pid": os.getpid()})
        self.assertIn("stopped before it finished", V.status()["setup"]["error"])     # no lock file at all
        V.DATA.mkdir(parents=True, exist_ok=True)
        with open(V.DATA / "setup.lock", "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            held.write(f"{os.getpid()} listen\n")
            held.flush()
            self.assertEqual(V.status()["setup"]["step"], "downloading")               # a setup holds it
        self.assertIn("stopped before it finished", V.status()["setup"]["error"])     # free again

    def test_a_venv_that_does_not_load_is_made_again(self):
        # the engine itself does not load (exit 3): the venv is made again, and every part on disk checked in it
        self.setup()
        self.setup("speak")
        self.check_fails = 3
        r, out = self.setup()
        self.assertEqual(r, 1, out)
        self.assertFalse(V.READY.exists())
        self.assertTrue((V.MODELS / V.KWS[0]).exists() and (V.MODELS / V.TTS[0]).exists())     # the models kept
        self.check_fails = False
        self.calls.clear()
        self.assertEqual(self.setup()[0], 0)
        self.assertTrue([c for c in self.calls if c[1:3] == ["-m", "venv"]])
        self.assertTrue(V.ready() and V.ready(V.PARTS["speak"]))                                 # the voice too

    def test_models_that_do_not_load_are_downloaded_again(self):
        # a model the engine refuses (exit 1): only that part's models go, unmarked; the venv and the voice stay
        self.setup()
        self.setup("speak")
        self.check_fails = 1
        r, _ = self.setup()
        self.assertEqual(r, 1)
        self.assertFalse((V.MODELS / V.KWS[0]).exists())
        self.assertEqual(V.read_json(V.READY)["models"], [V.TTS[0]])
        self.assertFalse(V.ready())
        self.assertTrue(V.ready(V.PARTS["speak"]))
        self.check_fails = False
        self.calls.clear()
        self.assertEqual(self.setup()[0], 0)
        self.assertFalse([c for c in self.calls if c[1:3] == ["-m", "venv"]])

    def test_a_check_that_was_killed_keeps_the_download(self):
        # out of memory beside the language model, say (killed: exit -9): unmarked, but not downloaded again
        self.setup()
        self.check_fails, self.failing = -9, {"speak"}
        r, _ = self.setup("speak")
        self.assertEqual(r, 1)
        self.assertTrue((V.MODELS / V.TTS[0]).exists())
        self.assertFalse(V.ready(V.PARTS["speak"]))
        self.assertTrue(V.ready())

    def test_an_engine_that_crashes_on_import_is_made_again(self):
        # a damaged library in the venv kills its python (SIGSEGV) before check could say exit 3: the venv is made
        # again, the models left alone (review 2026-11-19)
        self.setup()
        self.import_fails = -signal.SIGSEGV
        r, out = self.setup("speak")
        self.assertEqual(r, 1)
        self.assertIn("the speech engine does not load", out)
        self.assertFalse(V.READY.exists())
        self.assertTrue((V.MODELS / V.KWS[0]).exists() and (V.MODELS / V.TTS[0]).exists())
        self.import_fails = 0
        self.calls.clear()
        self.assertEqual(self.setup("speak")[0], 0)
        self.assertTrue([c for c in self.calls if c[1:3] == ["-m", "venv"]])
        self.assertTrue(V.ready() and V.ready(V.PARTS["speak"]))

    def test_a_check_that_crashed_downloads_again(self):
        # a damaged model can crash the engine (SIGSEGV, SIGBUS): kept, it would crash every setup after
        self.setup()
        self.check_fails, self.failing = -signal.SIGSEGV, {"speak"}
        self.assertEqual(self.setup("speak")[0], 1)
        self.assertFalse((V.MODELS / V.TTS[0]).exists())

    def test_the_voices_setup_brings_a_stopped_listener_back(self):
        # a new Python: the listener stopped on "not set up"; the owner turns speak answers off and on, and the voice's
        # setup, which makes the new venv and checks listening in it, starts the listener again (review 2026-11-19)
        self.setup()
        self.setup("speak")
        V.write_json(V.READY, dict(V.read_json(V.READY), python="3.13"))
        V.write_json(V.STATUS, {"state": "error", "error": "not set up: press Set up in the panel's Voice tab"})
        self.calls.clear()
        self.assertEqual(self.setup("speak")[0], 0)
        self.assertTrue(V.ready())
        self.assertFalse(V.STATUS.exists())
        self.assertIn(["systemctl", "--user", "restart", V.UNIT], self.calls)
        # a voice setup that did not touch listening leaves the listener alone
        self.calls.clear()
        shutil.rmtree(V.MODELS / V.TTS[0])
        V.mark([V.TTS], checked=False)
        self.assertEqual(self.setup("speak")[0], 0)
        self.assertFalse([c for c in self.calls if c[0] == "systemctl"])

    def test_a_new_venv_for_one_part_checks_the_other(self):
        self.setup()
        self.setup("speak")
        V.write_json(V.READY, dict(V.read_json(V.READY), python="3.13"))       # a Fedora update to another Python
        self.calls.clear()
        self.assertEqual(self.setup()[0], 0)
        self.assertTrue(V.ready() and V.ready(V.PARTS["speak"]))
        self.assertIn(["check", "speak"], [c[-2:] for c in self.calls])
        # the other part not loading there is said, and leaves this part's setup done
        V.write_json(V.READY, dict(V.read_json(V.READY), python="3.13"))
        self.check_fails, self.failing = 1, {"speak"}
        r, out = self.setup()
        self.assertEqual(r, 0, out)
        self.assertIn("speak: its models do not load in the new venv", out)
        self.assertTrue(V.ready())
        self.assertFalse(V.ready(V.PARTS["speak"]))

    def test_each_part_sees_only_its_own_setup_running(self):
        V.write_json(V.SPEECH_PROGRESS, {"fraction": 0.4, "step": "downloading", "pid": 1})
        V.DATA.mkdir(parents=True, exist_ok=True)
        with open(V.DATA / "setup.lock", "a+") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            held.write("123 listen\n")
            held.flush()                                                       # the listening setup at work:
            self.assertTrue(V.setup_running("listen") and V.setup_running())
            self.assertFalse(V.setup_running("speak"))                         # the voice's progress is a leftover
            self.assertIn("stopped before it finished", V.speech_status()["setup"]["error"])
            self.assertTrue(V.speech_status()["busy"])                         # but a setup is at work
        self.assertFalse(V.speech_status()["busy"])

    def test_the_load_check_uses_the_default_wake_phrase(self):
        # the owner's phrase may be one the spotter refuses: that is for the listener to report, not a failed setup
        asked = []
        with mock.patch.multiple(V, into_venv=lambda argv: None, spotter=lambda cfg: asked.append(cfg["wake"]),
                                 recogniser=lambda: None, engine_problem=lambda: "", CONFIG=self.d / "config.json"):
            V.write_json(V.CONFIG, {"wake": "bad phrase"})
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(V.check(), 0)
        self.assertEqual(asked, ["hey computer"])

    def test_settings_that_do_not_read_come_with_no_commands(self):
        with mock.patch.object(V, "CONFIG", self.d / "config.json"):
            V.CONFIG.write_text("{broken")
            st = V.status()
        self.assertEqual(st["config"]["commands"], [])
        self.assertTrue(st["config_error"])

    def test_the_voice_is_set_up_on_its_own(self):
        self.setup()
        # the owner's board: set up before the voice came, its marker naming the two listening models
        V.write_json(V.READY, {"packages": V.PACKAGES, "models": [V.KWS[0], V.ASR[0]], "python": V.python_version(),
                               "at": "2026-11-18 19:39:31"})
        self.assertTrue(V.ready())
        self.assertFalse(V.ready(V.PARTS["speak"]))
        self.calls.clear()
        r, out = self.setup("speak")
        self.assertEqual((r, out.strip()), (0, "the voice is set up"))
        self.assertEqual(V.read_json(V.READY)["models"], [V.KWS[0], V.ASR[0], V.TTS[0]])
        self.assertTrue(V.ready() and V.ready(V.PARTS["speak"]))
        self.assertFalse([c for c in self.calls if c[1:3] == ["-m", "venv"]])          # the venv kept
        self.assertIn(["check", "speak"], [c[-2:] for c in self.calls])
        self.assertFalse([c for c in self.calls if c[0] == "systemctl"])               # the listener left alone
        self.assertEqual(self.where, [V.PROGRESS, V.PROGRESS, V.SPEECH_PROGRESS])      # its own progress file
        self.assertFalse(V.SPEECH_PROGRESS.exists())
        self.assertTrue((V.DATA / "setup-speak.log").exists())

    def test_a_voice_that_does_not_load_leaves_listening_as_it_is(self):
        self.setup()
        self.check_fails = True
        r, out = self.setup("speak")
        self.assertEqual(r, 1, out)
        self.assertIn("setup-speak.log", out)
        self.assertTrue(V.ready())
        self.assertFalse(V.ready(V.PARTS["speak"]))
        self.assertFalse((V.MODELS / V.TTS[0]).exists())
        self.assertIn("non-zero exit status", V.read_json(V.SPEECH_PROGRESS)["error"])   # for the Assistant tab
        self.assertFalse(V.PROGRESS.exists())                                            # not the Voice tab's

    def test_the_voice_first_then_listening(self):
        r, _ = self.setup("speak")
        self.assertEqual(r, 0)
        self.assertTrue(V.ready(V.PARTS["speak"]))
        self.assertFalse(V.ready())
        self.calls.clear()
        self.assertEqual(self.setup()[0], 0)
        self.assertFalse([c for c in self.calls if c[1:3] == ["-m", "venv"]])
        self.assertEqual(V.read_json(V.READY)["models"], [V.KWS[0], V.ASR[0], V.TTS[0]])
        # a new Python: a new venv, in which the voice is set up and listening checked again (its models kept)
        V.write_json(V.READY, dict(V.read_json(V.READY), python="3.13"))
        self.setup("speak")
        self.assertEqual(V.read_json(V.READY)["models"], [V.KWS[0], V.ASR[0], V.TTS[0]])
        self.assertTrue(V.ready())


class CommandLineTest(unittest.TestCase):
    """The program as the panel runs it: settings in a scratch HOME, the commands it would start recorded by a fake
    setsid, systemctl answering what the test says."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "bin").mkdir()
        self.log = d / "started"
        (d / "bin" / "setsid").write_text(f'#!/bin/sh\n[ "$1" = -f ] && shift\necho "$*" >> {self.log}\n')
        (d / "bin" / "systemctl").write_text(f'#!/bin/sh\necho "$*" >> {d}/systemctl\ncase "$2" in is-active) echo "$FAKE_ACTIVE";; '
                                             'is-enabled) echo "$FAKE_ENABLED";; esac\n')
        for f in ("setsid", "systemctl"):
            (d / "bin" / f).chmod(0o755)
        self.run_dir = d / "run"
        self.run_dir.mkdir()
        self.d = d
        self.env = dict(os.environ, HOME=str(d / "home"), PATH=f"{d}/bin:/usr/bin:/bin", XDG_RUNTIME_DIR=str(self.run_dir),
                        FAKE_ACTIVE="inactive", FAKE_ENABLED="disabled")
        self.config = d / "home" / ".config" / "a7a-voice" / "config.json"

    def voice(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True, text=True,
                              env=self.env, timeout=30)

    def test_save_then_test(self):
        r = self.voice("save", stdin=json.dumps({"wake": "hello board", "commands": [{"say": "say hi", "run": "notify-send hi"}]}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads(self.config.read_text())["wake"], "hello board")
        r = self.voice("test", "please say hi")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("“say hi”", r.stdout)
        self.assertEqual(self.log.read_text(), "systemd-run --user --scope --quiet --collect -- /bin/sh -c notify-send hi\n")
        r = self.voice("test", "something else")
        self.assertEqual(r.returncode, 1)
        r = self.voice("test", "say high")
        self.assertIn("the closest is \u201csay hi\u201d", r.stdout)
        self.assertEqual(len(self.log.read_text().splitlines()), 1)

    def test_save_refuses_what_is_not_settings(self):
        for bad in ("not json", "[1, 2]"):
            r = self.voice("save", stdin=bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn("not saved", r.stdout)
        self.assertFalse(self.config.exists())

    def test_status(self):
        r = self.voice("status")
        s = json.loads(r.stdout)
        self.assertEqual((s["ready"], s["running"], s["enabled"], s["setup"]), (False, False, False, None))
        self.assertEqual(s["config"], dict(V.clean(None), wake=SHIPPED_WAKE))     # the script run as it ships
        # a listener that stopped on an error leaves the error, a running one its whole report
        (self.run_dir / "a7a-voice.json").write_text(json.dumps({"state": "error", "error": "no microphone", "heard": "x"}))
        s = json.loads(self.voice("status").stdout)
        self.assertEqual((s["error"], "heard" in s), ("no microphone", False))
        self.env.update(FAKE_ACTIVE="active", FAKE_ENABLED="enabled")
        (self.run_dir / "a7a-voice.json").write_text(json.dumps({"state": "listening", "heard": "volume up", "ran": "volume up",
                                                                  "at": 5, "pid": 9}))
        s = json.loads(self.voice("status").stdout)
        self.assertEqual((s["running"], s["enabled"], s["state"], s["ran"], "pid" in s), (True, True, "listening", "volume up", False))
        # the online assistants: the panel's, until the listener says which it listens for (review 2026-11-19)
        panel = self.d / "home" / ".config" / "a7a-panel" / "assistant.json"
        panel.parent.mkdir(parents=True)
        panel.write_text(json.dumps({"online": [{"name": "google", "key": "k"}, {"name": "ai", "key": "k"}]}))
        self.assertEqual(json.loads(self.voice("status").stdout)["online"], ["google", "ai"])
        (self.run_dir / "a7a-voice.json").write_text(json.dumps({"state": "listening", "online": ["google"],
                                                                  "left_out": ["\u201chey ai\u201d: too short"]}))
        s = json.loads(self.voice("status").stdout)
        self.assertEqual((s["online"], s["left_out"]), (["google"], ["\u201chey ai\u201d: too short"]))
        self.assertNotIn("listening", s)            # a listener that has not said: nothing to compare with
        # the phrases it wakes for, beside the saved ones, for the panel to tell a refused one (2026-11-24)
        config = self.d / "home" / ".config" / "a7a-voice" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"wake": "hey computer"}))
        read = config.stat().st_mtime_ns
        (self.run_dir / "a7a-voice.json").write_text(json.dumps({"state": "listening", "wake": "hey computer",
                                                                  "assistant": "hey assistant", "read": read}))
        s = json.loads(self.voice("status").stdout)
        self.assertEqual(s["listening"], {"wake": "hey computer", "assistant": "hey assistant"})
        # saved since, not read yet: nothing to compare (review 2026-11-24: a false "did not take" for a second)
        os.utime(config, ns=(read, read + 10**9))
        self.assertNotIn("listening", json.loads(self.voice("status").stdout))

    def test_listen_and_on_before_setup(self):
        r = self.voice("listen")
        self.assertEqual(r.returncode, 0)                 # the service must not start again and again
        self.assertIn("not set up", json.loads((self.run_dir / "a7a-voice.json").read_text())["error"])
        self.assertEqual(self.voice("on").returncode, 1)

    def test_usage(self):
        self.assertEqual(self.voice().returncode, 2)
        self.assertEqual(self.voice("test").returncode, 2)


class UnitTest(unittest.TestCase):
    def test_the_unit_runs_the_installed_listener_in_the_session_and_gives_up(self):
        text = UNIT.read_text()
        for line in ("ExecStart=/usr/local/bin/a7a-voice listen", "WantedBy=graphical-session.target",
                     "PartOf=graphical-session.target", "Restart=on-failure", "RestartSec=5",
                     "StartLimitIntervalSec=120", "StartLimitBurst=4"):
            self.assertIn(line + "\n", text)
        # the limit lives in [Unit] (systemd ignores it in [Service]), and must be reachable: 4 starts 5 s apart
        unit, service = text.split("[Service]")
        self.assertIn("StartLimitBurst", unit)


class ShippedDefaultTest(unittest.TestCase):
    def test_the_image_wakes_on_hey_cubie(self):
        self.assertEqual(SHIPPED_WAKE, "hey cubie")
        self.assertEqual(V.DEFAULTS["assistant"], "hey assistant")


if __name__ == "__main__":
    unittest.main()
