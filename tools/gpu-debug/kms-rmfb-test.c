/* kms-rmfb-test: does the sunxi display engine stop fetching a buffer when DRM removes it from the plane?
 * (test for the DE IOMMU fault storm, docs/DE-IOMMU-STORM-2026-09-07.md)
 *
 * Runs as root on an INACTIVE VT (chvt 3 first: the compositor's DRM master is then revoked by logind) and takes
 * DRM master on card0. Legacy KMS API (routed through the atomic helpers in sunxi-drm).
 *   disable   set a red buffer on the CRTC, page-flip to a green one, wait for the flip event, then drmModeRmFB() the
 *             green buffer WHILE IT IS DISPLAYED (the DRM core must disable the plane first) and destroy its GEM.
 *             The IOVA is unmapped at that moment: a DE fault afterwards = the DE ignored the plane disable.
 *             Then the red buffer is set again (proves the DE still takes updates).
 *   flipfree  every frame: create a buffer, flip to it, wait for the flip event, then immediately RmFB + destroy the
 *             PREVIOUS buffer (what a compositor is allowed to do once the event says the flip completed). A fault =
 *             the driver signals the flip before the DE really switched (sunxi_de_atomic_flush's poll/timeout path).
 *   dpms      red on screen, DPMS off (CRTC inactive), then try a page flip to green (the core may refuse it),
 *             free red, DPMS on: which buffer does the DE come back on? A fault = it came back on the freed one.
 *   modeset   red on screen, then a full mode-set (same mode) straight to green, free red immediately.
 *   both      disable, then flipfree   (all = disable, flipfree, dpms, modeset)
 * LESSON (2026-09-07): drop DRM master BEFORE restoring the VT's text mode. With a master alive, the sunxi fbdev
 * path waits (unbounded) for that master's next atomic flush, and fbcon's unblank on KDSETMODE(KD_TEXT) runs that
 * path: the ioctl never returns, the console lock is held forever (chvt/fgconsole/compositor freeze). drm-unstick.c
 * recovers it. A driver bug on its own (docs/DE-IOMMU-STORM-2026-09-07.md).
 * Watch with:  sudo dmesg -w | grep -E "Bug is in|fault storm|sunxi-drm: (fb|gem)|atomic_flush"
 * build (board):  gcc -O1 -o /tmp/kms-rmfb-test kms-rmfb-test.c $(pkg-config --cflags --libs libdrm)
 * usage:          sudo /tmp/kms-rmfb-test [-n /dev/dri/card0] [-t /dev/tty3] [-f frames] disable|flipfree|dpms|modeset|both|all
 */
#include <errno.h>
#include <fcntl.h>
#include <linux/kd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

static int fd = -1;
static uint32_t crtc_id, conn_id;
static drmModeModeInfo mode;

static double now_s(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

struct buf { uint32_t handle, fb, pitch; uint64_t size; void *map; };

static int buf_create(struct buf *b, uint32_t colour)
{
	struct drm_mode_create_dumb c = { .width = mode.hdisplay, .height = mode.vdisplay, .bpp = 32 };
	if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &c)) { perror("CREATE_DUMB"); return -1; }
	b->handle = c.handle; b->pitch = c.pitch; b->size = c.size;
	struct drm_mode_map_dumb m = { .handle = c.handle };
	if (drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &m)) { perror("MAP_DUMB"); return -1; }
	b->map = mmap(NULL, c.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, m.offset);
	if (b->map == MAP_FAILED) { perror("mmap"); return -1; }
	for (uint64_t i = 0; i < c.size / 4; i++) ((uint32_t *)b->map)[i] = colour;
	uint32_t handles[4] = { c.handle }, pitches[4] = { c.pitch }, offsets[4] = { 0 };
	if (drmModeAddFB2(fd, mode.hdisplay, mode.vdisplay, DRM_FORMAT_XRGB8888, handles, pitches, offsets, &b->fb, 0)) {
		perror("AddFB2"); return -1;
	}
	return 0;
}

static void buf_destroy(struct buf *b, const char *why)
{
	double t0 = now_s();
	int r = drmModeRmFB(fd, b->fb);
	double t1 = now_s();
	if (b->map) munmap(b->map, b->size);
	struct drm_mode_destroy_dumb d = { .handle = b->handle };
	drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &d);
	if (why) {
		printf("[%.3f] %s: RmFB(fb %u) -> %d (took %.1f ms), GEM destroyed\n", t1, why, b->fb, r, (t1 - t0) * 1000);
		fflush(stdout);
	}
	memset(b, 0, sizeof(*b));
}

static int flip_done;
static void flip_handler(int f, unsigned int seq, unsigned int sec, unsigned int usec, void *data) { (void)f; (void)seq; (void)sec; (void)usec; (void)data; flip_done = 1; }

/* page-flip to b and wait for the completion event; returns the event latency in ms or -1 on timeout */
static double flip_wait(struct buf *b)
{
	drmEventContext ev = { .version = 2, .page_flip_handler = flip_handler };
	double t0 = now_s();
	flip_done = 0;
	if (drmModePageFlip(fd, crtc_id, b->fb, DRM_MODE_PAGE_FLIP_EVENT, NULL)) { perror("PageFlip"); return -1; }
	while (!flip_done) {
		fd_set fds; FD_ZERO(&fds); FD_SET(fd, &fds);
		struct timeval tv = { 1, 0 };
		int r = select(fd + 1, &fds, NULL, NULL, &tv);
		if (r <= 0) { printf("[%.3f] no flip event within 1 s\n", now_s()); return -1; }
		drmHandleEvent(fd, &ev);
	}
	return (now_s() - t0) * 1000;
}

static int test_disable(void)
{
	struct buf red = { 0 }, green = { 0 };
	if (buf_create(&red, 0x00ff0000) || buf_create(&green, 0x0000ff00)) return 1;
	if (drmModeSetCrtc(fd, crtc_id, red.fb, 0, 0, &conn_id, 1, &mode)) { perror("SetCrtc"); return 1; }
	printf("[%.3f] disable: red on screen (fb %u)\n", now_s(), red.fb); fflush(stdout);
	sleep(1);
	double lat = flip_wait(&green);
	printf("[%.3f] disable: flipped to green (fb %u), event after %.1f ms\n", now_s(), green.fb, lat); fflush(stdout);
	sleep(1);
	printf("[%.3f] disable: removing the green FB while it is displayed...\n", now_s()); fflush(stdout);
	buf_destroy(&green, "disable");
	printf("[%.3f] disable: sleeping 3 s: a DE fault now means the DE ignored the plane disable\n", now_s()); fflush(stdout);
	sleep(3);
	if (drmModeSetCrtc(fd, crtc_id, red.fb, 0, 0, &conn_id, 1, &mode)) perror("SetCrtc(red again)");
	printf("[%.3f] disable: red set again\n", now_s()); fflush(stdout);
	sleep(1);
	buf_destroy(&red, "disable/cleanup");
	return 0;
}

static int test_flipfree(int frames)
{
	struct buf prev = { 0 }, cur = { 0 };
	double worst = 0, sum = 0; int timeouts = 0;
	if (buf_create(&prev, 0x00000080)) return 1;
	if (drmModeSetCrtc(fd, crtc_id, prev.fb, 0, 0, &conn_id, 1, &mode)) { perror("SetCrtc"); return 1; }
	printf("[%.3f] flipfree: %d frames, freeing the previous buffer right after each flip event\n", now_s(), frames);
	fflush(stdout);
	for (int i = 0; i < frames; i++) {
		if (buf_create(&cur, (i & 1) ? 0x00ffffff : 0x00202020)) return 1;
		double lat = flip_wait(&cur);
		if (lat < 0) timeouts++; else { sum += lat; if (lat > worst) worst = lat; }
		buf_destroy(&prev, i % 30 == 0 ? "flipfree" : NULL);   /* prints every 30th */
		prev = cur; memset(&cur, 0, sizeof(cur));
	}
	printf("[%.3f] flipfree: done, event latency avg %.1f ms worst %.1f ms, %d timeouts\n", now_s(),
	       frames - timeouts ? sum / (frames - timeouts) : 0, worst, timeouts);
	fflush(stdout);
	sleep(2);
	buf_destroy(&prev, "flipfree/cleanup");
	return 0;
}

static int set_dpms(drmModeConnector *conn, int on)
{
	for (int i = 0; i < conn->count_props; i++) {
		drmModePropertyRes *p = drmModeGetProperty(fd, conn->props[i]);
		if (!p) continue;
		int is_dpms = !strcmp(p->name, "DPMS");
		drmModeFreeProperty(p);
		if (is_dpms) return drmModeConnectorSetProperty(fd, conn->connector_id, conn->props[i], on ? DRM_MODE_DPMS_ON : DRM_MODE_DPMS_OFF);
	}
	fprintf(stderr, "no DPMS property\n");
	return -1;
}

static drmModeConnector *g_conn;

static int test_dpms(void)
{
	struct buf red = { 0 }, green = { 0 };
	if (buf_create(&red, 0x00ff0000) || buf_create(&green, 0x0000ff00)) return 1;
	if (drmModeSetCrtc(fd, crtc_id, red.fb, 0, 0, &conn_id, 1, &mode)) { perror("SetCrtc"); return 1; }
	printf("[%.3f] dpms: red on screen (fb %u)\n", now_s(), red.fb); fflush(stdout);
	sleep(1);
	int r = set_dpms(g_conn, 0);
	printf("[%.3f] dpms: DPMS off -> %d\n", now_s(), r); fflush(stdout);
	sleep(1);
	r = drmModePageFlip(fd, crtc_id, green.fb, 0, NULL);
	printf("[%.3f] dpms: page flip to green while off -> %d (%s)\n", now_s(), r, r ? strerror(-r) : "accepted"); fflush(stdout);
	sleep(1);
	buf_destroy(&red, "dpms (red freed while off)");
	r = set_dpms(g_conn, 1);
	printf("[%.3f] dpms: DPMS on -> %d; sleeping 3 s (fault = DE came back on the freed red buffer)\n", now_s(), r); fflush(stdout);
	sleep(3);
	if (drmModeSetCrtc(fd, crtc_id, green.fb, 0, 0, &conn_id, 1, &mode)) perror("SetCrtc(green)");
	sleep(1);
	buf_destroy(&green, "dpms/cleanup");
	return 0;
}

static int test_modeset(void)
{
	struct buf red = { 0 }, green = { 0 };
	if (buf_create(&red, 0x00ff0000) || buf_create(&green, 0x0000ff00)) return 1;
	if (drmModeSetCrtc(fd, crtc_id, red.fb, 0, 0, &conn_id, 1, &mode)) { perror("SetCrtc"); return 1; }
	printf("[%.3f] modeset: red on screen (fb %u)\n", now_s(), red.fb); fflush(stdout);
	sleep(1);
	double t0 = now_s();
	int r = drmModeSetCrtc(fd, crtc_id, green.fb, 0, 0, &conn_id, 1, &mode);
	printf("[%.3f] modeset: SetCrtc(green) -> %d in %.1f ms; freeing red now\n", now_s(), r, (now_s() - t0) * 1000); fflush(stdout);
	buf_destroy(&red, "modeset");
	printf("[%.3f] modeset: sleeping 3 s (fault = the DE still scans the freed red buffer)\n", now_s()); fflush(stdout);
	sleep(3);
	buf_destroy(&green, "modeset/cleanup");
	return 0;
}

int main(int argc, char **argv)
{
	const char *node = "/dev/dri/card0", *tty = "/dev/tty3";
	int frames = 120, opt, ttyfd = -1, ret = 0;
	while ((opt = getopt(argc, argv, "n:t:f:")) != -1) {
		if (opt == 'n') node = optarg; else if (opt == 't') tty = optarg; else if (opt == 'f') frames = atoi(optarg);
	}
	const char *what = optind < argc ? argv[optind] : "both";

	fd = open(node, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror(node); return 1; }
	if (drmSetMaster(fd)) { perror("drmSetMaster (another master alive? chvt away from the compositor first)"); return 1; }
	drmModeRes *res = drmModeGetResources(fd);
	if (!res) { perror("GetResources"); return 1; }
	drmModeConnector *conn = NULL;
	for (int i = 0; i < res->count_connectors && !conn; i++) {
		drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
		if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes) conn = c; else drmModeFreeConnector(c);
	}
	if (!conn) { fprintf(stderr, "no connected connector with modes\n"); return 1; }
	conn_id = conn->connector_id;
	mode = conn->modes[0];
	crtc_id = 0;
	if (conn->encoder_id) {
		drmModeEncoder *e = drmModeGetEncoder(fd, conn->encoder_id);
		if (e) { crtc_id = e->crtc_id; drmModeFreeEncoder(e); }
	}
	if (!crtc_id) crtc_id = res->crtcs[0];
	drmModeCrtc *cur = drmModeGetCrtc(fd, crtc_id);
	if (cur && cur->mode_valid) mode = cur->mode;
	printf("connector %u crtc %u mode %ux%u@%u, current fb %u\n", conn_id, crtc_id, mode.hdisplay, mode.vdisplay,
	       mode.vrefresh, cur ? cur->buffer_id : 0);
	if (cur) drmModeFreeCrtc(cur);

	/* keep fbcon off this VT while we own the display (an fbcon pan would block on the DRM master's flush) */
	ttyfd = open(tty, O_RDWR | O_CLOEXEC);
	if (ttyfd >= 0) ioctl(ttyfd, KDSETMODE, KD_GRAPHICS); else perror(tty);

	g_conn = conn;
	int all = !strcmp(what, "all"), both = !strcmp(what, "both");
	if (!strcmp(what, "disable") || both || all) ret |= test_disable();
	if (!strcmp(what, "flipfree") || both || all) ret |= test_flipfree(frames);
	if (!strcmp(what, "dpms") || all) ret |= test_dpms();
	if (!strcmp(what, "modeset") || all) ret |= test_modeset();

	/* ORDER MATTERS: master first, console mode second (see LESSON in the header) */
	drmModeFreeConnector(conn);
	drmModeFreeResources(res);
	drmDropMaster(fd);
	close(fd);
	fd = -1;
	if (ttyfd >= 0) { ioctl(ttyfd, KDSETMODE, KD_TEXT); close(ttyfd); }
	printf("[%.3f] done (%s), master dropped, console restored\n", now_s(), ret ? "with errors" : "ok");
	return ret;
}
