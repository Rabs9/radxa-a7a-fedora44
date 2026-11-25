#!/usr/bin/python3
"""image-limit-check.py [--layers N] [--layer L]: the device check for the held-back patches 7-8 of
build-pvr-vulkan.sh (maxImageArrayLayers and maxImageDimension3D 2048, upstream Mesa af80126eed / e4b29dedd8; held back
on 2026-09-10 until pco's 32-bit array-layer offsets and zink's >256-layer clears are dealt with, see the script).

Three GL checks on layer (slice) L of an image with N layers (default N = 2048, L = N - 1); the sampling checks also
read layers 256, 1024 and N - 2, so a sampler clamping to the last layer cannot pass. Every layer holds
(l & 255, l >> 8, 90, 255), so a wrong layer changes the pixel, and a layer index cut to 8 bits (the old 256 limit)
lands on another value; the comparison is exact (a tolerance of one would pass layer 2046 for 2047):
  array   sample layer L of a 1x1xN GL_TEXTURE_2D_ARRAY (GLSL 1.20 + GL_EXT_texture_array, texture2DArray)
  3d      sample slice L of a 1x1xN GL_TEXTURE_3D (texture3D at z = (L + 0.5) / N, nearest)
  render  draw into layer L of a zero-filled 4x4xN array texture (glFramebufferTextureLayer) and read it back; the
          layer L mod 256 must still be zero
EGL surfaceless, a desktop-GL context. One JSON line last. Exit 0 PASS, 1 FAIL, 3 unavailable (no context, or
GL_MAX_ARRAY_TEXTURE_LAYERS / GL_MAX_3D_TEXTURE_SIZE below N: the open stack reports 256 before patches 7-8).
docs/GPU-GAP-2026-09-10.md, step 4.
"""
import argparse
import ctypes as C
import json
import sys

EGL_NONE, EGL_PLATFORM_SURFACELESS_MESA, EGL_OPENGL_API = 0x3038, 0x31DD, 0x30A2
GL_VERSION, GL_RENDERER, GL_EXTENSIONS = 0x1F02, 0x1F01, 0x1F03
GL_FRAGMENT_SHADER, GL_VERTEX_SHADER, GL_COMPILE_STATUS, GL_LINK_STATUS = 0x8B30, 0x8B31, 0x8B81, 0x8B82
GL_TEXTURE_2D, GL_TEXTURE_2D_ARRAY, GL_TEXTURE_3D = 0x0DE1, 0x8C1A, 0x806F
GL_RGBA8, GL_RGBA, GL_UNSIGNED_BYTE, GL_FLOAT = 0x8058, 0x1908, 0x1401, 0x1406
GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_NEAREST, GL_TEXTURE0 = 0x2801, 0x2800, 0x2600, 0x84C0
GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FRAMEBUFFER_COMPLETE = 0x8D40, 0x8CE0, 0x8CD5
GL_COLOR_BUFFER_BIT, GL_TRIANGLES = 0x4000, 0x0004
GL_MAX_ARRAY_TEXTURE_LAYERS, GL_MAX_3D_TEXTURE_SIZE = 0x88FF, 0x8073


def emit(res, rc):
    print(json.dumps(res))
    sys.stdout.flush()
    return rc


def value(layer):
    return [layer & 255, (layer >> 8) & 255, 90, 255]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--layers", type=int, default=2048, help="layers of each image (default 2048)")
    ap.add_argument("--layer", type=int, help="the layer to check (default the last)")
    a = ap.parse_args()
    n = a.layers
    layer = n - 1 if a.layer is None else a.layer
    res = {"layers": n, "layer": layer, "checks": {}}
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

    u, i32, f32, ptr = C.c_uint, C.c_int, C.c_float, C.c_void_p
    GetString = gl("glGetString", C.c_char_p, u)
    GetIntegerv = gl("glGetIntegerv", None, u, C.POINTER(i32))
    GetError = gl("glGetError", u)
    res.update(renderer=(GetString(GL_RENDERER) or b"").decode(), version=(GetString(GL_VERSION) or b"").decode())
    lim = {}
    # GL_MAX_FRAMEBUFFER_LAYERS: Mesa derives it from the array-layer limit (st_extensions.c:580) while the Vulkan
    # maxFramebufferLayers may stay lower; recorded so a report can see the mismatch
    for name, enum in (("GL_MAX_ARRAY_TEXTURE_LAYERS", GL_MAX_ARRAY_TEXTURE_LAYERS), ("GL_MAX_3D_TEXTURE_SIZE", GL_MAX_3D_TEXTURE_SIZE),
                       ("GL_MAX_FRAMEBUFFER_LAYERS", 0x9317)):
        v = (i32 * 1)()
        GetIntegerv(enum, v)
        lim[name] = None if GetError() else v[0]
    res["limits"] = lim
    if "GL_EXT_texture_array" not in set((GetString(GL_EXTENSIONS) or b"").decode().split()):
        return emit(dict(res, result="unavailable", reason="no GL_EXT_texture_array"), 3)
    if (lim["GL_MAX_ARRAY_TEXTURE_LAYERS"] or 0) < n or (lim["GL_MAX_3D_TEXTURE_SIZE"] or 0) < n:
        return emit(dict(res, result="unavailable", reason="GL limits below %d layers: %s" % (n, lim)), 3)
    if not 0 <= layer < n:
        return emit(dict(res, result="FAIL", reason="layer %d outside 0..%d" % (layer, n - 1)), 1)
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
        GetUniformLocation = gl("glGetUniformLocation", i32, u, C.c_char_p)
        Uniform1i = gl("glUniform1i", None, i32, i32)
        Uniform1f = gl("glUniform1f", None, i32, f32)
        Uniform4f = gl("glUniform4f", None, i32, f32, f32, f32, f32)
        ActiveTexture = gl("glActiveTexture", None, u)
        GenTextures = gl("glGenTextures", None, i32, C.POINTER(u))
        BindTexture = gl("glBindTexture", None, u, u)
        TexImage2D = gl("glTexImage2D", None, u, i32, i32, i32, i32, i32, u, u, ptr)
        TexImage3D = gl("glTexImage3D", None, u, i32, i32, i32, i32, i32, i32, u, u, ptr)
        TexParameteri = gl("glTexParameteri", None, u, u, i32)
        GenFramebuffers = gl("glGenFramebuffers", None, i32, C.POINTER(u))
        BindFramebuffer = gl("glBindFramebuffer", None, u, u)
        FramebufferTexture2D = gl("glFramebufferTexture2D", None, u, u, u, u, i32)
        FramebufferTextureLayer = gl("glFramebufferTextureLayer", None, u, u, u, i32, i32)
        CheckFramebufferStatus = gl("glCheckFramebufferStatus", u, u)
        Viewport = gl("glViewport", None, i32, i32, i32, i32)
        ClearColor = gl("glClearColor", None, f32, f32, f32, f32)
        Clear = gl("glClear", None, u)
        EnableVertexAttribArray = gl("glEnableVertexAttribArray", None, u)
        VertexAttribPointer = gl("glVertexAttribPointer", None, u, i32, u, C.c_ubyte, i32, ptr)
        DrawArrays = gl("glDrawArrays", None, u, i32, i32)
        Finish = gl("glFinish", None)
        ReadPixels = gl("glReadPixels", None, i32, i32, i32, i32, u, u, ptr)
    except RuntimeError as e:
        return emit(dict(res, result="FAIL", reason=str(e)), 1)

    def program(fs):
        p = CreateProgram()
        for kind, src in ((GL_VERTEX_SHADER, "#version 120\nattribute vec2 pos;\n"
                                             "void main() { gl_Position = vec4(pos, 0.0, 1.0); }\n"), (GL_FRAGMENT_SHADER, fs)):
            s = CreateShader(kind)
            ShaderSource(s, 1, (C.c_char_p * 1)(src.encode()), None)
            CompileShader(s)
            ok = (i32 * 1)()
            GetShaderiv(s, GL_COMPILE_STATUS, ok)
            if not ok[0]:
                log = C.create_string_buffer(4096)
                GetShaderInfoLog(s, 4096, None, log)
                raise RuntimeError("compile: %s" % log.value.decode(errors="replace").strip())
            AttachShader(p, s)
        BindAttribLocation(p, 0, b"pos")
        LinkProgram(p)
        ok = (i32 * 1)()
        GetProgramiv(p, GL_LINK_STATUS, ok)
        if not ok[0]:
            log = C.create_string_buffer(4096)
            GetProgramInfoLog(p, 4096, None, log)
            raise RuntimeError("link: %s" % log.value.decode(errors="replace").strip())
        UseProgram(p)
        return p

    def texture(target, w, h, depth, data):
        t = (u * 1)()
        GenTextures(1, t)
        BindTexture(target, t[0])
        TexParameteri(target, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        TexParameteri(target, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        if target == GL_TEXTURE_2D:
            TexImage2D(target, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
        else:
            TexImage3D(target, 0, GL_RGBA8, w, h, depth, 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
        return t[0]

    def attach(fbo, tex, lay=None):
        BindFramebuffer(GL_FRAMEBUFFER, fbo)
        if lay is None:
            FramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
        else:
            FramebufferTextureLayer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, tex, 0, lay)
        if CheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE:
            raise RuntimeError("framebuffer incomplete (layer %s)" % lay)

    def read():
        px = (C.c_ubyte * 4)()
        ReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, C.cast(px, ptr))
        return list(px)

    tri = (C.c_float * 6)(-1.0, -1.0, 3.0, -1.0, -1.0, 3.0)
    EnableVertexAttribArray(0)
    VertexAttribPointer(0, 2, GL_FLOAT, 0, 0, C.cast(tri, ptr))
    fb = (u * 1)()
    GenFramebuffers(1, fb)
    out = texture(GL_TEXTURE_2D, 1, 1, 1, None)                    # the 1x1 target of the sampling checks
    layers_data = (C.c_ubyte * (4 * n))(*[x for lv in range(n) for x in value(lv)])
    expected = value(layer)

    def check(name, fn):
        c = {"layer": layer, "expected": expected}
        try:
            c.update(fn())
            ok = not c.get("gl_error") and c["pixel"] == expected \
                and c.get("alias_pixel", [0, 0, 0, 0]) == [0, 0, 0, 0] \
                and all(v == value(int(lv)) for lv, v in c.get("others", {}).items())
            c["result"] = "PASS" if ok else "FAIL"
        except RuntimeError as e:
            c.update(result="FAIL", reason=str(e))
        res["checks"][name] = c

    checked = sorted({lv for lv in (256, 1024, n - 2, n - 1, layer) if 0 <= lv < n})

    def sample(target, fs, uname, coord):
        tex = texture(target, 1, 1, n, C.cast(layers_data, ptr))
        attach(fb[0], out)
        Viewport(0, 0, 1, 1)
        p = program(fs)
        ActiveTexture(GL_TEXTURE0)
        BindTexture(target, tex)
        Uniform1i(GetUniformLocation(p, b"t"), 0)
        got = {}
        for lv in checked:
            Uniform1f(GetUniformLocation(p, uname), coord(lv))
            ClearColor(0.0, 0.0, 0.0, 0.0)
            Clear(GL_COLOR_BUFFER_BIT)
            DrawArrays(GL_TRIANGLES, 0, 3)
            Finish()
            got[lv] = read()
        return {"pixel": got[layer], "gl_error": GetError(), "layers_checked": checked,
                "others": {str(lv): px for lv, px in got.items() if lv != layer}}

    check("array", lambda: sample(GL_TEXTURE_2D_ARRAY,
                                  "#version 120\n#extension GL_EXT_texture_array : require\nuniform sampler2DArray t;\n"
                                  "uniform float layer;\nvoid main() { gl_FragColor = texture2DArray(t, vec3(0.5, 0.5, layer)); }\n",
                                  b"layer", float))
    check("3d", lambda: dict(sample(GL_TEXTURE_3D,
                                    "#version 120\nuniform sampler3D t;\nuniform float z;\n"
                                    "void main() { gl_FragColor = texture3D(t, vec3(0.5, 0.5, z)); }\n",
                                    b"z", lambda lv: (lv + 0.5) / n), depth=n))

    def render():
        zeros = (C.c_ubyte * (4 * 4 * 4 * n))()
        tex = texture(GL_TEXTURE_2D_ARRAY, 4, 4, n, C.cast(zeros, ptr))
        attach(fb[0], tex, layer)
        Viewport(0, 0, 4, 4)
        p = program("#version 120\nuniform vec4 c;\nvoid main() { gl_FragColor = c; }\n")
        Uniform4f(GetUniformLocation(p, b"c"), *[x / 255.0 for x in expected])
        DrawArrays(GL_TRIANGLES, 0, 3)
        Finish()
        r = {"pixel": read(), "gl_error": GetError()}
        alias = layer % 256
        if alias != layer:                                        # a layer index cut to 8 bits would have hit this one
            attach(fb[0], tex, alias)
            r.update(alias_layer=alias, alias_pixel=read())
        return r

    check("render", render)
    res["result"] = "PASS" if all(c["result"] == "PASS" for c in res["checks"].values()) else "FAIL"
    return emit(res, 0 if res["result"] == "PASS" else 1)


if __name__ == "__main__":
    sys.exit(main())
