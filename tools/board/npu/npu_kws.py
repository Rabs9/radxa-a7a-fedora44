"""A keyword spotter with the zipformer's encoder on the NPU (npunet) and the rest on the CPU in numpy: the same
search as sherpa-onnx's KeywordSpotter (transducer modified beam search over a keyword graph, strict matching,
max_active_paths 4, num_trailing_blanks 1, average token probability >= the threshold), with the same calls a7a-voice
uses: create_stream(), stream.accept_waveform(rate, samples), is_ready(), decode_stream(), get_result(),
reset_stream().

Weights for the decoder and joiner come from kws_cpu.npz (made from the ONNX files by make_npz()); features are
kaldi-native-fbank's, as sherpa-onnx computes them (80 bins, no dither, no edge snipping, 20 Hz to Nyquist-400).
`encoder` is any object with run(list of float arrays) -> list of float arrays in the frozen encoder's order
(npunet.Net on the board; an onnxruntime session wrapper for checks on a PC)."""
import json
import math
from collections import deque

import numpy as np

T, SHIFT, CONTEXT, BLANK = 45, 32, 2, 0


class Node:
    __slots__ = ("token", "token_score", "node_score", "output_score", "is_end", "level", "phrase", "threshold",
                 "next", "fail", "output")

    def __init__(self, token, token_score, node_score, is_end, level, phrase="", threshold=0.25):
        self.token, self.token_score, self.node_score = token, token_score, node_score
        self.output_score, self.is_end, self.level = 0.0, is_end, level
        self.phrase, self.threshold = phrase, threshold
        self.next, self.fail, self.output = {}, None, None


class Graph:
    """sherpa-onnx's ContextGraph (Aho-Corasick with per-token boosts) as the keyword spotter builds it."""

    def __init__(self, keywords, score=1.0, threshold=0.25):
        self.root = Node(-1, 0.0, 0.0, False, 0)
        self.root.fail = self.root
        for tokens, phrase, boost, thr in keywords:
            node = self.root
            for i, t in enumerate(tokens):
                end = i == len(tokens) - 1
                b = score if boost is None else boost
                if t not in node.next:
                    node.next[t] = Node(t, b, node.node_score + b, end, i + 1)
                else:  # sherpa keeps the larger boost on a shared prefix
                    n = node.next[t]
                    n.token_score = max(b, n.token_score)
                    n.node_score = node.node_score + n.token_score
                    n.is_end = n.is_end or end
                node = node.next[t]
                if end:
                    node.phrase, node.threshold = phrase, threshold if thr is None else thr
        self._fail()

    def _fail(self):
        q = deque()
        for n in self.root.next.values():
            n.fail = self.root
            q.append(n)
        while q:
            cur = q.popleft()
            for t, n in cur.next.items():
                f = cur.fail
                while True:
                    if t in f.next:
                        f = f.next[t]
                        break
                    if f.token == -1:
                        break
                    f = f.fail
                n.fail = f
                o = f
                while o.token != -1 and not o.is_end:
                    o = o.fail
                n.output = o if o.token != -1 else None
                n.output_score = n.node_score if n.is_end else 0.0
                if n.output is not None:
                    n.output_score += n.output.output_score
                q.append(n)

    def step(self, state, token):
        if token in state.next:
            node = state.next[token]
            score = node.token_score
        else:
            node = state.fail
            while token not in node.next:
                node = node.fail
                if node.token == -1:
                    break
            if token in node.next:
                node = node.next[token]
            score = node.node_score - state.node_score
        return score + node.output_score, node

    @staticmethod
    def matched(state):
        if state.is_end:
            return state
        return state.output


class Hyp:
    __slots__ = ("ys", "log_prob", "probs", "state", "blanks")

    def __init__(self, ys, log_prob, probs, state, blanks):
        self.ys, self.log_prob, self.probs, self.state, self.blanks = ys, log_prob, probs, state, blanks


def log_softmax(x):
    m = x.max(axis=-1, keepdims=True)
    y = x - m
    return y - np.log(np.exp(y).sum(axis=-1, keepdims=True))


class Stream:
    def __init__(self, spotter):
        self.sp = spotter
        self.fbank = spotter.features()
        self.used = 0                        # feature frames already decoded (a multiple of SHIFT)
        self.hyps = spotter.initial_hyps()
        self.result = ""

    def accept_waveform(self, rate, samples):
        if rate != 16000:
            raise ValueError("16 kHz only")
        self.fbank.accept_waveform(16000, np.asarray(samples, np.float32).tolist())

    def input_finished(self):
        self.fbank.input_finished()


class Spotter:
    def __init__(self, encoder, npz, tokens, keywords_file, keywords_threshold=0.25, keywords_score=1.0,
                 max_active_paths=4, num_trailing_blanks=1):
        w = np.load(npz)
        self.emb, self.conv, self.dproj_w, self.dproj_b = w["emb"], w["conv"], w["dproj_w"], w["dproj_b"]
        self.out_w, self.out_b = w["out_w"], w["out_b"]
        self.state_shapes = [tuple(s) for s in json.loads(str(w["state_shapes"]))]
        self.encoder = encoder
        ids = {}
        for line in open(tokens, encoding="utf-8"):
            p = line.split()
            if len(p) == 2:
                ids[p[0]] = int(p[1])
        kws = []
        for line in open(keywords_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phrase = None
            if "@" in line:
                line, phrase = line.split("@", 1)
                phrase = phrase.strip()
            boost = thr = None
            words = []
            for x in line.split():
                if x.startswith(":"):
                    boost = float(x[1:]) or None                       # 0: the default, as in sherpa-onnx
                elif x.startswith("#"):
                    thr = float(x[1:]) or None
                else:
                    words.append(x)
            if any(x not in ids for x in words):
                raise ValueError(f"keyword {line!r}: pieces the model does not know")
            kws.append(([ids[x] for x in words], phrase if phrase else "".join(words).replace("\u2581", " ").strip(), boost, thr))
        self.unk = ids.get("<unk>", -1)                                 # treated as blank, as sherpa-onnx does
        self.graph = Graph(kws, keywords_score, keywords_threshold)
        self.beam, self.trailing = max_active_paths, num_trailing_blanks
        self._dec_cache = {}
        # the encoder's caches belong to the spotter, not to a stream: they carry on across create_stream() and
        # reset_stream(), as the warm encoder (processed_lens folded to a constant) assumes a long-running stream
        self.states = self.initial_states()
        ins = getattr(encoder, "inputs", None)
        if ins is not None:                     # the caches go back in by position: the network must take them so
            want = [T * 80] + [int(np.prod(sh)) for sh in self.state_shapes]
            got = [t.count for t in ins]
            outs = [t.count for t in encoder.outputs]
            if got != want or outs[1:] != want[1:] or outs[0] != SHIFT // 4 * self.out_w.shape[1]:
                raise ValueError("the encoder network does not match the CPU part's weights (its inputs and "
                                 "outputs are not the caches kws_cpu.npz names, in order)")

    # --- the small networks, as their ONNX graphs compute them ---
    def decoder(self, ys):
        key = tuple(ys)
        if key in self._dec_cache:
            return self._dec_cache[key]
        y = np.array(ys, np.int64)
        e = self.emb[np.clip(y, 0, None)] * (y >= 0)[:, None]          # (2, 320), unknown (-1) = zeros
        c = self.conv                                                    # (320, 4, 2): grouped conv, kernel 2
        groups = self.emb.shape[1] // c.shape[1]                         # 80 groups of 4 channels
        x = e.T.reshape(groups, c.shape[1], CONTEXT)                     # (80, 4, 2)
        out = np.einsum("goik,gik->go", c.reshape(groups, -1, c.shape[1], CONTEXT), x).reshape(-1)
        d = np.maximum(out, 0) @ self.dproj_w.T + self.dproj_b
        if len(self._dec_cache) > 4096:
            self._dec_cache.clear()
        self._dec_cache[key] = d
        return d

    def joiner(self, enc, decs):
        return np.tanh(enc[None, :] + decs) @ self.out_w.T + self.out_b

    def initial_states(self):
        return [np.zeros(s, np.float32) for s in self.state_shapes]

    def initial_hyps(self):
        return {(-1, BLANK): Hyp([-1, BLANK], 0.0, [], self.graph.root, 0)}

    @staticmethod
    def features():
        """kaldi-native-fbank's online features, as sherpa-onnx makes them for this model."""
        import kaldi_native_fbank as knf
        o = knf.FbankOptions()
        o.frame_opts.dither = 0
        o.frame_opts.snip_edges = False
        o.frame_opts.samp_freq = 16000
        o.mel_opts.num_bins = 80
        o.mel_opts.low_freq = 20
        o.mel_opts.high_freq = -400
        return knf.OnlineFbank(o)

    def create_stream(self):
        return Stream(self)

    def is_ready(self, s):
        return s.fbank.num_frames_ready - s.used >= T

    def reset_stream(self, s):
        s.hyps = self.initial_hyps()
        s.result = ""

    def get_result(self, s):
        r, s.result = s.result, ""
        return r

    def decode_stream(self, s):
        x = np.stack([s.fbank.get_frame(s.used + i) for i in range(T)])[None].astype(np.float32)
        outs = self.encoder.run([x] + self.states)
        enc = outs[0].reshape(-1, self.out_w.shape[1])                   # (8, 320)
        self.states = [o.reshape(sh) for o, sh in zip(outs[1:], self.state_shapes)]
        s.used += SHIFT
        if hasattr(s.fbank, "pop"):             # frames before the next window: kaldi-native-fbank keeps the numbering
            s.fbank.pop(SHIFT)
        for frame in enc:
            if self._frame(s, frame):
                return

    def _frame(self, s, frame):
        hyps = list(s.hyps.values())
        decs = np.stack([self.decoder(h.ys[-CONTEXT:]) for h in hyps])
        lp = log_softmax(self.joiner(frame, decs))                       # (n, V)
        probs = np.exp(lp)
        tot = lp + np.array([h.log_prob for h in hyps])[:, None]
        flat = tot.reshape(-1)
        k = min(self.beam, flat.size)
        top = np.argpartition(-flat, k - 1)[:k]
        top = top[np.argsort(-flat[top])]
        V = lp.shape[1]
        new = {}
        for idx in top:
            hi, tok = divmod(int(idx), V)
            h = hyps[hi]
            ys, pr, st, bl = h.ys, h.probs, h.state, h.blanks
            cscore = 0.0
            if tok != BLANK and tok != self.unk:
                ys = ys + [tok]
                pr = pr + [float(probs[hi, tok])]
                bl = 0
                cscore, st = self.graph.step(st, tok)
                if st.token == -1:                                       # off every keyword: forget the history
                    ys, pr = [-1, BLANK], []
            else:
                bl += 1
            nh = Hyp(ys, float(flat[idx]) + cscore, pr, st, bl)
            key = tuple(ys)
            if key in new:                                               # same token sequence: add the probabilities
                o = new[key]
                m = max(o.log_prob, nh.log_prob)
                lpsum = m + math.log(math.exp(o.log_prob - m) + math.exp(nh.log_prob - m))
                if nh.log_prob > o.log_prob:
                    nh.log_prob = lpsum
                    new[key] = nh
                else:
                    o.log_prob = lpsum
            else:
                new[key] = nh
        s.hyps = new
        best = max(new.values(), key=lambda h: h.log_prob)
        m = Graph.matched(best.state)
        if m is not None and best.blanks > self.trailing and m.level <= len(best.probs):
            # the keyword's own tokens: the last `level` probabilities. sherpa-onnx averages the first `level` of the
            # hypothesis; they differ only when a failure arc kept earlier tokens ("HEY HEY COMPUTER"), where these
            # are the keyword's. A hit also ends the chunk here (sherpa decodes its remaining frames with fresh
            # hypotheses; a7a-voice resets the stream after a hit anyway).
            avg = sum(best.probs[-m.level:]) / m.level
            if avg >= m.threshold:
                s.result = m.phrase
                self.reset_stream(s)
                s.result = m.phrase
                return True
        return False


def make_npz(decoder_onnx, joiner_onnx, encoder_onnx, out):
    """kws_cpu.npz: the decoder's and joiner's weights and the encoder's state shapes (on a PC, with onnx)."""
    import onnx
    from onnx import numpy_helper
    d = {i.name: numpy_helper.to_array(i) for i in onnx.load(decoder_onnx).graph.initializer}
    j = {i.name: numpy_helper.to_array(i) for i in onnx.load(joiner_onnx).graph.initializer}
    e = onnx.load(encoder_onnx)
    shapes = [[x.dim_value for x in i.type.tensor_type.shape.dim] for i in e.graph.input[1:]]
    np.savez(out, emb=d["decoder.embedding.weight"], conv=d["decoder.conv.weight"],
             dproj_w=d["decoder_proj.weight"], dproj_b=d["decoder_proj.bias"],
             out_w=j["output_linear.weight"], out_b=j["output_linear.bias"],
             state_shapes=np.array(json.dumps(shapes)))
