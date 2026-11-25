// pvr-tqfirst-shim.c: LD_PRELOAD workaround for the PowerVR DDK 24.2 transfer-queue stray write (a7a-fedora, 2026-09-07).
//
// The DDK's first transfer-queue job writes one pixel one page below the transfer context's own buffer. When the
// process created its render context's free lists first, that page is the PM/META-protected free list and the GPU
// faults ("Guilty Lockup"). Creating the transfer context BEFORE any render context (one texture upload on a
// throw-away context) moves that page to harmless memory: docs/GPU-TQ-FAULT-2026-09-07.md, reproducer flag +tqfirst.
//
// This shim interposes gbm_create_device(): right after the real device is created it makes a private EGL display on
// it, a GLES2 context, uploads a 64x64 texture, waits, and keeps the context alive (never destroyed, so its memory is
// never reused). It runs once per process and only when PVR_TQFIRST=1 is set, so it is inert everywhere else.
//
// build (board):  gcc -O1 -shared -fPIC -o /usr/local/lib/pvr-tqfirst-shim.so pvr-tqfirst-shim.c -ldl -lEGL -lGLESv2
// test:           PVR_TQFIRST=1 LD_PRELOAD=/usr/local/lib/pvr-tqfirst-shim.so LD_LIBRARY_PATH=/opt/pvr/lib \
//                 LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri ./glamor-repro /dev/dri/renderD128 bo 24 24
// use:            add Environment=PVR_TQFIRST=1 and Environment=LD_PRELOAD=/usr/local/lib/pvr-tqfirst-shim.so to the
//                 plasma-kwin_wayland.service drop-in (Xwayland inherits KWin's environment).
#define _GNU_SOURCE
#define EGL_EGLEXT_PROTOTYPES
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct gbm_device;
typedef struct gbm_device *(*gbm_create_device_fn)(int fd);

static void log_msg(const char *m) { fprintf(stderr, "pvr-tqfirst-shim: %s\n", m); }

static void warm_transfer_context(struct gbm_device *gbm)
{
    static int done = 0;
    if (done) return;
    done = 1;
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_display = (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress("eglGetPlatformDisplayEXT");
    EGLDisplay dpy = get_display ? get_display(EGL_PLATFORM_GBM_KHR, gbm, NULL) : EGL_NO_DISPLAY;
    if (dpy == EGL_NO_DISPLAY) { log_msg("no GBM EGL display, skipping"); return; }
    if (!eglInitialize(dpy, NULL, NULL)) { log_msg("eglInitialize failed, skipping"); return; }
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    EGLContext ctx = eglCreateContext(dpy, EGL_NO_CONFIG_KHR, EGL_NO_CONTEXT, ctx_attribs);
    if (ctx == EGL_NO_CONTEXT) { log_msg("eglCreateContext failed, skipping"); return; }
    EGLDisplay prev_dpy = eglGetCurrentDisplay();
    EGLContext prev_ctx = eglGetCurrentContext();
    EGLSurface prev_draw = eglGetCurrentSurface(EGL_DRAW), prev_read = eglGetCurrentSurface(EGL_READ);
    if (!eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, ctx)) { log_msg("eglMakeCurrent failed, skipping"); return; }
    const GLubyte *renderer = glGetString(GL_RENDERER);
    if (!renderer || !strstr((const char *)renderer, "PowerVR")) {
        eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        eglDestroyContext(dpy, ctx);
        log_msg("not the PowerVR DDK, nothing to do");
        return;
    }
    /* warm-up steps, selectable with PVR_TQFIRST_STEPS (default "u64"): uN = upload an NxN texture, cN = clear an NxN FBO */
    const char *steps = getenv("PVR_TQFIRST_STEPS");
    char buf[256];
    strncpy(buf, steps && steps[0] ? steps : "u64", sizeof buf - 1);
    buf[sizeof buf - 1] = 0;
    unsigned char *px = malloc(1024 * 1024 * 4);
    memset(px, 0x42, 1024 * 1024 * 4);
    for (char *tok = strtok(buf, ","); tok; tok = strtok(NULL, ",")) {
        int n = atoi(tok + 1);
        if (n <= 0 || n > 1024) continue;
        GLuint tex = 0;
        glGenTextures(1, &tex);
        glBindTexture(GL_TEXTURE_2D, tex);
        if (tok[0] == 'u') {
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, n, n, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
            glFinish();
        } else if (tok[0] == 'c') {
            GLuint fbo = 0;
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, n, n, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
            glGenFramebuffers(1, &fbo);
            glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
            glViewport(0, 0, n, n);
            glClearColor(0, 0, 0, 1);
            glClear(GL_COLOR_BUFFER_BIT);
            glFinish();
            glBindFramebuffer(GL_FRAMEBUFFER, 0);
        }
        fprintf(stderr, "pvr-tqfirst-shim: step %s done\n", tok);
    }
    free(px);
    /* the texture and the context stay alive on purpose: freeing them would let the transfer context's memory be reused */
    if (prev_ctx != EGL_NO_CONTEXT) eglMakeCurrent(prev_dpy, prev_draw, prev_read, prev_ctx);
    else eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    log_msg("transfer context created ahead of the render context");
}

struct gbm_device *gbm_create_device(int fd)
{
    static gbm_create_device_fn real = NULL;
    if (!real) real = (gbm_create_device_fn)dlsym(RTLD_NEXT, "gbm_create_device");
    struct gbm_device *dev = real ? real(fd) : NULL;
    const char *on = getenv("PVR_TQFIRST");
    if (dev && on && on[0] == '1') warm_transfer_context(dev);
    return dev;
}
