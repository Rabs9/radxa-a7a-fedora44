# Re-graft of the open GPU driver from upstream master (2026-09-09, evening)

`tools/pvr-graft.sh` now grafts drm/imagination from upstream **master at 50d05c7c76c9** (7.3-rc2 + the 2026-09-09
merges) instead of v6.16. `PVR_UPSTREAM=v6.16 tools/pvr-graft.sh` reproduces the previous graft (kernel
`6.6.98-a7apvr.202609091531`, patches in `tools/patches/pvr-open/v6.16/`; exercised 2026-09-09 18:21: master -> v6.16
-> master leaves the tree byte-identical). The marker `work/kernel-6.6/.pvr-grafted` holds the pin and a fingerprint of
the patch set (core patches, driver patches, compat header, compat-include): a change to any of them, another pin, or
`PVR_REGRAFT=1` un-grafts (git checkout of the core files, grafted files removed) and grafts again on the next run.
Builds: `6.6.98-a7apvr.202609091719` (first, without the MODULE_FIRMWARE line), then the one in "Results".

## What the pin brings (89 commits touch the driver since v6.16; the ones matching our symptoms)

| commit | what | our symptom |
|---|---|---|
| `4baf9e70cb75` + the 2026-04-08 job-submission series | paired fragment job fitted in the correct CCCB | `pvr_cccb.c:178` WARN_ON storms (194 in one session) |
| `2224d6642136` | scheduler timeout period above the firmware timeout | "Job timeout" storms while the firmware was still working |
| `ee7cb7425314` | no timeout for a job whose fence is already signaled | same (needs the 6.17 NO_HANG scheduler status: core patch 0002) |
| `74ef7844dd8c` + `2d7f05cddf4c` | disable + synchronize the IRQ before suspending the GPU | resets right after a runtime power-off |
| `51e39ceeca7e` + `62a6f98cda4e` | firmware power-off for the RISC-V (layout_mars) firmware, no 2nd-thread DM association | "Innocent lockup" on an idle DM after a warm boot |
| `a55c2a5c8d68` | soft-reset deadlock | |
| `42577ba79fbf` + `fe49308cb80d` | FW initialised flag with READ/WRITE_ONCE | |
| `d994acc526c7` (+ `da173557a2b0`, `d2f83a6cd598`) | context reset decode (pvr_dump.c) | was our backport; dropped from the graft |
| `4af24c27a39b`, `d431b4012fd2`, `8dc8f3f4c238`, `cf385cf6e713`, `17e2030f3760`, `de1e8a590f4e`, `6b991ad8dc3a` | assorted fixes (entity double fini, DEV_QUERY size, uobj array stride, vm ctx lookup, vm lock, FW ctx id, no export of protected objects) | |
| `c1079aebb4de` | tracepoints (`pvr_job_create/submit_ioctl/submit_fw/done`) | new diagnostic |
| `a331631496a0`, `ee184ab0ffb6`, `c6978643ea1c`, `812062e74a39` | firmware trace: `init_fw_trace_mask` module parameter loaded at init, debugfs `pvr_fw/trace_mask` | see "renames" |
| `1c21f240fbc1`, `3bf74137340a` | support table + `gpuid`/`exp_hw_support` parameters | REFUSES our GPU: driver patch 0002 |

## What the 6.6 tree has to absorb (all in tools/, nothing in the 79 upstream files)

- **Core patches `tools/patches/pvr-core/`** (shared by BOTH kernel flavours: the DDK flavour builds from the same
  tree, so it needs one boot test before anything is packaged from it):
  - 0001 GEM: `struct drm_gem_object.gpuva` gets the 7.x mutex, initialised/destroyed by the GEM core for every object
    (master's drm_gpuvm.c immediate mode; the driver no longer calls `drm_gem_gpuva_init`), the two-argument
    `drm_gem_gpuva_assert_lock_held(gpuvm, obj)`, the `drm_gem_for_each_gpuvm_bo` iterators. Replaces 6.6's
    lockdep-only `gpuva.lock_dep_map` whose only user (drm_gpuva_mgr.c, nouveau) is out of the build.
  - 0002 scheduler: `DRM_GPU_SCHED_STAT_NO_HANG` + the 6.17 re-insert path in `drm_sched_job_timedout()` (6.6 removes
    the timed-out job from the pending list before calling the driver; a NO_HANG return would leak it), `RESET` as an
    alias of NOMINAL.
  - 0003 Kconfig/Makefile: `DRM_GPUVM`, `imagination/`, `drm_gpuva_mgr.o` out of drm.ko, and `drm_gpuvm.o` built with
    the driver's compat header (`kzalloc_obj`, `init_llist_node`, 2-argument `drm_exec_for_each_locked_object`,
    3-argument `drm_exec_init`).
- **Compat header `powervr-backport/pvr_compat66.h`** (new shims): `drm_sched_start(sched, errno)` -> full recovery
  (moved in from the script), `drm_sched_job_init` with the 6.17 `drm_client_id`, `kmalloc_obj/kzalloc_obj/kvmalloc_obj`
  + `_objs` with the 7.0 default-GFP_KERNEL argument, `dev_pm_domain_attach_list/detach_list` + structs (6.10 code as
  static inlines; never executed here, the gpu node has no `power-domains`), 2-argument `strscpy`, `init_llist_node`
  / `llist_on_list`, `drm_exec_for_each_locked_object(exec, obj)`. `compat-include/linux/pwrseq/consumer.h` is
  master's header (stubs without CONFIG_POWER_SEQUENCING; only the TH1520 uses pwrseq).
- **Driver patches `tools/patches/pvr-open/`**:
  - 0001 runtime-PM robustness, rebased: (1) clear `fwif_sysinit->firmware_started` before every `pvr_fw_start()` in
    `pvr_power_fw_enable()` (master still reads the stale heap flag in `pvr_wait_for_fw_boot()`); (2) restart the
    firmware when `pvr_power_fw_disable()` fails inside runtime suspend (master still returns the error; the new
    3-argument disable re-enables the IRQ itself on failure, so the restart passes `rpm_resume = false`); (3) a gpu
    node without `power-domains` = 0 domains (master still aborts the probe on -ENOENT). Dropped: the per-poll
    `pvr_fw_stop()` warnings (diagnostic; master rewrote the function).
  - 0002 GPU support table: `36.56.104.183` (our BXM-4-64 MC1) as supported. Without it master's driver returns -ENODEV
    at probe unless `powervr.exp_hw_support=1` (and then warns "unsupported hardware").
- **Kept from before, still needed**: the `drm_sched_start` full-recovery mapping (master passes errno 0), `.date`
  (6.6 still has the field), the `.remove` int wrapper. **Dropped**: our copy of d994acc5 and the reset-reason
  defines (in master), the `drm_exec_init` edit of drm_gpuvm.c (the compat header covers it in master mode).

## Renames on the board

- Firmware trace: module parameter `init_fw_trace_mask=` (group mask, loaded at init) instead of `fw_trace_mask=`;
  debugfs `pvr_fw/trace_mask` (group mask, `0x67a`; the TRACE bit is implied and REJECTED) instead of
  `pvr_params/fw_trace_mask` (raw log type, `0x67b`). `pvrwatch.sh`, `fwtrace-tail.sh` and `a7a-gpu-check` detect
  which knob exists; `tools/board/modprobe.d/powervr-trace.conf` carries both names (each kernel ignores the other
  with one "unknown parameter" line).
- `pvr_params/` (debugfs) is gone in master; `pvr_fw/trace_0`, `trace_1` stay.

## Build and deploy

```
A7A_RUN=1 A7A_PVR=1 tools/build-kernel.sh            # graft (idempotent) + kernel, ~1 min warm
A7A_RUN=1 A7A_SKIP_PVRSRVKM=1 tools/build-extras.sh  # aic8800 for the release
```
Install on the board as a NEW extlinux entry next to the working one (this session: by hand, the update stage would
reboot on its own), then reboot with the serial recorder running.

## Test plan (owner's additions)

Baseline on `6.6.98-a7apvr.202609091531`, clear desktop, GPU kept powered, 1008 MHz (2026-09-09 14:0x):
`a7a-gpu-check --bench` 640x480 **323 fps / ~0 ms** KWin, 1024x600 **39 fps / 11 ms**; glmark2 client 640x480 build
**329**, texture **439**, score **383**. (With windows open the same run gave 125 / 22 fps and 131 / 200: measure on a
clear desktop only.)

On the new kernel: `control=auto` (runtime power-down ON), pvrwatch + fwtrace-tail, serial recorder on the laptop,
login + 15 minutes of desktop use: **0 context resets, 0 job timeouts**, plus the two measurements above. If it holds,
the udev rule `90-powervr-no-runtime-suspend.rules` comes off the board and a7a-gpu-check flips to expecting `auto`.

## Review (independent reviewer pass, 2026-09-09 18:00)

No critical issue. Verified against the originals: every shim is a verbatim copy of the upstream definition and every
call form the driver and drm_gpuvm.c use was checked; the scheduler backport matches 6.17 (enum NONE=0, NOMINAL=RESET=1,
ENODEV=2, NO_HANG=3; the extra wake-up is needed because 6.6's kthread only looks at the head of the pending list);
the GEM mutex is inert here (LOCKDEP off, no immediate mode, nouveau off); driver patch 0001 is right against the new
3-argument `pvr_power_fw_disable` (all its failure paths leave the IRQ enabled, so the restart passes rpm_resume =
false). Fixed after the review: the v6.16 mode died at the patch step (its patch inserted the -ENOENT block a second
time; hunk removed), no way to re-graft at the same pin after editing a patch (fingerprint in the marker, PVR_REGRAFT),
`$(srctree)/$(src)` in the kbuild flags, `fw_trace_mask=0x67b` (raw log type) for the v6.16 name in modprobe.d.
Upstream-inherited, left alone: `validate_group_mask(NULL, ...)` on an invalid `init_fw_trace_mask` only survives
because `base` is the first member of `struct pvr_device`; `pvr_queue_timedout_job()` dereferences
`s_fence->parent` unconditionally. Note for the soak: a NO_HANG false timeout prints nothing on master, so "0 job
timeouts" counts real hangs only.

## Results (2026-09-09, kernel `6.6.98-a7apvr.202609091821`, board time 15:31-15:51)

**PASS.** Booted as the default extlinux entry (the v6.16 kernel `202609091531` kept as entry 2, the DDK kernel as entry
3). Driver bound, firmware `rogue_36.56.104.183_v1.fw` v1.1 loaded, no "Unknown GPU" (patch 0002 works), the trace
mask `0x67a` arrived through `init_fw_trace_mask` at init (one "unknown parameter 'fw_trace_mask' ignored" line, as
designed). KWin on `libvulkan_powervr_mesa.so`. Runtime power-down switched to `auto` at 15:32:40, ~30 s after the
owner's login (the login itself therefore ran with the GPU kept powered; a logout/login under `auto` is the one
untested path), then 15 minutes of desktop use plus the benchmarks:

| | |
|---|---|
| context resets / job timeouts / driver WARNINGs | **0 / 0 / 0** (18 samples, and after the benchmark load) |
| GPU runtime suspend time accumulated | 509 s in 15 min; the firmware trace shows 47 host power-offs and 35 firmware cold boots ("State of firmware's private data at boot time: 1", "Core clock set to 1008000000"), no HWR/lockup marker |
| D-state kworkers | only `kworker/u16:0+flush-179:0` / jbd2 (SD-card writeback, block-layer stack), never a powervr worker |
| `a7a-gpu-check --bench` (3 passes) | 640x480 **264-277 fps / 1 ms** KWin; 1024x600 **34-40 fps / 7-12 ms** (baseline 323 / 39 fps, 11 ms) |
| glmark2 client 640x480 (build / texture / score) | **270-289 / 325-378 / 306-323** (baseline 329 / 439 / 383) |
| A/B on the same kernel, `control=on` vs `auto`, glmark2 twice each | 307 vs 307, 311 vs 323: runtime PM costs nothing |

Reading: compositing cost is equal or better (1024x600 at 39-40 fps with 7 ms of KWin CPU per frame vs 11 ms), the
small-window client fps is 10-15 % under the same-day baseline sample but inside the range documented for the a7a
driver (220-270), and the difference is not runtime PM. Whether it is the new submit path (paired-job CCCB fit,
scheduler timeout) or sampling noise needs a back-to-back measurement of both kernels under identical conditions
(two reboots; optional follow-up). Final `a7a-gpu-check`: **0 fail, 0 warn**, "runtime power-down active
(control=auto, status=suspended)".

Consequences applied: `/etc/udev/rules.d/90-powervr-no-runtime-suspend.rules` moved to
`/etc/a7a/disabled/…v616-graft-only` (the v6.16 fallback kernel would need it back), `a7a-gpu-check` now expects
`auto` on the master graft and `on` on the v6.16 graft (it tells them apart by the trace knob / module parameter).

## Open after this session

1. DONE 16:39-16:47 board time, **PASS**: DDK-flavour boot test, see the section below. The tree can be packaged for
   both flavours.
2. DONE 16:00-16:05 board time: logout + login under `control=auto` (the login path is where the v6.16 resets used
   to happen). Clean logout (no KWin hang), new session KWin 78383 on the PowerVR Mesa driver, **0 resets, 0 timeouts,
   0 warnings**, 854 s of GPU suspend time accumulated since boot.
3. Optional: back-to-back benchmark of `202609091531` vs `202609091821` on a fresh session each to settle the
   small-window fps difference.
4. Upstream candidates from this work: the power-domain `-ENOENT` fix, the `firmware_started` clear before a
   firmware restart, the A733 BVNC entry (all still missing in 7.3-rc2).

## DDK-flavour boot test (2026-09-09, kernel `6.6.98-a7a.202609091931`, board time 16:39-16:47)

**PASS: 0 HWR.** The vendor flavour built from the grafted tree (core patches 0001-0003 applied, no A7A_PVR) boots,
binds pvrsrvkm, runs the DDK desktop and a GPU benchmark without a hardware recovery. Nothing in the tree needs to
change for the DDK flavour; Item B (packaging) can build both kernels from it.

Build (laptop, 19:31-19:33): `A7A_RUN=1 tools/build-kernel.sh` (1 min warm, 1352 modules, DTB `img,gpu` only at
1008 MHz, the DDK's clock names, no `assigned-clocks`), then `A7A_RUN=1 tools/build-extras.sh` (aic8800 + pvrsrvkm
with patches 0001 fdinfo + 0005 dma-buf importer, both already applied in the rogue_km tree). Kbuild recompiled
exactly ONE DDK object, `services/server/env/linux/physmem_dmabuf.o`: it is the only pvrsrvkm source that includes
`drm_gem.h`, and it EMBEDS a `struct drm_gem_object` (`sBase`, the dma-buf exporter's object) that core patch 0001
enlarges by the gpuva mutex. That is the whole ABI exposure of the DDK flavour to the graft; the scheduler patch
(0002) and Kconfig/Makefile (0003) touch nothing pvrsrvkm links against. vermagic of the new module
`6.6.98-a7a.202609091931 SMP preempt mod_unload aarch64`.

Install (by hand, the update stage would prune and reboot): kernel, DTB dir, modules, firmware, depmod, the
fdinfo-fixed marker, then `a7a-extlinux --check-files add 6.6.98-a7a.202609091931` WITHOUT `--default` (entry 2,
right behind the open-stack default; nothing pruned). Switch for the test = backtoddk.sh's steps without its
reboot: `pvr-desktop on`, cap_sys_nice stripped + `a7a-kwin-nocap.path` enabled, `a7a-extlinux default
fedora-6.6.98-a7a.202609091931`, owner's go, `systemctl reboot` with the serial recorder running.

| | |
|---|---|
| boot | ssh back after 34 s; `pvrsrvkm` bound to 1800000.gpu, `sunxi_parse_dts clk_rate:1008000000`, regulator default 800000, `Read BVNC 36.56.104.183`, `Initialized pvr 24.2.6603887` on minor 1 (card1/renderD128); aic8800_fdrv loaded |
| dmesg | 0 Oops / BUG / WARNING / Call trace, 0 IOMMU fault lines; the usual `pdtest ... deferred probe timeout` pair |
| greeter | plasmalogin up (software KWin), `/sys/kernel/debug/pvr/status` Driver OK / Firmware OK / Server Errors 0 |
| login | owner's login 16:42; KWin 1987 maps `/opt/pvr/lib/libEGL.so.1`; firmware `rgx.fw.36.56.104.183` + shader image loaded at 59.6 s |
| `a7a-gpu-check` | **0 fail, 0 warn** (stack ddk): 1008 MHz @ 800 mV validated pair, 0 HWR, no D-state kworkers, DDK drop-in present, cap stripped, compositor on the DDK |
| `pvr-run glmark2-es2 -s 640x480` (Xwayland, build/texture/shading/bump 3 s each) | `PowerVR B-Series BXM-4-64`: **1754 / 1775 / 1479 / 2459 fps, score 1865**; HWR 0 before and after |
| DDK status after the run | HWR 0, CRR 0, SLR 0, WGP 0, TRP 0, FWF 0, APM events 2569 (the DDK's own power management) |
| KWin journal | only the known DDK-desktop lines (CAP_SYS_NICE stripped on purpose, `/dev/dri/none` from KWIN_RENDER_NODES, blur downsampling shader, one DRI2 rebind warning); no reset / guilty / lockup |
| final `a7a-gpu-check` | 0 fail, 0 warn |

Reference for the score: 1267 (2026-09-08, first Xwayland-glamor measurement) and 1874-1918 (2026-09-09, second
rootless Xwayland) on the pre-graft `kernel-a7a` at the same clock.

Return: `prepare616.sh` (pvr-desktop off + none, software.conf removed, cap_sys_nice back, nocap units disabled,
default -> newest a7apvr entry = `202609091821`), then the reboot. `prepare616.sh` was edited on the board first
(backup `/usr/local/sbin/prepare616.sh.bak-20260909`): its "open-stack environment" section still re-created the zink
+ conformance overrides (`91-powervr-mesa.conf/.sh`, the kwin and plasmalogin `mesa.conf` drop-ins) that the a7a Mesa
driver made unnecessary and that `a7a-gpu-check` warns about; the section now parks any such file in
`/etc/a7a/disabled/` instead (0 present after the run). `backtoddk.sh` is unchanged and still boots `g_1008_800`
(the 2026-09-07 DDK kernel with the a7a-clock DTB); the new entry `fedora-6.6.98-a7a.202609091931` is the equivalent
built from the grafted tree.

Small things met on the way: (1) `python3 -m src.cli serial --record` exits with `# port error: Resource temporarily
unavailable` (EAGAIN on the CH343) in the middle of the shutdown sequence; restarted by hand it caught the rest of the
boot (log `output/logs/serial-record-20260909-19392*.log`); the record loop should retry EAGAIN. (2) An X11 client
from ssh needs `XAUTHORITY=/run/user/1000/xauth_*` besides `DISPLAY=:0` (KWin's Xwayland runs with `-auth`); the
off-screen variant is the X11 build too, so `pvr-run glmark2-es2 --off-screen` without a display fails with "Could not
initialize canvas". (3) `HWR Event Count` is absent from `/sys/kernel/debug/pvr/status` until the firmware has been
loaded by the first GPU client (the greeter is software); `a7a-gpu-check` counts the missing line as 0, which is right.
(4) Two failed units at boot on this kernel, `lm_sensors.service` and `logrotate.service`; unrelated to the GPU (to
compare on the open-stack boot).

Return boot (16:50-16:53 board time): the owner had logged out on the DDK desktop and logged in AGAIN after
`prepare616.sh` had run: black screen. Not the GPU (HWR still 0, no D-state): the new session's KWin (pid 12717) ran
on Fedora's llvmpipe without `KWIN_COMPOSE=Q`, 24x `Error creating EGLImageKHR: EGL_BAD_ALLOC`, the 2026-09-07 trap
(`pvr-desktop off` + software.conf removed = no drop-in at all on the DDK kernel). Rule: after `prepare616.sh` on the
DDK kernel, reboot before any login; the script now says so. `systemctl reboot` over ssh -> `202609091821` back in
44 s, greeter up, owner's login: KWin 2607 on `/usr/local/lib64/libvulkan_powervr_mesa.so`, `a7a-gpu-check` **0 fail,
0 warn**, runtime power-down `auto`. Right after that boot the check had reported "1 IOMMU fault line": the boot
message `iommu: Default domain type: Translated` matched `fault` inside "Default" (case-insensitive grep; it only
shows while the line is still in the dmesg ring). Fixed in `tools/board/a7a-gpu-check` (`\bfault`) and installed on
the board. Failed units on BOTH kernels, unrelated to the GPU: `lm_sensors`, `logrotate`, `plocate-updatedb`.
Serial log of both boots: `output/logs/serial-record-20260909-193953.log`.
