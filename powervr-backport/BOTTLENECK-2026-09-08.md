# Why the open stack is slow, measured

**2026-09-08.** The GPU works. The desktop is slow. These are two different
statements and the numbers separate them cleanly.

## Two costs, not one

| path | glmark2 | vs the DDK |
|---|---|---|
| DDK, GLES native, X11 | **1267** | baseline |
| open stack, off-screen (no compositor) | **156-204** | ~6-8x slower |
| open stack, Wayland client through KWin | **24** | ~50x slower |

So there are two independent losses:

1. **zink over an immature Vulkan driver: ~6x.** Mesa's powervr is Vulkan-only,
   so every GL call goes through zink, on a driver that is explicitly
   non-conformant for this part and only reaches GL 2.1 / GLES 2.0 (the DDK gave
   GLES 3.2). This is the cost of the userspace being young, and it is visible
   with the compositor entirely out of the picture.

2. **The scanout path: a further ~8.5x.** This one has a concrete cause.

## The scanout cause

`drm_info /dev/dri/card0`:

```
└───Planes
    ├───Plane 0
        ├───"IN_FORMATS" (immutable): blob = 93
        │   └───DRM_FORMAT_MOD_LINEAR (0x0000000000000000)
```

The display engine's primary plane accepts **linear buffers only**. It also
advertises `ARM_AFBC(...)` modifiers on other planes - but AFBC is ARM Mali's
framebuffer compression, and a PowerVR GPU cannot produce it. There is no
modifier both devices understand.

PowerVR is a tile-based renderer and its native output is tiled. So every frame
has to be converted to linear before it can be scanned out. That conversion is
the 204 -> 24.

Corroborating, from KWin's log:

```
MESA: warning: zink: PERF WARNING! > 100 copy boxes detected for 0xffff084c7c00
```

And the two devices genuinely cannot share tiled memory:

```
card0  sunxi-drm  DRM_CAP_ADDFB2_MODIFIERS = 1     (accepts modifiers, but only LINEAR on the plane)
card1  powervr    DRM_CAP_ADDFB2_MODIFIERS not supported
                  DRM_CAP_PRIME = 3                 (import + export works)
```

PRIME sharing works - that is why anything displays at all. What does not exist
is a *tiled* format both sides accept, so the sharing is always through linear.

## Is it fixable

Partly, and not by us alone.

- **Not by a modifier match.** The display hardware decodes AFBC, not PowerVR
  tiling. No software change makes those meet.
- **Possibly by rendering straight into linear scanout buffers** rather than
  rendering tiled and resolving. Whether Mesa already does this here, or is
  doing an extra copy on top, is the open question and the next thing to
  measure.
- **The 6x zink cost is upstream work**, not configuration. It improves when
  Mesa's powervr driver matures, or if a native GL driver ever appears.

## What was fixed along the way

Two real defects in the backport, both found by chasing this:

**`CONFIG_EXT4_FS_SECURITY` / `CONFIG_SECURITY_NETWORK` were unset.** The
backport inherited the Debian board's config. Without them `setcap` returns
*Operation not supported*, so `kwin_wayland` cannot hold `cap_sys_nice` and the
compositor never gets real-time priority - which is exactly what "sometimes
smooth, sometimes crawling" feels like. It also stops rpm setting file
capabilities, which breaks `dnf`. a7a-fedora ca5f111 had already fixed this for
the Fedora board; the backport reintroduced it. Rebuilt with both, and
`getcap` now reports `cap_sys_nice=ep`, RT priority errors this boot: 0.

**`tx-delay = <12>` in the DTB.** Folded patch 0003 into
`arch/arm64/boot/dts/allwinner/sun60i-a733-cubie-a7a.dts`, rebuilt, verified: a
200 KB transfer over the built-in port now completes, where before anything over
~250 bytes stalled.

## Honest position

The open stack is architecturally better - Wayland clients render on the GPU,
GLX exists, glvnd works, `VK_EXT_robustness2` is present, no per-application
environment wrapping. Every structural limit of the DDK is gone.

It is also, today, substantially slower than the DDK for real work. Both things
are true.
