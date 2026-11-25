// glamor-repro.c: replay Xwayland's glamor GBM path on the PowerVR DDK, one step at a time.
//   build (on the board, needs gcc mesa-libgbm-devel mesa-libEGL-devel mesa-libGLES-devel libdrm-devel):
//     gcc -O1 -w -o glamor-repro glamor-repro.c -lgbm -lEGL -lGLESv2
//   run (DDK libraries, no compositor, no display):
//     LD_LIBRARY_PATH=/opt/pvr/lib LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri ./glamor-repro /dev/dri/renderD128 bo 24 24
//   usage: glamor-repro <drm node> <variant>[+flag...] [width height]
//   variants: tex      plain glTexImage2D texture -> FBO -> clear           (glmark2-style, works)
//             bo       gbm_bo_create(RENDERING) -> EGL_NATIVE_PIXMAP_KHR image -> FBO -> clear   (Xwayland 24.1 path; FAULTS for small sizes)
//             bomod    gbm_bo_create_with_modifiers2(LINEAR) -> native pixmap image -> FBO -> clear
//             mods     allocate with every modifier the EGL display advertises (dmabuf-feedback style)
//             dmabuf   gbm_bo -> fd -> EGL_LINUX_DMA_BUF_EXT image -> FBO -> clear         (KWin import path)
//             draw     like bo, but a shader triangle instead of glClear                   (glamor_solid partial fills)
//             botex    like bo, then also sample the image as a texture into a plain FBO   (glamor composite)
//             sample   draw sampling the bo image into a plain FBO (read-only GPU access)  (HANGS: USC slots stuck)
//             upload   glTexSubImage2D into the bo image (the DDK refuses: GL error 0x502)
//   flags:    +dbg     enable KHR_debug synchronous output like glamor_setup_debug_output()
//             +vbo     (draw) vertices from a persistent coherent buffer-storage VBO like glamor_get_vbo_space()
//             +map     gbm_bo_map READ_WRITE + memset + unmap before GPU use      (heals the bo fault: VA layout shift)
//             +rmap    gbm_bo_map READ only + unmap before GPU use                (also heals it)
//             +scissor (bo) clear only a 5x5 sub-rectangle (forces a tile load)
//             +tqfirst a texture upload right after context creation (transfer context before the free lists: heals bo)
//             +warm    a plain-texture FBO clear + sampled draw before the variant (the sampled draw itself hangs)
// Every step prints a timestamp; glFinish is timed (a firmware lockup+reset shows as ~45-60 ms).
// Findings 2026-09-07: see docs/GPU-TQ-FAULT-2026-09-07.md.
#define EGL_EGLEXT_PROTOTYPES
#define GL_GLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <gbm.h>
#include <libdrm/drm_fourcc.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static double now_ms(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000.0 + t.tv_nsec / 1e6; }
static double t0;
#define STEP(...) do { printf("[%7.1f ms] ", now_ms() - t0); printf(__VA_ARGS__); printf("\n"); fflush(stdout); } while (0)
#define FAIL(...) do { STEP(__VA_ARGS__); printf("RESULT: FAIL\n"); exit(2); } while (0)

static const char *vs_src = "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }";
static const char *fs_src = "precision mediump float; void main(){ gl_FragColor = vec4(0.0, 1.0, 0.0, 1.0); }";
static const char *fs_tex = "precision mediump float; uniform sampler2D s; void main(){ gl_FragColor = texture2D(s, vec2(0.5, 0.5)); }";

static GLuint shader(GLenum type, const char *src) {
    GLuint s = glCreateShader(type); glShaderSource(s, 1, &src, NULL); glCompileShader(s);
    GLint ok = 0; glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) { char log[512]; glGetShaderInfoLog(s, sizeof log, NULL, log); FAIL("shader compile failed: %s", log); }
    return s;
}
static GLuint program(const char *fs) {
    GLuint p = glCreateProgram(); glAttachShader(p, shader(GL_VERTEX_SHADER, vs_src)); glAttachShader(p, shader(GL_FRAGMENT_SHADER, fs));
    glBindAttribLocation(p, 0, "p"); glLinkProgram(p);
    GLint ok = 0; glGetProgramiv(p, GL_LINK_STATUS, &ok); if (!ok) FAIL("program link failed");
    return p;
}
static void GL_APIENTRY debug_cb(GLenum src, GLenum type, GLuint id, GLenum sev, GLsizei len, const GLchar *msg, const void *user) {
    (void)src; (void)type; (void)id; (void)sev; (void)len; (void)user; STEP("  GL debug: %s", msg);
}
static void enable_debug_output(void) {   // what glamor_setup_debug_output() does with GL_KHR_debug
    PFNGLDEBUGMESSAGECONTROLKHRPROC ctl = (void *)eglGetProcAddress("glDebugMessageControlKHR");
    PFNGLDEBUGMESSAGECALLBACKKHRPROC cb = (void *)eglGetProcAddress("glDebugMessageCallbackKHR");
    if (!ctl || !cb) { STEP("KHR_debug entry points missing"); return; }
    ctl(GL_DONT_CARE, GL_DONT_CARE, GL_DONT_CARE, 0, NULL, GL_FALSE);
    ctl(GL_DEBUG_SOURCE_API_KHR, GL_DEBUG_TYPE_ERROR_KHR, GL_DONT_CARE, 0, NULL, GL_TRUE);
    ctl(GL_DEBUG_SOURCE_SHADER_COMPILER_KHR, GL_DEBUG_TYPE_ERROR_KHR, GL_DONT_CARE, 0, NULL, GL_TRUE);
    cb(debug_cb, NULL);
    glEnable(GL_DEBUG_OUTPUT_KHR);
    glEnable(GL_DEBUG_OUTPUT_SYNCHRONOUS_KHR);
    STEP("KHR_debug output enabled (synchronous), err 0x%x", glGetError());
}
static void check_gl(const char *what) { GLenum e = glGetError(); if (e != GL_NO_ERROR) STEP("  GL error 0x%x after %s", e, what); }

static void finish_and_read(const char *what) {
    double a = now_ms(); glFinish(); double b = now_ms();
    STEP("glFinish after %s took %.1f ms", what, b - a);
    unsigned char px[4] = {0, 0, 0, 0};
    glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, px); check_gl("glReadPixels");
    STEP("pixel(0,0) = %d %d %d %d", px[0], px[1], px[2], px[3]);
    GLenum fb = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    if (fb != GL_FRAMEBUFFER_COMPLETE) STEP("  framebuffer status 0x%x", fb);
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <drm node> <tex|bo|bomod|mods|dmabuf|draw|botex|sample|upload>[+dbg|+vbo|+map|+rmap|+scissor|+tqfirst|+warm] [w h]\n", argv[0]); return 1; }
    const char *node = argv[1]; char variant[32]; strncpy(variant, argv[2], 31); variant[31] = 0;
    int want_debug = 0, want_vbo = 0, want_map = 0, want_up = 0; char *plus;
    while ((plus = strrchr(variant, '+'))) {
        if (!strcmp(plus, "+dbg")) want_debug |= 1; if (!strcmp(plus, "+vbo")) want_vbo = 1;
        if (!strcmp(plus, "+map")) want_map = 1; if (!strcmp(plus, "+up")) want_up = 1;
        if (!strcmp(plus, "+rmap")) want_map = 2; if (!strcmp(plus, "+scissor")) want_vbo = 2;
        if (!strcmp(plus, "+tqfirst")) want_debug |= 2; if (!strcmp(plus, "+warm")) want_debug |= 4; *plus = 0; }
    int w = argc > 3 ? atoi(argv[3]) : 1024, h = argc > 4 ? atoi(argv[4]) : 768;
    t0 = now_ms();
    alarm(20);   // a wedged call must not hang the harness

    int fd = open(node, O_RDWR | O_CLOEXEC);
    if (fd < 0) FAIL("open %s failed", node);
    struct gbm_device *gbm = gbm_create_device(fd);
    if (!gbm) FAIL("gbm_create_device failed");
    STEP("gbm device on %s, backend %s", node, gbm_device_get_backend_name(gbm));

    PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
    EGLDisplay dpy = get_platform_display ? get_platform_display(EGL_PLATFORM_GBM_KHR, gbm, NULL) : eglGetDisplay((EGLNativeDisplayType)gbm);
    if (dpy == EGL_NO_DISPLAY) FAIL("no EGL display");
    EGLint maj, min;
    if (!eglInitialize(dpy, &maj, &min)) FAIL("eglInitialize failed 0x%x", eglGetError());
    STEP("EGL %d.%d vendor '%s'", maj, min, eglQueryString(dpy, EGL_VENDOR));
    STEP("EGL extensions: %s", eglQueryString(dpy, EGL_EXTENSIONS));

    eglBindAPI(EGL_OPENGL_ES_API);
    // exactly what xwayland-glamor-gbm.c does for -glamor es: no config, GLES2 client version
    const EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    EGLContext ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) FAIL("eglCreateContext(EGL_NO_CONFIG_KHR) failed 0x%x", eglGetError());
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) FAIL("eglMakeCurrent(surfaceless) failed 0x%x", eglGetError());
    STEP("context current: %s / %s", glGetString(GL_RENDERER), glGetString(GL_VERSION));
    if (want_debug & 1) enable_debug_output();
    if (want_debug & 2) {   // force a transfer-queue job (texture upload) before anything else
        GLuint t; unsigned char *px = malloc(64 * 64 * 4); memset(px, 0x42, 64 * 64 * 4);
        glGenTextures(1, &t); glBindTexture(GL_TEXTURE_2D, t);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, px); glFinish();
        STEP("early texture upload done (TQ context created first)"); free(px);
    }

    if (want_debug & 4) {   // warm the render context: a plain-texture FBO clear + a draw sampling a plain texture
        GLuint wt, wf, st; glGenTextures(1, &wt); glBindTexture(GL_TEXTURE_2D, wt);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        glGenFramebuffers(1, &wf); glBindFramebuffer(GL_FRAMEBUFFER, wf);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, wt, 0); glViewport(0, 0, 64, 64);
        glClearColor(0, 1, 0, 1); glClear(GL_COLOR_BUFFER_BIT);
        unsigned char *px = malloc(16 * 16 * 4); memset(px, 0x80, 16 * 16 * 4);
        glGenTextures(1, &st); glBindTexture(GL_TEXTURE_2D, st); glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 16, 16, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST); glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        GLuint wp = program(fs_tex); glUseProgram(wp); glUniform1i(glGetUniformLocation(wp, "s"), 0);
        static const float wtri[] = { -1, -1, 3, -1, -1, 3 };
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, wtri); glEnableVertexAttribArray(0);
        glDrawArrays(GL_TRIANGLES, 0, 3); glFinish();
        unsigned char rb[4] = {0}; glReadPixels(0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, rb);
        STEP("warm-up render done, sampled plain texture -> %d %d %d %d", rb[0], rb[1], rb[2], rb[3]);
        glBindFramebuffer(GL_FRAMEBUFFER, 0); glBindTexture(GL_TEXTURE_2D, 0); free(px);
    }
    PFNEGLCREATEIMAGEKHRPROC create_image = (void *)eglGetProcAddress("eglCreateImageKHR");
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC image_target = (void *)eglGetProcAddress("glEGLImageTargetTexture2DOES");
    if (!create_image || !image_target) FAIL("missing eglCreateImageKHR / glEGLImageTargetTexture2DOES");

    GLuint tex = 0, fbo = 0;
    glGenTextures(1, &tex); glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);

    struct gbm_bo *bo = NULL; EGLImageKHR img = EGL_NO_IMAGE_KHR;
    if (!strcmp(variant, "tex")) {
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL); check_gl("glTexImage2D");
        STEP("plain texture %dx%d", w, h);
    } else {
        if (!strcmp(variant, "mods")) {
            // what Xwayland does with a dmabuf-feedback tranche: allocate with every modifier the EGL display advertises
            PFNEGLQUERYDMABUFMODIFIERSEXTPROC qmods = (void *)eglGetProcAddress("eglQueryDmaBufModifiersEXT");
            if (!qmods) FAIL("no eglQueryDmaBufModifiersEXT");
            EGLint n = 0; qmods(dpy, DRM_FORMAT_XRGB8888, 0, NULL, NULL, &n);
            EGLuint64KHR mods[64]; EGLBoolean ext[64]; if (n > 64) n = 64;
            qmods(dpy, DRM_FORMAT_XRGB8888, n, mods, ext, &n);
            STEP("DDK advertises %d modifiers for XRGB8888:", n);
            for (int i = 0; i < n; i++) STEP("  modifier[%d] = 0x%016llx external_only=%d", i, (unsigned long long)mods[i], ext[i]);
            if (n == 0) FAIL("no modifiers advertised");
            uint64_t list[64]; for (int i = 0; i < n; i++) list[i] = mods[i];
            bo = gbm_bo_create_with_modifiers2(gbm, w, h, GBM_FORMAT_XRGB8888, list, n, GBM_BO_USE_RENDERING);
            STEP("gbm_bo_create_with_modifiers2(all %d): %p", n, (void *)bo);
        } else if (!strcmp(variant, "bomod")) {
            const uint64_t mods[] = { DRM_FORMAT_MOD_LINEAR };
            bo = gbm_bo_create_with_modifiers2(gbm, w, h, GBM_FORMAT_XRGB8888, mods, 1, GBM_BO_USE_RENDERING);
            STEP("gbm_bo_create_with_modifiers2(LINEAR): %p", (void *)bo);
        } else {
            bo = gbm_bo_create(gbm, w, h, GBM_FORMAT_XRGB8888, GBM_BO_USE_RENDERING);
            STEP("gbm_bo_create(XRGB8888, RENDERING): %p", (void *)bo);
        }
        if (!bo) FAIL("gbm_bo_create failed");
        STEP("bo stride %u modifier 0x%llx handle %u", gbm_bo_get_stride(bo), (unsigned long long)gbm_bo_get_modifier(bo), gbm_bo_get_handle(bo).u32);
        if (want_map) {   // CPU-touch every page of the buffer before any GPU access
            uint32_t stride = 0; void *map_data = NULL;
            void *m = gbm_bo_map(bo, 0, 0, w, h, want_map == 2 ? GBM_BO_TRANSFER_READ : GBM_BO_TRANSFER_READ_WRITE, &stride, &map_data);
            STEP("gbm_bo_map(%s) -> %p stride %u", want_map == 2 ? "READ" : "READ_WRITE", m, stride);
            if (!m) FAIL("gbm_bo_map failed");
            if (want_map == 2) { volatile unsigned char sum = 0; for (size_t i = 0; i < (size_t)stride * h; i += 64) sum += ((unsigned char *)m)[i]; STEP("CPU read bytes, sum %d", sum); }
            else memset(m, 0x55, (size_t)stride * h);
            gbm_bo_unmap(bo, map_data);
            STEP("unmapped");
        }
        if (!strcmp(variant, "dmabuf")) {
            int dfd = gbm_bo_get_fd(bo);
            if (dfd < 0) FAIL("gbm_bo_get_fd failed");
            const EGLint attrs[] = {
                EGL_WIDTH, w, EGL_HEIGHT, h, EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_XRGB8888,
                EGL_DMA_BUF_PLANE0_FD_EXT, dfd, EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0, EGL_DMA_BUF_PLANE0_PITCH_EXT, (EGLint)gbm_bo_get_stride(bo),
                EGL_NONE };
            img = create_image(dpy, EGL_NO_CONTEXT, EGL_LINUX_DMA_BUF_EXT, NULL, attrs);
            STEP("eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT fd %d): %p err 0x%x", dfd, img, eglGetError());
        } else {
            img = create_image(dpy, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR, bo, NULL);
            STEP("eglCreateImageKHR(EGL_NATIVE_PIXMAP_KHR, bo): %p err 0x%x", img, eglGetError());
        }
        if (img == EGL_NO_IMAGE_KHR) FAIL("no EGLImage");
        image_target(GL_TEXTURE_2D, img); check_gl("glEGLImageTargetTexture2DOES");
        STEP("texture bound to the image");
        if (want_up) {
            unsigned char *px = malloc((size_t)w * h * 4); memset(px, 0x7f, (size_t)w * h * 4);
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px); check_gl("glTexSubImage2D");
            glFinish(); STEP("glTexSubImage2D upload done before GPU use");
        }
    }

    if (!strcmp(variant, "sample")) {
        // read-only GPU access to the bo-backed image: sample it into a plain 64x64 FBO
        GLuint tex2, fbo2; glGenTextures(1, &tex2); glBindTexture(GL_TEXTURE_2D, tex2);
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
        glGenFramebuffers(1, &fbo2); glBindFramebuffer(GL_FRAMEBUFFER, fbo2);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex2, 0);
        glViewport(0, 0, 64, 64);
        GLuint p = program(fs_tex); glUseProgram(p);
        glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tex); glUniform1i(glGetUniformLocation(p, "s"), 0);
        static const float tri[] = { -1, -1, 3, -1, -1, 3 };
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, tri); glEnableVertexAttribArray(0);
        STEP("sampling the %dx%d bo image into a plain FBO (read-only access)", w, h);
        glDrawArrays(GL_TRIANGLES, 0, 3); check_gl("glDrawArrays(sample)");
        finish_and_read("sample");
        glDrawArrays(GL_TRIANGLES, 0, 3); finish_and_read("sample again");
        printf("RESULT: OK\n"); return 0;
    }
    if (!strcmp(variant, "upload")) {
        // CPU -> GPU transfer into the bo-backed image (no 3D render), then read back
        unsigned char *px = malloc((size_t)w * h * 4); memset(px, 0x7f, (size_t)w * h * 4);
        STEP("glTexSubImage2D upload into the %dx%d bo image", w, h);
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px); check_gl("glTexSubImage2D");
        glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
        finish_and_read("upload");
        printf("RESULT: OK\n"); return 0;
    }
    glGenFramebuffers(1, &fbo); glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
    GLenum fbs = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    STEP("framebuffer status 0x%x (0x8cd5 = complete)", fbs);
    glViewport(0, 0, w, h);

    if (!strcmp(variant, "draw")) {
        GLuint p = program(fs_src); glUseProgram(p);
        static const float tri[] = { -1, -1, 3, -1, -1, 3 };
        if (want_vbo) {
            PFNGLBUFFERSTORAGEEXTPROC storage = (void *)eglGetProcAddress("glBufferStorageEXT");
            PFNGLMAPBUFFERRANGEEXTPROC maprange = (void *)eglGetProcAddress("glMapBufferRange");
            if (!storage || !maprange) FAIL("no glBufferStorageEXT/glMapBufferRange");
            GLuint vbo; glGenBuffers(1, &vbo); glBindBuffer(GL_ARRAY_BUFFER, vbo);
            storage(GL_ARRAY_BUFFER, 1 << 20, NULL, GL_MAP_WRITE_BIT_EXT | GL_MAP_PERSISTENT_BIT_EXT | GL_MAP_COHERENT_BIT_EXT);
            void *vb = maprange(GL_ARRAY_BUFFER, 0, 1 << 20, GL_MAP_WRITE_BIT_EXT | GL_MAP_INVALIDATE_BUFFER_BIT_EXT | GL_MAP_PERSISTENT_BIT_EXT | GL_MAP_COHERENT_BIT_EXT);
            STEP("persistent coherent VBO mapped at %p, err 0x%x", vb, glGetError());
            if (!vb) FAIL("map failed");
            memcpy(vb, tri, sizeof tri);
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, (void *)0);
        } else {
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, tri);
        }
        glEnableVertexAttribArray(0);
        STEP("drawing a full-screen triangle (shader)");
        glDrawArrays(GL_TRIANGLES, 0, 3); check_gl("glDrawArrays");
        finish_and_read("draw");
    } else {
        glClearColor(1.0f, 0.0f, 0.0f, 1.0f);
        if (want_vbo == 2) { glEnable(GL_SCISSOR_TEST); glScissor(3, 3, 5, 5); STEP("scissored 5x5 clear at (3,3) on the %dx%d FBO", w, h); }
        else STEP("glClear on the FBO");
        glClear(GL_COLOR_BUFFER_BIT); check_gl("glClear");
        finish_and_read("clear");
        if (!strcmp(variant, "botex")) {
            // glamor composite: sample the bo-backed texture into another render target
            GLuint tex2, fbo2; glGenTextures(1, &tex2); glBindTexture(GL_TEXTURE_2D, tex2);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, 64, 64, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
            glGenFramebuffers(1, &fbo2); glBindFramebuffer(GL_FRAMEBUFFER, fbo2);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex2, 0);
            glViewport(0, 0, 64, 64);
            GLuint p = program(fs_tex); glUseProgram(p);
            glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, tex); glUniform1i(glGetUniformLocation(p, "s"), 0);
            static const float tri[] = { -1, -1, 3, -1, -1, 3 };
            glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, tri); glEnableVertexAttribArray(0);
            STEP("sampling the bo texture into a plain FBO");
            glDrawArrays(GL_TRIANGLES, 0, 3); check_gl("glDrawArrays(sample)");
            finish_and_read("sample");
        }
    }
    // a second job on the same context, like glamor's next operation
    glClearColor(0.0f, 0.0f, 1.0f, 1.0f); glClear(GL_COLOR_BUFFER_BIT);
    finish_and_read("second clear");

    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglDestroyContext(dpy, ctx); eglTerminate(dpy);
    if (bo) gbm_bo_destroy(bo);
    gbm_device_destroy(gbm); close(fd);
    printf("RESULT: OK\n");
    return 0;
}
