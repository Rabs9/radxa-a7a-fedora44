#!/bin/bash
# test-ddk-wayland.sh [--pre] [--no-full] [--no-stk]: step 3 of docs/DDK-WAYLAND-2026-09-10.md, ON THE BOARD from an
# ssh shell while the owner's Plasma session is up on the DDK kernel. The tests OPEN WINDOWS ON THE OWNER'S SCREEN
# (glmark2, vkcube, SuperTuxKart). It borrows the session's WAYLAND_DISPLAY / DISPLAY / XAUTHORITY (caller's uid).
# Clients get the Wayland-capable DDK Mesa per process through pvr-wl-run (next to this script); the X11 references
# use Radxa's stack through pvr-run. Nothing on the system changes.
#   --pre      on the open-stack kernel: only what needs no GPU context (the EGL client extensions and the Vulkan
#              instance extensions, queried without a display or a device); nothing opens a window
#   --no-full  skip the two full glmark2 suites (~5 min each)
#   --no-stk   skip the SuperTuxKart benchmarks
#   --vk       also the Vulkan group (known result 2026-09-10: no Wayland surface, the X11 swapchain crashes)
#   --system   after a7a-gpu-session made /opt/pvr-wl the system stack (step 4): every client runs as a normal app
#              would (no pvr-wl-run, no LD_LIBRARY_PATH, no override, the session's own environment taken from
#              plasmashell), and an app section checks what Qt Quick (kinfocenter) and Firefox actually map
# Logs in $OUT (default /home/radxa/mesa-img/test-<stamp>), summary in $OUT/summary.txt. pvrsrvkm's hardware recovery
# count (HWR) is read after every group and must stay at its start value.
set -u
PRE=0; FULL=1; STK=1; SYSTEM=0; VK=0
for a in "$@"; do case "$a" in --pre) PRE=1 ;; --no-full) FULL=0 ;; --no-stk) STK=0 ;; --system) SYSTEM=1 ;; --vk) VK=1 ;; *) echo "usage: $0 [--pre] [--no-full] [--no-stk] [--system] [--vk]"; exit 2 ;; esac; done
D=$(cd "$(dirname "$0")" && pwd)
RUN=${PVR_WL_RUN:-$D/pvr-wl-run}
OUT=${OUT:-/home/radxa/mesa-img/test-$(date +%Y%m%d-%H%M)}; mkdir -p "$OUT"
S=$OUT/summary.txt; : > "$S"
say() { echo "$*" | tee -a "$S"; }
if [ "$SYSTEM" = 1 ]; then
  RUN=/usr/bin/env
  P=$(pgrep -u "$(id -u)" -x plasmashell | head -1)
  [ -n "$P" ] || { echo "--system needs the owner's Plasma session (no plasmashell)"; exit 1; }
  while IFS= read -r l; do case "$l" in *=*) export "$l" ;; esac; done < <(tr '\0' '\n' < "/proc/$P/environ")
  unset LD_LIBRARY_PATH LIBGL_DRIVERS_PATH MESA_LOADER_DRIVER_OVERRIDE VK_ICD_FILENAMES
fi
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0} DISPLAY=${DISPLAY:-:0}
XA=$(ls -t "$XDG_RUNTIME_DIR"/xauth_* 2>/dev/null | head -1); [ -n "${XAUTHORITY:-}" ] || { [ -n "$XA" ] && export XAUTHORITY=$XA; }
DRV=$(readlink /sys/bus/platform/devices/1800000.gpu/driver 2>/dev/null); DRV=${DRV##*/}
hwr() { sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status 2>/dev/null | grep -oE "[0-9]+$" || echo n/a; }
H0=$(hwr)
check_hwr() { local h; h=$(hwr); if [ "$h" = "$H0" ]; then say "  HWR after $1: $h (unchanged)"; else say "  HWR after $1: $h (WAS $H0) <-- HARDWARE RECOVERY"; fi; }
say "kernel $(uname -r), GPU driver ${DRV:-none}, HWR at start: $H0, logs: $OUT"
if [ "$PRE" = 0 ] && [ "$DRV" != pvrsrvkm ]; then say "not the DDK kernel (GPU driver ${DRV:-none}): use --pre"; exit 1; fi

# ---- 1. client-side capabilities that need no display and no device (also valid on the open-stack kernel)
say "== EGL client extensions (eglQueryString(EGL_NO_DISPLAY), /opt/pvr-wl/lib/libEGL.so.1)"
"$RUN" python3 - > "$OUT/egl-client-ext.txt" 2>&1 <<'PY'
import ctypes
egl = ctypes.CDLL("libEGL.so.1")
egl.eglQueryString.restype = ctypes.c_char_p
egl.eglQueryString.argtypes = [ctypes.c_void_p, ctypes.c_int]
print(egl.eglQueryString(None, 0x3055).decode())   # EGL_EXTENSIONS
PY
say "  platforms: $(grep -oE 'EGL_(EXT|KHR|MESA)_platform_[a-z_]+' "$OUT/egl-client-ext.txt" | sort -u | tr '\n' ' ')"
say "== Vulkan instance extensions (vkEnumerateInstanceExtensionProperties, DDK ICD + /opt/pvr-wl WSI)"
"$RUN" python3 - > "$OUT/vk-instance-ext.txt" 2>&1 <<'PY'
import ctypes
class Ext(ctypes.Structure):
    _fields_ = [("name", ctypes.c_char * 256), ("spec", ctypes.c_uint32)]
vk = ctypes.CDLL("libvulkan.so.1")
n = ctypes.c_uint32(0)
print("result", vk.vkEnumerateInstanceExtensionProperties(None, ctypes.byref(n), None), "count", n.value)
arr = (Ext * n.value)()
vk.vkEnumerateInstanceExtensionProperties(None, ctypes.byref(n), arr)
for e in arr: print(e.name.decode())
PY
say "  surfaces: $(grep -oE '^VK_(KHR|EXT)_[a-z_]*surface[a-z_]*|^VK_KHR_display$' "$OUT/vk-instance-ext.txt" | sort -u | tr '\n' ' ')"
if [ "$PRE" = 1 ]; then say "--pre: done"; exit 0; fi

# ---- 2. EGL displays on every platform, with and without the loader override
say "== eglinfo -B (pvr-wl-run)"
"$RUN" eglinfo -B > "$OUT/eglinfo-B.txt" 2>&1
grep -E "platform:|^EGL driver name|profile renderer|profile version|failed|error" "$OUT/eglinfo-B.txt" | sed 's/^/  /' | tee -a "$S"
say "== eglinfo -B without MESA_LOADER_DRIVER_OVERRIDE"
PVR_WL_NO_OVERRIDE=1 "$RUN" eglinfo -B > "$OUT/eglinfo-B-nooverride.txt" 2>&1
grep -E "platform:|^EGL driver name|profile renderer|profile version|failed|error" "$OUT/eglinfo-B-nooverride.txt" | sed 's/^/  /' | tee -a "$S"
check_hwr eglinfo

# ---- 3. glmark2: Wayland-native on /opt/pvr-wl vs the X11 references (Radxa's stack and ours). Never 1024x600 (the
# screen size: KWin flips between scanout and compositing, bimodal numbers); a discarded warm-up run first
gm() {   # gm <label> <command...>
  local label=$1; shift
  "$@" > "$OUT/glmark2-$label.txt" 2>&1
  local r s; r=$(grep -m1 -E "GL_RENDERER:" "$OUT/glmark2-$label.txt" | sed 's/.*GL_RENDERER: *//')
  s=$(grep -oE "glmark2 Score: [0-9]+" "$OUT/glmark2-$label.txt" | grep -oE "[0-9]+$")
  say "  glmark2 $label: score ${s:-FAILED}, renderer ${r:-?}"
}
say "== glmark2"
"$RUN" glmark2-es2-wayland -s 320x240 -b build:duration=3 > "$OUT/glmark2-warmup.txt" 2>&1
gm wayland-640x480-build+texture "$RUN" glmark2-es2-wayland -s 640x480 -b build -b texture
gm wayland-640x480-build+texture-nooverride env PVR_WL_NO_OVERRIDE=1 "$RUN" glmark2-es2-wayland -s 640x480 -b build -b texture
gm x11-radxa-640x480-build+texture pvr-run glmark2-es2 -s 640x480 -b build -b texture
gm x11-pvrwl-640x480-build+texture "$RUN" glmark2-es2 -s 640x480 -b build -b texture
if [ "$FULL" = 1 ]; then
  gm wayland-full-suite "$RUN" glmark2-es2-wayland
  gm x11-radxa-full-suite pvr-run glmark2-es2
fi
check_hwr glmark2

# ---- 4. GLX under Xwayland: /opt/pvr-wl ships no libGL (the DDK has no desktop GL), so a GLX client resolves
# glvnd's libGL.so.1 -> Fedora's Mesa (llvmpipe) either way; this checks that it still does
say "== glxinfo -B (glvnd -> Fedora's Mesa expected)"
"$RUN" glxinfo -B > "$OUT/glxinfo.txt" 2>&1
say "  $(grep -E 'OpenGL (renderer|version) string|Error|error' "$OUT/glxinfo.txt" | head -3 | tr '\n' '|')"
check_hwr glxinfo

# ---- 5. apps as the session starts them (--system only): which EGL/DRI libraries they map, and that they live
app_maps() {   # app_maps <label> <process name> <seconds> <command...>
  local label=$1 name=$2 secs=$3 bg p; shift 3
  "$@" > "$OUT/app-$label.txt" 2>&1 &
  bg=$!; sleep "$secs"
  p=$(pgrep -u "$(id -u)" -x "$name" | head -1)
  if [ -z "$p" ]; then say "  $label: NOT RUNNING after ${secs}s: $(tail -2 "$OUT/app-$label.txt" | tr '\n' '|' | cut -c1-200)"
  else
    say "  $label (pid $p) maps: $(grep -oE '/opt/pvr-wl/lib/(libEGL|libgbm|libGLESv2)[^ ]*|/opt/pvr-wl/lib/dri/[a-z_-]+_dri\.so|/opt/pvr/lib/libGLESv2_PVR_MESA[^ ]*|/usr/lib64/(libEGL|libEGL_mesa|libgallium|libGLX_mesa)[^ ]*' /proc/$p/maps | sort -u | tr '\n' ' ')"
    kill "$p" 2>/dev/null; sleep 2; kill -9 "$p" 2>/dev/null
  fi
  wait "$bg" 2>/dev/null
}
if [ "$SYSTEM" = 1 ]; then
  say "== apps (cache-only, the session's environment)"
  app_maps kinfocenter kinfocenter 10 kinfocenter
  mkdir -p "$OUT/ff-profile"
  app_maps firefox firefox 25 firefox --no-remote --profile "$OUT/ff-profile" about:support
  check_hwr apps
fi

# ---- 6. Vulkan (--vk): the DDK device with the Wayland WSI (known result: no Wayland surface, X11 swapchain crash)
if [ "$VK" = 1 ]; then
say "== Vulkan"
"$RUN" vulkaninfo --summary > "$OUT/vulkaninfo-summary.txt" 2>&1
say "  $(grep -m1 -E 'deviceName' "$OUT/vulkaninfo-summary.txt" | sed 's/^ *//'), $(grep -m1 -E 'driverInfo|driverName' "$OUT/vulkaninfo-summary.txt" | sed 's/^ *//')"
"$RUN" vulkaninfo > "$OUT/vulkaninfo.txt" 2>&1
say "  instance surface extensions: $(grep -oE 'VK_KHR_(wayland|xcb|xlib)_surface' "$OUT/vulkaninfo.txt" | sort -u | tr '\n' ' ')"
vkc() {   # vkc <label> <wsi> [env...]: 600 frames, exit code, wall time, the GPU vkcube selected
  local label=$1 w=$2 t0 t1 rc; shift 2
  t0=$(date +%s.%N); env "$@" timeout 40 "$RUN" vkcube --wsi "$w" --c 600 > "$OUT/vkcube-$label.txt" 2>&1; rc=$?; t1=$(date +%s.%N)
  say "  vkcube $label (--wsi $w), 600 frames: exit $rc in $(awk "BEGIN{printf \"%.1f\", $t1-$t0}") s; $(grep -m1 -iE "selected gpu|error" "$OUT/vkcube-$label.txt" | cut -c1-120)"
}
vkc ddk-only-xcb xcb
vkc ddk-only-wayland wayland
# The closed libVK_IMG (the DDK's "xorg" build) carries only xcb/xlib/headless surface extensions in its own list,
# so with the DDK as the only driver the loader refuses VK_KHR_wayland_surface at vkCreateInstance. With a second
# driver that has it (Fedora's lavapipe) the loader accepts the instance extension, creates the Wayland surface
# itself (VkIcdSurfaceWayland) and hands it to the DDK driver, which passes it to the Mesa WSI in /opt/pvr-wl (built
# with the Wayland platform). vkcube prefers the integrated GPU (the PowerVR) over the CPU device.
LVP=$(ls /usr/share/vulkan/icd.d/lvp_icd.*.json 2>/dev/null | head -1)
if [ -n "$LVP" ]; then
  PVR_WL_VK_ICDS=/opt/pvr/icd/powervr_icd.json:$LVP "$RUN" vulkaninfo > "$OUT/vulkaninfo-ddk+lvp.txt" 2>&1
  sed -n '/^Presentable Surfaces/,/^Device Properties and Extensions/p' "$OUT/vulkaninfo-ddk+lvp.txt" > "$OUT/presentable-ddk+lvp.txt"
  say "  DDK + lavapipe: instance has $(grep -oE 'VK_KHR_wayland_surface' "$OUT/vulkaninfo-ddk+lvp.txt" | head -1); presentable surfaces per GPU: $(grep -E 'GPU id|VK_KHR_(wayland|xcb|xlib)_surface' "$OUT/presentable-ddk+lvp.txt" | sed 's/^[[:space:]]*//' | tr '\n' ' ' | cut -c1-300)"
  vkc ddk+lvp-wayland wayland PVR_WL_VK_ICDS=/opt/pvr/icd/powervr_icd.json:$LVP
fi
check_hwr vulkan
fi

# ---- 6. SuperTuxKart --benchmark: native Wayland on /opt/pvr-wl vs X11 on Radxa's stack (docs/XAPPS-ON-GPU-2026-09-08.md)
stk() {   # stk <label> <command prefix...>
  local label=$1; shift; local L=$HOME/.config/supertuxkart/config-0.10/stdout.log t0 t1 rc
  t0=$(date +%s.%N); "$@" supertuxkart --benchmark > "$OUT/stk-$label.console" 2>&1; rc=$?; t1=$(date +%s.%N)
  cp "$L" "$OUT/stk-$label.log" 2>/dev/null
  local prof ren ver fixed
  # STK 1.5: "Profiler: Frame count '1165', Time (ms) '38114', Steady FPS '19', Mostly stable FPS '22', Typical FPS '29'"
  prof=$(grep -oE "Profiler: Frame count '[0-9]+', Time \(ms\) '[0-9]+'.*" "$OUT/stk-$label.log" | tail -1 \
         | awk -F"'" '{printf "%s frames in %.1f s = %.1f fps average; steady %s, mostly stable %s, typical %s", $2, $4/1000, $2*1000/$4, $6, $8, $10}')
  ren=$(grep -m1 -oE "IrrDriver: OpenGL renderer: .*" "$OUT/stk-$label.log" | sed 's/IrrDriver: OpenGL renderer: //')
  ver=$(grep -m1 -oE "IrrDriver: OpenGL version string: .*" "$OUT/stk-$label.log" | sed 's/IrrDriver: OpenGL version string: //')
  fixed=$(grep -c "Using the fixed pipeline" "$OUT/stk-$label.log")
  say "  STK $label: exit $rc, $(awk "BEGIN{printf \"%.0f\", $t1-$t0}") s wall; ${prof:-no profile line}; renderer ${ren:-?}; ${ver:-?}; fixed-pipeline warnings: $fixed"
}
if [ "$STK" = 1 ]; then
  say "== SuperTuxKart --benchmark"
  # --system: as a menu launch would start it (SDL picks its own video driver: Wayland in the Plasma session)
  if [ "$SYSTEM" = 1 ]; then stk session-default "$RUN"; else stk wayland-pvrwl env SDL_VIDEODRIVER=wayland "$RUN"; fi
  stk x11-radxa env SDL_VIDEODRIVER=x11 SDL_VIDEO_X11_FORCE_EGL=1 pvr-run
  check_hwr supertuxkart
fi
say "done: $OUT"
