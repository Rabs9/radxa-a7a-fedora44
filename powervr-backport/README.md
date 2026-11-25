# PowerVR in-tree driver, backported to the 6.6.98 A7A BSP

**Status: builds. Not yet loaded, not yet bound, never run.**

This is the open `drm/imagination` driver (Linux 6.8) plus its one missing DRM
core dependency, grafted onto the Allwinner BSP kernel the A7A actually boots.
The goal is to replace the DDK entirely, which removes at a stroke the four
structural limits documented in `docs/UPSTREAM-DRIVER-2026-09-08.md`: no Wayland
EGL platform, no GLX, no glvnd vendor entry point, no `VK_EXT_robustness2`.

Do not put this on GitHub until it is proven on hardware.

## Why v6.8 and not something newer

`include/uapi/drm/pvr_drm.h` is **byte-identical from v6.8 through v6.16**
(sha256 `29ba017ceed95d4f...`, 40166 bytes at every tag checked). The UAPI has
not moved since the driver landed, so the oldest version still satisfies current
Mesa - and it is the closest to our 6.6 base, which makes it the cheapest
backport. Take the driver and `drm_gpuvm` from the *same* tag so their internal
API agrees.

## What gets imported verbatim

| From v6.8 | To |
|---|---|
| `drivers/gpu/drm/imagination/` (72 files, 32,801 lines) | same path |
| `drivers/gpu/drm/drm_gpuvm.c` (2,752 lines) | same path |
| `include/drm/drm_gpuvm.h` (1,247 lines) | same path |
| `include/uapi/drm/pvr_drm.h` | same path |

## What had to be adapted

`0001-powervr-backport-adaptations.patch` - **46 lines added, 6 removed**, total.
Five API drifts and one config conflict, none of them semantic:

1. **`drm_gem_for_each_gpuvm_bo{,_safe}`** - added to `drm_gem.h`. 6.8 introduced
   these; the underlying `obj->gpuva.list` field is identical in 6.6, so they
   apply verbatim. They coexist with 6.6's `drm_gem_for_each_gpuva()` because
   the two walk the same list at different container offsets, and no GEM object
   is ever used by both the old and the new manager.

2. **`SZ_64G` / `SZ_128G`** - 6.6's `sizes.h` stops at `SZ_32G`. Scoped into
   `pvr_rogue_heap_config.h` rather than added to the core header: `sizes.h` is
   included almost kernel-wide, and editing it forces a near-total rebuild on
   every iteration.

3. **`drm_exec_init()`** gained a third argument (a preallocation hint) in 6.8.
   Dropped - 6.6 allocates a `PAGE_SIZE` array up front regardless. 3 call sites.

4. **`drm_sched_job_init()`** gained a `credits` argument. The driver passes 1,
   which is exactly 6.6's fixed behaviour. Dropped.

5. **`drm_sched_init()`** gained `submit_wq` and `num_rqs`. 6.6 runs a kthread
   per scheduler rather than a shared workqueue, and always allocates one run
   queue per priority. Both dropped. The driver's entity uses
   `DRM_SCHED_PRIORITY_KERNEL`, which exists in 6.6.

6. **`drm_gpuva_mgr` conflict.** 6.6's `drm_gpuva_mgr.c` is `drm_gpuvm` one
   rename and one rework earlier, and exports the same `drm_gpuva_*` symbols.
   It sits in the unconditional `drm-y` list, so its exports live in vmlinux and
   modpost rejects the duplicate. Upstream solved this by replacing it and
   porting its only user, nouveau. We do the same minus the port: nouveau drives
   NVIDIA hardware, of which an A733 has none. `CONFIG_DRM_NOUVEAU=n` and the
   object drops out of `drm-y`.

## Config delta

```
+ CONFIG_DRM_GPUVM=m
+ CONFIG_DRM_POWERVR=m
- CONFIG_DRM_NOUVEAU=m           (and its sub-options)
```

## Build

```
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- BSP_TOP=bsp/
make olddefconfig
make -j$(nproc) Image modules
```

`BSP_TOP=bsp/` is not optional on this tree: `bsp/Kconfig` uses it, and without
it syncconfig aborts *and deletes* `include/generated/autoconf.h`, which breaks
the tree for everything else.

Result:

```
powervr.ko            1,222,704 bytes   vermagic 6.6.98+ SMP preempt mod_unload aarch64
drm_gpuvm.ko            190,960
drm_exec.ko              54,848
gpu-sched.ko            273,256
drm_shmem_helper.ko      85,768
Image                40,159,240
```

Build time on 24 cores, from the pre-built BSP tree: about 1m30s.

## What is NOT done - the device tree

This is the unpredictable part, and it is where the work now is.

The v6.8 driver binds **`img,img-axe` only** (`img,img-rogue` was added later).
The BSP node is:

```
gpu: gpu@1800000 {
        compatible = "img,gpu";
        reg = <0x0 0x01800000 0x0 0x8ffff>;
        interrupts = <GIC_SPI 63 ...>, <GIC_SPI 64 ...>;
        interrupt-names = "IRQGPU", "IRQGPUDVFS";
        clocks = <&ccu CLK_PLL_GPU0>, <&ccu CLK_GPU0>, <&ccu CLK_BUS_GPU0>, ...;
        clock-names = "clk_parent", "clk", "clk_bus", "clk_800", ...;
        resets = <&ccu RST_BUS_GPU0>;
        reset-names = "reset_bus";
        /*power-domains = <&pd SUN60IW2_PCK_GPU_TOP>;*/   <-- commented out
        operating-points-v2 = <&gpu_opp_table>;
};
```

Four gaps between what the driver wants and what the node provides:

| Driver wants | BSP node has | Gap |
|---|---|---|
| `compatible = "img,img-axe"` (or `img,img-rogue`) | `img,gpu` | rename, and add `img,img-rogue` to the driver's `dt_match` |
| clock named **`core`** (required), `sys` / `mem` (optional) | `clk_parent`, `clk`, `clk_bus`, `clk_800`.. | remap: `core` = `CLK_GPU0` |
| `platform_get_irq(dev, 0)` | `IRQGPU` is first - OK | none |
| pm_runtime power domain | power-domains **commented out** | the GPU power island may never come up |
| *(driver never touches resets)* | `RST_BUS_GPU0` present | **nothing deasserts the reset** - likely fatal |

The last two are the real risks. The DDK's `pvrsrvkm` handles power and reset
itself; the in-tree driver expects the platform to have done it, which on this
SoC means either a working power-domain provider or a few lines of driver glue.

Also: `pvrsrvkm` must not be loaded at the same time - both would claim the node.

## Files here

```
0001-powervr-backport-adaptations.patch   the entire hand-written delta
import-v68.sh                            fetch v6.8 + graft into a tree copy
```

The import script builds an isolated copy of the BSP tree - it never touches
`a7a-kernel/kernel-6.6`, which builds the kernel the board actually boots.
