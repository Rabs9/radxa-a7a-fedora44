/* drm-unstick: complete a sunxi fbdev wait that a stuck DRM master can never satisfy.
 *
 * sunxi_fbdev_plane_update() waits (unbounded) for the DRM master's next atomic flush when a master exists. A
 * master that switches its VT back to text mode before dropping master therefore hangs in the KDSETMODE ioctl
 * (fbcon unblank -> pan -> that wait), holding the console lock: chvt, fgconsole and the compositor freeze.
 * This tool duplicates the stuck process's DRM file (pidfd_getfd, root) and performs one mode-set through it;
 * the resulting atomic flush completes the wait, the ioctl returns and the process finishes normally.
 *
 * build (board): gcc -O1 -o /tmp/drm-unstick drm-unstick.c $(pkg-config --cflags --libs libdrm)
 * usage:         sudo /tmp/drm-unstick <pid> [/dev/dri/card0]
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

int main(int argc, char **argv)
{
	if (argc < 2) { fprintf(stderr, "usage: drm-unstick <pid> [node]\n"); return 2; }
	int pid = atoi(argv[1]);
	const char *node = argc > 2 ? argv[2] : "/dev/dri/card0";
	struct stat want;
	if (stat(node, &want)) { perror(node); return 1; }

	char dir[64]; snprintf(dir, sizeof dir, "/proc/%d/fd", pid);
	DIR *d = opendir(dir);
	if (!d) { perror(dir); return 1; }
	int target = -1;
	struct dirent *e;
	while ((e = readdir(d))) {
		char p[128]; struct stat st;
		if (e->d_name[0] == '.') continue;
		snprintf(p, sizeof p, "%s/%s", dir, e->d_name);
		if (stat(p, &st) == 0 && st.st_rdev == want.st_rdev && S_ISCHR(st.st_mode)) { target = atoi(e->d_name); break; }
	}
	closedir(d);
	if (target < 0) { fprintf(stderr, "pid %d has no fd on %s\n", pid, node); return 1; }

	int pidfd = syscall(SYS_pidfd_open, pid, 0);
	if (pidfd < 0) { perror("pidfd_open"); return 1; }
	int fd = syscall(438 /* SYS_pidfd_getfd */, pidfd, target, 0);
	if (fd < 0) { perror("pidfd_getfd"); return 1; }
	printf("duplicated fd %d of pid %d as fd %d; is master: %d\n", target, pid, fd, drmIsMaster(fd));

	drmModeRes *res = drmModeGetResources(fd);
	if (!res) { perror("GetResources"); return 1; }
	drmModeConnector *conn = NULL;
	for (int i = 0; i < res->count_connectors && !conn; i++) {
		drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
		if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes) conn = c; else drmModeFreeConnector(c);
	}
	if (!conn) { fprintf(stderr, "no connected connector\n"); return 1; }
	drmModeModeInfo mode = conn->modes[0];
	uint32_t crtc_id = 0;
	if (conn->encoder_id) { drmModeEncoder *en = drmModeGetEncoder(fd, conn->encoder_id); if (en) { crtc_id = en->crtc_id; drmModeFreeEncoder(en); } }
	if (!crtc_id) crtc_id = res->crtcs[0];

	struct drm_mode_create_dumb c = { .width = mode.hdisplay, .height = mode.vdisplay, .bpp = 32 };
	if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &c)) { perror("CREATE_DUMB"); return 1; }
	uint32_t handles[4] = { c.handle }, pitches[4] = { c.pitch }, offsets[4] = { 0 }, fb;
	if (drmModeAddFB2(fd, mode.hdisplay, mode.vdisplay, DRM_FORMAT_XRGB8888, handles, pitches, offsets, &fb, 0)) { perror("AddFB2"); return 1; }
	int r = drmModeSetCrtc(fd, crtc_id, fb, 0, 0, &conn->connector_id, 1, &mode);
	printf("SetCrtc(crtc %u, fb %u, %ux%u) -> %d\n", crtc_id, fb, mode.hdisplay, mode.vdisplay, r);
	sleep(1);
	/* the stuck process should have moved on; leave the FB in place (its process owns the file and cleans up) */
	drmModeFreeConnector(conn);
	drmModeFreeResources(res);
	close(fd);
	return r ? 1 : 0;
}
