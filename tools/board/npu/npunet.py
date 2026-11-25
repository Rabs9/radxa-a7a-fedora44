"""VIPLite 2.0 (the A733's VIP9000 NPU) from Python through ctypes: Net(path).run([float arrays]) -> [float32 arrays].

No compiled part: the image's own /opt/pvr/lib/libNBGlinker.so (the VIPLite user library) is called directly, and
numpy converts to and from each tensor's format (FP32, FP16, UINT8/INT8/INT16 asymmetric or dynamic fixed point,
INT32, INT64). The layout of vip_buffer_create_params_t and the enum values are VIPLite 2.0's vip_lite.h (the SDK's
viplite-tina/lib/glibc-gcc13_2_0/v2.0/inc, the build whose library the image carries)."""
import ctypes
import os

import numpy as np

LIB = os.environ.get("NPUNET_VIPLITE", "/opt/pvr/lib/libNBGlinker.so")
SUCCESS = 0
FROM_FILE = 0x01                                         # VIP_CREATE_NETWORK_FROM_FILE
INPUT_COUNT, OUTPUT_COUNT, PROFILING, SET_TIME_OUT = 1, 2, 7, 68   # VIP_NETWORK_PROP_*
QUANT_FORMAT, NUM_OF_DIMENSION, SIZES_OF_DIMENSION, DATA_FORMAT, FIXED_POINT_POS, TF_SCALE, TF_ZERO_POINT, NAME = range(8)
FLUSH, INVALIDATE = 1, 2                                 # VIP_BUFFER_OPER_TYPE_*
DFP, ASYMM = 1, 2                                        # VIP_BUFFER_QUANTIZE_*
FORMATS = {0: np.float32, 1: np.float16, 2: np.uint8, 3: np.int8, 4: np.uint16, 5: np.int16, 8: np.int32,
           9: np.uint32, 10: np.int64, 11: np.uint64, 12: np.float64}


class _Affine(ctypes.Structure):
    _fields_ = [("scale", ctypes.c_float), ("zero_point", ctypes.c_int32)]


class _Quant(ctypes.Union):
    _fields_ = [("fixed_point_pos", ctypes.c_int32), ("affine", _Affine)]


class Params(ctypes.Structure):                          # vip_buffer_create_params_t
    _fields_ = [("num_of_dims", ctypes.c_uint32), ("sizes", ctypes.c_uint32 * 6), ("data_format", ctypes.c_int32),
                ("quant_format", ctypes.c_int32), ("quant_data", _Quant), ("memory_type", ctypes.c_uint32)]


class Profile(ctypes.Structure):                         # vip_inference_profile_t
    _fields_ = [("inference_time", ctypes.c_uint32), ("total_cycle", ctypes.c_uint32)]


_lib = None


def lib():
    global _lib
    if _lib is None:
        # libNBGlinker needs libVIPhal from its own directory, which is not on the loader's path: loaded first
        ctypes.CDLL(os.path.join(os.path.dirname(LIB), "libVIPhal.so"), mode=ctypes.RTLD_GLOBAL)
        L = ctypes.CDLL(LIB)
        P, V, U = ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint32
        L.vip_init.argtypes = []
        L.vip_create_network.argtypes = [ctypes.c_char_p, U, ctypes.c_int, V]
        L.vip_query_network.argtypes = [P, ctypes.c_int, P]
        L.vip_query_input.argtypes = [P, U, ctypes.c_int, P]
        L.vip_query_output.argtypes = [P, U, ctypes.c_int, P]
        L.vip_create_buffer.argtypes = [ctypes.POINTER(Params), U, V]
        L.vip_prepare_network.argtypes = [P]
        L.vip_set_input.argtypes = [P, U, P]
        L.vip_set_output.argtypes = [P, U, P]
        L.vip_run_network.argtypes = [P]
        L.vip_map_buffer.argtypes = [P]
        L.vip_map_buffer.restype = P
        L.vip_unmap_buffer.argtypes = [P]
        L.vip_flush_buffer.argtypes = [P, ctypes.c_int]
        L.vip_finish_network.argtypes = [P]
        L.vip_destroy_buffer.argtypes = [P]
        L.vip_destroy_network.argtypes = [P]
        for f in ("vip_init", "vip_create_network", "vip_query_network", "vip_query_input", "vip_query_output",
                  "vip_create_buffer", "vip_prepare_network", "vip_set_input", "vip_set_output", "vip_run_network",
                  "vip_unmap_buffer", "vip_flush_buffer", "vip_finish_network", "vip_destroy_buffer",
                  "vip_destroy_network"):
            getattr(L, f).restype = ctypes.c_int
        if L.vip_init() != SUCCESS:
            raise OSError("vip_init failed (is /dev/vipcore there?)")
        _lib = L
    return _lib


class Tensor:
    """One input or output: its VIP buffer and how its values are stored."""

    def __init__(self, net, index, out):
        L = lib()
        q = L.vip_query_output if out else L.vip_query_input
        p = Params()
        self.buf = ctypes.c_void_p()
        for prop, field in ((DATA_FORMAT, Params.data_format), (NUM_OF_DIMENSION, Params.num_of_dims),
                            (SIZES_OF_DIMENSION, Params.sizes), (QUANT_FORMAT, Params.quant_format)):
            if q(net, index, prop, ctypes.byref(p, field.offset)) != SUCCESS:
                raise OSError(f"{'output' if out else 'input'} {index}: its property {prop} could not be read")
        if not 0 < p.num_of_dims <= 6:
            raise OSError(f"{'output' if out else 'input'} {index}: {p.num_of_dims} dimensions")
        name = ctypes.create_string_buffer(256)
        q(net, index, NAME, name)
        if p.quant_format == DFP:
            q(net, index, FIXED_POINT_POS, ctypes.byref(p.quant_data, 0))
        elif p.quant_format == ASYMM:
            q(net, index, TF_SCALE, ctypes.byref(p.quant_data.affine, _Affine.scale.offset))
            q(net, index, TF_ZERO_POINT, ctypes.byref(p.quant_data.affine, _Affine.zero_point.offset))
        if p.data_format not in FORMATS:
            raise OSError(f"{'output' if out else 'input'} {index}: data format {p.data_format} not handled")
        self.name = name.value.decode(errors="replace")
        self.params, self.dtype = p, FORMATS[p.data_format]
        self.count = int(np.prod([p.sizes[d] for d in range(p.num_of_dims)]))
        if L.vip_create_buffer(ctypes.byref(p), ctypes.sizeof(p), ctypes.byref(self.buf)) != SUCCESS:
            raise OSError(f"vip_create_buffer failed for {self.name}")

    def encode(self, x):
        p = self.params
        if p.quant_format == ASYMM:
            x = np.rint(x / p.quant_data.affine.scale) + p.quant_data.affine.zero_point
        elif p.quant_format == DFP:
            x = np.rint(np.ldexp(x, p.quant_data.fixed_point_pos))
        elif np.issubdtype(self.dtype, np.integer):
            x = np.rint(x)
        if np.issubdtype(self.dtype, np.integer):
            i = np.iinfo(self.dtype)
            x = np.clip(x, i.min, i.max)
        return x.astype(self.dtype)

    def decode(self, raw):
        p = self.params
        x = raw.astype(np.float32)
        if p.quant_format == ASYMM:
            x = (x - p.quant_data.affine.zero_point) * p.quant_data.affine.scale
        elif p.quant_format == DFP:
            x = np.ldexp(x, -p.quant_data.fixed_point_pos)
        return x

    def view(self):
        """The buffer's memory as a numpy array (valid until unmapped)."""
        addr = lib().vip_map_buffer(self.buf)
        if not addr:
            raise OSError(f"vip_map_buffer failed for {self.name}")
        size = self.count * np.dtype(self.dtype).itemsize
        return np.frombuffer((ctypes.c_char * size).from_address(addr), self.dtype)


class Net:
    TIME_OUT_MS = 2000                                   # a run that takes longer fails instead of hanging the caller

    def __init__(self, path):
        L = lib()
        self.inputs, self.outputs = [], []
        self.net = ctypes.c_void_p()
        if L.vip_create_network(str(path).encode(), 0, FROM_FILE, ctypes.byref(self.net)) != SUCCESS:
            self.net = None
            raise OSError(f"could not load the network {path}")
        try:
            self._setup(path)
        except BaseException:
            self.close()                                 # a half-made network and its buffers go back to the NPU
            raise

    def _setup(self, path):
        L = lib()
        t = ctypes.c_uint32(self.TIME_OUT_MS)
        L.vip_set_network.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        L.vip_set_network.restype = ctypes.c_int
        L.vip_set_network(self.net, SET_TIME_OUT, ctypes.byref(t))
        n = ctypes.c_uint32()
        L.vip_query_network(self.net, INPUT_COUNT, ctypes.byref(n))
        for i in range(n.value):
            self.inputs.append(Tensor(self.net, i, False))
        if L.vip_prepare_network(self.net) != SUCCESS:
            raise OSError(f"vip_prepare_network failed for {path}")
        L.vip_query_network(self.net, OUTPUT_COUNT, ctypes.byref(n))
        for i in range(n.value):
            self.outputs.append(Tensor(self.net, i, True))
        for i, t in enumerate(self.outputs):
            if L.vip_set_output(self.net, i, t.buf) != SUCCESS:
                raise OSError(f"vip_set_output failed for {t.name}")
        for i, t in enumerate(self.inputs):
            if L.vip_set_input(self.net, i, t.buf) != SUCCESS:
                raise OSError(f"vip_set_input failed for {t.name}")

    def run(self, arrays):
        L = lib()
        if len(arrays) != len(self.inputs):
            raise ValueError(f"{len(arrays)} inputs, the network takes {len(self.inputs)}")
        for a, t in zip(arrays, self.inputs):
            a = np.asarray(a, np.float32).reshape(-1)
            if a.size != t.count:
                raise ValueError(f"input {t.name}: {a.size} values, the network takes {t.count}")
            t.view()[:] = t.encode(a)
            L.vip_flush_buffer(t.buf, FLUSH)
            L.vip_unmap_buffer(t.buf)
        if L.vip_run_network(self.net) != SUCCESS:
            raise OSError("vip_run_network failed")
        res = []
        for t in self.outputs:
            L.vip_flush_buffer(t.buf, INVALIDATE)
            res.append(t.decode(t.view()))                # decode copies, so the buffer can go
            L.vip_unmap_buffer(t.buf)
        return res

    def npu_us(self):
        """The last run's time on the NPU, microseconds (the driver's profile)."""
        p = Profile()
        lib().vip_query_network(self.net, PROFILING, ctypes.byref(p))
        return p.inference_time

    def close(self):
        if self.net:
            L = lib()
            L.vip_finish_network(self.net)
            for t in self.inputs + self.outputs:
                if t.buf:
                    L.vip_destroy_buffer(t.buf)
            L.vip_destroy_network(self.net)
            self.net = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
