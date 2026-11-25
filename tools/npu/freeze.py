#!/usr/bin/env python3
"""tools/npu/freeze.py [--cold] [--no-rewrite] [--no-batch-first]: freeze the English KWS zipformer (gigaspeech 3.3M, chunk-16) to N=1 static shapes for ACUITY, shapes probed with
onnxruntime; rewrite the conv-cache slices (Radxa's fix, rewrite_conv_slice.py); write zero NPY datasets."""
import glob, sys
from pathlib import Path
import numpy as np, onnx, onnxruntime as ort
from onnx import TensorProto
sys.path.insert(0, str(Path(__file__).parent))
from rewrite_conv_slice import rewrite

ROOT = Path(__file__).resolve().parents[2] / "output/npu-kws"   # the working tree: the model, frozen ONNX, datasets
SRC = ROOT / "src/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
OUT, DS = ROOT / "onnx_fixed", ROOT / "dataset"

def probe(path, feeds_fn):
    s = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    feeds = {}
    for i in s.get_inputs():
        sh = [1 if not isinstance(d, int) else d for d in i.shape]
        feeds[i.name] = np.zeros(sh, np.int64 if "int64" in i.type else np.float32)
    feeds_fn(feeds)
    outs = s.run(None, feeds)
    return ({k: v.shape for k, v in feeds.items()}, {o.name: r.shape for o, r in zip(s.get_outputs(), outs)},
            {k for k, v in feeds.items() if v.dtype == np.int64}, feeds)

def set_dims(vi, shape, et):
    tt = vi.type.tensor_type; tt.elem_type = et
    while len(tt.shape.dim) < len(shape): tt.shape.dim.add()
    while len(tt.shape.dim) > len(shape): tt.shape.dim.pop()
    for d, n in zip(tt.shape.dim, shape):
        d.ClearField("dim_param"); d.dim_value = int(n)

def freeze(src, dst, ins, outs, ints):
    m = onnx.load(str(src))
    def apply(m):
        for i in m.graph.input: set_dims(i, ins[i.name], TensorProto.INT64 if i.name in ints else TensorProto.FLOAT)
        for o in m.graph.output:
            set_dims(o, outs[o.name], TensorProto.INT64 if o.name.endswith("processed_lens") else TensorProto.FLOAT)
    apply(m)    # no onnx.shape_inference: its value_info for the downsample pads says 16 where ORT computes 17
    # (2026-11-19: ORT then failed in /downsample/Reshape_1); ACUITY infers the shapes itself
    return m

WARM = 100000   # processed_lens, frames: far past every left context, so every cached frame counts (no padding masks)


def warm(m):
    """The encoder for a stream already longer than its left context: processed_lens a constant, its masks folded
    away (28 of the 30 Expand ops, which ACUITY could not compile: "Setup node EXPAND_BROADCAST fail"), and no
    new_processed_lens output. The spotter keeps the encoder's state for the life of the listener, so only its first
    ~2 s differ from the streaming model (the zero caches count as heard instead of being masked)."""
    import onnxslim
    from onnx import helper, numpy_helper
    g = m.graph
    pl = [i for i in g.input if i.name == "processed_lens"][0]
    g.input.remove(pl)
    g.initializer.append(numpy_helper.from_array(np.array([WARM], np.int64), "processed_lens"))
    g.output.remove([o for o in g.output if o.name == "new_processed_lens"][0])
    m = onnxslim.slim(m)
    left = sum(n.op_type == "Expand" for n in m.graph.node)
    print("warm encoder:", len(m.graph.node), "nodes,", left, "Expand left")
    return m


def batch_first(m):
    """ACUITY takes every tensor's first dimension as the batch (its simulator refused "Batch must be the same, got
    [64, 1, 128], [1, 45, 80]"), and the NBG compiled with the caches' left context there disagreed with ONNX. Each
    cache that starts with its length ([L, 1, D]: keys and values) is passed as [1, L, D] instead: a Transpose after
    the input and before the output, inside the graph."""
    from onnx import helper
    g = m.graph
    n = 0
    for i in list(g.input):
        dims = [d.dim_value for d in i.type.tensor_type.shape.dim]
        if len(dims) == 3 and dims[0] != 1 and dims[1] == 1:
            outer = i.name + "_b1"
            for node in g.node:
                node.input[:] = [i.name + "_t" if x == i.name else x for x in node.input]
            g.node.insert(0, helper.make_node("Transpose", [outer], [i.name + "_t"], name=i.name + "_bt", perm=[1, 0, 2]))
            i.name = outer
            set_dims(i, [1, dims[0], dims[2]], TensorProto.FLOAT)
            n += 1
    for o in list(g.output):
        dims = [d.dim_value for d in o.type.tensor_type.shape.dim]
        if len(dims) == 3 and dims[0] != 1 and dims[1] == 1:
            inner = o.name + "_t"
            for node in g.node:
                node.output[:] = [inner if x == o.name else x for x in node.output]
            g.node.append(helper.make_node("Transpose", [inner], [o.name + "_b1"], name=o.name + "_bt", perm=[1, 0, 2]))
            o.name = o.name + "_b1"
            set_dims(o, [1, dims[0], dims[2]], TensorProto.FLOAT)
            n += 1
    print("batch-first transposes:", n)
    return m


def main():
    OUT.mkdir(exist_ok=True)
    for part in ("encoder", "decoder", "joiner"):
        src = glob.glob(str(SRC / f"{part}-*-left-64.onnx"))[0]
        ins, outs, ints, feeds = probe(src, lambda f: None)
        m = freeze(src, OUT / f"{part}.onnx", ins, outs, ints)
        if part == "encoder" and "--no-rewrite" not in sys.argv:
            print("conv-cache slices rewritten:", rewrite(m))
        if part == "encoder" and "--cold" not in sys.argv:
            m = warm(m)
            del feeds["processed_lens"]
        if part == "encoder" and "--no-batch-first" not in sys.argv:
            m = batch_first(m)
            feeds = {(k + "_b1" if v.ndim == 3 and v.shape[0] != 1 and v.shape[1] == 1 else k):
                     (v.transpose(1, 0, 2) if v.ndim == 3 and v.shape[0] != 1 and v.shape[1] == 1 else v)
                     for k, v in feeds.items()}
        onnx.checker.check_model(m)
        onnx.save(m, str(OUT / f"{part}.onnx"))
        d = DS / part; d.mkdir(parents=True, exist_ok=True)
        for k, v in feeds.items(): np.save(d / f"{k}.npy", v)
        print(part, "in", ins if part != "encoder" else f"{len(ins)} inputs, x {ins['x']}", "out",
              outs if part != "encoder" else f"encoder_out {outs['encoder_out']}")
        if part == "encoder":
            (OUT / "encoder_io.txt").write_text("\n".join(f"in {k} {list(v)}" for k, v in ins.items()) + "\n" +
                                                "\n".join(f"out {k} {list(v)}" for k, v in outs.items()) + "\n")

main()
