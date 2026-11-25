/* pvr-import-test: import a GPU (pvrsrvkm) dma-buf into the display device, the operation that wiped the IOMMU.
 *
 * Root cause of the DE IOMMU fault storm (docs/DE-IOMMU-STORM-2026-09-07.md): pvrsrvkm's dma-buf exporter never
 * dma_map_sg()s for an importer but dma_unmap_sg()'d on unmap; on the IOMMU-backed sunxi-drm device that unmapped
 * a gigantic range of the shared domain. KWin triggers it when it tries direct scanout of a GPU client buffer.
 * This does the same thing on purpose, headlessly (no DRM master needed):
 *   1. gbm device on the GPU render node (/dev/dri/renderD128 through the DDK's GBM), create a bo, export its dma-buf
 *   2. drmPrimeFDToHandle() on /dev/dri/card0 (sunxi-drm): expected to FAIL (scattered pages, -EINVAL), and with
 *      the fixed pvrsrvkm the failure must be harmless: no IOMMU fault, the desktop keeps running, no spinning thread
 *   3. repeat a few times
 * Before the fix this call spun in the kernel for minutes and the display engine faulted within seconds.
 *
 * build (board):  gcc -O1 -o /tmp/pvr-import-test pvr-import-test.c $(pkg-config --cflags --libs gbm libdrm)
 * run:            pvr-run /tmp/pvr-import-test [-n 3] [-w 1024 -h 600]     (pvr-run = the DDK's GBM/EGL libraries)
 * watch:          sudo dmesg -w | grep -E "Bug is in|stopped after|overlaps"
 */
#include <errno.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

static double now_s(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + ts.tv_nsec / 1e9; }

int main(int argc, char **argv)
{
	int w = 1024, h = 600, n = 3, opt;
	const char *render = "/dev/dri/renderD128", *card = "/dev/dri/card0";
	while ((opt = getopt(argc, argv, "n:w:h:r:c:")) != -1) {
		if (opt == 'n') n = atoi(optarg); else if (opt == 'w') w = atoi(optarg); else if (opt == 'h') h = atoi(optarg);
		else if (opt == 'r') render = optarg; else if (opt == 'c') card = optarg;
	}
	int rfd = open(render, O_RDWR | O_CLOEXEC), cfd = open(card, O_RDWR | O_CLOEXEC);
	if (rfd < 0) { perror(render); return 1; }
	if (cfd < 0) { perror(card); return 1; }
	drmVersionPtr v = drmGetVersion(rfd);
	printf("render node %s driver %s, display %s\n", render, v ? v->name : "?", card);
	if (v) drmFreeVersion(v);
	struct gbm_device *gbm = gbm_create_device(rfd);
	if (!gbm) { fprintf(stderr, "gbm_create_device(%s) failed\n", render); return 1; }
	printf("gbm backend: %s\n", gbm_device_get_backend_name(gbm));

	int failures = 0;
	for (int i = 0; i < n; i++) {
		struct gbm_bo *bo = gbm_bo_create(gbm, w, h, GBM_FORMAT_XRGB8888, GBM_BO_USE_RENDERING);
		if (!bo) { fprintf(stderr, "gbm_bo_create failed: %s\n", strerror(errno)); return 1; }
		int fd = gbm_bo_get_fd(bo);
		uint32_t handle = 0;
		double t0 = now_s();
		int r = drmPrimeFDToHandle(cfd, fd, &handle);
		double dt = (now_s() - t0) * 1000;
		printf("[%d] GPU bo %dx%d dmabuf fd %d -> drmPrimeFDToHandle(card0) = %d (%s), handle %u, %.1f ms\n",
		       i, w, h, fd, r, r ? strerror(errno) : "ok", handle, dt);
		fflush(stdout);
		if (r == 0) {
			/* it imported: add a framebuffer so the kernel side can be inspected, then release it */
			uint32_t handles[4] = { handle }, strides[4] = { gbm_bo_get_stride(bo) }, offsets[4] = { 0 }, fb = 0;
			int ar = drmModeAddFB2(cfd, w, h, DRM_FORMAT_XRGB8888, handles, strides, offsets, &fb, 0);
			printf("    drmModeAddFB2 -> %d (%s), fb %u; kernel view:\n", ar, ar ? strerror(errno) : "ok", fb);
			fflush(stdout);
			if (ar == 0) {
				char cmd[256];
				snprintf(cmd, sizeof cmd, "grep -A14 'allocated by = pvr-import' /sys/kernel/debug/dri/0/framebuffer 2>/dev/null | grep -E 'imported|dma_addr|size=' | head -4");
				if (system(cmd) != 0) printf("    (debugfs not readable: run as root)\n");
				drmModeRmFB(cfd, fb);
			}
			drmCloseBufferHandle(cfd, handle);
		} else {
			failures++;
		}
		if (dt > 1000) printf("    WARNING: the ioctl took %.0f ms (the old kernel spun here)\n", dt);
		close(fd);
		gbm_bo_destroy(bo);
	}
	printf("done: %d/%d imports failed (expected: all fail, quickly, and nothing else happens)\n", failures, n);
	gbm_device_destroy(gbm);
	close(cfd); close(rfd);
	return 0;
}
