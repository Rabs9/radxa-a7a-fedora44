// egl-es-config-shim.c: LD_PRELOAD test of the "Qt fix" for the DDK glue (a7a-fedora, 2026-11-15).
//
// Fedora's Qt is a desktop-GL build: on EGL it asks eglChooseConfig for EGL_RENDERABLE_TYPE = EGL_OPENGL_BIT. The
// vendor DDK has no desktop-GL module, so /opt/pvr-wl's EGL has no such config, Qt prints "Cannot find EGLConfig" and
// the window gets no surface (docs/DDK-WAYLAND-2026-09-10.md, section 4). Qt switches itself to OpenGL ES when the
// config it is handed has no EGL_OPENGL_BIT (qeglconvenience.cpp, q_glFormatFromConfig), so the planned fix is a
// lenient eglChooseConfig in the glue: on a display without desktop GL, drop EGL_OPENGL_BIT from the request
// (docs/NEXT-SESSION-PROMPT.md, route D (b)). This shim does exactly that from outside, so the idea can be tested on
// one program in a minute, before the glue is rebuilt. It changes nothing on a display that offers desktop GL.
//
// build (board):  gcc -O1 -shared -fPIC -o /home/radxa/egl-es-config-shim.so egl-es-config-shim.c -ldl
// test (DDK boot, inside the session; one command line):
//                 QT_FORCE_STDERR_LOGGING=1 QSG_INFO=1 EGL_ES_CONFIG_SHIM_DEBUG=1
//                   LD_PRELOAD=/home/radxa/egl-es-config-shim.so pvr-wl-run kinfocenter
//                 pass = the scene graph names the PowerVR renderer and no "Cannot find EGLConfig" line appears.
// unit check without a GPU: a fake libEGL that prints the attributes it receives (done 2026-11-15 on the board: a
// GL-only request reaches the driver as ES2, GL|ES3 as ES3, ES and DONT_CARE unchanged, the caller's list untouched,
// and nothing changes when EGL_CLIENT_APIS lists "OpenGL").
#define _GNU_SOURCE
#include <EGL/egl.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifndef EGL_OPENGL_ES3_BIT
#define EGL_OPENGL_ES3_BIT 0x0040
#endif
#define ES_BITS (EGL_OPENGL_ES_BIT | EGL_OPENGL_ES2_BIT | EGL_OPENGL_ES3_BIT)
#define MAX_ATTRIBS 64

// EGL_CLIENT_APIS is a space-separated list: "OpenGL" as a whole word means desktop GL ("OpenGL_ES" does not)
static int display_has_desktop_gl(EGLDisplay dpy)
{
    const char *apis = eglQueryString(dpy, EGL_CLIENT_APIS);
    if (!apis)
        return 1;                       // unknown: leave the request alone
    for (const char *p = apis; (p = strstr(p, "OpenGL")); p += 6)
        if ((p == apis || p[-1] == ' ') && (p[6] == ' ' || p[6] == 0))
            return 1;
    return 0;
}

EGLBoolean eglChooseConfig(EGLDisplay dpy, const EGLint *attribs, EGLConfig *configs, EGLint size, EGLint *num)
{
    static EGLBoolean (*real)(EGLDisplay, const EGLint *, EGLConfig *, EGLint, EGLint *);
    EGLint copy[2 * MAX_ATTRIBS + 1];
    int n = 0, changed = 0;

    if (!real)
        real = (EGLBoolean (*)(EGLDisplay, const EGLint *, EGLConfig *, EGLint, EGLint *))dlsym(RTLD_NEXT, "eglChooseConfig");
    if (attribs && !display_has_desktop_gl(dpy)) {
        for (; attribs[n] != EGL_NONE && n < 2 * MAX_ATTRIBS; n += 2) {
            copy[n] = attribs[n];
            copy[n + 1] = attribs[n + 1];
            if ((attribs[n] == EGL_RENDERABLE_TYPE || attribs[n] == EGL_CONFORMANT) &&
                attribs[n + 1] != EGL_DONT_CARE && (attribs[n + 1] & EGL_OPENGL_BIT)) {
                copy[n + 1] &= ~EGL_OPENGL_BIT;
                if (attribs[n] == EGL_RENDERABLE_TYPE && !(copy[n + 1] & ES_BITS))
                    copy[n + 1] |= EGL_OPENGL_ES2_BIT;
                changed = 1;
            }
        }
        if (attribs[n] != EGL_NONE)
            changed = 0;                // longer than the copy: pass the caller's list through untouched
        copy[n] = EGL_NONE;
    }
    EGLBoolean ok = real(dpy, changed ? copy : attribs, configs, size, num);
    if (changed && getenv("EGL_ES_CONFIG_SHIM_DEBUG"))
        fprintf(stderr, "egl-es-config-shim: desktop-GL config request answered with ES configs: %s, %d config(s)\n",
                ok ? "ok" : "failed", (ok && num) ? *num : 0);
    return ok;
}
