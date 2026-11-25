# The display-engine IOMMU fault storm under GPU compositing (started 2026-09-07 evening)

Board: Radxa Cubie A7A, Fedora 44 KDE, kernel 6.6.98+, GPU at 600 MHz (`g_600_800`), DDK 24.2 under /opt/pvr.
Symptom (docs/GPU-TQ-FAULT-2026-09-07.md, "New blocker"): KWin on the DDK (drop-in `pvr-desktop on`, DRM backend on
card0) shows the desktop for a while, then the serial console floods with
`Bug is in DE0 module, invalid address: 0xfe800000, data:0x0, id:0x4` / `L1 PageTable Invalid`, CPU0 starves, the
network dies, power cycle. Tail of the storm: `output/logs/de-iommu-storm-20260907-1330.log`.

## What the salvaged journal of the stormed boot says

journalctl ignores that boot's system journal (`system@...-0f5e667dce82f1b8.journal~`, "truncated"), but `strings`
on the file still gives the messages in order. Monotonic times of the stormed boot:

| t (s) | event |
|---|---|
| ~313-335 | user session starts on tty2 (plasmalogin: greeter KWin stays alive on tty1 until the session is up) |
| ~335 | greeter's `plasma-login-kwin_wayland.service` exits ("Consumed 18.897s CPU time over 22.838s wall clock") |
| 335-342 | powerdevil helper services start (normal session startup) |
| **342.855** | **first fault: `L2 PageTable Invalid`, `0xfea46000 is not mapped`, `Bug is in DE0 module, invalid address: 0xfea46000`** |
| 342.879 | `exception record !!!` (sunxi-drm's fault callback; its ring dump never ran, CPU0 was gone) |
| +0.0x | `WARNING ... sunxi-iommu-v2.c:405 sunxi_iommu_irq` + `iommu_master de0_iommu: Runtime PM usage count underflow!` |
| then | every later fault: `L1 PageTable Invalid` at `0xfe800000` (the tail log, 83 s later, still shows it) |

Reading (the display is 1024x600, KWin logged `src KWin::RectF(0,0 1024x600)`):

- `0xfea46000 - 0xfe800000 = 0x246000 = 582 lines x 4096 bytes`: the DE was **582 lines into a 1024x600 ARGB
  scanout buffer based at 0xfe800000 when the buffer's mapping disappeared**, i.e. the unmap happened mid-frame, not
  at a frame boundary. The next frame started at 0xfe800000 again and faulted forever: the DE was never re-pointed.
- The L2-then-L1 pattern is exactly one `iommu_unmap` of the whole buffer (`sunxi_pgtable_delete_l2_tables` frees an
  L2 table only for a fully covered 1 MiB region: regions 0xfe800000 and 0xfe900000 lost their L1 entry, region
  0xfea00000 kept its L2 table because only the buffer's tail lives there). No collateral-unmap bug in the pgtable code;
  mapping over a live PTE would have printed `WARN_ON(*pent)` and did not.
- The runtime-PM WARNING is noise from the fault path itself: the per-master register accessors
  (`sunxi_iommu_read/write` for MIC registers) do `pm_runtime_get_if_in_use()` + `WARN_ON` + unconditional
  `pm_runtime_put()`, and the de0_iommu master device autosuspends 5 s after use. One backtrace per fault, and the
  PM core undoes the underflow. It does NOT mean the DE was powered down.
- The fault handler (`__dump_int_from_one_instance`) prints 2-3 lines, resets the master's micro-TLB and returns; the
  master retries and faults again: an unbounded interrupt storm by design, with a 115200-baud console in the middle.

## Under the DDK, KWin's scanout buffers are sunxi-drm DUMB buffers, not PRIME-imported GPU memory

`tools/gpu-debug/gbm-scanout-probe.c` (gbm bo 1024x600 SCANOUT|RENDERING on card0 + drmModeAddFB2, then read
`/sys/kernel/debug/dri/0/framebuffer`): with `pvr-run env MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm` (the drop-in's
stack) the DDK's GBM does `DRM_IOCTL_MODE_CREATE_DUMB` on card0, exports it (`PRIME_HANDLE_TO_FD`, exporter `drm`,
size 2457600) and the GPU side imports that dma-buf for rendering; the FB is `imported=no`, modifier reported as
INVALID. Same as mesa/llvmpipe. So the buffer that vanished under the DE was a dumb buffer freed by sunxi-drm's own
`sunxi_gem_dma_free` -> `dma_free_wc` -> IOMMU unmap, which only happens when the GEM's last reference goes: KWin's
handle(s), the DRM framebuffer, and the dma-buf (held by pvrsrvkm's import while the GPU can render into it).
Note the same IOVA slot is in use right now by the software desktop (ring: 0xfe800000 / 0xfe400000 / 0xfe000000).

Also: `sunxi_drm_atomic_helper_commit_tail` has `drm_atomic_helper_wait_for_vblanks` commented out; the DE flush
(`sunxi_de_atomic_flush`) instead polls the RCQ "update finished" flag for up to 50 ms and then sends the flip event
and continues **even on timeout** (`DRM_INFO "... timeout"`), at which point DRM core believes the old FB is off the
plane while the DE may still scan it. Candidate mechanism; unproven until the trace below says who freed the buffer.

## Kernel changes made for the capture (tools/patches, applied to work/allwinner-bsp-1.4.8 by build-kernel.sh)

- `0002-sunxi-iommu-fault-storm-protection.patch` (keep): full details for the first 8 faults per instance, one summary
  line per second after that, IRQ line disabled after 512 faults (the master stays on its fault, the board lives);
  MIC register accessors only put what they got, no WARN.
- `0003-sunxi-drm-buffer-lifetime-trace.patch` (debug): `pr_info` on every FB create/destroy and GEM free on card0
  (dma_addr, size, dumb/imported + exporter, `comm[pid]`); `/proc/sunxi-drm/debug` ring gains the fb id and its
  fault-time dump runs on the unbound workqueue.
- Installed on the board as `/boot/vmlinuz-6.6.98+.iommu`, extlinux label `g_600_800_iommu` (entry 4, now default);
  entry 3 `g_600_800` = the previous kernel, untouched (`extlinux.conf.bak-20260907-iommu`). Modules unchanged.
- The `update` stage cannot do this: every entry points at `/boot/vmlinuz-6.6.98+` and `a7a-extlinux add 6.6.98+
  --default` would overwrite it in place and make the 1200 MHz fdtdir the default. To fix before item 4.

## Capture procedure (next)

1. laptop: `nohup python3 -m src.cli serial --record` (log in output/logs/serial-record-*.log)
2. board: reboot into `g_600_800_iommu`; verify `uname`, `dmesg | grep -c "sunxi-drm: fb"` (trace live),
   `sudo sysctl -w kernel.printk="7 4 1 7"` (pr_info on the console too), `pvr-desktop on --debug`, wait 10 s
3. The owner logs in; watch `dmesg -w` over ssh; the storm now stops itself after 512 faults
4. read: dmesg (fb/gem trace around the first fault), `/proc/sunxi-drm/debug` (ring with unmap-ts), KWin's journal
   (`journalctl --user -u plasma-kwin_wayland`), `/proc/sunxi-drm/status`; then `pvr-desktop off` + restart plasmalogin

## Night, kernel v2 (patches 0002-0004): the fault reproduced on its own, and the buffer was NEVER freed

Login at ~45 s, KWin (1949) on the DDK with three dumb buffers 0xfe800000 / 0xfe400000 / 0xfe000000 (fb 163/170/171).
KWin's last commit: 54.89 s (idle desktop, fb 163 at 0xfe800000 stays on the plane). 56.14 s: `L1 PageTable Invalid`,
`0xfe8be000 is not mapped` (line 190 of that buffer). The lifetime trace shows NO `fb destroy` and NO `gem free` for
0xfe800000 (the only frees between 48.3 s and the fault are the greeter's three buffers at 0xfec00000/0xff400000/
0xff000000), and `/proc/sunxi-drm/status` still lists fb 163 at 0xfe800000 on the plane afterwards. So the buffer was
alive and on screen while its whole 1 MiB page-table region disappeared: **another user of the shared IOMMU domain
unmapped a range covering it** (an unmap with a wrong/stale address or a double free: the sunxi page-table code frees
an L2 table for any full-region unmap and does not know who owns the pages). Attached masters: the video engines
(cedar_dev, root-only nodes), the deinterlacer, the DE and sunxi-drm; USB is not behind this IOMMU. Nothing was logged
in the 8 s before the fault. The earlier "flush timeout / KWin lost master" chain is therefore NOT the mechanism; that
KWin lost master in the stormed boot was a consequence (or unrelated).

Recovery (patch 0004) worked as designed: one fault, `de 0 stopped after an IOMMU fault`, no storm, ssh alive, KWin
alive. The DE restart is armed on the next atomic flush, and an idle KWin does not flush (screen stays the TCON's
underflow colour, whitish blue, until something repaints). The KMS tests (`kms-rmfb-test`: disable, flipfree, dpms,
modeset) all passed with zero faults, and `drm-unstick` recovered the fbdev console deadlock found on the way.

Next: kernel v3 adds an unmap tripwire (patch 0002/0003: every IOMMU unmap that overlaps a buffer committed to a DE
plane is reported with a stack trace) -> reboot, log in, wait for the fault, read the culprit's backtrace in dmesg.
Also to do: restart the DE from the fault path itself (schedule a flush) so an idle compositor is not left on a blank
screen; ftrace is not built into this kernel (CONFIG_FTRACE unset), which is why the tripwire is in-driver.

## ROOT CAUSE (kernel v3, 2026-09-07 late): pvrsrvkm's dma-buf exporter unmaps what it never mapped

Reproduced on kernel v3 at 62.8 s after login; the tripwire on IOMMU unmaps stayed silent, yet KWin's main thread
sat at 100 % system time. A SysRq "show all CPUs" (`echo l > /proc/sysrq-trigger`, which ignores kernel.sysrq)
gave its kernel stack:

    __iommu_unmap <- iommu_unmap_fast <- __iommu_dma_unmap <- iommu_dma_unmap_sg <- dma_unmap_sg_attrs
    <- PVRDmaBufOpsUnmapCommon [pvrsrvkm] <- dma_buf_unmap_attachment <- drm_gem_prime_import_dev (error path)
    <- drm_prime_fd_to_handle_ioctl

`PVRDmaBufOpsMapCommon()` (rogue_km services/server/env/linux/physmem_dmabuf.c) never calls dma_map_sg(): it writes
the PMR's physical addresses into sg_dma_address()/sg_dma_len() itself (fine for an importer that addresses memory
physically). `PVRDmaBufOpsUnmapCommon()` nevertheless called dma_unmap_sg() on the importer's device. On the A733 the
display engine is behind the IOMMU, so dma-iommu took those physical "DMA addresses" as an IOVA range and unmapped a
gigantic span of the shared domain: every other mapping died (the compositor's scanout buffers first, the DE faulted
mid-frame on the region it was scanning), and the importing thread spun in the kernel for minutes (KWin dead, no
commits, "lost master" in the original boot). The trigger is any GPU dma-buf import into card0: KWin's direct-scanout
attempt on a client buffer (Xwayland's GPU-backed window when glamor is allowed fits the timing); the import failed
on the scattered pages (or "succeeded" with physical addresses when the pages were contiguous) and the error/free
path ran the unmap. The tripwire stayed silent because it only watched ranges overlapping a live scanout buffer at
the *start* of the unmap; the bogus unmap started at a physical address below the DE's IOVAs and swept upward.

Fix: `tools/patches/0005-pvr_dmabuf-map-for-importer.patch` (pvrsrvkm, applied by build-extras.sh): one sg_table per
map call built from the PMR's pages, `dma_map_sg()` for the importer's device, `dma_unmap_sg()` on unmap, PMR pages
locked while any mapping exists. Verified with `tools/gpu-debug/pvr-import-test.c` (GPU bo on renderD128 -> import
into card0 -> AddFB2): before the fix the import spun and wiped the domain; with only the unmap removed it failed
with EINVAL or handed out physical addresses; with the full fix every import succeeds, the DE gets a proper IOVA
(`fb 165 create ... dma_addr=0x00000000fe800000 imported`, `gem free imported ... exporter=pvrsrvkm`), zero faults.
Module reloaded via `systemctl stop plasmalogin; rmmod pvrsrvkm; modprobe pvrsrvkm; systemctl start plasmalogin`.

Kept as defence in depth: 0002 (storm cap, PM noise), 0003 (lifetime trace, ring fb id, fault-test knob, unmap
tripwire), 0004 (AHB fallback on RCQ timeout, DE stop on fault + restart on the next flush, bounded fbdev wait).
Known limitation of 0004: an idle compositor does not flush, so after a fault stop the screen stays the TCON's
underflow colour until something repaints; with the root cause fixed this path should never run.

## After the Fedora update (2026-09-07, late night): KWin 6.7.4

`dnf update` (984 packages) first aborted because the BSP kernel config lacks `CONFIG_EXT4_FS_SECURITY` (rpm cannot
set file capabilities: "cap_set_file failed - Operation not supported"); the build now enables it (versioned release
`6.6.98-a7a.<stamp>`, GPU 600 MHz DTB, installed as the `kernel-a7a` RPM through dnf, boot entry added by its %post,
previous kernel kept as entry 2). The completed update brought KWin 6.7.4, with three consequences, all handled:

1. Fedora's kwin sets `cap_sys_nice` on kwin_wayland: with capabilities now storable, the binary ran in the loader's
   secure mode and ignored LD_LIBRARY_PATH/LIBGL_DRIVERS_PATH/MESA_LOADER_DRIVER_OVERRIDE, so the DDK drop-in was
   silently dropped. `a7a-kwin-nocap.{path,service}` strip it (and `pvr-desktop on` does too).
2. KWin 6.7 + Fedora mesa (llvmpipe) cannot create EGL images here (EGL_BAD_ALLOC on every format): the greeter and
   the software session use `KWIN_COMPOSE=Q` (QPainter, dumb buffers; drop-ins written by pvr-desktop off / the image).
3. KWin 6.7's new GpuManager pairs a platform-bus display device with the most capable platform render node
   (renderD128, the GPU), and the DDK's EGL opened on the render node reports every dma-buf format as external-only
   (tools/gpu-debug/egl-dmabuf-mods.c: on card0 LINEAR ARGB/XRGB are renderable, on renderD128 nothing is), so
   `EglGbmLayerSurface::createSurface` rejects every format before any allocation ("Applying output configuration
   failed!", no buffer ever created, and the compositor hangs at logout). `KWIN_RENDER_NODES=/dev/dri/none` makes
   KWin fall back to card0's own EGL (`DrmGpu::updateRenderDevice`), the 6.6 behaviour: GPU desktop back on 6.7.4.
   (`KWIN_RENDER_NODES=/dev/dri/card0` does not work: KWin already holds card0 through logind, EBUSY.)

## GPU clock ceiling (2026-09-09)

`tools/gpu-debug/clock-ceiling.sh` (16 glamor-repro cases per round, HWR counter from debugfs): 600 MHz 2 rounds
clean; **1008 MHz @ 800 mV 4 rounds clean, 0 hardware recoveries, 35 C**, after two days of DDK desktop use on the
`g_1008_800` entry (created by `a7a-clock gpu 1008`). The build's DTB default is now 1008 (`A7A_GPU_MHZ`).
Remaining candidate: 1200 @ 920 mV (the tool's default for 1200; the failing 2026-09-07 entry was 1200 @ 800 mV).
