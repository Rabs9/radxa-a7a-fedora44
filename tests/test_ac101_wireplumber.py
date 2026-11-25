"""tools/board/wireplumber/51-a7a-ac101.conf: the AC101 jack's playback node in 16-bit (the BSP's I2S driver sends
24-bit audio 48 dB too quiet through that card's 16-bit slots). Parsed with PipeWire's own SPA-JSON reader when this
system has it; the node names are the board's (2026-11-18), matched the way PipeWire matches "~" values: an unanchored
extended regex."""
import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
CONF = P / "tools" / "board" / "wireplumber" / "51-a7a-ac101.conf"
DTB_TOOL = P / "tools" / "board" / "a7a-ac101-dtb"
AC101 = "alsa_output.platform-soc_3000000_i2s0_mach_ac101.stereo-fallback"
# its capture side too: the I2S has one sample-resolution field for both directions, and a 24-bit capture silenced a
# running 16-bit playback (2026-11-18)
AC101_IN = "alsa_input.platform-soc_3000000_i2s0_mach_ac101.stereo-fallback"
OTHERS = ["alsa_output.platform-soc_3000000_i2s3_mach.stereo-fallback",        # HDMI
          "alsa_input.platform-soc_3000000_i2s3_mach.stereo-fallback",
          "alsa_output.platform-soc_3000000_i2s0_mach.stereo-fallback",        # the packaged tree's AC101B card
          "alsa_input.platform-soc_3000000_i2s0_mach.stereo-fallback"]


@unittest.skipUnless(shutil.which("spa-json-dump"), "needs spa-json-dump (pipewire)")
class AC101WirePlumberTest(unittest.TestCase):
    def setUp(self):
        r = subprocess.run(["spa-json-dump", str(CONF)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.conf = json.loads(r.stdout)

    def test_one_rule_sets_16_bit(self):
        rules = self.conf["monitor.alsa.rules"]
        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0]["actions"], {"update-props": {"audio.format": "S16LE"}})

    def test_the_jack_is_the_default_output_once_switched_on(self):
        # the owner had to find the jack in KDE's sound menu after the switch (2026-11-24): its output now outranks
        # HDMI and a USB adapter (priority.session about 1000) where nobody chose another output; a choice made in
        # the sound menu is WirePlumber's configured default and still wins
        rule = self.conf["monitor.alsa.rules"][1]
        self.assertEqual(rule["actions"], {"update-props": {"priority.session": 2000, "priority.driver": 2000}})
        hits = lambda name: [m for m in rule["matches"] if re.search(m["node.name"][1:], name)]
        self.assertEqual(len(hits(AC101)), 1)
        self.assertEqual(hits(AC101_IN), [])                 # the output only: not the jack's microphone side
        for name in OTHERS:
            self.assertEqual(hits(name), [], name)

    def test_it_matches_both_directions_of_the_ac101_card_only(self):
        matches = self.conf["monitor.alsa.rules"][0]["matches"]
        for match in matches:
            self.assertEqual(list(match), ["node.name"])
            self.assertTrue(match["node.name"].startswith("~"))
        hits = lambda name: [m for m in matches if re.search(m["node.name"][1:], name)]
        self.assertEqual(len(hits(AC101)), 1)
        self.assertEqual(len(hits(AC101_IN)), 1)
        for name in OTHERS:
            self.assertEqual(hits(name), [], name)

    def test_it_names_the_card_that_a7a_ac101_dtb_adds(self):
        # the node name carries the card node's name: renamed there, the rule would match nothing and the jack would
        # be 48 dB quiet again
        loader = importlib.machinery.SourceFileLoader("a7a_ac101_dtb", str(DTB_TOOL))
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader("a7a_ac101_dtb", loader))
        loader.exec_module(m)
        for rule in self.conf["monitor.alsa.rules"]:
            for match in rule["matches"]:
                self.assertIn(f"_{m.CARD}.", match["node.name"])
        self.assertIn(m.CARD, AC101)


if __name__ == "__main__":
    unittest.main()
