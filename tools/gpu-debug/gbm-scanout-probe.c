/* gbm-scanout-probe: what does a GBM scanout buffer on /dev/dri/card0 look like to the kernel?
 *
 * Creates one gbm_bo with GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING through whatever GBM/DRI stack the
 * environment selects (pvr-run + MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm = the Imagination DDK, as in KWin's
 * drop-in; nothing = Fedora's mesa), adds a DRM framebuffer for it (no DRM master needed), prints the
 * handle/stride/size/modifier and the dma-buf fd, then sleeps so the kernel side can be read:
 *     sudo cat /sys/kernel/debug/dri/0/framebuffer      (imported=yes|no, dma_addr = the DE's address)
 *     sudo cat /proc/<pid>/fdinfo/<dmabuf fd>            (exporter name, size)
 * Optionally destroys the bo first (-d) while the FB still exists, to see whether the GEM survives.
 *
 * build (board):  gcc -O1 -o /tmp/gbm-scanout-probe gbm-scanout-probe.c $(pkg-config --cflags --libs gbm libdrm)
 * run:            pvr-run env MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm /tmp/gbm-scanout-probe [-w 1024 -h 600] [-s seconds] [-d]
 *                 strace -f -e trace=ioctl -o /tmp/probe.strace ... shows CREATE_DUMB vs PRIME_FD_TO_HANDLE
 */
#include <errno.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

int main(int argc, char **argv)
{
	int w = 1024, h = 600, secs = 30, destroy_first = 0, opt;
	const char *node = "/dev/dri/card0";
	while ((opt = getopt(argc, argv, "w:h:s:dn:")) != -1) {
		if (opt == 'w') w = atoi(optarg);
		else if (opt == 'h') h = atoi(optarg);
		else if (opt == 's') secs = atoi(optarg);
		else if (opt == 'd') destroy_first = 1;
		else if (opt == 'n') node = optarg;
	}
	int fd = open(node, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror(node); return 1; }
	drmVersionPtr v = drmGetVersion(fd);
	printf("node %s driver %s, pid %d\n", node, v ? v->name : "?", getpid());
	if (v) drmFreeVersion(v);

	struct gbm_device *gbm = gbm_create_device(fd);
	if (!gbm) { fprintf(stderr, "gbm_create_device failed\n"); return 1; }
	printf("gbm backend: %s\n", gbm_device_get_backend_name(gbm));
	struct gbm_bo *bo = gbm_bo_create(gbm, w, h, GBM_FORMAT_XRGB8888, GBM_BO_USE_SCANOUT | GBM_BO_USE_RENDERING);
	if (!bo) { fprintf(stderr, "gbm_bo_create %dx%d failed: %s\n", w, h, strerror(errno)); return 1; }
	uint32_t handle = gbm_bo_get_handle(bo).u32, stride = gbm_bo_get_stride(bo);
	uint64_t mod = gbm_bo_get_modifier(bo);
	int dmabuf = gbm_bo_get_fd(bo);
	printf("bo %dx%d handle %u stride %u modifier 0x%llx dmabuf fd %d planes %d\n", w, h, handle, stride,
	       (unsigned long long)mod, dmabuf, gbm_bo_get_plane_count(bo));

	uint32_t handles[4] = { handle }, strides[4] = { stride }, offsets[4] = { 0 }, fb_id = 0;
	int r = drmModeAddFB2(fd, w, h, DRM_FORMAT_XRGB8888, handles, strides, offsets, &fb_id, 0);
	printf("drmModeAddFB2: %s, fb_id %u\n", r ? strerror(-r) : "ok", fb_id);

	if (destroy_first) { printf("destroying the bo now (FB %u still exists)\n", fb_id); gbm_bo_destroy(bo); bo = NULL; }
	printf("sleeping %d s: read /sys/kernel/debug/dri/0/framebuffer and /proc/%d/fdinfo/%d now\n", secs, getpid(), dmabuf);
	fflush(stdout);
	sleep(secs);
	if (fb_id) drmModeRmFB(fd, fb_id);
	if (bo) gbm_bo_destroy(bo);
	gbm_device_destroy(gbm);
	close(fd);
	printf("done\n");
	return 0;
}
