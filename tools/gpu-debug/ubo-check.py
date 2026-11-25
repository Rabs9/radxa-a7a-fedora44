#!/usr/bin/python3
"""ubo-check.py [--blocks N]: the device check of build-pvr-vulkan.sh patch 6 (13 Vulkan uniform buffers per stage,
so that Mesa's GL state tracker exposes GL_ARB_uniform_buffer_object with 12 blocks per stage).

A GL program reads N (default 12) uniform blocks, first in the fragment stage, then in the vertex stage, draws a
triangle covering a 16x16 texture and reads one pixel back. Block i holds ((i+1)/255, 200/255, 100/255, 0) and the
colour is (sum of every block's .x, the last block's .y, the first block's .z + a plain uniform k = 50/255, 1), so any
block that reads as zero or as another block's data changes the pixel: 12 blocks = (78, 200, 150, 255), 11 blocks =
(66, 200, 150, 255). k matters: zink keeps GL's default uniform block in a buffer of its own, which is the 13th
Vulkan uniform buffer of the stage. The comparison is exact (every value is an exact 8-bit unorm; a tolerance of one
would pass block 11 reading block 10's data). A reported limit is a claim; this is the driver reading all 13.

EGL surfaceless, a desktop-GL context, GLSL 1.20 + GL_ARB_uniform_buffer_object (the open stack's GL stays 2.1
compatibility until transform feedback exists; the extension is what patch 6 turns on). One JSON line last.
Exit 0 PASS, 1 FAIL, 3 unavailable (no context, or no uniform buffers in it: the open stack before patch 6).
docs/GPU-GAP-2026-09-10.md, step 4.
"""
import argparse
import ctypes as C
import json
import sys

EGL_NONE, EGL_PLATFORM_SURFACELESS_MESA, EGL_OPENGL_API = 0x3038, 0x31DD, 0x30A2
GL_VENDOR, GL_RENDERER, GL_VERSION, GL_EXTENSIONS, GL_NUM_EXTENSIONS = 0x1F00, 0x1F01, 0x1F02, 0x1F03, 0x821D
GL_FRAGMENT_SHADER, GL_VERTEX_SHADER, GL_COMPILE_STATUS, GL_LINK_STATUS = 0x8B30, 0x8B31, 0x8B81, 0x8B82
GL_UNIFORM_BUFFER, GL_STATIC_DRAW, GL_INVALID_INDEX = 0x8A11, 0x88E4, 0xFFFFFFFF
GL_TEXTURE_2D, GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE, GL_FLOAT = 0x0DE1, 0x8058, 0x1908, 0x1401, 0x1406
GL_TEXTURE_MIN_FILTER, GL_NEAREST = 0x2801, 0x2600
GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FRAMEBUFFER_COMPLETE = 0x8D40, 0x8CE0, 0x8CD5
GL_COLOR_BUFFER_BIT, GL_TRIANGLES = 0x4000, 0x0004
LIMITS = {"GL_MAX_VERTEX_UNIFORM_BLOCKS": 0x8A2B, "GL_MAX_FRAGMENT_UNIFORM_BLOCKS": 0x8A2D,
          "GL_MAX_UNIFORM_BUFFER_BINDINGS": 0x8A2F, "GL_MAX_UNIFORM_BLOCK_SIZE": 0x8A30}
HEAD = "#version 120\n#extension GL_ARB_uniform_buffer_object : require\n"


def emit(res, rc):
    print(json.dumps(res))
    sys.stdout.flush()
    return rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--blocks", type=int, default=12, help="uniform blocks per stage (default 12)")
    n = ap.parse_args().blocks
    res = {"blocks": n, "stages": {}}
    try:
        egl = C.CDLL("libEGL.so.1")
    except OSError as e:
        return emit(dict(res, result="unavailable", reason="libEGL.so.1: %s" % e), 3)
    vp = C.c_void_p
    egl.eglGetProcAddress.restype, egl.eglGetProcAddress.argtypes = vp, [C.c_char_p]
    egl.eglInitialize.restype, egl.eglInitialize.argtypes = C.c_uint, [vp, C.POINTER(C.c_int), C.POINTER(C.c_int)]
    egl.eglBindAPI.restype, egl.eglBindAPI.argtypes = C.c_uint, [C.c_uint]
    egl.eglCreateContext.restype, egl.eglCreateContext.argtypes = vp, [vp, vp, vp, C.POINTER(C.c_int)]
    egl.eglMakeCurrent.restype, egl.eglMakeCurrent.argtypes = C.c_uint, [vp, vp, vp, vp]
    egl.eglGetError.restype = C.c_int
    addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT")
    if not addr:
        return emit(dict(res, result="unavailable", reason="no eglGetPlatformDisplayEXT"), 3)
    dpy = C.CFUNCTYPE(vp, C.c_uint, vp, vp)(addr)(EGL_PLATFORM_SURFACELESS_MESA, None, None)
    ma, mi = C.c_int(), C.c_int()
    if not dpy or not egl.eglInitialize(dpy, C.byref(ma), C.byref(mi)) or not egl.eglBindAPI(EGL_OPENGL_API):
        return emit(dict(res, result="unavailable", reason="no surfaceless desktop-GL display (0x%x)" % egl.eglGetError()), 3)
    ctx = egl.eglCreateContext(dpy, None, None, (C.c_int * 1)(EGL_NONE))
    if not ctx or not egl.eglMakeCurrent(dpy, None, None, ctx):
        return emit(dict(res, result="unavailable", reason="no desktop-GL context (0x%x)" % egl.eglGetError()), 3)

    def gl(name, restype, *argtypes):
        p = egl.eglGetProcAddress(name.encode())
        if not p:
            raise RuntimeError("no %s" % name)
        return C.CFUNCTYPE(restype, *argtypes)(p)

    u, i32, ptr = C.c_uint, C.c_int, C.c_void_p
    GetString = gl("glGetString", C.c_char_p, u)
    GetIntegerv = gl("glGetIntegerv", None, u, C.POINTER(i32))
    GetError = gl("glGetError", u)
    res.update(renderer=(GetString(GL_RENDERER) or b"").decode(), version=(GetString(GL_VERSION) or b"").decode())
    exts = set((GetString(GL_EXTENSIONS) or b"").decode().split())
    if "GL_ARB_uniform_buffer_object" not in exts:
        return emit(dict(res, result="unavailable",
                         reason="no GL_ARB_uniform_buffer_object in this %s context" % res["version"]), 3)
    lim = {}
    for name, enum in LIMITS.items():
        v = (i32 * 1)()
        GetIntegerv(enum, v)
        lim[name] = None if GetError() else v[0]
    res["limits"] = lim
    try:
        CreateShader = gl("glCreateShader", u, u)
        ShaderSource = gl("glShaderSource", None, u, i32, C.POINTER(C.c_char_p), ptr)
        CompileShader = gl("glCompileShader", None, u)
        GetShaderiv = gl("glGetShaderiv", None, u, u, C.POINTER(i32))
        GetShaderInfoLog = gl("glGetShaderInfoLog", None, u, i32, C.POINTER(i32), C.c_char_p)
        CreateProgram = gl("glCreateProgram", u)
        AttachShader = gl("glAttachShader", None, u, u)
        BindAttribLocation = gl("glBindAttribLocation", None, u, u, C.c_char_p)
        LinkProgram = gl("glLinkProgram", None, u)
        GetProgramiv = gl("glGetProgramiv", None, u, u, C.POINTER(i32))
        GetProgramInfoLog = gl("glGetProgramInfoLog", None, u, i32, C.POINTER(i32), C.c_char_p)
        UseProgram = gl("glUseProgram", None, u)
        GetUniformBlockIndex = gl("glGetUniformBlockIndex", u, u, C.c_char_p)
        GetUniformLocation = gl("glGetUniformLocation", i32, u, C.c_char_p)
        Uniform1f = gl("glUniform1f", None, i32, C.c_float)
        UniformBlockBinding = gl("glUniformBlockBinding", None, u, u, u)
        GenBuffers = gl("glGenBuffers", None, i32, C.POINTER(u))
        BindBuffer = gl("glBindBuffer", None, u, u)
        BufferData = gl("glBufferData", None, u, C.c_ssize_t, ptr, u)
        BindBufferBase = gl("glBindBufferBase", None, u, u, u)
        GenTextures = gl("glGenTextures", None, i32, C.POINTER(u))
        BindTexture = gl("glBindTexture", None, u, u)
        TexImage2D = gl("glTexImage2D", None, u, i32, i32, i32, i32, i32, u, u, ptr)
        TexParameteri = gl("glTexParameteri", None, u, u, i32)
        GenFramebuffers = gl("glGenFramebuffers", None, i32, C.POINTER(u))
        BindFramebuffer = gl("glBindFramebuffer", None, u, u)
        FramebufferTexture2D = gl("glFramebufferTexture2D", None, u, u, u, u, i32)
        CheckFramebufferStatus = gl("glCheckFramebufferStatus", u, u)
        Viewport = gl("glViewport", None, i32, i32, i32, i32)
        ClearColor = gl("glClearColor", None, C.c_float, C.c_float, C.c_float, C.c_float)
        Clear = gl("glClear", None, u)
        EnableVertexAttribArray = gl("glEnableVertexAttribArray", None, u)
        VertexAttribPointer = gl("glVertexAttribPointer", None, u, i32, u, C.c_ubyte, i32, ptr)
        DrawArrays = gl("glDrawArrays", None, u, i32, i32)
        Finish = gl("glFinish", None)
        ReadPixels = gl("glReadPixels", None, i32, i32, i32, i32, u, u, ptr)
    except RuntimeError as e:
        return emit(dict(res, result="FAIL", reason=str(e)), 1)

    def shader(kind, src):
        s = CreateShader(kind)
        ShaderSource(s, 1, (C.c_char_p * 1)(src.encode()), None)
        CompileShader(s)
        ok = (i32 * 1)()
        GetShaderiv(s, GL_COMPILE_STATUS, ok)
        if not ok[0]:
            log = C.create_string_buffer(4096)
            GetShaderInfoLog(s, 4096, None, log)
            raise RuntimeError("compile: %s" % log.value.decode(errors="replace").strip())
        return s

    def program(vs, fs):
        p = CreateProgram()
        AttachShader(p, shader(GL_VERTEX_SHADER, vs))
        AttachShader(p, shader(GL_FRAGMENT_SHADER, fs))
        BindAttribLocation(p, 0, b"pos")
        LinkProgram(p)
        ok = (i32 * 1)()
        GetProgramiv(p, GL_LINK_STATUS, ok)
        if not ok[0]:
            log = C.create_string_buffer(4096)
            GetProgramInfoLog(p, 4096, None, log)
            raise RuntimeError("link: %s" % log.value.decode(errors="replace").strip())
        return p

    # one 16-byte std140 buffer per block
    bufs = (u * n)()
    GenBuffers(n, bufs)
    datas = []
    for i in range(n):
        d = (C.c_float * 4)((i + 1) / 255.0, 200 / 255.0, 100 / 255.0, 0.0)
        datas.append(d)
        BindBuffer(GL_UNIFORM_BUFFER, bufs[i])
        BufferData(GL_UNIFORM_BUFFER, 16, C.cast(d, ptr), GL_STATIC_DRAW)
    # render target: a 16x16 RGBA8 texture
    tex, fbo = (u * 1)(), (u * 1)()
    GenTextures(1, tex)
    BindTexture(GL_TEXTURE_2D, tex[0])
    TexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 16, 16, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
    TexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    GenFramebuffers(1, fbo)
    BindFramebuffer(GL_FRAMEBUFFER, fbo[0])
    FramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex[0], 0)
    if CheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE:
        return emit(dict(res, result="FAIL", reason="framebuffer incomplete"), 1)
    Viewport(0, 0, 16, 16)
    tri = (C.c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)        # one triangle covering the viewport
    EnableVertexAttribArray(0)
    VertexAttribPointer(0, 2, GL_FLOAT, 0, 0, C.cast(tri, ptr))

    blocks = "".join("layout(std140) uniform B%d { vec4 v%d; };\n" % (i, i) for i in range(n))
    blocks += "uniform float k;\n"                              # GL's default block: zink's 13th uniform buffer
    colour = "vec4(%s, v%d.y, v0.z + k, 1.0)" % (" + ".join("v%d.x" % i for i in range(n)), n - 1)
    expected = [n * (n + 1) // 2, 200, 150, 255]
    sources = {
        "fragment": (HEAD + "attribute vec2 pos;\nvoid main() { gl_Position = vec4(pos, 0.0, 1.0); }\n",
                     HEAD + blocks + "void main() { gl_FragColor = %s; }\n" % colour),
        "vertex": (HEAD + blocks + "attribute vec2 pos;\nvarying vec4 c;\n"
                   "void main() { c = %s; gl_Position = vec4(pos, 0.0, 1.0); }\n" % colour,
                   HEAD + "varying vec4 c;\nvoid main() { gl_FragColor = c; }\n"),
    }
    for stage, (vs, fs) in sources.items():
        st = {"expected": expected}
        try:
            p = program(vs, fs)
            UseProgram(p)
            Uniform1f(GetUniformLocation(p, b"k"), 50 / 255.0)
            for i in range(n):
                idx = GetUniformBlockIndex(p, b"B%d" % i)
                if idx == GL_INVALID_INDEX:
                    raise RuntimeError("uniform block B%d is not active" % i)
                UniformBlockBinding(p, idx, i)
                BindBufferBase(GL_UNIFORM_BUFFER, i, bufs[i])
            ClearColor(0.0, 0.0, 0.0, 0.0)
            Clear(GL_COLOR_BUFFER_BIT)
            DrawArrays(GL_TRIANGLES, 0, 3)
            Finish()
            px = (C.c_ubyte * 4)()
            ReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, C.cast(px, ptr))
            st.update(pixel=list(px), gl_error=GetError())
            st["result"] = "PASS" if not st["gl_error"] and st["pixel"] == expected else "FAIL"
        except RuntimeError as e:
            st.update(result="FAIL", reason=str(e))
        res["stages"][stage] = st
    res["result"] = "PASS" if all(s["result"] == "PASS" for s in res["stages"].values()) else "FAIL"
    return emit(res, 0 if res["result"] == "PASS" else 1)


if __name__ == "__main__":
    sys.exit(main())
