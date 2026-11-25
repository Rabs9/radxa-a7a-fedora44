/* egl-dmabuf-mods: what dma-buf formats/modifiers does an EGL display offer for import (EGL_EXT_image_dma_buf_import_modifiers)?
 *
 * KWin 6.7 intersects the EGL display's non-external-only modifiers with the KMS plane's IN_FORMATS list and refuses the
 * output when the intersection is empty (KWin 6.6 did not intersect). This prints, per DRM node, the EGL vendor/version and
 * the modifiers for a few formats, with the external-only flag, so the intersection can be checked by hand:
 *     pvr-run env MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm /tmp/egl-dmabuf-mods /dev/dri/card0     (the DDK on the display node)
 *     pvr-run /tmp/egl-dmabuf-mods /dev/dri/renderD128                                          (the DDK on the GPU node)
 *     /tmp/egl-dmabuf-mods /dev/dri/card0                                                       (Fedora's mesa, llvmpipe)
 * build (board): gcc -O1 -o /tmp/egl-dmabuf-mods egl-dmabuf-mods.c $(pkg-config --cflags --libs gbm egl)
 */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <drm_fourcc.h>

static const char *modname(uint64_t m)
{
	static char buf[32];
	if (m == DRM_FORMAT_MOD_LINEAR) return "LINEAR";
	if (m == DRM_FORMAT_MOD_INVALID) return "INVALID";
	snprintf(buf, sizeof buf, "0x%llx", (unsigned long long)m);
	return buf;
}

int main(int argc, char **argv)
{
	const char *node = argc > 1 ? argv[1] : "/dev/dri/card0";
	int fd = open(node, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror(node); return 1; }
	struct gbm_device *gbm = gbm_create_device(fd);
	if (!gbm) { fprintf(stderr, "gbm_create_device failed\n"); return 1; }
	PFNEGLGETPLATFORMDISPLAYEXTPROC getPlatformDisplay = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
	EGLDisplay dpy = getPlatformDisplay ? getPlatformDisplay(EGL_PLATFORM_GBM_KHR, gbm, NULL) : eglGetDisplay((EGLNativeDisplayType)gbm);
	if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, NULL, NULL)) { fprintf(stderr, "EGL init failed on %s (0x%x)\n", node, eglGetError()); return 1; }
	printf("%s: gbm backend %s, EGL %s / %s\n", node, gbm_device_get_backend_name(gbm), eglQueryString(dpy, EGL_VENDOR), eglQueryString(dpy, EGL_VERSION));
	const char *exts = eglQueryString(dpy, EGL_EXTENSIONS);
	printf("  dma_buf_import: %s, import_modifiers: %s\n",
	       strstr(exts, "EGL_EXT_image_dma_buf_import ") || strstr(exts, "EGL_EXT_image_dma_buf_import\0") ? "yes" : "no",
	       strstr(exts, "EGL_EXT_image_dma_buf_import_modifiers") ? "yes" : "NO");
	PFNEGLQUERYDMABUFFORMATSEXTPROC qf = (void *)eglGetProcAddress("eglQueryDmaBufFormatsEXT");
	PFNEGLQUERYDMABUFMODIFIERSEXTPROC qm = (void *)eglGetProcAddress("eglQueryDmaBufModifiersEXT");
	if (!qf || !qm) { printf("  (no modifier query entry points)\n"); return 0; }
	EGLint nfmt = 0;
	qf(dpy, 0, NULL, &nfmt);
	EGLint *fmts = calloc(nfmt ? nfmt : 1, sizeof(EGLint));
	qf(dpy, nfmt, fmts, &nfmt);
	printf("  %d importable formats\n", nfmt);
	const EGLint want[] = { DRM_FORMAT_ARGB8888, DRM_FORMAT_XRGB8888, DRM_FORMAT_ABGR8888, DRM_FORMAT_XBGR8888, DRM_FORMAT_ARGB2101010, DRM_FORMAT_ABGR2101010 };
	for (unsigned i = 0; i < sizeof want / sizeof *want; i++) {
		int present = 0;
		for (int j = 0; j < nfmt; j++) if (fmts[j] == want[i]) present = 1;
		char name[5] = { want[i] & 0xff, (want[i] >> 8) & 0xff, (want[i] >> 16) & 0xff, (want[i] >> 24) & 0xff, 0 };
		if (!present) { printf("  %s: not importable\n", name); continue; }
		EGLint nmod = 0;
		qm(dpy, want[i], 0, NULL, NULL, &nmod);
		EGLuint64KHR *mods = calloc(nmod ? nmod : 1, sizeof(EGLuint64KHR));
		EGLBoolean *ext = calloc(nmod ? nmod : 1, sizeof(EGLBoolean));
		qm(dpy, want[i], nmod, mods, ext, &nmod);
		printf("  %s: %d modifiers:", name, nmod);
		for (int k = 0; k < nmod; k++) printf(" %s%s", modname(mods[k]), ext[k] ? "(external-only)" : "");
		printf("\n");
		free(mods); free(ext);
	}
	eglTerminate(dpy);
	gbm_device_destroy(gbm);
	close(fd);
	return 0;
}
