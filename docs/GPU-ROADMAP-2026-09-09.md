# GPU roadmap: everything on the GPU, Wayland and Xwayland, seamlessly (2026-09-09)

Goal (owner's words): GPU working on both X11 and Wayland with acceleration, all hardware at full capacity,
changeable with the clocks app, able to install packages without breaking. "Seamless" = no per-application
environment variables, every toolkit (Qt, GTK, SDL, Chromium, games) lands on the GPU by itself.

Two stacks exist on the board. Both are kept; read `HANDOFF-WAYLAND.md` (DDK) and
`powervr-backport/HANDOFF-2026-09-09.md` (open stack) for the details behind every line below.

## 1. Where things stand, verified on hardware

| layer | vendor DDK (pvrsrvkm + /opt/pvr, 24.2@6603887) | open stack (drm/imagination v6.16 backport + Mesa pvr + zink) |
|---|---|---|
| kernel driver | patched pvrsrvkm, stable (patches 0001, 0005) | `6.6.98-a7apvr.<stamp>` built here: NO WEDGE any more (recovery fix); context resets after GPU runtime power-off -> GPU kept powered by udev rule; docs/OPEN-STACK-WEDGE-2026-09-09.md |
| compositor (KWin 6.7 Wayland) | ON THE GPU, 60 fps, 0 faults, needs drop-in env + cap_sys_nice stripped | on the GPU, survives resets; SLOW for client buffers taller than ~512 rows (zink re-uploads them through the CPU every frame, ~117 ms; the 1024x600 desktop surfaces are on the slow side) |
| Wayland-native clients (Qt/GTK/SDL Wayland) | software (llvmpipe): the DDK EGL/Vulkan have NO Wayland window system | ON THE GPU (Mesa EGL Wayland platform, zink GL, Vulkan) |
| X11 clients via Xwayland, EGL/GLES | ON THE GPU with `pvr-run` (glamor + DRI3 on the DDK; glmark2-es2 1267/1918 vs 136; Qt xcb_egl and SDL proven; docs/XAPPS-ON-GPU-2026-09-08.md) | Mesa, works without configuration (Xwayland on the PowerVR driver, 2026-09-09 evening) |
| X11 clients via Xwayland, GLX / desktop GL | llvmpipe: the DDK has no GLX and no desktop libGL | VERIFIED 2026-09-09 evening: glxinfo = zink Vulkan 1.2 (PowerVR BXM-4-64), direct rendering, GL 2.1; glxgears 59.5 fps (vsync); Xwayland's glamor itself on the PowerVR; no configuration |
| Vulkan clients | X11 only (VK_KHR_xcb/xlib_surface; no wayland_surface) | Mesa pvr Vulkan 1.2, 279 extensions, Wayland WSI |
| GPU clock | 1008 MHz @ 800 mV validated (clock-ceiling.sh), 600 proven, 1200 @ 920 mV untested | same DTB |
| package updates | safe: kernel-a7a RPM, versioned releases, dnf excludes Fedora kernels | the pvr kernel is hand-installed (entry fedora-6.6.98-pvr+) |

The DDK Vulkan reports 1.3.277 with VK_EXT_external_memory_dma_buf, VK_EXT_image_drm_format_modifier and
VK_EXT_physical_device_drm (checked 2026-09-09), the pieces a dma-buf-based presentation path needs.

## 2. Claims to take with a grain of salt (corrections found today)

2.1 "Xwayland's glamor falls back to software on the DDK because of the depth-30 read-format mismatch": WRONG.
    xserver `glamor/glamor.c` (xwayland-24.1 branch) only sets `texture_only = TRUE` for that one format and prints
    "Falling back to software"; glamor stays enabled. In today's glamor-allowed login Xwayland had loaded
    `/opt/pvr/lib/dri/sunxi-drm_dri.so`. The A7Z image measured 100% GPU utilisation from an X11 CopyArea workload
    with exactly this warning present. VERIFIED here 2026-09-08/09 with X clients (route B1).
2.2 "Wayland-native apps can never use the DDK; nothing on our side changes that": too strong. The window-system
    layer is missing, not the GPU access. Three ways around it exist (routes B, C, D below); none is free.
2.3 "The DDK Vulkan lacks VK_EXT_robustness2 so zink cannot initialise" (other chat): Mesa's zink documentation
    does not require robustness2 at all (robustBufferAccess only for GL 4.3). Re-test with the exact zink error.
2.4 "The open driver reads garbage because FW-shared memory is CPU-cached" (other chat): plausible, unproven.
    Upstream `pvr_gem.c`/`pvr_mmu.c` default to write-combined CPU mappings and only switched to cached mappings
    with the 2025 series "Use cached memory with dma_coherent" (for dma-coherent platforms). So check first HOW
    the v6.16 backport maps FW objects on this kernel, and test the competing hypothesis: 16 GB of RAM puts pages
    above 4 GB physical; a 32-bit address somewhere (firmware, DMA mask, MMU config) reads/writes a different
    page than the CPU. `mem=3G` on the kernel command line is a five-minute test.
2.5 "1200 MHz is unstable": true at 800 mV (the shipped entry). Never tested at the tool's 920 mV.
2.6 Radxa (forum, 2026-05-14): "Currently there is no Wayland support" for the DDK. True for the build Radxa
    ships; Allwinner's own SDKs (Tina/LongAn) ship weston with a Wayland WSEGL build of the same DDK on other
    SoCs. Whether one exists for the A733 at 24.2@6603887 is a check, not a known answer (route D).

## 3. Routes

### Route A: the open stack (upstream driver + Mesa). The only route that is seamless by construction.
Mesa is a glvnd vendor with Wayland, X11, GBM platforms, GLX, EGL and Vulkan WSI: every toolkit works unchanged.
Blocker: the wedge (garbage FW reads -> job timeouts -> leaked kworkers). Checks, in order:
  A0 (added 2026-09-09, do FIRST) GPU clock and voltage under the upstream driver. It only clk_prepare_enable()s
     "core"/"sys"/"mem" and never sets a rate or touches a regulator; the DDK sets the core clock from the DT's
     `clk_rate` and the dcdc4 voltage from its DVFS table. So the open stack runs the GPU at the clock controller's
     and PMIC's boot defaults, unmeasured. The DDK's "computed-looking" faults were a 1200 MHz / 800 mV margin
     problem (docs/GPU-TQ-FAULT-2026-09-07.md); garbage in firmware-written fields after minutes of load fits that
     pattern. Measure with `powervr-backport/clockcheck616.sh` right after booting the pvr entry; if the pair is not
     a validated one (600 or 1008 @ 800 mV), pin it in the DTB (assigned-clocks/assigned-clock-rates on the gpu
     node, dcdc4 min=max constraints) and re-soak before any driver theory.
  A1 DONE 2026-09-09 (v6.16 sources, byte-identical in the backport): `pvr_gem_object_create` sets
     `shmem_obj->map_wc = !(flags & PVR_BO_CPU_CACHED)` and PVR_BO_CPU_CACHED is only added when
     device_get_dma_attr() == DEV_DMA_COHERENT, i.e. the DT has `dma-coherent`. Ours does not, so EVERY GEM object,
     firmware ones included, is already write-combined on the CPU (kernel vmap and user mmap); GPU page tables get
     CC=0 and SLC-bypass for FW objects, the same as the DDK's SNOOP_NONE setup. The other session's fix #1 ("map FW
     objects uncached") is therefore already the case and cannot be the fix; A3's dma_sync experiment is moot for
     kernel mappings. Also checked: both drivers use identity CPU->GPU physical addresses (DDK sysconfig.c), the DDK
     uses a 48-bit DMA mask and allocates GPU/firmware memory anywhere in the 12 GB (1-13 GB physical), so the GPU
     bus itself handles addresses above 4 GB; a >4 GB bug would have to be firmware/driver-specific (A2 stays cheap).
  A0 DONE 2026-09-09: 840 MHz @ 800 mV under the upstream driver, inside the validated envelope. Not the cause.
  ** WEDGE DECODED 2026-09-09 (docs/OPEN-STACK-WEDGE-2026-09-09.md): "unknown FWCCB command 2abc0069" is
     ROGUE_FWIF_FWCCB_CMD_CONTEXT_RESET_NOTIFICATION (105 | magic 0x2abc<<16) = the firmware reporting GPU context
     resets, which v6.16 ignores (upstream d994acc526c7 decodes it). A reset becomes a permanent wedge because the
     backport calls 6.6's drm_sched_start(sched, bool full_recovery) with v6.16's errno argument 0 = NO recovery.
     Next build: compat fix + upstream decode + FW trace on; then read WHY contexts reset (lockup vs page fault).
  A2 DROPPED: the decode says Innocent lockup, never FW_PAGEFAULT; no address problem.
  A9 (new) Firmware warm-boot after runtime power-off = the reset source. Interim: GPU kept powered (udev rule).
     Real fix: make the power-off a real reset (order of clock gating vs reset assert, the SMMU resets the DDK lists,
     pvr_fw_stop before suspend) or understand the FW's warm-boot path. Verify with fwtrace-tail.sh: the FW clock
     must restart at every resume.
  A10 (new) Compositor performance: KWin/zink re-uploads tall client buffers through the CPU each frame. Steps:
     Mesa 26.2.2 (Fedora 44 update), ZINK_DEBUG=map in KWin's drop-in, perf profile of kwin_wayland. The DDK does
     the same desktop at 60 fps, so this is Mesa-side, not hardware.
  A3 Diagnostic: dma_sync/invalidate before READ_ONCE of FW-written fields (pvr_ccb.c, pvr_cccb.c); if the WARN
     and the "unknown FWCCB command" garbage vanish, the coherency hypothesis is proven.
  A4 READ 2026-09-09: the DDK's rgx_sunxi/sysconfig.c picks SNOOP_CPU_ONLY only when built with the
     SUPPORT_AXI_ACE_TEST tunable AND the GPU reports the AXI ACE-lite feature bit; the shipped build takes the
     else branch, SNOOP_NONE. So Allwinner runs this GPU non-coherent on purpose; `dma-coherent` in the DT would be
     a lie unless the ACE-lite port is proven wired. Not a fix candidate without that proof.
  A5 Fix the kworker leak in pvr_queue_timedout_job regardless (unbounded dma_fence_default_wait).
  A6 Scanout: with the open stack KWin 6.7's GpuManager choosing renderD128 is CORRECT (Mesa renders there):
     remove `KWIN_RENDER_NODES=/dev/dri/none` and the a7a-kwin-nocap units when running it; check the Mesa pvr
     driver offers LINEAR render targets for the sunxi plane (egl-dmabuf-mods.c on renderD128 with Mesa).
  A7 Stability test = a real desktop session for hours (off-screen benchmarks passed while the desktop wedged).
  A8 Fold the kernel into the a7a-fedora build (config option DRM_POWERVR, firmware in the payload, RPM).

### Route B: DDK + Xwayland for X11 apps (the near-term "apps on the GPU"). Wayland-native apps stay in software.
  B1 DONE (other session 2026-09-08, re-verified 2026-09-09): glamor is active on the DDK once XWAYLAND_NO_GLAMOR
     is gone; glmark2-es2 X11 1267 full-screen / 1918 in a 640x480 window, 0 hardware recoveries. pvr-desktop no
     longer writes the variable; the `-glamor es` wrapper and KWIN_DRM_PREFER_COLOR_DEPTH=24 were measured as
     unnecessary (docs/XAPPS-ON-GPU-2026-09-08.md, follow-up section).
  B2 DONE for the client side: Qt (QT_QPA_PLATFORM=xcb QT_XCB_GL_INTEGRATION=xcb_egl), SDL (SDL_VIDEODRIVER=x11
     SDL_VIDEO_X11_FORCE_EGL=1) and plain EGL/GLES clients land on the DDK through `pvr-run`. Still per-application:
     the DDK libraries cannot go system-wide without taking EGL away from Wayland clients, and Plasma's session
     forces QT_QPA_PLATFORM=wayland, so /etc/environment.d does not make it seamless. Seamless = route A or F.
  B3 GLX/desktop GL apps: no GLX in the DDK. Options: gl4es (GL 2.x + GLX on top of GLES2, the standard trick on
     blob-GPU SBCs), or Mesa GLX over zink on the DDK Vulkan via VK_KHR_xcb_surface (route E). Measure both.
  B4 DONE: under `KWIN_RENDER_NODES=/dev/dri/none` Xwayland gets card0 from KWin and renders through the DDK's
     sunxi-drm_dri.so on it (the DDK opens renderD128 internally). No KWin patch needed on 6.7.
  B5 X11 session (`pvr-desktop all`): everything X11 on the DDK like the Debian card; greeter already shielded
     (QPainter). Untested on Fedora. Fallback for daily use.

### Route C: a Wayland EGL layer over the DDK's GBM platform (make the vendor stack "Wayland-native").
The DDK renders into dumb buffers on card0 through its GBM platform (that is how KWin uses it). A library that
implements EGL_PLATFORM_WAYLAND + wl_egl_window on top of it (gbm bo per frame -> dma-buf -> linux-dmabuf-v1
wl_buffer -> commit, frame callbacks, buffer release) gives every Wayland client GPU rendering with zero copies
(KWin imports the dumb buffer's dma-buf directly; direct scanout possible). Precedent: NVIDIA's egl-wayland
(EGL External Platform). Estimated 1.5-3k lines of C. Checks before starting: C1 the DDK's EGL on a card0 gbm
device accepts gbm_surface / gbm_bo render targets from a non-master client (KWin is master; a client is not:
test with gbm-scanout-probe as a normal user); C2 how the DDK is loaded without LD_LIBRARY_PATH (route F).

### Route D: a Wayland-capable DDK build from Allwinner. Check, do not assume.
  D1 Allwinner Tina/LongAn SDK for A733 (sun60iw2): does its PowerVR package carry libEGL with
     EGL_EXT_platform_wayland / a WSEGL wayland module and libVK with VK_KHR_wayland_surface, at DDK 24.2@6603887
     (must match firmware rgx.fw.36.56.104.183)? Sources: Allwinner SDK dumps, other A733 boards' images,
     Radxa's own `allwinner-target` repos, vamrs-feng (Radxa, merged a PR of ours) as a direct contact.
  D2 Imagination's DDK is built per window system; Radxa could build one. Ask, with the exact ask from the forum
     thread (libEGL with EGL_EXT_platform_wayland, libVK_IMG with VK_KHR_wayland_surface).
  D3 CLOSED 2026-09-10 (docs/DDK-WAYLAND-2026-09-10.md): the DDK's window-system layer is open source, so no vendor
     build is needed for EGL.
     - Imagination's Mesa glue (TI github.com/TexasInstruments/mesa, powervr/24.0.1, the Mesa version of Radxa's
       /opt/pvr) negotiates DRI interface v8 with the board's closed `libpvr_dri_support.so`, a complete match.
     - Rebuilt WITH the Wayland platform as the `mesa-img-a7a` RPM (/opt/pvr-wl).
     - Measured on the DDK boot, 0 HWR:
       - Wayland EGL/GLES 3.2 on the PowerVR with no override;
       - glmark2-es2-wayland 2008-2049 (open stack 351);
       - full suite 1216, vs X11 1204 on the same boot;
       - SuperTuxKart native Wayland, modern renderer, 30.7 fps (open stack 4.1, fixed pipeline).
     What stays closed:
     - Radxa's drop has no desktop-GL module (`libGL_PVR_MESA.so`, which the support library asks for).
     - The closed `libVK_IMG` ("xorg" build) lists no Wayland surface, and its X11 swapchains crash in the WSI with
       Radxa's own build too. So Vulkan cannot present.
     - Fedora's Qt is desktop-GL. With the DDK EGL first system-wide, Qt Quick windows get no EGL config (the logout
       prompt quit). So the system-wide switch is OPT-IN (`/etc/a7a/pvr-wl.enabled`, applied by a7a-gpu-session only
       on the DDK boot, removed on every other boot); per-app `pvr-wl-run` needs nothing.
     The D2 ask is now exact: `libGL_PVR_MESA.so` for 24.2@6603887, and a `libVK_IMG` with `VK_KHR_wayland_surface`
     (or at least working X11 swapchains). The open alternative for desktop-GL clients: the glue answers a GL
     request with a GLES 3.2 context (Qt/Firefox then take their ES paths). Not built.

### Route E: zink (Mesa GL) on the DDK Vulkan.
  E1 Get the exact zink initialisation error with the DDK ICD (`MESA_LOADER_DRIVER_OVERRIDE=zink`, X11 window
     via Xwayland); compare against Mesa's documented requirements (VK_KHR_maintenance5, imageless framebuffer,
     external_memory, scalar block layout; robustBufferAccess only for GL 4.3).
  E2 If zink initialises: GLX on X11 comes for free (kopper + VK_KHR_xcb_surface). Wayland stays out (no
     VK_KHR_wayland_surface) unless Mesa's kopper can present via dmabuf export (check: it cannot today).

### Route F: make the DDK a proper vendor on the system (removes the env juggling for every route above).
  F1 The DDK exports no `__egl_Main`, so glvnd cannot dispatch to it and LD_LIBRARY_PATH is used instead,
     which the loader ignores for capability-bearing binaries (the cap_sys_nice story). A thin glvnd vendor
     library (libEGL_pvr.so with __egl_Main forwarding to the DDK's libEGL) lets `__EGL_VENDOR_LIBRARY_FILENAMES`
     / a glvnd JSON select the DDK per process or system-wide, keeps Mesa for everyone else, and lets KWin keep
     cap_sys_nice. Check: glvnd's EGL vendor ABI version the DDK's entry points can satisfy.
  F2 fdinfo `drm-engine-*` in pvrsrvkm so KDE's process monitor shows GPU use; a ksystemstats backend for the
     "missing sensors" page.

### Route G: clocks and hardware capacity (independent of the GPU stack)
  G1 1200 MHz @ 920 mV: `a7a-clock gpu 1200`, reboot, `clock-ceiling.sh 3`, then a desktop soak.
  G2 The DDK has no DVFS here (no devfreq); power management (tuned-ppd) only touches the CPU: no conflict.
  G3 The a7a-clock GUI: expose the validated ceiling and the mV rule (the tool already defaults 1200 -> 920).

## 4. Order of work (what to do next, one item per session)

1. DONE: B1+B2, X11 EGL apps run on the GPU through Xwayland (see route B). Route C's precondition C1 is answered
   by the same test: a non-master client (Xwayland) creates a gbm device + DDK EGL display on card0, renders, and
   hands dma-bufs to KWin.
2. A0 then A2 on the open stack's blocker: measure the GPU clock/voltage the upstream driver leaves the GPU at
   (clockcheck616.sh, one boot of the pvr entry) and pin a validated pair if needed; then the `mem=3G` boot.
   A1 is done: the CPU mappings are already write-combined, so the coherency fix is off the table.
3. G1: 1200 @ 920 mV.
4. Depending on 2: A3/A4/A5 and a real-session soak; if the open stack becomes stable, it is the endgame
   (A6-A8) and routes C/D/E/F become unnecessary.
5. If the open stack stalls: F1 (glvnd vendor wrapper) then C (Wayland layer over GBM) for the DDK; D in
   parallel as an ask, not a wait.

Sources checked 2026-09-09: forum.radxa.com/t/gpu-wayland/30823; github.com/cuihuir/radxa-a7z-debian12 (status
table, validation-records/2026-07-16-a733-xwayland-24.1.6-test.md, patches/kwin/0001-linux-dmabuf-use-egl-render-
node.patch); docs.mesa3d.org/drivers/zink.html; xserver xwayland-24.1 glamor/glamor.c; LKML "[PATCH v5 15/18]
drm/imagination: Use cached memory with dma_coherent"; the DDK's vulkaninfo on the board.
