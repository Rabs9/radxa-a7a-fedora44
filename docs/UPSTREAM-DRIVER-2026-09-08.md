# The upstream driver route: the firmware was never the blocker

**2026-09-08.** Written after the X-apps and games work, in answer to a simple
question: instead of wrapping every application in three environment variables,
can the A733 just use the open PowerVR driver that Imagination already wrote?

The answer changed twice while checking. Final position:

> **The firmware already on this board is, as far as every structural test can
> tell, exactly the file the in-tree driver wants.** The remaining blocker is a
> kernel, and the only DRM core piece it is missing is `drm_gpuvm`.

Nothing here has been *run* yet - the driver cannot load on 6.6.98. But the
route is no longer blocked on a vendor.

---

## Why look at all

The DDK userspace works - `glmark2-es2` goes 136 -> 1267 once an app is wrapped -
but it has four limitations that are structural, not configuration:

| Limitation | Evidence | Consequence |
|---|---|---|
| No Wayland EGL platform | extension list has `EGL_KHR_platform_x11`, `_gbm`, `_device`, `_surfaceless` - no `_wayland` | No Wayland client can touch the GPU. Qt's `libqwayland-egl.so` links this libEGL, so Plasma services abort. |
| No GLX, no desktop libGL | no `libGL.so.1`, no `libGLX.so.0` in the DDK tree | SDL takes GLX on X11 and lands on llvmpipe. Needs `SDL_VIDEO_X11_FORCE_EGL=1`. |
| Not a glvnd vendor | `nm -D libEGL.so` has no `__egl_Main` | Cannot coexist with Mesa in one process. All-or-nothing per app. |
| Vulkan lacks `VK_EXT_robustness2` | 129 extensions enumerated, that one absent | zink refuses to initialise, so desktop GL cannot be layered over Vulkan either. |

All four disappear if the GPU is driven by the in-tree driver plus Mesa, because
then it is an ordinary DRM device with ordinary Mesa userspace.

## What is already in place

- **Mesa's `powervr` driver names this GPU.** `libvulkan_powervr_mesa.so` lists
  `AXE-1-16, BXE-2-32, BXE-4-32, BXM-4-64, BXS-4-64`. Ours is BXM-4-64 - the
  exact part, not a family guess.
- **The kernel driver is already on the SD card**, in Fedora's unused 6.19.10
  kernel: `drivers/gpu/drm/imagination/powervr.ko.xz`, 411 KB uncompressed.
- **The firmware is on the board too.** See below - this is the finding.

## The firmware: three steps, and the answer reversed twice

### Step 1 - "the blob is not published"

The in-tree driver loads `/lib/firmware/powervr/rogue_<BVNC>_v1.fw`.
linux-firmware publishes three; ours (36.56.104.183) is not among them.

BVNC identifies the *silicon*, not a release, so 36.52 and 36.53 cannot be
substituted and there is no "filling in the gaps" between them.

### Step 2 - "but the driver supports our firmware processor"

The worry was that 36.56 needed a firmware processor the driver lacks. ELF
headers say otherwise:

```
rogue_33.15.11.3_v1.fw        MIPS R3000   entry 0xbfc00001
rogue_36.52.104.182_v1.fw     MIPS R3000   entry 0xbfc00001
rogue_36.53.104.796_v1.fw     RISC-V       entry 0xc0000000
rgx.fw.36.56.104.183  (ours)  MIPS R3000   entry 0xbfc00001
```

and `strings powervr.ko` shows the driver implements **Meta** and **Mips**.
Same branch, same processor, same entry point as an already-published blob.

*(An earlier note read the MIPS/RISC-V split as fatal. It is not - that split is
between two of mainline's own blobs.)*

### Step 3 - the blob is not "unpublished", it is **renamed**

The driver does not carry a BVNC table. It builds the filename at runtime:

```
powervr/rogue        (prefix)
%s_%d.%d.%d.%d_v%d.fw
```

and takes its feature/quirk/enhancement configuration *from the firmware image
itself* (`pvr_fw_validate_init_device_info`, and the errors "Unsupported
features / quirks / enhancements in firmware image"). It reads the hardware BVNC
from a register (`has_pbvnc_coreid_reg`) and compares `fw_bvnc` to `hw_bvnc`.
The three `MODULE_FIRMWARE` entries are packaging declarations, not a whitelist.
There are **zero** BVNC-shaped strings in the module.

So the question became: is mainline's `_v1.fw` a different container, or the
same file renamed? Compare `rogue_36.52.104.182_v1.fw` against our
`rgx.fw.36.56.104.183`:

```
                       mainline 36.52        ours 36.56
ELF class              ELF32                 ELF32
endianness             little                little
type                   EXEC                  EXEC
machine                MIPS R3000            MIPS R3000
entry                  0xbfc00001            0xbfc00001
program headers        5                     5
section headers        19                    19
LOAD vaddrs            9fc02000 bfc00000     9fc02000 bfc00000
                       c0000000 c0032000     c0000000 c0038000
                       c0032008              c0038008
MIPS ABI attribute     Soft float            Soft float
.comment               GCC: (Codescape GNU Tools 2014.07-1
                             for MIPS Bare Metal) 4.9.0     <- IDENTICAL
```

19 sections, identical names, identical order:
`.bootandnmitext .exctext .text .text.startup .bss .data .shdata .shdatabss
.rodata .data_noncritical .rodata_noncritical .eh_frame .mdebug.abi32 .reginfo
.pdr .comment .gnu.attributes .shstrtab`

Only the sizes differ, because it is a different build for different silicon.

**linux-firmware's `rogue_*_v1.fw` files are DDK `rgx.fw` files under a new
name.** Imagination published a subset of what they already ship in every BSP.

Which means the whole "missing firmware" problem is very likely:

```
cp /lib/firmware/rgx.fw.36.56.104.183 \
   /lib/firmware/powervr/rogue_36.56.104.183_v1.fw
```

Local use of firmware already licensed to this board - not redistribution.

### One open question

The DDK also ships `rgx.sh.36.56.104.183` (272 KB, raw data, not an ELF). The
in-tree driver loads exactly **one** file - a single "failed to load firmware"
path - and resolves addresses across the blob's LOAD segments. Whether the
separate shader payload is needed, embedded, or unused by the open driver is not
yet known. It is the most likely thing to bite.

## The real blocker: a kernel

Neither kernel on this board can do the job:

| | boots the A733 | has drm/imagination |
|---|---|---|
| BSP 6.6.98 | yes | no (`CONFIG_DRM_POWERVR` not set; driver landed in 6.8) |
| Fedora 6.19.10 | no (no `SUN60I` support, no A733 DT) | yes |

Backporting the driver to 6.6.98 is the cheaper direction, and the BSP is closer
than expected. Of the DRM core helpers `powervr.ko` needs:

```
drm_exec              CONFIG_DRM_EXEC=m              present, module on disk
gpu-sched             CONFIG_DRM_SCHED=m             present, module on disk
drm_shmem_helper      CONFIG_DRM_GEM_SHMEM_HELPER=m  present, module on disk
drm_gpuvm             --                             ABSENT
```

**`drm_gpuvm` is the single missing piece** - one self-contained file pair
(`drivers/gpu/drm/drm_gpuvm.c`, `include/drm/drm_gpuvm.h`) added in 6.7, built
on drm_mm and dma-resv, both of which 6.6 has. That is the kind of thing that
backports cleanly.

So the shape of the remaining work:

1. ~~Backport `drm_gpuvm` into the 6.6.98 BSP tree.~~ **DONE**
2. ~~Backport `drivers/gpu/drm/imagination` on top of it, `DRM_POWERVR=m`.~~ **DONE**
3. Build with `pvrsrvkm` disabled so the two drivers do not fight for the node.
4. Copy `rgx.fw.36.56.104.183` to `powervr/rogue_36.56.104.183_v1.fw`.
5. DT: the v6.8 driver binds `img,img-axe` only; ours is `img,gpu` plus
   `allwinner,img-operating-points`. Clocks, resets and the power domain are the
   least predictable step - see below.

### Steps 1-2: done, and smaller than expected

`powervr-backport/` in this repo. The whole hand-written delta is **46 lines
added, 6 removed** - five API drifts (`drm_exec_init` gained an argument,
`drm_sched_job_init` gained `credits`, `drm_sched_init` gained `submit_wq` and
`num_rqs`, `drm_gem_for_each_gpuvm_bo` was new in 6.8, `SZ_128G` postdates 6.6)
and one config conflict (6.6's `drm_gpuva_mgr` exports the same symbols as
`drm_gpuvm`; nouveau is its only user and an A733 has no NVIDIA hardware, so
`CONFIG_DRM_NOUVEAU=n`). None of them semantic.

v6.8 was chosen because `pvr_drm.h` is byte-identical from v6.8 to v6.16 - the
UAPI has not moved since the driver landed, so the oldest version still
satisfies current Mesa while being closest to our 6.6 base.

It builds:

```
powervr.ko            1,222,704 bytes   vermagic 6.6.98+ SMP preempt mod_unload aarch64
drm_gpuvm.ko            190,960
Image                40,159,240
```

Nothing has been loaded or bound yet.

### Step 5 is now the whole problem

| Driver wants | BSP node has | Gap |
|---|---|---|
| `compatible = "img,img-axe"` | `img,gpu` | rename, or add `img,img-rogue` to `dt_match` |
| clock named **`core`** (required) | `clk_parent`, `clk`, `clk_bus`, `clk_800`.. | remap `core` = `CLK_GPU0` |
| `platform_get_irq(dev, 0)` | `IRQGPU` first | fine |
| pm_runtime power domain | `power-domains` **commented out** in the dtsi | the GPU power island may never come up |
| *(never touches resets)* | `RST_BUS_GPU0` present | **nothing deasserts the reset** - likely fatal |

The last two are the real risks. `pvrsrvkm` handles power and reset itself; the
in-tree driver expects the platform to have done it, which here means either a
working power-domain provider or a few lines of driver glue.

Note the board has no kernel headers installed (its `build` symlink is dangling),
so this is a full BSP tree build, not an out-of-tree module build.

## What this does to the vendor ask

It shrinks it, possibly to nothing.

The previously drafted issue asked Radxa to fix three separate holes in a
proprietary stack they do not build. The rewrite asked them to publish one
firmware blob. This finding says **test the blob we already have first** - if it
works, there is no ask at all, only work.

If a vendor issue is still wanted afterwards, the useful version is narrow:
*"please confirm rgx.fw.BVNC and linux-firmware's rogue_BVNC_v1.fw are the same
artifact, and consider publishing 36.56.104.183 so distributions can ship it."*
That is a confirmation request, not a favour.

**Issue status: HOLD.** Do not post until the copy-and-load test has been run.

## Method note

All of this was read off the hardware rather than inferred from source:
`readelf` header/section/program/attribute/comment dumps on each decompressed
blob, `nm -u` and `strings` on the Fedora-shipped `powervr.ko`, `modinfo`, the
DT `compatible` from `/proc/device-tree`, and the BSP config from
`/proc/config.gz`. Scripts: `bvnc.sh`, `fedkern.sh`, `fwformat.sh`, `fwarch.sh`,
`upstream.sh`, `verify.sh` through `verify6.sh`.

The board uses a randomised MAC, so its DHCP address moves between boots and it
is not recognisable in the ARP table. Find it by sweeping for port 22 and
identifying by hostname.
