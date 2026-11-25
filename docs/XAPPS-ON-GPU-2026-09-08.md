# X11 apps on the GPU — solved 2026-09-08 (glmark2-es2 1267)

`XWAYLAND_NO_GLAMOR=1` was the whole blocker. Removing it gives X11 clients under
Xwayland hardware acceleration on the PowerVR.

```
client with Fedora's mesa :  llvmpipe                     glmark2  136
client with the DDK       :  PowerVR B-Series BXM-4-64    glmark2 1267
                             GL_VENDOR: Imagination Technologies
GPU faults during the run :  0
```

9.3x. Measured on the `kernel-a7a` RPM kernel at GPU 1008 MHz / 800 mV, KWin
6.7.4 compositing on the DDK.

## Why it was blocked

No glamor means no DRI3. Without DRI3 an X client cannot obtain a GPU device at
all, so it falls back to software no matter what libraries it is given. The
environment was never the problem; the missing DRI3 was.

## The depth-30 message is not fatal — it never was

```
glamor: Implementation returned 0x1908/0x1401 read format/type for depth 30,
        expected 0x1908/0x8368.  Falling back to software.
```

This still appears, and it is harmless. From `glamor/glamor.c:glamor_add_format()`
in xwayland-24.1:

```c
if (!texture_only && (format != read_format || type != read_type)) {
    ErrorF("glamor: Implementation returned ... Falling back to software.\n");
    texture_only = TRUE;          /* this depth only */
}
f->texture_only = texture_only;   /* and carry on */
```

The function returns `void`. One format is marked texture-only; glamor keeps
running. Nothing on this board uses depth 30. The wording reads like a fatal
error and is not one — the previous handoff treated it as the blocker, and that
was a misreading.

The underlying cause of the message is still a DDK defect worth reporting: it
advertises `GL_EXT_texture_type_2_10_10_10_REV`, so glamor adds a depth-30
format, and then it answers the readback query with `GL_UNSIGNED_BYTE` (8-bit)
instead of `GL_UNSIGNED_INT_2_10_10_10_REV`. Third instance of the same pattern
after the missing EGL Wayland platform and the absent `VK_EXT_robustness2`:
capability advertised, not honoured.

## Glamor no longer locks the GPU

`XWAYLAND_NO_GLAMOR=1` was set because "its glamor on the DDK locked the GPU on
its first 3D job" (2026-09-07). That did not recur — glamor ran through a full
glmark2 with zero hardware recoveries. The dma-buf exporter fix
(`0005-pvrsrvkm-dmabuf-map-for-importer.patch`) is the likely reason: the lockup
was the IOMMU domain being destroyed by an imported buffer, not glamor itself.

## Board changes (2026-09-08)

`/etc/systemd/user/plasma-kwin_wayland.service.d/pvr.conf`:

```diff
- Environment=XWAYLAND_NO_GLAMOR=1
+ Environment=KWIN_DRM_PREFER_COLOR_DEPTH=24
```

`/usr/local/bin/Xwayland` — a wrapper adding `-glamor es`. `/usr/local/bin` is
ahead of `/usr/bin` in the session PATH and KWin resolves Xwayland through it.

`/usr/local/bin/drm-unstick` — built from `tools/gpu-debug/drm-unstick.c` and
installed properly. It previously lived in `/tmp` and vanished at each reboot,
which is the wrong place for a recovery tool.

`~/.config/kwinrc` `[Wayland] InputMethod=` — the on-screen keyboard
(`plasma-keyboard`) was aborting every few seconds, 36 times in one boot. Not
wanted on a board with a physical keyboard.

Backups: `pvr.conf.bak-20260908-1346`, `kwinrc.bak-20260908-1402`.

**Only the `XWAYLAND_NO_GLAMOR` removal is proven necessary.**
`KWIN_DRM_PREFER_COLOR_DEPTH=24` and the `-glamor es` wrapper were applied in the
same cycle and neither has been shown to matter. Remove them one at a time and
re-measure before treating either as required.

## The measurement trap, again

Two renderer readings in this session were wrong because `glxinfo` and `glmark2`
were run from an ssh shell with the default environment, which loads Fedora's
Mesa and reports llvmpipe regardless of what the X server is doing. A client only
reaches the DDK with:

```sh
LD_LIBRARY_PATH=/opt/pvr/lib LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri
```

Same trap as the "llvmpipe faked 115" note in the recovery repo. A score without
the `PowerVR` renderer line means nothing.

## Still open

- **Seamless.** Apps get the GPU only when launched with the two variables above
  — that is what `pvr-run` does. Making it automatic means `pvr-desktop all`
  (the DDK system-wide via `/etc/ld.so.conf.d/00-pvr.conf`), which would then
  shadow Fedora's Mesa for Wayland-native clients, and those cannot use the DDK
  at all (no Wayland platform). That is a real trade-off, not an oversight.
- Trim the config to the one change that is required.
- Wayland-native apps remain in software. Both routes are now closed with
  reasons: the DDK's EGL has no Wayland platform, and zink cannot bridge it
  because the PowerVR Vulkan driver does not implement `VK_EXT_robustness2`
  (tested 2026-09-08; the driver enumerates fine and reports 129 extensions,
  robustness2 is not among them).

## Real applications need three things, not one (2026-09-08)

Getting glamor working is necessary but not sufficient. SuperTuxKart, installed
from Fedora and launched normally, still rendered in software at **418% CPU**
even with the DDK on its library path. Three separate limitations have to be
defeated at once:

```sh
SDL_VIDEODRIVER=x11         # the DDK has no Wayland platform: a Wayland client
                            # cannot reach the GPU at all, whatever it is given
SDL_VIDEO_X11_FORCE_EGL=1   # the DDK has no GLX: an X11 client using GLX stays
                            # on system mesa/llvmpipe (recovery repo said this)
pvr-run <game>              # LD_LIBRARY_PATH=/opt/pvr/lib + LIBGL_DRIVERS_PATH
```

Result on SuperTuxKart:

```
before  /usr/lib64/libGL.so.1.7.0            CPU 418%
        /usr/lib64/libGLX_mesa.so.0.0.0

after   /opt/pvr/lib/libEGL.so.1             CPU  47%
        /opt/pvr/lib/libGLESv2_PVR_MESA.so
        /opt/pvr/lib/libGLESv2.so.2
```

Nine times less CPU, and playable. Confirmed on screen by the owner.

The second variable is the one that is easy to miss. The game had the DDK on its
path and ignored it, because SDL on X11 asks for GLX by default and the DDK
implements only EGL/GLES. `SDL_VIDEO_X11_FORCE_EGL=1` needs SDL 2.0.18+.
GTK/GDK applications need `GDK_BACKEND=x11` for the first of the three; Qt
applications need `QT_QPA_PLATFORM=xcb`.

This is why "install a game and it runs on the GPU" is not true here and cannot
be made true with a single system-wide setting: the DDK cannot be the default
without breaking every Wayland client, so each application has to be routed
individually. The practical fix is generated `.desktop` overrides in
`~/.local/share/applications/` carrying the three variables - the menu entry then
works with a normal click. Not built yet.

## 2026-09-09 follow-up: trimmed to the one change, made durable

The 09-08 board edit was lost the same day: `pvr-desktop on` rewrites the
drop-in from `tools/board/pvr-desktop`, which still wrote `XWAYLAND_NO_GLAMOR=1`,
so the next re-run (during the KWin 6.7 / RPM-kernel work) silently put glamor
back off. The tool is now the source of truth and no longer sets it.

Measured with a second rootless Xwayland inside the live session (no re-login,
`/tmp/xw-glamor-test.sh`, glmark2-es2 in a 640x480 window on the DDK):

```
Xwayland default (-glamor unset)   : "Failed to make EGL context current with GL", then GLES
                                     DRI3 + Present up, PowerVR renderer, glmark2 1918, HWR +0
Xwayland -glamor es (the wrapper)  : same minus the two GL lines, glmark2 1874, HWR +0
```

So:
- `XWAYLAND_NO_GLAMOR` removal: the only required change (confirmed).
- `/usr/local/bin/Xwayland -glamor es` wrapper: not required (Xwayland falls back to
  GLES by itself; the wrapper's comment about llvmpipe answering the depth-30 query
  was a guess). Moved to `/etc/a7a/disabled/Xwayland-glamor-es-wrapper`.
- `KWIN_DRM_PREFER_COLOR_DEPTH=24`: not required (the session running today never
  had it and composites fine).
- Which DRM device Xwayland gets from KWin under `KWIN_RENDER_NODES=/dev/dri/none`:
  card0 (one fd) and it works, because the DDK's `sunxi-drm_dri.so` on card0 is the
  same path KWin uses; the DDK opens renderD128 internally (8 fds). The A7Z KWin
  patch (advertise the render node in linux-dmabuf feedback) is not needed here.
- Qt Quick on X11: `QT_QPA_PLATFORM=xcb QT_XCB_GL_INTEGRATION=xcb_egl pvr-run kinfocenter`
  maps the DDK's libEGL/libGLESv2/sunxi-drm_dri.so (not Mesa). Together with the
  SDL result above, EGL/GLES, Qt and SDL clients are all proven on the DDK.

What "seamless" needs from here is either the open stack (route A in
`docs/GPU-ROADMAP-2026-09-09.md`) or a per-platform vendor split (glvnd, route F):
the DDK cannot be the system default without taking EGL away from Wayland clients.
