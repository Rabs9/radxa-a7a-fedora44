# The open-stack wedge, measured on 2026-09-09: not garbage, not coherency, not the clock

Board on entry `fedora-6.6.98-pvr+` (drm/imagination v6.16 backport, firmware `rogue_36.56.104.183_v1.fw`
"v1.1 build 6976702", Mesa 26.1.8 powervr + zink). Serial recorder on the laptop
(`output/logs/serial-record-20260909-104355.log`), `powervr-backport/clockcheck616.sh` right after boot,
`wedgewatch.sh` (10 s samples) after the owner logged in.

## What the measurements say

| check | result |
|---|---|
| A0 GPU clock / voltage under the upstream driver | pll-gpu = gpu0 = **840 MHz**, dcdc4 = **800 mV**: inside the validated envelope (1008 @ 800 passed the matrix). The clock is not the cause. |
| session compositor | KWin 6.7 on `libvulkan_powervr_mesa.so` + zink, cap_sys_nice=ep, both nocap units off |
| time to wedge | login ~120 s; first anomaly 217.9 s; owner: "moves like a turtle but sometimes normal"; frozen ("pressed start, not responding") ~330 s |
| sequence in dmesg | 217.904 s `WARN_ON` at `pvr_cccb.c:178` (client CCB has no room), thread `pvr-queue`; 218.645 s first `Received unknown FWCCB command 2abc0069`; 326.2 s first `Job timeout`; then 2 kworkers (`kworker/u16:*+powervr-sched`) in D state, load 2.3, desktop dead, ssh fine |
| counts at the end | 194 CCB WARNs, 30 FWCCB "unknown" (all `2abc0069`), 4 job timeouts, 0 resets seen by the driver |
| firmware trace, enabled live at the wedge (`/sys/kernel/debug/dri/1/pvr_params/fw_trace_mask`) | firmware alive and idle: only KCCB health checks (`Cmd=0x2abc0073`), "Fence Counters: Check: 19317 - Update: 19317", "Any RD-DM pending? 0". The firmware has NO outstanding work while the host has jobs it considers running. |

## Decoding "2abc0069": it is a context reset notification, not garbage

`pvr_rogue_fwif.h` (v6.16) defines every firmware-to-host command as `number | ROGUE_CMD_MAGIC_DWORD_SHIFTED`
with the magic `0x2abc` in the upper 16 bits:

```
ROGUE_FWIF_FWCCB_CMD_CONTEXT_RESET_NOTIFICATION = 105 | 0x2abc0000  -> 0x2abc0069   (what we saw today, 30 times)
ROGUE_FWIF_FWCCB_CMD_UPDATE_STATS               = 107 | 0x2abc0000  -> 0x2abc006b   (the other value seen 09-08)
```

The v6.16 driver's `pvr_fwccb_process_one()` handles only REQUEST_GPU_RESTART, FREELISTS_RECONSTRUCTION and
FREELIST_GROW; everything else prints "unknown". So the firmware was reporting **GPU context resets** (its
hardware-recovery path) and the driver threw the report away. The value "changing between reads" on 09-08 was
two different commands (reset notification vs stats update), not corrupted memory. The other session's
coherency theory is therefore withdrawn: the CPU mappings are write-combined anyway (`pvr_gem.c`,
`map_wc = !(flags & PVR_BO_CPU_CACHED)`, cached only with `dma-coherent` in the DT), and the messages are valid.

Upstream fixed the decode after v6.16: commit `d994acc526c7` (2026-03-23, "drm/imagination: Implement handling
of context reset notification", adds `pvr_dump.c/h`, prints reason / data master / job ref / fault address) and
silences UPDATE_STATS. That decode is the diagnostic we need next: it says WHY the firmware resets contexts
(guilty lockup = a hung shader, most likely Mesa's immature BXM compiler; FW_PAGEFAULT = a memory/MMU problem).

## Why one reset becomes a permanent wedge: a backport defect

v6.16 `pvr_queue_timedout_job()` ends with `drm_sched_start(sched, 0)`, where the second argument is an
**errno** (6.12+ signature `drm_sched_start(struct drm_gpu_scheduler *, int errno)`). Kernel 6.6 has
`drm_sched_start(struct drm_gpu_scheduler *, bool full_recovery)`. The call compiles unchanged, so the backport
restarts every timed-out scheduler with `full_recovery = false`, and 6.6's implementation then skips
`dma_fence_add_callback()` on every pending job and does not restart the timeout:

```
	list_for_each_entry_safe(s_job, tmp, &sched->pending_list, list) {
		...
		if (!full_recovery)
			continue;                 /* <- no completion callback is ever re-armed */
		r = dma_fence_add_callback(fence, &s_job->cb, drm_sched_job_done_cb);
```

After the first job timeout on a queue, that queue can never complete another job: the app blocks on its fences,
the compositor freezes. `pvr_compat66.h` already wraps `drm_sched_init()` for the 6.14 argument change; this
one was missed. Fix: `#define drm_sched_start(sched, errno) drm_sched_start((sched), true)` in the compat header.
(The D-state `powervr-sched` kworkers are the timeout work itself; capture `/proc/<pid>/stack` at the next wedge
to see what it waits on.)

Second, cosmetic: `[drm] Initialized powervr 1.0.0 (null)` and the `WARNING at drm_ioctl.c:477 drm_copy_field`
from KWin at boot: 6.14 removed `struct drm_driver.date`, so the v6.16 `pvr_drv.c` leaves it NULL on 6.6 and the
version ioctl warns once. Set `.date` in the compat layer.

## Plan (what the next kernel build carries)

1. compat: `drm_sched_start` -> full recovery (the wedge); `.date` (the warning).
2. backport `d994acc526c7` (pvr_dump.c/h + pvr_ccb.c hunk): decode context resets, silence UPDATE_STATS.
3. boot with the firmware trace on (module parameter `powervr.fw_trace_mask=`, HWR + MAIN groups at least) and
   snapshot `/sys/kernel/debug/dri/1/pvr_fw/trace_*` to /home every few seconds during the session (the ring is
   small and /tmp is lost at reboot: today's copies in /tmp were lost).
4. At the next reset: reason + DM + fault address from (2), the firmware's own HWR lines from (3), and whether the
   desktop now survives a reset (with (1) the queue should recover). If resets are guilty lockups in Mesa-generated
   shaders, the work moves to Mesa (newer pvr driver, BXM fixes); if page faults, to the kernel/MMU side.
5. Build on the laptop from the a7a-fedora tree (the box is unreachable; roadmap A8 wants this anyway).

## Traps met today

- `/tmp` on the board is tmpfs: evidence saved there is gone after `backtoddk.sh`. Save under /home/radxa.
- The firmware trace mask defaults to 0; enable it before the run, not after.
- The wedge leaves ssh working; `backtoddk.sh` (reboot to the DDK) is the recovery.

## The open-stack kernel now builds from this repo (2026-09-09, afternoon)

`A7A_RUN=1 A7A_PVR=1 tools/build-kernel.sh` then `A7A_RUN=1 A7A_SKIP_PVRSRVKM=1 tools/build-extras.sh`: release
`6.6.98-a7apvr.<stamp>`, one minute with a warm ccache. `tools/pvr-graft.sh` (idempotent) puts v6.16's
drm/imagination + drm_gpuvm into the 6.6 tree with the other session's compat header and adds what was missing:

- the `drm_sched_start` full-recovery mapping and the `.date` field (above);
- the power-domain probe fix (`pvr_power.c`: a node without `power-domains` returned -ENOENT and aborted the
  probe: "probe of 1800000.gpu failed with error -2"; the other session had fixed it by hand on the box);
- upstream `d994acc526c7` (context reset decode; reset reasons 6..11 defined for pvr_dump.c);
- `CONFIG_DRM_NOUVEAU` off: nouveau was the only user of 6.6's drm_gpuva_mgr, which the graft removes from drm.ko.

Board: entry `fedora-6.6.98-a7apvr.202609091208`, `/etc/modprobe.d/powervr-trace.conf` sets
`fw_trace_mask=0x67a` (MAIN, CLEANUP, CSW, BIF, PM, POW, HWR), `/usr/local/sbin/pvrwatch.sh` snapshots
the firmware trace rings, dmesg and D-state stacks under `/home/radxa/pvr-logs/<stamp>/`.
`prepare616.sh` (session config for the open stack) is in /usr/local/sbin now: the /tmp copy vanished at the first
reboot, which is why the first boot of the new kernel came up with the DDK session config.

## Results with the fixed kernel (6.6.98-a7apvr.202609091208, afternoon soak)

**The wedge is gone.** Resets and job timeouts still happen, but the desktop survives every one of them (D-state
kworkers 0, session alive through 28 resets and ~270 timeouts): the `drm_sched_start` recovery fix works.

**Every reset decoded as `Reset reason=2 (Innocent lockup)`**, alternating Data Master 2 (Geometry) and 3
(Fragment), no page faults, no guilty context. The firmware's own HWR log (groups MAIN|CLEANUP|CSW|BIF|PM|POW|HWR,
`fwtrace-tail.sh`) shows the detector tripping on an EMPTY pipe: "Full Signature Check result for Core0, DM3 is
HWRNeeded=1", "USC Slots result ... USCSlotsUsedByDM=0", "DM3 has taken N ticks and deadline is 98430000 ticks",
"At least one other DM is running okay so DM3 will get another chance" - right after a host-initiated power-off
("OS requested forced IDLE", "OS requested pow off (forced = 1)", "GPU units deinit", "GPU deinit"). After some
power-offs the firmware clock restarts ("State of firmware's private data at boot time: 1 (initialised)" at tick
~144), after others it keeps counting: the bus reset the driver asserts at suspend does not reliably reset the
firmware core, and a warm-resumed firmware believes DM3 still has work.

**Experiment: keep the GPU powered** (`echo on > /sys/bus/platform/devices/1800000.gpu/power/control`):
three glmark2 runs and desktop use with **0 resets, 0 timeouts**; with `auto` the same run produced +6 resets and
hundreds of timeouts (not on every cycle: one later cycle was clean). Installed on the board as
`/etc/udev/rules.d/90-powervr-no-runtime-suspend.rules` (matches DRIVER=="powervr" only, so the DDK kernel is
untouched). Cost: GPU clocks stay on while idle (the firmware still idles the units internally). Proper fix later:
find out why the warm boot goes wrong (reset order with clocks gated? the `reset_bus_smmu_gpu*` resets the DDK
also lists? `pvr_fw_stop` before power-off?) - upstream on TH1520/AM62 cuts a real power domain and cold-boots.

**Clock cleared, coherency cleared, the 09-08 "garbage" explained.** Nothing left of the original theories.

**Separate performance problem, not GPU-bound: KWin's compositing of tall client buffers.** With the GPU kept
powered and idle 98% of the frame (firmware timestamps ~3.2 M ticks/s, a whole frame is ~0.8 ms of GPU time):

| glmark2 window | fps | KWin CPU per client frame |
|---|---|---|
| 640x480, 640x512, 800x510, 1024x384, 832x480 | ~80-118 | ~9 ms |
| 640x632, 800x592/600/608, 1024x576/600, 512x800 | ~12-18 | ~117 ms |
| fullscreen 1024x600 (direct scanout, no compositing) | ~48 under load | ~11 ms |

Not width, not area, not tile alignment: every fast case is at most ~512 rows tall, every slow case is taller
(under the extra load of an open system monitor the edge blurs). In the slow case KWin (zink on the pvr Vulkan
driver) issues ~107 `PVR_CREATE_BO`+`VM_MAP`+`VM_UNMAP`+`GEM_CLOSE` cycles and ~56 syncobj create/destroy per
frame and maps ~11 x 128 KiB staging chunks: the client's dma-buf is re-uploaded through the CPU each frame
instead of sampled directly, although `pvr_formats.c` advertises sampling for the LINEAR modifier. The desktop's
own surfaces are 1024x600, i.e. on the slow side - this is the "turtle" the owner feels even without resets.
Direction: Mesa. First cheap step: Fedora 44 ships Mesa 26.2.2 (the board runs 26.1.8; 26.2.2 has the explicit
36.56.104.183 device entry, identical to 36.52.104.182's); update and re-measure the table. Then `ZINK_DEBUG=map`
in KWin's environment and a `perf` profile (install `perf`; debuginfod for symbols) to find who copies.

Board scripts: `/usr/local/sbin/pvrwatch.sh`, `fwtrace-tail.sh` (continuous FW trace, reboot-safe),
`clockcheck616.sh`, `prepare616.sh` / `backtoddk.sh`. Logs: `/home/radxa/pvr-logs/`.

## Correction and the real cause of the compositor slowness (2026-09-09, later)

The "all sizes fast" measurement at 09:57 was an artifact: SuperTuxKart was fullscreen at the time, so KWin never
composited the hidden benchmark window. On a fresh session the slow path is there every time (800x600 client:
~100 ms of KWin CPU per frame, 10-15 fps; 640x480: ~5 ms, ~100 fps), with GPU runtime power management on or off.

`perf record` on kwin_wayland (perf installed on the board; symbols via debuginfod):
- 47% kernel: `memset`, `__pi_clear_page`, `__pi_dcache_clean_poc`, vmap/shmem alloc+free = creating and zeroing GEM
  buffers. Caller chain: `zink_draw -> zink_batch_rp -> pvr_rogue_CmdBeginRendering -> pvr_rogue_render_state_setup
  -> pvr_spm_scratch_buffer_get_buffer -> pvr_bo_alloc -> DRM_IOCTL_PVR_CREATE_BO`.
- 29% libvulkan_powervr_mesa.so: `pco_ra`, `pco_opt`, `nir_lower_tex`, `blake3`, `ralloc` = the driver COMPILING
  shaders inside CmdBeginRendering (the per-render load-op program, `pvr_arch_load_op_shader_generate`, no cache).

Root cause 1 (`src/imagination/vulkan/pvr_spm.c`, `pvr_spm_scratch_buffer_get_buffer`): the partial-render
scratch buffer store keeps one buffer and reuses it only when `head_ref->size == size`. KWin's renders alternate
between sizes, so every BeginRendering with a large render area frees the buffer and allocates a new multi-MB one,
which the kernel zeroes. Fix: reuse when `head_ref->size >= size` (one line). Root cause 2: the load-op shader is
regenerated per BeginRendering (compile cost ~1-2 ms each, many per frame); a cache keyed on the load-op
parameters is the follow-up.

Build: `tools/mesa/build-pvr-vulkan.sh` on the board (Fedora's mesa-26.1.8 src.rpm, Vulkan driver only, LLVM
needed for the driver's CLC programs, installs to /usr/local and masks Fedora's ICD JSON). Also patches the
conformance list so PVR_I_WANT_A_BROKEN_VULKAN_DRIVER is no longer needed anywhere.

## Power fix soak (kernel 6.6.98-a7apvr.202609091325)

With GPU runtime power-down re-enabled (`control=auto`): 0 resets, 0 timeouts, no "idle poll failed" and no
"suspend aborted" warnings through 10+ minutes of desktop use and GPU bursts (before the fix: 22 resets in the
first two minutes). The kernel-side patch (tools/patches/pvr-open/0001-runtime-pm-robustness.patch): clear the
stale `firmware_started` flag before every firmware start; if the idle polls of `pvr_fw_stop` fail, restart the
firmware instead of leaving runtime PM believing an already-shut-down GPU is active; report which poll failed.
Which of the three mattered is not separated yet. The clock pin via `assigned-clock-rates` on CLK_GPU0 did not
take (840 MHz): above 800 MHz the DDK programs the parent PLL, so the next build pins CLK_PLL_GPU0 too.

## Upstream leads found 2026-09-09 (SearXNG at localhost:8080; freedesktop/kernel.org are behind Anubis)

Kernel, drm/imagination commits after v6.16 that match our symptoms (GitHub mirror of torvalds/linux):
`4baf9e70cb75 Fit paired fragment job in the correct CCCB` (2026-03-30; our pvr_cccb.c:178 WARN_ON),
`2224d6642136 Set scheduler timeout period higher than Firmware timeout` (2026-07-29; our Job timeout storms),
`ee7cb7425314 Don't timeout job if its fence has been signaled` (2026-05-19), `74ef7844dd8c` + `2d7f05cddf4c`
Disable/Synchronize interrupts before suspending the GPU (2026-03-10), `51e39ceeca7e Improve firmware power off for
layout_mars config` + `62a6f98cda4e Skip 2nd thread DM association for non META Firmware` (2026-03-13; pvr_fw_stop on
a RISC-V firmware), `a55c2a5c8d68 Fix deadlock in soft reset sequence`, `42577ba79fbf`/`fe49308cb80d` FW booted ->
initialised with READ/WRITE_ONCE (2026-05-19), plus the 2026-03-30 job-submission series. Plan: move the graft from
v6.16 to current upstream (compat header grows: kmalloc_obj, dev_pm_domain_attach_list, pwrseq, trace points).

Mesa: 26.2.0 adds 21 pvr extensions (no perf notes); Imagination blog 2026-03-16 "PowerVR: the path to open-source
zink and OpenGL ES support". github.com/ayiejosh/a733-powervr-fex (Debian 13 + BSP + vendor stack on the A7A): their
"wall" (a live compositor deadlocks pvrsrvkm) is our patch 0005; zink over the VENDOR Vulkan blob reaches glmark2
off-screen 661 (Mesa pvr Vulkan + zink: ~210 here), so the vendor ICD + zink is a faster GL path on the DDK side.

## Mesa PowerVR driver: three caches, measured in KWin (2026-09-09 afternoon, `tools/mesa/build-pvr-vulkan.sh`)

KWin CPU per composited frame of a client window (glmark2, GPU kept powered, cache hit rates read from KWin's
journal with `/tmp/pvr-a7a-cache-debug`):

| KWin driver | 640x480 | 800x600 | 1024x600 |
|---|---|---|---|
| Fedora 26.1.8 (morning) | ~9 ms | ~100-126 ms (10-15 fps) | ~117 ms |
| + scratch buffer reuse by size | ~3 ms | ~43 ms | ~46 ms |
| + render-state cache (648 hits / 23 misses = distinct sizes) | ~1 ms | ~14 ms | ~15 ms |
| + load-op program cache (666 / 5) | ~1 ms | ~18 ms (noise) | ~20 ms |
| + sub-allocator chunk free list | ~1 ms | ~13 ms | **~8 ms (42 fps)** |

Client-side (glmark2 640x480, its own driver): build 194 -> 267, texture 263 -> 359 fps. What remains in KWin's
profile: libc malloc/free (17%), control-stream chunk allocation (`pvr_rogue_csb_alloc_dwords -> pvr_bo_alloc`),
per-draw state validation, a few shader variants (`pco_ra_func`), i.e. general per-draw driver overhead.

Also found from the cache keys: KWin composites in 10-bit (VkFormat 64 = A2B10G10R10, two output registers per
pixel) on this display. Untested lever: `KWIN_DRM_PREFER_COLOR_DEPTH=24` in a KWin drop-in (8-bit framebuffer).

The patched driver needs NO environment variables: `/usr/local/lib64/libvulkan_powervr_mesa.so` with its ICD JSON
in `/usr/local/share/vulkan/icd.d/`, Fedora's `powervr_mesa_icd.aarch64.json` masked (`.disabled-by-a7a`);
`/etc/environment.d/91-powervr-mesa.conf`, `/etc/profile.d/91-powervr-mesa.sh` and both `mesa.conf` drop-ins moved
to `/etc/a7a/disabled/`. SuperTuxKart launched from the menu runs on the GPU as a native Wayland client (~40% of a
core; 418% in software).

## Cheap wins round (2026-09-09, 15:30-16:00)

- GPU clock pinned for the open flavour: `assigned-clocks = <&ccu CLK_PLL_GPU0>, <&ccu CLK_GPU0>` + rates (the divider
  clock alone did not take; the DDK programs the PLL above 800 MHz). Kernel `6.6.98-a7apvr.202609091531`: gpu0 =
  pll-gpu = 1008 MHz at 800 mV, the validated pair. 0 resets, 0 timeouts through the session (GPU kept powered).
- 8-bit framebuffer: NOT achievable by configuration. `KWIN_DRM_PREFER_COLOR_DEPTH=24` and `maxBitsPerColor: 8` in
  kwinoutputconfig.json only limit the link bpc (`DrmOutput::decideAutomaticBpcLimit`, `setMaxBpc`); the buffer
  format comes from `OutputLayer::filterAndSortFormats` (kwin v6.7.4 src/core/outputlayer.cpp), which for equal
  bits-per-pixel always prefers more bits per color, so AB30 wins over AR24 whenever the driver offers it. Forcing
  8-bit would mean hiding the 2101010 formats in our Mesa driver; same 4 bytes per pixel either way, so not pursued.
  Scanout stays `1024x600 AB30`. Measurements at 1008 MHz: 640x480 ~250 fps / 1 ms KWin, 1024x600 38-46 fps / 8-12 ms.
- Owner: "it seems more responsive". Open request: GPU usage in System Monitor (roadmap F2: fdinfo drm-engine
  accounting in the kernel driver for the per-process column; a ksystemstats backend for the GPU page).
