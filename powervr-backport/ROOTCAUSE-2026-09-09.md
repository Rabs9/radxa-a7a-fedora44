# Why the open driver wedges on the A733: the host reads garbage from firmware memory

**2026-09-09.** The desktop on v6.16 hung. The diagnosis is complete and it is
not a configuration problem.

## What was observed

Load average 4.3 on an idle desktop, keyboard apparently dead, but ssh fully
responsive - the machine was not frozen, it was blocked.

```
kworker/u16:5+powervr-sched   D   04:32:57
kworker/u16:2+powervr-sched   D   04:25:49
kworker/u16:4+powervr-sched   D   04:19:10
kworker/u16:6+powervr-sched   D   04:17:00
```

Four GPU scheduler workers stuck in uninterruptible sleep, all with the same
stack:

```
dma_fence_default_wait
dma_fence_wait_timeout
drm_sched_stop                [gpu_sched]
pvr_queue_timedout_job        [powervr]
drm_sched_job_timedout        [gpu_sched]
```

The job-timeout handler waits on a fence that cannot signal, because the GPU it
is trying to recover is already wedged. Each timeout leaks one worker
permanently. Four of them is the load average, and it is why the desktop stops
responding while ssh keeps working.

## What actually broke first

```
WARNING: CPU: 4 PID: 169554 at drivers/gpu/drm/imagination/pvr_cccb.c:178
	pvr_cccb_write_command_with_header
	<- pvr_queue_submit_job_to_cccb
	<- pvr_queue_run_job
```

That line:

```c
read_offset = READ_ONCE(ctrl->read_offset);
cccb_space = get_ccb_space(pvr_cccb->write_offset, read_offset, pvr_cccb->size);
if (WARN_ON(cccb_space < required_size))
	return;
```

`ctrl->read_offset` is **written by the firmware** and read by the host. If the
host reads a bogus value, `cccb_space` is nonsense, the WARN fires, the command
is never written, and the job it belonged to can never complete - so it times
out, and the timeout handler deadlocks as above.

## One cause, two symptoms

The other message all session has been:

```
[drm] Received unknown FWCCB command 2abc006b
[drm] Received unknown FWCCB command 2abc0069
```

`cmd_type` is also read out of firmware-written shared memory, and those values
are not commands - the driver's enum has 19 entries, and the value *changes*
between reads. It is garbage, not an unknown opcode.

Both symptoms are the same thing: **the host is not reliably seeing what the
firmware wrote.**

## Why this SoC and not the others

The vendor DDK says outright that this GPU is not cache-coherent:

```c
/* rgx_sunxi/sysconfig.c */
psDevConfig->eCacheSnoopingMode = PVRSRV_DEVICE_SNOOP_CPU_ONLY;
psDevConfig->eCacheSnoopingMode = PVRSRV_DEVICE_SNOOP_NONE;
```

The in-tree driver allocates the CCB control structures with:

```c
pvr_cccb->ctrl = pvr_fw_object_create_and_map(pvr_dev, sizeof(*pvr_cccb->ctrl),
					      PVR_BO_FW_FLAGS_DEVICE_UNCACHED, ...);
```

and

```c
#define PVR_BO_FW_FLAGS_DEVICE_UNCACHED DRM_PVR_BO_BYPASS_DEVICE_CACHE
```

That flag bypasses the **GPU's** cache. It says nothing about how the memory is
mapped on the **CPU** side. The driver then polls these structures with plain
`READ_ONCE()` and no `dma_sync_*` anywhere in `pvr_ccb.c` or `pvr_cccb.c` -
which is correct only if the CPU's view is coherent with the device.

The platforms this driver is developed on - TI AM62x and the T-Head TH1520 -
have IO-coherent GPU interconnects, so upstream never encounters this. The A733
does not, by the vendor's own configuration.

## Why it looked fine in benchmarks

`glmark2-es2-gbm --off-screen` ran three full passes with **zero** timeouts and
steady scores (206/206/208). The desktop wedged within minutes. The difference
is submission pattern: a benchmark keeps one context hammering a queue, while a
compositor plus clients means many contexts, frequent small submissions and
constant CCB wraparound - far more opportunities to read a stale offset.

So the benchmark result was real but not representative, and this correction
matters: **v6.16 fixed the timeouts under the load I tested, and did not fix
them under the load that matters.**

## What would actually fix it

In rough order of likelihood:

1. **Map firmware-shared objects uncached or write-combine on the CPU side.**
   The device-side flag already asks to bypass the GPU cache; the CPU mapping
   needs to match on a non-coherent SoC.
2. **Find out whether the A733's GPU can be made IO-coherent.** The DDK selects
   between `SNOOP_CPU_ONLY` and `SNOOP_NONE` at runtime, which implies the
   hardware has a coherency mode worth understanding. If it can snoop, marking
   the node `dma-coherent` would be the clean fix.
3. **Fix the timeout handler regardless.** `pvr_queue_timedout_job` leaking a
   worker per timeout into an unbounded `dma_fence_default_wait` is a bug in its
   own right - a wedged GPU should not cost a kworker permanently. This is worth
   reporting upstream separately, since it is platform-independent.

## Status

Board is back on the DDK: `g_1008_800`, `pvrsrvkm` loaded, `card1` present,
desktop working as before. Everything for the open stack stays installed and is
two commands away.

**The open stack is not a usable desktop yet.** What it is, is a fully
identified problem with a specific cause, on hardware that had no open driver at
all yesterday.
