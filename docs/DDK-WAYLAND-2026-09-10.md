# The vendor DDK with a Wayland window system: Imagination's open Mesa glue rebuilt (route D, 2026-09-10)

Goal (owner's brief): a Wayland-capable vendor userspace, so the DDK boot runs Wayland apps, X11 apps and Vulkan on
the GPU with no per-app configuration. Starting point: the DDK's window-system layer is open source. Imagination's
DDK ships with a patched Mesa: `src/gallium/frontends/pvr/` is the DRI glue that dlopens the closed
`libpvr_dri_support.so` and negotiates a versioned interface, and `src/pvr/wsi/` is the Mesa-side WSI of the closed
Vulkan driver `libVK_IMG`, with `pvr_wsi_wayland.c` and `pvr_wsi_x11.c`. TI publishes it at
github.com/TexasInstruments/mesa. Radxa built it with the X11 platform only, which is why the DDK had no Wayland.

Board: radxa-cubie-a7a. Open stack default (`fedora-6.6.98-a7apvr.202609091821`), DDK kernel
`fedora-6.6.98-a7a.202609091931` on entry 2, DDK userspace in `/opt/pvr` (24.2@6603887). Board times are 3 h behind
the laptop's.

## 1. The interface check and the branch (board, open-stack boot, 2026-09-10 16:20-16:40 board time)

### What Radxa's `/opt/pvr` build is

| item | finding | how |
|---|---|---|
| Mesa version | **24.0.1** | `strings dri/pvr_dri.so`: "Mesa 24.0.1 implementation error" |
| prefix / libdir | `/usr/local`, `lib` (not lib64) | `lib/pkgconfig/{dri,egl,gbm}.pc` |
| window systems | X11 (+ gbm, surfaceless, device); **no Wayland** | libEGL carries "Wayland platform not built" and links no libwayland; `libpvr_mesa_wsi.so` links xcb only |
| LLVM | none (swrast = softpipe) | `pvr_dri.so` links no libLLVM; no `llvmpipe`/`gallivm` strings |
| APIs | GLES1 + GLES2 over a shared glapi, GBM, no GLX (no `libGL.so`), no glvnd | file list |
| DRI drivers | one 15 MB megadriver: `pvr_dri.so` = `sunxi-drm_dri.so` = `swrast_dri.so` = `kms_swrast_dri.so` | `cmp`, sizes |
| the closed side | `libpvr_dri_support.so` exports ONLY `PVRDRIRegisterVersionedCallbacksV2`; it dlopens `libGLESv1_CM_PVR_MESA.so`, `libGLESv2_PVR_MESA.so`, `libGL_PVR_MESA.so`, `libPVROCL.so`, `libsrv_um.so` by name | `nm -D`, `strings` |
| desktop GL | **`libGL_PVR_MESA.so` is NOT in Radxa's drop** (the support library's "Up to version 3.3 of OpenGL 3 is supported" is the version check in front of a module that is not shipped). Not on the laptop either (Debian-card extras in output/ddk), not in the two public A733 repos (cuihuir/radxa-a7z-debian12, ayiejosh/a733-powervr-fex) | `ls`, `find`, GitHub trees |
| GLES ABI towards Mesa | `libGLESv2_PVR_MESA.so` imports no Mesa or glapi symbols (it needs `libglapi.so.0` only as DT_NEEDED; its imports are the DDK's own `KEGL*`, `GLSL*`) | `nm -D --undefined-only` |
| Vulkan WSI | `libVK_IMG.so` dlopens `libpvr_mesa_wsi.so` by name and calls `pvr_mesa_wsi_init/get_version/sym_addr/...` | `strings` |

So the only interface between an open Mesa build and the closed libraries is (a) the versioned DRI support interface
and (b) the WSI library interface (`pvr_mesa_wsi_interface.h`, versions 0-4); plus the `libglapi.so.0` soname.

### The DRI support interface, negotiated with the real library

`tools/mesa-img/pvr-dri-negotiate.c` performs the glue's registration handshake against the board's
`/opt/pvr/lib/libpvr_dri_support.so`, without a GPU context. The handshake: `PVRDRICompatInit` in the glue's
`pvrcompat.c` calls `PVRDRIRegisterVersionedCallbacksV2(callbacks, version, min)`; the library calls back
`RegisterSupportInterface(interface, version, min)`. Every combination runs in its own process.

```
callbacks v5 (min 0): ACCEPTED support interface offered: v8 (min 0); 24.0.1 glue accepts it
  functions set per part: v0=51/51 v1=1/1 v2=1/1 v4=1/1 v5=3/4 v6=1/1 v7=1/1 v8=1/1
callbacks v0 (min 0): ACCEPTED support interface offered: v1 (min 0)
callbacks v1..v8 (min 0): ACCEPTED support interface offered: v8 (min 0)
callbacks v5 (min 1..5): refused  no support interface offered
```

- TI 24.0.1's glue (`pvrdri.c`: `PVRDRICompatInit(&sDRICallbacksV2, 5, 0)`) registers callbacks **v5, min 0**:
  accepted.
- The library offers support interface **v8, min 0**. `MODSUPRegisterSupportInterfaceV2` in 24.0.1 accepts any
  version with minimum 0 and knows v0-v8, so it copies the whole structure. Every entry point is present except one
  of v5's four (the glue probes `SetInFenceFD` with `DRISUPHaveSetInFenceFd`; the others fall back to NULL results).
- The library also accepts callbacks up to v8, and refuses any minimum above 0.

### Which branch: 24.0.1

TI has `powervr/23.2.1`, `powervr/24.0.1` and `powervr/25.2.8` (plus older kirkstone/dunfell branches).

- **24.0.1** is the Mesa version Radxa's working `/opt/pvr` build reports. Its glue offers callbacks v5 and takes
  interface v0-v8, and the 24.2 library offers exactly v8: a complete match, with no entry point unused on either
  side. The same glue ABI already runs on this board: DDK X11 glmark2-es2 1865, KWin on the DDK.
- **25.2.8** is a different design. The glue moved from `src/gallium/frontends/pvr` into a real gallium driver
  (`src/gallium/drivers/pvr/*`) plus a DDK winsys (`src/gallium/winsys/pvr/ddk/`), with support interface v0-v10
  and callbacks v6. It would negotiate down to v8 (minimum 0 on both sides), but it is written for a newer DDK,
  untested with 24.2, and none of its additions (v9, v10) exist in this library.
- **23.2.1** is older than what Radxa built and knows fewer interface versions.

Pinned: `powervr/24.0.1` @ `7c82c1eebc67` (2026-03-13, "c11/threads: fix build on c23"), shallow clone in
`/home/radxa/mesa-img/mesa-24.0.1` on the board.

## 2. Build (tools/mesa-img/build-mesa-img.sh, on the board)

Meson options (final, after reviewer pass 1), matching Radxa's build where it had one: `--prefix=/opt/pvr-wl
--libdir=lib -Dplatforms=x11,wayland -Dgallium-drivers=pvr,swrast -Dgallium-pvr-alias=sunxi-drm -Dvulkan-drivers=pvr
-Degl=enabled -Dgbm=enabled -Dglx=disabled -Dglvnd=false -Dgles1=enabled -Dgles2=enabled -Dshared-glapi=enabled
-Dllvm=disabled`. Everything else (VA, VDPAU, XA, nine, OpenCL, tools, tests, valgrind, libunwind, lm_sensors) is off.
- `-Dpvr-vulkan-display` defaults to the gallium alias (sunxi-drm).
- `-Dllvm=disabled` also sidesteps Mesa 24.0 against Fedora 44's LLVM 22.
- The first build used the brief's `-Dplatforms=wayland,x11 -Dglx=dri`; step 3 and the review changed both:
  - GLX fails every context here, because the DDK has no desktop GL;
  - x11 first keeps EGL's default native platform the same as Fedora's.

Build dependency added on the board: `systemd-devel` (libudev, used by the Vulkan display WSI; Radxa's
`libpvr_mesa_wsi.so` links it). The script checks the stage and refuses it on any failure: libEGL must carry the
Wayland platform, `libpvr_mesa_wsi.so` must link libwayland-client, both loader entry points must be exported, and
there must be no `libGL.so.1`. Then it stages a tarball for the RPM, which is the only install path.

One source edit was needed (the script applies it to a pristine tree, `build-pvr-vulkan.sh` style). The first build
stopped in `src/gallium/targets/dri/target.c`, `<command-line>: error: expected '=' ... before '-' token`, because
`DEFINE_LOADER_PVR_ALIAS_ENTRYPOINT(GALLIUM_PVR_ALIAS)` pastes the alias into the loader entry point
`__driDriverGetExtensions_<alias>`, and `sunxi-drm` is not a C identifier. Mesa's loader maps `-` to `_` when it looks
the symbol up (`loader_get_extensions_name`), and Radxa's `pvr_dri.so` exports `__driDriverGetExtensions_sunxi_drm`,
so Radxa made the same change. Only the entry point's define is underscorified
(`src/gallium/drivers/pvr_alias/meson.build`: `gallium_pvr_alias.underscorify()`). The file name `sunxi-drm_dri.so`
and the two string uses keep the kernel's name: `PVR_DRIVER_ALIAS` in `pvrdri.c` and `PVR_DISPLAY_DRIVER` in
`pvr_wsi.c` are compared with `drmGetVersion()->name`, and Radxa's `libpvr_mesa_wsi.so` carries the string
`sunxi-drm`. Radxa's library also leaks the vendor build path, `.../binary_sunxi_linux_xorg_release/...`: the closed
userspace is the DDK's "xorg" window-system build, whose window-system work is done by this Mesa glue.

### Result (board, 16:43-16:51 board time)

- 1117 ninja steps in 7 min 21 s on the board; stage = 60 files and links, 24 MB installed, tarball 6.9 MB
  (`mesa-img-a7a-24.0.1-7c82c1eebc67-202609101650.tar.gz`). `libglapi.so.0` exports are identical to Radxa's (the
  closed GLES modules only need its soname). DRI megadriver names: `pvr`, `sunxi-drm`, `swrast`, `kms_swrast`, plus
  TI's `tidss`, `tilcdc`, `omapdrm` (unused here).
- ELF dependencies outside the package and the DDK: 27 sonames from 18 Fedora packages (glibc, libgcc, libstdc++,
  expat, zlib-ng-compat, libzstd, libdrm, systemd-libs, libwayland-client/-server, libxcb, libX11, libX11-xcb,
  libXext, libXfixes, libXxf86vm, libxshmfence, xcb-util-keysyms). `ldd` of every file with
  `/opt/pvr-wl/lib:/opt/pvr/lib`: nothing missing.
- Unpacked into `/opt/pvr-wl` on the board, on no library path, with no config changed.

### Checks without a GPU context (open-stack boot, `tools/mesa-img/test-ddk-wayland.sh --pre`)

These run on either kernel: `eglQueryString(EGL_NO_DISPLAY)` and `vkEnumerateInstanceExtensionProperties` through
ctypes, with no display, device or driver screen.

- **EGL client extensions** of the new libEGL: `EGL_EXT_platform_wayland`, `EGL_KHR_platform_wayland`, plus
  `x`/`xcb`/`gbm`/`device`/`surfaceless`. Radxa's has no Wayland platform.
- **Loader cache order.** A private cache built from `/opt/pvr-wl/lib` + the system conf (`ldconfig -X -C`, nothing
  written to /etc) lists `/opt/pvr-wl/lib` first for every duplicate soname: libEGL.so.1, libgbm.so.1,
  libGLESv2.so.2, libGL.so.1. `libglapi.so.0` exists only there. An ld.so.conf.d entry therefore puts this stack in
  front of glvnd's for every process, including capability-bearing ones (which ignore LD_LIBRARY_PATH but use the
  cache).
- **Vulkan: the closed ICD decides the surface list.** With the DDK ICD, the instance extensions are
  `VK_KHR_surface`, `VK_KHR_xcb_surface`, `VK_KHR_xlib_surface`, `VK_EXT_headless_surface` and
  `VK_KHR_get_surface_capabilities2`. There is no `VK_KHR_wayland_surface`, although the new `libpvr_mesa_wsi.so` is
  built with Wayland (its strings carry `VK_KHR_wayland_surface`, `VK_KHR_display` ...). `strings` of
  `libVK_IMG.so.1` shows only those five surface names plus the swapchain ones: the "xorg" DDK build compiled its
  instance-extension list without Wayland. The WSI library implements the surface work (support, formats, present
  modes, swapchain), but the ICD never advertises the Wayland surface. One route stays open without changing the
  blob. The Vulkan loader implements `vkCreateWaylandSurfaceKHR` itself (a `VkIcdSurfaceWayland` handed to each
  driver), and it only needs one driver that lists the extension to accept it at `vkCreateInstance`. With Fedora's
  lavapipe loaded next to the DDK, the PowerVR device may present to Wayland through the Mesa WSI. The DDK-boot test
  covers that (`vulkaninfo` "Presentable Surfaces" per GPU, `vkcube --wsi wayland`).

### Package

`tools/build-mesa-img-rpm.sh` → `mesa-img-a7a-24.0.1-202609101650.aarch64.rpm`: 5.2 MB, 86 files, 7 tests in
tests/test_mesa_img.py.
- Everything lives under `/opt/pvr-wl`. `AutoReqProv: no`: it never provides `libEGL.so.1()` & co to dnf, and its
  `Requires` are the 18 Fedora packages above.
- `lib/ddk/` holds seven links to the closed libraries the glue and the GLES modules load: `libpvr_dri_support`,
  `libGLESv2_PVR_MESA`, `libGLESv1_CM_PVR_MESA`, `libsrv_um`, `libglslcompiler`, `libusc`, `libufwriter`. All have
  unversioned sonames. `libVK_IMG.so.1` is reached by absolute path from the ICD JSON and dlopens `libpvr_mesa_wsi.so`
  by name. So a library path entry exposes exactly these, and not the rest of `/opt/pvr/lib` (Radxa's older
  `libvulkan.so.1` 1.3.280, `libxcvt`, the opencv aliases, its X11-only libEGL/libgbm).
- `/etc/ld.so.conf.d/00-a7a-pvr-wl.conf` and `/etc/vulkan/icd.d/a7a-ddk-powervr_icd.json` are `%ghost`
  (`%attr 0644`): the session unit writes them on the DDK boot, an erase removes them, `%post`/`%postun` run
  ldconfig.

## 3. The DDK boot (shared with the GPU-gap session)

Owner's go at 19:55 laptop time. `a7a-gpu-check` before, on the open stack: 0 fail, 0 warn. `backtoddk.sh`: default
`fedora-6.6.98-a7a.202609091931`, reboot. Order on this boot:
1. The owner logs in on the stock DDK session config.
2. The GPU-gap session's `a7a-gpu-gap snapshot --bench --bench-full` (~25 min, board idle, nothing else running).
3. `tools/mesa-img/test-ddk-wayland.sh`, per-process `/opt/pvr-wl` via `pvr-wl-run`; nothing system-wide.

### Results (board 17:16-17:35 board time)

`tools/mesa-img/test-ddk-wayland.sh`, per-process `/opt/pvr-wl`; logs in /home/radxa/mesa-img/test-ddk-1716 and
test-vk-1733. HWR 0 before, after every group, at the end.

| test | result |
|---|---|
| EGL client platforms (new libEGL) | wayland, x11, xcb, gbm, device, surfaceless |
| `eglinfo -B` | **Wayland**, GBM, X11, surfaceless, device: OpenGL ES 3.2 build 24.2@6603887 on "PowerVR B-Series BXM-4-64". No desktop GL profile on the PowerVR (the device list's softpipe entry offers GL 3.3). EGL client APIs `OpenGL_ES`, same as Radxa's build |
| `eglinfo -B` without `MESA_LOADER_DRIVER_OVERRIDE` | identical: **no override needed** on any platform |
| glmark2-es2-wayland 640x480, build+texture | **2008** on the PowerVR (open stack: 351); without the override 2003 |
| glmark2-es2 X11 640x480, build+texture | 1936 Radxa's stack (`pvr-run`), 1944 ours |
| glmark2 full suite, 800x600 | Wayland native **1216**, X11 on Radxa's stack **1204**, same boot. The 1865 of 2026-09-09 came from a different invocation; the same-boot pair is the comparison |
| `glxinfo -B`, our GLX (`-Dglx=dri`) | `X Error ... BadValue ... X_GLXCreateNewContext`: no desktop-GL module. Fedora's glvnd GLX: llvmpipe, GL 4.6 |
| glmark2-wayland (a desktop-GL client), ours | `eglChooseConfig() didn't return any configs`, exits. Fedora's Mesa today: llvmpipe, score 227 |
| SuperTuxKart `--benchmark`, native Wayland (`SDL_VIDEODRIVER=wayland`) | GLES 3.2 on the PowerVR, "GLSL supported", **0 fixed-pipeline warnings = the modern renderer**; 1165 frames in 38.1 s = **30.6 fps** average (steady 19, typical 29). Open stack: 4.1 fps on the fixed pipeline |
| SuperTuxKart, X11 on Radxa's stack (`pvr-run` + x11 + `SDL_VIDEO_X11_FORCE_EGL`) | 1183 frames in 38.1 s = 31.0 fps (steady 21, typical 29) |
| Vulkan device | "PowerVR B-Series BXM-4-64 MC1", "PowerVR B-Series Vulkan Driver"; instance surfaces: xcb, xlib, headless only |
| `vkcube --wsi xcb` | **segfault** in `wsi_common_get_images` (`libpvr_mesa_wsi.so`) at the first swapchain: with Radxa's loader + Radxa's WSI as well, so it predates this work |
| `vkcube --wsi wayland`, DDK + lavapipe, Fedora's loader | the loader accepts `VK_KHR_wayland_surface` (lavapipe lists it), but the PowerVR is **not presentable** on Wayland (`vulkaninfo`: only llvmpipe lists the Wayland surface), and vkcube picks llvmpipe |

What this means:
1. **Wayland EGL/GLES on the vendor stack works**, at X11 speed, on every platform, with no per-app variable. Games
   that use GLES (SuperTuxKart) get the modern renderer natively on Wayland.
2. **Desktop OpenGL is not available from this DDK drop.** The module `libGL_PVR_MESA.so` is missing, so GLX
   contexts fail with BadValue and EGL offers no GL configs. A desktop-GL-only app that runs on llvmpipe today
   cannot start with this stack in front.
3. **Vulkan presentation is blocked on the closed side.** `libVK_IMG` (the "xorg" DDK build) has no Wayland surface
   in its extension list, and the loader does not offer the device for a surface type its driver lacks.
   X11 swapchains crash in the Mesa WSI with Radxa's build as well. Radxa's `/opt/pvr/lib/libvulkan.so.1` is a
   Vulkan loader built without Wayland (`vkCreateWaylandSurfaceKHR` 0 times in its strings, 4 times in Fedora's
   1.4.341). The first Vulkan runs above had it on LD_LIBRARY_PATH through `pvr-wl-run`, which now uses the package's
   `lib/ddk` links instead.

### Reviewer pass 1 (independent, laptop only, before anything system-wide) and what was done

1. "Every Wayland client lands on softpipe: KWin's dmabuf feedback names card0, which has no render node":
   contradicted by the measurement (the no-override Wayland glmark2 ran on the PowerVR, 2003). It is re-checked after
   activation with a cache-only client (no LD_LIBRARY_PATH), which is the activation gate.
2. "The unit would activate the hand-unpacked `/opt/pvr-wl` (no `lib/ddk`) = software rendering everywhere":
   **fixed**. `wl_installed` requires `lib/ddk/libpvr_dri_support.so` and `lib/ddk/libsrv_um.so` to resolve (`-e`
   follows the links into /opt/pvr), `build-mesa-img.sh` lost `--install` (the RPM is the only install path), and a
   test covers it.
3. "`libGL.so.1` in the cached directory shadows glvnd's GLX": **confirmed** by the BadValue above, **fixed** with
   `-Dglx=disabled`. The build refuses to stage a `libGL.so.1`, and the RPM builder refuses a tarball carrying one
   (test).
4. "EGL's default native platform is wayland": **fixed** with `-Dplatforms=x11,wayland` (Fedora's order). The doc's
   earlier claim that it only matters for `EGL_DEFAULT_DISPLAY` was wrong: an `eglGetDisplay(<X11 Display*>)`
   without `EGL_PLATFORM` would have been read as a wl_display.
5. "The manual marker leaves the cache on the open kernel = KWin on the DDK's Mesa, black screen": **fixed**.
   `apply` computes the stack first and removes the pvr-wl entry on any stack but the DDK even with the marker
   (tests: open + marker = removed, DDK + marker = kept).
6. "The greeter's Qt app falls to softpipe (its KWin is QPainter, no linux-dmabuf)": observed in step 4. The greeter
   UI is `plasma-login.service` (`/usr/libexec/plasma-login-greeter`). Mitigation ready: `LD_LIBRARY_PATH=/usr/lib64`
   for that unit keeps it on Fedora's Mesa exactly as today.
7. "Files written under a strict umask fail `rpm -V`": **fixed** (`umask 022`; test under umask 077). `status` saying
   `mixed` after a runtime change until the next `apply` is intended.
8. "No-driver path skips `wl_off` when `pvr-desktop off` fails": **fixed** (`wl_off` first; test with a failing
   `pvr-desktop`).
9. `pvr-desktop all`'s `00-pvr.conf` next to ours: ours sorts first. Low risk, documented here.
10. Every closed DDK library carries `DT_RPATH=/usr/local/lib` (ahead of LD_LIBRARY_PATH and the cache). That is
    harmless today (only the `dri` symlink lives there) but worth knowing before anything lands in `/usr/local/lib`.
11. ldconfig deletes dangling *versioned* links; the seven `lib/ddk` names are unversioned (spec comment; test).
12. "The test helper cannot reproduce the system-wide layout": `pvr-wl-run` uses `lib/ddk` now, and step 4 adds
    cache-only runs.

Checked by the reviewer and found fine:
- `%ghost` on rpm 6.0.2: probe package under `unshare -r`; an erase removes a present ghost; `rpm -V` is clean
  absent and present at 0644.
- `dnf install` on the open boot changes nothing.
- The glapi families of this build, glvnd and Mesa 26 are disjoint.
- The `ldconfig -p` format matches the unit's grep.
- Unit idempotency and both crash windows.

**Dropped from the design after step 3: the system-wide Vulkan ICD.** With the DDK ICD in /etc/vulkan/icd.d, apps
that prefer an integrated GPU would pick a device that cannot present. They would fail, or crash on X11, where
today they fall back to lavapipe. Vulkan on the DDK stays per-app (`pvr-run`, offscreen/compute), and the ICD
`%ghost` left the spec.

Also checked on the board: Fedora Mesa 26's `libEGL_mesa.so.0` and every `/usr/lib64/dri/*_dri.so` need
`libgbm.so.1`, which the pvr-wl entry resolves to this build's 24.0.1 libgbm. `libGLX_mesa`, `libgallium` and
lavapipe do not, so GLX apps through glvnd stay on llvmpipe. Firefox's `libxul` links `libgbm.so.1` directly, so
Firefox is in the step-4 test list.

## 4. System-wide on the DDK boot (the design after review 1; applied only after the second pass)

`a7a-gpu-session` gains a `pvr-wl` part of the ddk config. It is active when all of these hold:
- the DDK config is on (`pvr.conf` exists: `pvr-desktop on` accepted the DDK userspace and the fdinfo-fixed
  pvrsrvkm);
- mesa-img-a7a is installed with resolvable `lib/ddk` links: `/opt/pvr-wl/lib/libEGL.so.1`,
  `lib/ddk/libpvr_dri_support.so`, `lib/ddk/libsrv_um.so`. The links point into /opt/pvr, so this also proves the
  DDK userspace is there;
- there is no `/etc/a7a/pvr-wl.disabled`.

Then the unit writes `/etc/ld.so.conf.d/00-a7a-pvr-wl.conf` (`/opt/pvr-wl/lib`, `/opt/pvr-wl/lib/ddk`) and runs
ldconfig. Every process gets this build's libEGL/libGLESv1_CM/libGLESv2/libgbm/libglapi ahead of glvnd's,
capability-bearing ones included, plus the seven closed libraries. The DRI driver comes from the build's own path
(`/opt/pvr-wl/lib/dri`); no environment variable is set anywhere. KWin keeps its drop-in (Radxa's /opt/pvr through
LD_LIBRARY_PATH, inherited by Xwayland), so the compositor side stays exactly as proven. No Vulkan ICD is installed
(see above).

The entry is removed on every other config: the open stack, no driver bound, the DDK software fallback, opted out,
and any non-DDK stack even under the manual marker. The cache is rebuilt whenever it still names
`/opt/pvr-wl/lib/libEGL.so.1`, so a crash between the two steps heals at the next boot. `status` prints `pvr-wl:`,
and `config` counts it: an open boot with a leftover is `mixed`, `match: no`. The package owns the entry as `%ghost`,
so an erase removes it. 14 new tests in tests/test_gpu_session.py (a fake ldconfig whose cache follows the entry) and
7 in tests/test_mesa_img.py; 302 pass.

**The trade-off on the DDK boot with pvr-wl active (the owner decides at activation):**
- Gains: every EGL/GLES client on Wayland and X11 runs on the PowerVR with no per-app setting (Qt Quick, GTK's GL
  renderer, SDL/GLES games, glmark2-es2, Firefox if its GLES path holds).
- Loses: desktop-GL-only clients (glmark2-wayland: llvmpipe 227 today, does not start with pvr-wl). Escapes: per app,
  `LD_LIBRARY_PATH=/usr/lib64 <app>` puts glvnd and Fedora's Mesa first again; for the whole boot,
  `touch /etc/a7a/pvr-wl.disabled` + `sudo a7a-gpu-session apply`.
- Unchanged: GLX apps (glvnd → llvmpipe), Vulkan (lavapipe; the DDK's Vulkan per app with pvr-run), the open-stack
  boot.

**Step-4 test plan on this boot (owner's go, 20:50: "Activate after review"):**
1. `sudo rm -rf /opt/pvr-wl`: the hand-unpacked first build, which still held its `libGL.so.1`.
2. `dnf install` mesa-img-a7a: `rpm -V` clean, no unowned file under /opt/pvr-wl.
3. `dnf install` a7a-gpu-tools built from this branch: the new unit script and the greeter UI drop-in.
4. `sudo a7a-gpu-session apply`: pvr-wl active, `status` match yes.
5. Cache-only run, `test-ddk-wayland.sh --system`: no pvr-wl-run, no LD_LIBRARY_PATH, no override, the session's own
   environment. It covers EGL, glmark2-es2-wayland (the activation gate: renderer PowerVR), glxinfo (glvnd/llvmpipe
   expected), kinfocenter (Qt Quick) and Firefox with their `/proc/<pid>/maps`, and SuperTuxKart started the way a
   menu launch would start it.
6. The owner logs out and in. The greeter stays on Fedora's Mesa by its drop-in; Plasma comes up with plasmashell on
   the PowerVR. `a7a-gpu-check`, 0 HWR.
7. `prepare616.sh`, the open boot: the unit removes the entry, the cache holds no /opt/pvr-wl, `a7a-gpu-check` 0/0,
   KWin on the a7a driver. Hand the board over.

Rollback at any point: `touch /etc/a7a/pvr-wl.disabled; sudo a7a-gpu-session apply`.

### Activation on the board (20:55-21:15 laptop time): works for GLES clients, breaks Fedora's Qt; rolled back, now opt-in

1-5 of the plan ran as written:
- the hand-unpacked tree removed (it did still hold `libGL.so.1`);
- mesa-img-a7a-24.0.1-202609101747 installed, `rpm -V` clean, no unowned file;
- a7a-gpu-tools-20260910-202609102059 installed;
- `systemctl restart a7a-gpu-session.service`: 0.38 s, result success; `pvr-wl: active`, `match: yes`; the entry
  0644 root, the cache listing `/opt/pvr-wl` first;
- `test-ddk-wayland.sh --system` (logs /home/radxa/mesa-img/test-system-1800), cache-only, 0 HWR throughout.

| client (cache-only, no variable) | result |
|---|---|
| `eglinfo -B` | Wayland, GBM, X11, surfaceless, device: OpenGL ES 3.2 on the PowerVR |
| glmark2-es2-wayland 640x480 build+texture | **2049** (PowerVR) |
| glmark2-es2 X11 through the cache | 1960 (Radxa per-app: 1954) |
| SuperTuxKart as a menu launch starts it (no SDL variable) | **30.7 fps**, GLES 3.2 on the PowerVR, modern renderer; X11 on Radxa's stack 31.2 |
| glxinfo (GLX) | llvmpipe 4.6 through glvnd, as designed |
| kinfocenter (Qt Quick), Firefox | map /opt/pvr-wl's libEGL + pvr_dri.so, but see below: neither rendered on the GPU |

Step 6 failed: **the owner's logout did not happen.** The user journal names the cause:
```
Started dbus-:1.2-org.kde.LogoutPrompt@2.service.
Cannot find EGLConfig, returning null config
Could not create EGL surface (EGL error 0x3005)
eglSwapBuffers failed with 0x300d, surface: 0x0
Failed to get focus after 3 seconds, quitting
```
The logout prompt is a Qt Quick program started at logout, so it was the first Qt window to meet the new stack.
**Fedora builds Qt for desktop OpenGL** (`libQt6Gui` links glvnd's `libOpenGL.so.0`/`libGLX.so.0`), so on Wayland Qt
binds `EGL_OPENGL_API` and asks for `EGL_OPENGL_BIT` configs. The DDK stack has none (no desktop-GL module). The
window gets no surface, the prompt quits, and logout never proceeds. Plasmashell after a re-login would have been
next.

Confirmed per process with `QT_FORCE_STDERR_LOGGING=1` (Qt otherwise logs to the journal, which is why the earlier
app check saw no error): `pvr-wl-run kinfocenter` prints exactly those three lines, `kinfocenter` on Fedora's Mesa
does not. Qt Quick's software backend (`QT_QUICK_BACKEND=software`) does not avoid the EGL config request.

**Correction.** The earlier "kinfocenter and Firefox on the PowerVR" came from `/proc/<pid>/maps` and is wrong.
Mapped is not rendering: kinfocenter had no config, and Firefox logged `Failed to create EGLContext!: 0x3000` (a
desktop-GL context first) and fell back to software WebRender. The app check must look at what the app logs, not
only what it maps.

Rolled back at once with the documented switch: the entry removed, the cache clean, `match: yes`. The owner's
running session never lost anything, since only newly started Qt windows were affected. Decision: **pvr-wl is
opt-in**. `a7a-gpu-session` writes the entry only when `/etc/a7a/pvr-wl.enabled` exists; without it the entry is
removed like on any other stack. So neither the DDK boot nor a fresh image switches it on by itself, and
`pvr-wl-run` (per app, for GLES clients and games) needs no entry at all. Tests: 29 in tests/test_gpu_session.py.

What this leaves for the DDK boot, measured:
- **Per app, today:** `pvr-wl-run <game>` puts a Wayland-native GLES client on the PowerVR. SuperTuxKart runs its
  modern renderer at 30.7 fps against 4.1 on the open stack. The old X11 detour (`SDL_VIDEODRIVER=x11`,
  `SDL_VIDEO_X11_FORCE_EGL`, `pvr-run`) is no longer needed.
- **System-wide:** only after desktop-GL clients are handled. Three ways, none measured yet:
  - (a) Imagination/Radxa ship the desktop-GL module `libGL_PVR_MESA.so` for 24.2@6603887 (the support library
    already has the GL 3.3 code path and asks for that file);
  - (b) the glue answers a desktop-GL request with a GLES 3.2 context. Qt and Firefox read the version string and
    switch to their ES paths. Apps that truly need desktop GL would then fail later instead of at config time;
  - (c) keep Qt on Fedora's Mesa (llvmpipe) and give only non-Qt clients the DDK. That is not possible with one
    loader cache.

### Reviewer pass 2 (on the fixes) and what was done

Verdict: nothing in the code blocked activation. It confirmed every fix from pass 1: umask, the `wl_off` order, the
marker on both stacks, no ICD, no libGL in the build or the package, x11 first, RPM-only install, `pvr-wl-run` on
`lib/ddk`.

1. "`dnf install` over the hand-unpacked tree keeps its `libGL.so.1`, and the first `apply` puts it in the cache": the
   board was already installed the safe way round (the tree removed first; it did hold `libGL.so.1`). **Defence
   added**: `wl_installed` refuses a tree with a `libGL.so.1`, `status` says "stray libGL" (test).
2. The greeter UI is `plasma-login.service` of the plasmalogin user's manager, not a system unit. **The mitigation is
   applied up front** as a static a7a-gpu-tools drop-in, `/etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf`
   (`LD_LIBRARY_PATH=/usr/lib64`). That is glvnd + Fedora's Mesa, today's greeter, on both boots. The open boot is
   unchanged, because its cache has no /opt/pvr-wl.
3. "An unexpected driver or kernel keeps the entry": **fixed**, `wl_off` runs before both `return 1` (test).
4. The test script's GLX label and Vulkan group: the GLX check is relabelled (glvnd, llvmpipe expected), and Vulkan
   runs only with `--vk`. `--system` is the cache-only mode, with the session environment and an app section.
5. Under the marker, one boot without a driver binding de-activates pvr-wl until the marker goes. Intended; the
   script header says so.
6. `-e` on the links, the status consumers (anchored sed) and the upgrade path on the hand-unpacked tree were all
   found fine. a7a-gpu-check now prints the `pvr-wl:` line.
7. The pvr-wl-run header (no GLX, Vulkan offscreen only) and the spec's leftover `libX11` are fixed.

## 5. Back on the open stack, end state (2026-09-10, 21:17 laptop time)

- The removal, tested for real. Just before the reboot pvr-wl was opted in and active (18 cache entries).
  `prepare616.sh` then rebooted onto `fedora-6.6.98-a7apvr.202609091821`. The unit at boot logged `applied the open
  config: nocap-disabled pvr.conf-removed pvr-wl-removed ldconfig cap-restored (was: ddk, now: open)`. The entry was
  absent, the cache named /opt/pvr-wl 0 times, and `match: yes`. Afterwards the opt-in marker was removed, so the
  next DDK boot stays on the proven config.
- Fedora's Mesa and the a7a driver are untouched: `mesa-pvr-a7a-26.1.8-202609091222`, `rpm -V` clean, the driver and
  its ICD dated 2026-09-09, Fedora's ICD still masked. The owner logged in on the open desktop: `a7a-gpu-check`
  **0 fail, 0 warn**, the compositor on the a7a driver, `pvr-wl: absent (off: opt-in ...)`.
- Installed on the board: `mesa-img-a7a-24.0.1-202609101747` (inert on the open stack) and
  `a7a-gpu-tools-20260910-202609102113` (a7a-gpu-session with opt-in pvr-wl, and the greeter UI drop-in).
  Superseded builds: the GLX build `mesa-img-a7a-...1650` is in output/rpms/attic.
- Image + emulator: the image stage, `emulate.sh`, the image stage code and the tests carry mesa-img-a7a (313
  tests), but the real run needs the main checkout. Its `tools/priv.sh` is the path in sudoers; from this session's
  worktree the stage fell back to a dry run. The GPU-gap session runs it from the main checkout after its open-stack
  window. That image carries that session's `mesa-pvr-a7a-26.1.8-202609101604`, validated on the board the same
  evening (ubo-check exact PASS, `a7a-gpu-check` 0/0; installed in /usr/local there).
- Open items: docs/NEXT-SESSION-PROMPT.md, "Route D".

## 6. After the handover: "slow as shit", and irqbalance undoing Item E 1(a) (2026-09-10, 21:35-21:50 laptop time)

The owner found the desktop very slow right after the handover. The measured causes:
- **The GPU-gap session's benchmarks on his logged-in desktop.** Its glmark2 snapshot ran 18:22-18:36 board time,
  then a SuperTuxKart benchmark at 121 % CPU (the open stack's fixed pipeline, ~4 fps). Stopped at the owner's
  request at 18:40. The handover should have warned him that the other session would bench on his desktop at once.
- **The Item E GPU-interrupt fix was gone this boot.** irq 484 sat at 0-7 (effective cpu0). The udev rule matches the
  bind (`udevadm test --action=bind` shows the RUN), udevd is not sandboxed for /proc/irq, and this entry has no
  initramfs. The cause is irqbalance (enabled by Fedora, started after udev): `systemctl restart irqbalance` moved 484
  from 6-7 back to 0-7 within 13 s. The fix shipped the same afternoon had never been through a reboot before this
  one; it was set by hand then.
- Not causes: KWin (pinned 6-7; its render, output and input threads are all there, and only Mesa's two SCHED_BATCH
  disk-cache threads keep 0-7), rsyslogd (0 % now; `/var/log/messages` is 806 MB because logrotate.service fails,
  a known failed unit), temperature (31-38 degC) and clocks (2800/3000).

Measured on a quiet board with 484 back on 6-7 (`speed-bench.sh client 3`): 640x480 **292 fps** / KWin 0.8 ms, and
1000x560 **39/39/39 fps** / 7.1 ms. That is exactly the Item E result. The 25-38 fps spread of the first 1000x560 run
was the tail of the benchmark load.

Fix (a7a-gpu-tools-20260910-202609102156, 317 tests): irqbalance gets a policy script. `a7a-irqbalance-policy` prints
`ban=true` for the GPU interrupt, identified by its /proc/interrupts name, only while powervr is bound. It is wired
in by the drop-in `/etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf`, which repeats ExecStart with
`--policyscript=` (an `Environment=` would lose to the unit's EnvironmentFile lines). Every other interrupt stays
irqbalance's, and so does the DDK's GPU interrupt.

`%posttrans` applies it on an upgrade: `daemon-reload` first, because rpm runs `%posttrans` before systemd's file
trigger, then `try-restart irqbalance`, then `a7a-gpu-irq-affinity` only while powervr is bound. `a7a-gpu-check` now
checks the GPU interrupt on the open stack.

Reviewer pass 3 found nothing blocking. It traced irqbalance 1.9.5's source: a GPU interrupt that appears after
irqbalance started goes through its hotplug path, gets the policy, and a banned IRQ is never moved. irqbalance
passes `(null)` as the device path for platform IRQs, which the script ignores (test). It rehearsed the script under
the unit's full sandbox, and the DDK boot is unchanged.

Board, installed over 2113: `rpm -V` clean, irqbalance's ExecStart names the policy script, 484 on 6-7. After a
deliberate `systemctl restart irqbalance`, 484 stayed on 6-7 (effective cpu6) at +5, +10, +15 and +20 s; before
the fix it flipped to 0-7 at +13 s. `a7a-gpu-check`: "GPU interrupt 484 on the big cluster (cpu 6)", 0 fail, 0 warn.
