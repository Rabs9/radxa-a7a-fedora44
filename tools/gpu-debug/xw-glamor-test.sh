#!/bin/bash
# Runs ON THE BOARD as the logged-in user (scp it to /tmp): /tmp/xw-glamor-test.sh 5 [-glamor es]. Needs no re-login: it
# tests the glamor-on-DDK path (route B) without touching KWin's own Xwayland. Used 2026-09-09 (docs/XAPPS-ON-GPU-2026-09-08.md).
# xw-glamor-test.sh <display-number> [extra Xwayland args...]
# Start a second rootless Xwayland inside the running KWin session with glamor ENABLED on the DDK, report what
# glamor did (log lines, DRM node it opened), then run a short glmark2-es2 (X11 window system) on it through
# pvr-run and report the renderer string, the scores, and the GPU hardware-recovery / IOMMU-fault deltas.
N=$1; shift
export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0
export LD_LIBRARY_PATH=/opt/pvr/lib LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm
unset XWAYLAND_NO_GLAMOR
LOG=/tmp/xw$N.log
H0=$(sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status | grep -oE "[0-9]+$")
F0=$(sudo dmesg | grep -ciE "iommu.*(fault|unhandled|page)")
pkill -f "Xwayland :$N " 2>/dev/null; sleep 0.5
echo "=== Xwayland :$N args: $*"
setsid /usr/bin/Xwayland ":$N" -rootless -noreset -verbose 3 "$@" >"$LOG" 2>&1 &
XP=$!
for i in $(seq 1 40); do [ -S "/tmp/.X11-unix/X$N" ] && break; sleep 0.25; done
sleep 1
if ! kill -0 $XP 2>/dev/null; then echo "Xwayland died:"; tail -15 "$LOG"; exit 1; fi
echo "--- glamor/EGL/DRM lines from the Xwayland log:"
grep -iE "glamor|egl|dri|render|drm|device|dmabuf|falling" "$LOG" | head -20
echo "--- DRM nodes opened by Xwayland:"
ls -l /proc/$XP/fd 2>/dev/null | grep -oE "/dev/dri/\S+" | sort | uniq -c
echo "--- GL libs mapped by Xwayland:"
grep -E "libEGL|libGLES|libGL\.|_dri.so|libgbm" /proc/$XP/maps | awk '{print $6}' | sort -u
echo "--- DRI3 present on :$N: $(DISPLAY=:$N xdpyinfo 2>/dev/null | grep -cE '^\s+(DRI3|Present)$') of 2 (DRI3, Present)"
echo "--- glmark2-es2 on :$N via pvr-run:"
DISPLAY=:$N timeout 60 pvr-run glmark2-es2 -s 640x480 -b build:duration=3 -b texture:duration=3 -b shading:duration=3 2>&1 | grep -E "GL_VENDOR|GL_RENDERER|GL_VERSION|FPS|Score|error|Error|failed" | head -12
H1=$(sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status | grep -oE "[0-9]+$")
F1=$(sudo dmesg | grep -ciE "iommu.*(fault|unhandled|page)")
echo "--- GPU hardware recoveries: $H0 -> $H1 (delta $((H1-H0))), IOMMU fault lines: $F0 -> $F1"
echo "--- Xwayland still alive: $(kill -0 $XP 2>/dev/null && echo yes || echo NO)"
kill $XP 2>/dev/null
