// wl-render-node-shim.c: LD_PRELOAD test of "Wayland clients on the PowerVR without wl_drm" (a7a-fedora, 2026-11-16).
//
// Found on the board after `dnf upgrade` to KWin 6.8.3: the compositor no longer offers the wl_drm global
// (wayland-info: 0 lines), and its linux-dmabuf feedback names /dev/dri/card0 as the main device (sunxi-drm, the
// display controller: KWin runs under KWIN_RENDER_NODES=/dev/dri/none). /opt/pvr-wl's Mesa 24.0.1 handles the feedback
// in default_dmabuf_feedback_main_device(): loader_get_render_node(dev) = drmGetDeviceFromDevId() and the device's
// render node. card0 has none, so it falls back to wl_drm, finds none, and every client lands on softpipe
// (plasmashell maps swrast_dri.so, Xwayland "Main linux-dmabuf device has no render node", an SDL2 game "Couldn't find
// matching EGL config"). With KWin 6.7 the wl_drm fallback handed out card0 and authenticated it.
//
// The idea under test: a client that is told "card0" opens the GPU's own render node instead (pvrsrvkm,
// /dev/dri/renderD128), as on every other split display/GPU board. This shim does that from outside: when
// drmGetDeviceFromDevId() is asked about a device without a render node, it answers with the first device that has
// one. Nothing changes when the asked device has a render node.
//
// build (board):  gcc -O1 -shared -fPIC -o /home/radxa/wl-render-node-shim.so wl-render-node-shim.c -ldl
// test (inside the session; one command line):
//                 WL_RENDER_NODE_SHIM_DEBUG=1 LD_PRELOAD=/home/radxa/wl-render-node-shim.so eglinfo -B -p wayland
//                 pass = the renderer is the PowerVR, not softpipe.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <sys/types.h>

// the head of libdrm's drmDevice (xf86drm.h); only these two fields are read
struct drm_device_head {
    char **nodes;
    int available_nodes;
};
#define DRM_NODE_RENDER 2

typedef int (*get_dev_fn)(dev_t, uint32_t, struct drm_device_head **);
typedef void (*free_dev_fn)(struct drm_device_head **);

static int debug(void)
{
    return getenv("WL_RENDER_NODE_SHIM_DEBUG") != NULL;
}

int drmGetDeviceFromDevId(dev_t dev_id, uint32_t flags, struct drm_device_head **device)
{
    // a program that dlopen()s its EGL (SDL) has libdrm outside the global scope: RTLD_NEXT finds nothing there
    static void *libdrm;
    get_dev_fn real = (get_dev_fn)dlsym(RTLD_NEXT, "drmGetDeviceFromDevId");
    free_dev_fn real_free = (free_dev_fn)dlsym(RTLD_NEXT, "drmFreeDevice");
    if (!real || !real_free) {
        if (!libdrm)
            libdrm = dlopen("libdrm.so.2", RTLD_NOW | RTLD_LOCAL);
        if (libdrm) {
            real = (get_dev_fn)dlsym(libdrm, "drmGetDeviceFromDevId");
            real_free = (free_dev_fn)dlsym(libdrm, "drmFreeDevice");
        }
    }
    if (!real) {
        if (debug())
            fprintf(stderr, "wl-render-node-shim: libdrm's drmGetDeviceFromDevId not found\n");
        return -1;
    }
    int rc = real(dev_id, flags, device);
    if (rc < 0 || !*device || ((*device)->available_nodes & (1 << DRM_NODE_RENDER)) || !real_free)
        return rc;

    for (int n = 128; n < 136; n++) {
        char path[32];
        struct stat st;
        struct drm_device_head *gpu = NULL;
        snprintf(path, sizeof(path), "/dev/dri/renderD%d", n);
        if (stat(path, &st) != 0 || !S_ISCHR(st.st_mode))
            continue;
        if (real(st.st_rdev, flags, &gpu) < 0 || !gpu)
            continue;
        if (debug())
            fprintf(stderr, "wl-render-node-shim: device %u:%u has no render node, answering with %s\n",
                    major(dev_id), minor(dev_id), path);
        real_free(device);
        *device = gpu;
        return 0;
    }
    return rc;
}
