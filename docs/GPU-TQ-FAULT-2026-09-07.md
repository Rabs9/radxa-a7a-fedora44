# The Xwayland "Guilty Lockup" on the PowerVR DDK: root cause (2026-09-07)

Board: Radxa Cubie A7A, Fedora 44 KDE, kernel 6.6.98+ (the Rabs9 kernel), Imagination DDK 24.2@6603887 under /opt/pvr,
GPU BXM-4-64 (BVNC 36.56.104.183, features 0x000002181d51bf35: XE memory hierarchy, TFBC v10, FBCDC 5.0, 1 cluster,
16 KB SLC), pvrsrvkm from allwinner-bsp-1.4.8 rogue_km with the fdinfo fix.

## Verdict (evening update, supersedes the afternoon conclusion below)

**Root cause: the GPU overclock.** The Fedora image boots the overclocked device tree, which sets the GPU core clock
to **1200 MHz at 800 mV** (`clk_rate` in the gpu node; the driver's own default is 600 MHz, the owner's Debian daily is
1008). At that clock the DDK's transfer-via-3D shader paths fail deterministically: a stray 4-byte pixel write one
page below the transfer context (an MMU fault when a protected free-list page sits there), shader hangs on 192/256-px
uploads and on every EGLImage texture fetch, lost clears, a coherent VBO whose vertices never arrive, wrong dmabuf
readbacks. **At 600 MHz (`a7a-clock gpu 600`, boot entry `g_600_800`) every one of these cases passes**: 14 reproducer
variants with zero hardware recoveries and correct pixels, the 256-px upload 6 of 6 clean, and the headless
KWin + Xwayland-with-glamor harness with zero resets. Same binaries, same module, same VA layout.
The DDK, the kernel module and glamor are not at fault. Everything in "Evidence chain" below is real data, but its
interpretation as a driver bug was wrong; keep it as the record of what marginal silicon looks like from software
(a deterministic, computed-looking wrong address is *not* proof of a logic bug).

**New blocker, distinct from the GPU:** with the GPU stable, a real GPU-composited login (KWin on the DDK, DRM
backend on card0, glamor on) showed the desktop for a few seconds and then the machine went dark and off the
network: the serial console floods with sunxi IOMMU faults from the **display engine** (`Bug is in DE0 module,
invalid address: 0xfe800000, data:0x0, id:0x4` / `L1 PageTable Invalid`, hundreds per millisecond, CPU0 starved,
no ssh, SysRq blocked, power cycle needed). The DE is scanning out from an IOMMU address that is no longer mapped:
a scanout buffer produced through the DDK on card0 (PRIME-imported into sunxi-drm) was unmapped from the DE's IOMMU
domain while still on the plane. This does not happen with the software desktop (sunxi-drm's own dumb buffers), it
is a sunxi-drm / DDK-buffer lifetime problem, and it is the next thing to fix. The storm's tail is in
`output/logs/de-iommu-storm-20260907-1330.log`; its beginning was not captured because no serial recorder was
running (journald had stalled). Run `python3 -m src.cli serial --record` on the laptop before the next attempt.

## Evidence chain (afternoon, at 1200 MHz)

1. **Journal of the failing session (boot -1, 10:09:34).** Xwayland's glamor initialised on the DDK (`-glamor` fell
   back from GL to GLES2), ~1.2 s later the FW logged `Recovery 1: PID=19292/Xwayland, 3D DM, HWRTData=0, frame=0,
   Guilty Lockup`, all BIF/MMU fault registers zero. Only ONE hardware reset in that boot, yet KWin logged 876 "graphics
   reset" messages and the later attempt with `XWAYLAND_NO_GLAMOR=1` (kwin pid 24911) had **zero** hardware resets and
   still 839 reset messages. **The DDK reports a stale reset status to every new context for the rest of the boot
   after one real reset** (KWin's `glGetGraphicsResetStatus` loop). The "glamor off did not help" result was a
   false negative taken in a poisoned boot; it must be retried in a fresh boot.
2. **Headless reproduction with KWin's virtual backend** (`tools/gpu-debug/xwl-test.sh`): a private KWin on the DDK
   on renderD128, own D-Bus, Xwayland spawned through a wrapper. The `es` variant (no driver override, `-glamor es`,
   the A7Z community configuration) reproduced the reset on the first job. So: not the KMS/greeter handoff, not the
   leaked `MESA_LOADER_DRIVER_OVERRIDE`, not the GL-first attempt, not explicit sync (no DRM node advertises syncobj
   support, so Xwayland cannot use it).
3. **Counting mistake corrected.** `dmesg | grep -c "Guilty Lockup"` only sees the first reset per boot
   (`HWRDebugDumpLimit=1`). `/sys/kernel/debug/pvr/status` "HWR Event Count" showed **21** resets while dmesg showed 1:
   the debugfs ring lists them all, every one "PID=Xwayland", frames 0, 1, 1, 2, 6, 7, 8, 9, 10 (transfer jobs first,
   then real renders). Every job of every Xwayland process resets. The strace run also reset 8 times; "no lockup
   under strace/apitrace" was the counter, not the tracer.
4. **Standalone reproducer** (`glamor-repro.c`, GBM on renderD128, `EGL_NO_CONFIG_KHR` GLES2 context, surfaceless,
   exactly Xwayland's path). Results (HWR delta per run, from debugfs):
   - plain texture FBO clear (`tex`), any size: clean, correct pixels
   - gbm-bo-backed FBO clear (`bo`): **1x1, 8x8, 16x16, 24x24, 32x32, 64x16, 128x128, 256x256 reset** (mostly both
     clears, readback black = the clear never landed); 32x33, 64x64, 512x512, 1x1024, 1024x1, 1024x768 clean
   - the same on `/dev/dri/card0` (sunxi_drm personality, non-master): resets identically
   - `bo+scissor 1024x768` (partial clear): resets intermittently; `sample` (draw sampling the bo image): resets
     every time, any size; `draw+vbo` (glamor's persistent coherent VBO): no reset but the triangle is not drawn
   - `bo+map` / `bo+rmap` (gbm_bo_map + unmap before GPU use, even read-only): clean and correct. The kernel does
     nothing GPU-visible for a CPU map; the map path allocates extra buffers, which **shifts the VA layout**.
   - `bo+tqfirst` (one texture upload right after context creation, so the transfer context is created BEFORE the
     render context's free lists): **clean and correct, 3 of 3, also at 128x128**.
5. **Firmware trace** (`FirmwareLogType=trace`, `EnableLogGroup=main,mts,...,hwr` on gpu00, `fwtrace.sh`):
   ```
   T0-MAIN> Kick 3D TQ: FWCtx 0xc00519c0 @ 0 ... frame:0
   T0-HWR>  MMU fault event: 0x00000210 (debug info: mmu pf)
   T0-HWR>  BIF0 page fault detected (Core 0 MMU Status: 0x6005 Req Status: 0x0000748000596000)
   T0-MAIN> GPU-0 has locked up (see HWR logs for more info)
   ```
   Decoded (`decode-fault.py 0x6005 0x0000748000596000`): FAULT, **PM/META-RO violation**, data-level (the PTE was
   valid), address **0x8000596000**, requester **PBE0** (pixel back end), **write**. The same address faults again
   after the full hardware reset (second record), so it is not a stale TLB.
6. **Kernel-side VA map of the same run** (instrumented `DevmemIntMapPMR`/`UnmapPMR`): 0x8000596000 is a live,
   one-page `PMFreePageTableMem` mapping (the UM-allocated Parameter Manager free list, flags 0x1000000004004103 =
   GPU uncached + PMMETA_PROTECT), mapped before the kick and unmapped only at process exit. The transfer context's
   first sub-allocation (16 KB, GPU-only, uncached, flags 0x103) sits directly above it at 0x8000597000.
7. **The stray write itself.** With `RGX_MMUCTRL_PT_DATA_PM_META_PROTECT_EN` experimentally dropped from the PTEs, the
   `bo` cases stop faulting; dumping the free-list page at unmap shows exactly **one word written at offset 0:
   0xff0000ff / 0xffff0000, the clear colour**, everything else untouched (the free-list data starts at +0x14). So
   the transfer job's PBE emits one pixel of the clear to `transfer_context_base - 0x1000`, and the clear itself is
   lost or half-lost. Without protection the fault disappears but results stay wrong, so this is not a fix.
8. **Second defect.** `sample` (a fragment shader sampling the bo image) and even the plain-texture sampled draw in
   `+warm` hang the 3D data master with "DM3 timed out ... USC slots: 64 used by DM3, 0 empty": every shader instance
   waits on a texture fetch that never returns, no MMU fault, and it does not depend on the allocation order.
   The sampled value that does come back is correct (85,85,85 after a CPU write of 0x55), so the data path works
   and the hang is at the end of the job. Not characterised further.

## Ruled out, each by an experiment on the board

| Hypothesis | Experiment | Result |
|---|---|---|
| Compositor / greeter DRM-master handoff | headless virtual KWin | reproduces |
| `MESA_LOADER_DRIVER_OVERRIDE` leaking into Xwayland, GL-first glamor init | harness variants nooverride / es / es-override | all reproduce |
| Explicit sync (linux-drm-syncobj) | `DRM_CAP_SYNCOBJ` on all nodes | 0, unavailable |
| KHR_debug synchronous output, persistent coherent VBO (glamor init) | `bo+dbg`, `draw+vbo` | no reset (VBO draw shows no triangle, separate oddity) |
| Compressed modifiers from dmabuf feedback | `mods` | DDK advertises LINEAR + 8 external-only IMG modifiers, picks LINEAR |
| App-name keyed DDK behaviour ("Xwayland" string in libpvr_dri_support) | reproducer renamed `Xwayland` | no change |
| CPU cache lines dirty in page-pool pages | KM: flush every pool page on allocation | still resets |
| GPU SLC coherency | KM: SLC bypass on every mapping | still resets |
| Missing barrier before the firmware doorbell | KM: mb()+200 us before MTS_SCHEDULE | still resets |
| Stale/non-zero pool pages | KM: verify pool pages via cached and non-cached views | all zero |
| MMU cache invalidate ordering | firmware trace: MMUCACHE command precedes the kick; same fault after HW reset | not it |
| Page-table entry parity | rogue build has no MH_PARITY feature | n/a |
| Frame-buffer-compression state cache (FBSC) | volcanic-only; rogue kick paths pass an empty mask by design | n/a |
| `EnableSWTQ=1` in /etc/powervr.ini | reproducer | still resets (hint possibly not honoured) |
| GPU overclock (1200 MHz at 800 mV) | `a7a-clock gpu 600`, reboot, whole matrix | **everything passes: this was the cause** |

The afternoon's Debian comparison idea is moot: the A7Z image (github.com/cuihuir/radxa-a7z-debian12) and the owner's
Debian card both run the GPU at a sane clock.

## Evening session: the two real logins at 600 MHz

- First login with the drop-in (`pvr-desktop on`) raced the user manager: KWin started at the same second the drop-in
  was written and came up on Fedora's mesa (llvmpipe) with Xwayland in software. Looked fine, proved nothing.
  Lesson: run `pvr-desktop on` well before logging in, and confirm with `sudo grep -oE
  "/[^ ]*(pvr_dri|libgallium)[^ ]*" /proc/<kwin pid>/maps` (pvr_dri = DDK, libgallium = mesa).
- Second login (log out / in): KWin on the DDK with glamor allowed. Desktop visible for a few seconds, then the
  display-engine IOMMU storm described in the verdict; power cycle. The journal of that boot is truncated at
  13:29:35 (KWin start), so the sequence right before the storm is unknown.
- The evening's other tools: `tools/gpu-debug/pvr-tqfirst-shim.c` (LD_PRELOAD that creates the transfer context
  first; a layout workaround that is unnecessary at a stable clock, kept for reference; `PVR_TQFIRST=1`,
  `PVR_TQFIRST_STEPS=u64,c24,...`).

## State of the board after the evening session

- Stock pvrsrvkm loaded (the debug module died with the power cycle; nothing of it remains on disk). The rogue_km
  source tree in `work/` is stock plus the fdinfo patch; `tools/build-extras.sh` rebuilds a clean module.
- **Boot entry `g_600_800` is the default** (GPU 600 MHz, 800 mV, created by `a7a-clock gpu 600`); the original
  `fedora` entry (1200 MHz) is still there. The stable ceiling between 600 and 1200 is unknown; test candidates with
  the reproducer matrix (`run-matrix.sh`) before trusting them.
- `pvr-desktop` is **off** (software desktop), `/etc/powervr.ini` absent, `/tmp/.X11-unix` created by hand this boot
  (it was present on its own after the 13:23 reboot, so the tmpfiles problem is intermittent). Explained 2026-11-18:
  the /tmp clean-up removes it 15 minutes after boot (made at systemd's build date, still empty); a log-in before
  that keeps it. Fixed by `tools/board/tmpfiles.d/a7a-x11.conf`.
- Packages added on the board: gcc, mesa-libgbm/libEGL/libGLES-devel, libdrm-devel, apitrace, strace, gdb; that
  pulled mesa 26.0.3 -> 26.1.8 and glibc 2.43-2 -> 2.43-8.
- Files on the board (tmpfs, gone at reboot; sources in `tools/gpu-debug/`): /tmp/glamor-repro(.c), run-matrix.sh,
  xwl-test.sh, fwtrace.sh, pvr-tqfirst-shim.{c,so}.

## Next steps

1. **The display-engine IOMMU storm** is now the blocker for a GPU-composited desktop. Before the next attempt start
   `python3 -m src.cli serial --record` on the laptop so the first fault lines and the preceding kernel messages are
   captured, and enable the KWin DRM debug log (`QT_LOGGING_RULES=kwin_wayland_drm.debug=true` in the drop-in) to
   see which framebuffer/plane operation precedes it. Candidates: sunxi-drm unmapping a PRIME-imported (pvr)
   scanout buffer from the DE's IOMMU domain while the plane still shows it (missing fence / early
   `drm_gem_object_free`), cursor-plane handling (KWIN_FORCE_SW_CURSOR is already set), or the atomic commit
   path with DDK buffers. Compare with how Debian's Xorg presented (modesetting driver, no page flips by default:
   `FlipFB` commented out in 20-modesetting.conf). Kernel side: sunxi-drm prime import / `sunxi_iommu` map and
   unmap around plane updates in the BSP.
2. Once that is understood: `pvr-desktop on` well before login, log in, verify KWin maps `pvr_dri.so`, then keep
   an eye on `/sys/kernel/debug/pvr/status` "HWR Event Count" (must stay 0) and the serial recorder.
3. Find the stable GPU clock ceiling with the reproducer matrix (600 is proven; try 900 and 1008 with
   `a7a-clock gpu <MHz>` + reboot + `run-matrix.sh` over all variants and a few repeats); record it in
   `mirror`/the kernel repo notes so the overclocked DTB is not booted at 1200 by default.
4. Fix the image: make sure `/tmp/.X11-unix` exists after boot (tmpfiles x11.conf; add a checklist item). Done
   2026-11-18: `tools/board/tmpfiles.d/a7a-x11.conf` (see the note above).
5. The stale-reset-status loop means any GPU reset poisons the boot for KWin; count resets with the debugfs
   counter, not dmesg, and reboot between compositor experiments.
