"""tools/board/npu: the NPU wake spotter's CPU side. The keyword graph and search (npu_kws, a port of sherpa-onnx's
keyword spotter) on a tiny synthetic model whose joiner says which token each frame holds, and the tensor format
conversions of npunet (VIPLite through ctypes) without the library. The real model and the NPU are checked on the
board (docs/RADXA-SYNC-2026-11-15.md, the NPU section)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

P = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(P / "tools" / "board" / "npu"))
import npu_kws  # noqa: E402
import npunet  # noqa: E402

V, D = 8, 8                                   # tokens (0 blank, 2 unk) and the joiner's width


def tiny(d, keywords):
    """A model directory: weights of the right shapes (the joiner is replaced per test), tokens A..E = 3..7."""
    np.savez(Path(d) / "w.npz", emb=np.zeros((V, D), np.float32), conv=np.zeros((D, 4, 2), np.float32),
             dproj_w=np.zeros((D, D), np.float32), dproj_b=np.zeros(D, np.float32),
             out_w=np.zeros((V, D), np.float32), out_b=np.zeros(V, np.float32),
             state_shapes=np.array(json.dumps([[1, 2]])))
    (Path(d) / "tokens.txt").write_text("<blk> 0\n<sos/eos> 1\n<unk> 2\n" +
                                        "".join(f"▁{c} {3 + i}\n" for i, c in enumerate("ABCDE")))
    (Path(d) / "kw.txt").write_text("\n".join(keywords) + "\n")


class Frames:
    """The encoder: each frame is the token it should say (the joiner below reads it back), 8 frames a chunk; its
    cache output is the run's number, and it keeps the cache each run was given."""

    def __init__(self, tokens):
        self.tokens, self.given = list(tokens), []

    def run(self, arrays):
        self.given.append(float(arrays[1].reshape(-1)[0]))
        out = np.zeros((1, 8, D), np.float32)
        for i in range(8):
            out[0, i, 0] = self.tokens.pop(0) if self.tokens else 0
        return [out, np.full((1, 2), len(self.given), np.float32)]


class Fbank:
    """Features: only how many frames there are (the frames themselves go to the fake encoder, unread)."""

    def __init__(self):
        self.num_frames_ready = 0

    def accept_waveform(self, rate, samples):
        self.num_frames_ready += len(samples) // 160

    def get_frame(self, i):
        return np.zeros(80, np.float32)


def spotter(d, tokens, sure=0.9, **kw):
    s = npu_kws.Spotter(Frames(tokens), Path(d) / "w.npz", Path(d) / "tokens.txt", Path(d) / "kw.txt", **kw)
    rest = np.log((1 - sure) / (V - 1))

    def joiner(enc, decs):                    # the frame's token with probability `sure`, for every hypothesis
        lp = np.full((len(decs), V), rest, np.float32)
        lp[:, int(enc[0])] = np.log(sure)
        return lp
    s.joiner = joiner
    s.features = Fbank
    return s


def heard(s, chunks):
    st = s.create_stream()
    st.accept_waveform(16000, np.zeros(160 * (npu_kws.SHIFT * chunks + npu_kws.T), np.float32))   # chunks + 1 decodes
    hits = []
    while s.is_ready(st):
        s.decode_stream(st)
        if r := s.get_result(st):
            hits.append(r)
    return hits


class Search(unittest.TestCase):
    def test_a_keyword_said_then_silence_is_heard_once_by_its_phrase(self):
        with tempfile.TemporaryDirectory() as d:
            tiny(d, ["▁A ▁B ▁C", "▁D ▁E @dee ee"])
            self.assertEqual(heard(spotter(d, [3, 4, 5, 0, 0, 0]), 1), ["A B C"])     # as sherpa-onnx names it
            self.assertEqual(heard(spotter(d, [0, 6, 7, 0, 0]), 1), ["dee ee"])       # the @phrase when given

    def test_it_waits_for_a_trailing_blank_and_finds_a_keyword_after_others(self):
        with tempfile.TemporaryDirectory() as d:
            tiny(d, ["▁A ▁B ▁C"])
            self.assertEqual(heard(spotter(d, [3, 4, 5]), 1)[:1], ["A B C"])           # the chunk's blanks after it
            self.assertEqual(heard(spotter(d, [0, 0, 0, 0, 0, 3, 4, 5]), 0), [])      # its last frame: no blank yet
            self.assertEqual(heard(spotter(d, [0, 0, 0, 0, 0, 3, 4, 5]), 1), ["A B C"])  # the next chunk's blanks
            self.assertEqual(heard(spotter(d, [6, 7, 3, 4, 5, 0, 0, 0]), 1), ["A B C"])
            self.assertEqual(heard(spotter(d, [3, 4, 0, 0, 0, 0, 0, 0]), 1), [])     # only half of it said

    def test_the_threshold_is_the_average_token_probability(self):
        with tempfile.TemporaryDirectory() as d:
            tiny(d, ["▁A ▁B ▁C"])
            self.assertEqual(heard(spotter(d, [3, 4, 5, 0, 0], sure=0.5, keywords_threshold=0.4), 1), ["A B C"])
            self.assertEqual(heard(spotter(d, [3, 4, 5, 0, 0], sure=0.5, keywords_threshold=0.6), 1), [])
            tiny(d, ["▁A ▁B ▁C #0.4"])                                  # per keyword, as sherpa's file
            self.assertEqual(heard(spotter(d, [3, 4, 5, 0, 0], sure=0.5, keywords_threshold=0.9), 1), ["A B C"])

    def test_after_a_hit_it_listens_again_and_the_encoder_state_stays(self):
        with tempfile.TemporaryDirectory() as d:
            tiny(d, ["▁A ▁B"])
            s = spotter(d, [3, 4, 0, 0, 0, 0, 0, 0, 3, 4, 0, 0])
            self.assertEqual(heard(s, 2), ["A B", "A B"])
            heard(s, 1)                                                              # new streams, the same caches
            self.assertEqual(s.encoder.given, [0.0, 1.0, 2.0, 3.0, 4.0])             # 3 runs, then 2: each gets the last one's

    def test_a_keyword_with_pieces_the_model_lacks_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tiny(d, ["▁A ▁Z"])
            with self.assertRaisesRegex(ValueError, "does not know"):
                spotter(d, [])

    def test_the_graph_steps_like_sherpas_context_graph(self):
        g = npu_kws.Graph([([3, 4, 5], "abc", None, None), ([4, 5], "bc", None, None)], score=1.0)
        score, st = g.step(g.root, 3)
        self.assertEqual((score, st.level), (1.0, 1))
        score, st = g.step(st, 4)
        score, st = g.step(st, 5)
        self.assertIs(g.matched(st), st)
        self.assertEqual(st.phrase, "abc")
        score, st2 = g.step(g.root, 4)
        score, st2 = g.step(st2, 5)
        self.assertEqual(g.matched(st2).phrase, "bc")
        score, st3 = g.step(st, 6)                                                    # off the graph: the boosts go
        self.assertIs(st3, g.root)
        self.assertEqual(score, -st.node_score)


class Formats(unittest.TestCase):
    def tensor(self, fmt, quant=0, scale=0.0, zp=0, fp=0):
        t = object.__new__(npunet.Tensor)
        t.params = npunet.Params()
        t.params.data_format, t.params.quant_format = fmt, quant
        if quant == npunet.ASYMM:
            t.params.quant_data.affine.scale, t.params.quant_data.affine.zero_point = scale, zp
        elif quant == npunet.DFP:
            t.params.quant_data.fixed_point_pos = fp
        t.dtype = npunet.FORMATS[fmt]
        return t

    def test_each_format_round_trips_and_clips(self):
        x = np.array([-3.0, -0.5, 0.0, 0.25, 1.5, 100.0], np.float32)
        t = self.tensor(1)
        self.assertEqual(t.encode(x).dtype, np.float16)
        np.testing.assert_allclose(t.decode(t.encode(x)), x, rtol=1e-3)
        t = self.tensor(2, npunet.ASYMM, scale=0.1, zp=128)                           # uint8, asymmetric
        np.testing.assert_allclose(t.decode(t.encode(x)), [-3.0, -0.5, 0.0, 0.2, 1.5, 12.7], atol=1e-5)   # 2.5 rounds to even
        t = self.tensor(5, npunet.DFP, fp=8)                                          # int16, fixed point 8
        np.testing.assert_allclose(t.decode(t.encode(x)), [-3.0, -0.5, 0.0, 0.25, 1.5, 100.0])
        t = self.tensor(3, npunet.DFP, fp=6)                                          # int8: 127/64 at most
        self.assertEqual(float(t.decode(t.encode(np.array([100.0], np.float32)))[0]), 127 / 64)
        t = self.tensor(10)                                                           # int64 (token ids)
        self.assertEqual(t.encode(np.array([2.6], np.float32)).tolist(), [3])

    def test_the_create_params_match_vip_lite_h(self):
        # vip_buffer_create_params_t: num_of_dims, sizes[6], data_format, quant_format, a 8-byte union, memory_type
        self.assertEqual(npunet.Params.data_format.offset, 28)
        self.assertEqual(npunet.Params.quant_data.offset, 36)
        self.assertEqual(npunet.Params.memory_type.offset, 44)


if __name__ == "__main__":
    unittest.main()
