#!/bin/bash
# xwl-test.sh <variant>: headless reproduction of the "Guilty Lockup PID=Xwayland" GPU reset (a7a-fedora, 2026-09-07).
# Runs a private KWin (virtual backend, offscreen on renderD128, own D-Bus) on the PowerVR DDK,
# lets it spawn Xwayland through a wrapper that applies one environment variant, runs X clients,
# and counts GPU hardware recoveries. Never opens card0, never touches input or the screen.
# Copy to the board (/tmp/xwl-test.sh), run as the desktop user; /tmp/.X11-unix must exist
# (sudo systemd-tmpfiles --boot --create /usr/lib/tmpfiles.d/x11.conf).
#
# ⚠ Count HWRs with /sys/kernel/debug/pvr/status ("HWR Event Count"), not dmesg: the kernel prints the
#   "Guilty Lockup" dump only for the FIRST reset (HWRDebugDumpLimit=1). run-matrix.sh does it right.
#
# variants (what the Xwayland process gets):
#   keep         DDK libs + MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm, default glamor (GL first) = the failing session
#   nooverride   DDK libs, no override, default glamor (GL first)
#   es           DDK libs, no override, -glamor es           (= the A7Z community setup; STILL faults)
#   es-override  DDK libs + override, -glamor es
#   sw           system mesa, -glamor off                    (software Xwayland)
#   strace       like es, under strace -f (writes /tmp/xwl-test/xwl.strace)
#   apitrace     like es, under apitrace (aborts inside the DDK's no-config context; kept for reference)
set -u
V=${1:?variant}; D=/tmp/xwl-test; mkdir -p "$D/bin"
LOG="$D/$V.log"; : > "$LOG"
hwr() { v=$(sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status 2>/dev/null | grep -oE "[0-9]+$"); echo "${v:-0}"; }

cat > "$D/bin/Xwayland" <<'EOF'
#!/bin/bash
case "${XWL_VARIANT:-keep}" in
  keep)        export MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm ;;
  nooverride)  unset MESA_LOADER_DRIVER_OVERRIDE ;;
  es)          unset MESA_LOADER_DRIVER_OVERRIDE; set -- "$@" -glamor es ;;
  es-override) export MESA_LOADER_DRIVER_OVERRIDE=sunxi-drm; set -- "$@" -glamor es ;;
  sw)          export LD_LIBRARY_PATH=/usr/lib64; unset LIBGL_DRIVERS_PATH MESA_LOADER_DRIVER_OVERRIDE; set -- "$@" -glamor off ;;
  strace)      unset MESA_LOADER_DRIVER_OVERRIDE; set -- "$@" -glamor es
               echo "XWL-WRAP strace variant" >&2; exec strace -f -tt -e trace=ioctl,openat,mmap,poll,ppoll -o /tmp/xwl-test/xwl.strace /usr/bin/Xwayland "$@" ;;
  apitrace)    unset MESA_LOADER_DRIVER_OVERRIDE; set -- "$@" -glamor es
               echo "XWL-WRAP apitrace variant" >&2; exec apitrace trace --api egl -o /tmp/xwl-test/xwl.trace /usr/bin/Xwayland "$@" ;;
esac
echo "XWL-WRAP variant=${XWL_VARIANT:-keep} override=${MESA_LOADER_DRIVER_OVERRIDE:-unset} LD=${LD_LIBRARY_PATH:-unset} args=$*" >&2
exec /usr/bin/Xwayland "$@"
EOF
chmod +x "$D/bin/Xwayland"

cat > "$D/session.sh" <<'EOF'
#!/bin/bash
# runs inside the private KWin (DISPLAY/WAYLAND_DISPLAY/XAUTHORITY set by KWin); its exit ends KWin
exec >> "$XWL_LOG" 2>&1
hwr() { v=$(sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status 2>/dev/null | grep -oE "[0-9]+$"); echo "${v:-0}"; }
echo "== session: DISPLAY=$DISPLAY WAYLAND_DISPLAY=$WAYLAND_DISPLAY XAUTHORITY=$XAUTHORITY"
unset MESA_LOADER_DRIVER_OVERRIDE   # clients never carry the override (matches a real session)
echo "== wall clock before xdpyinfo: $(date +%T.%N) uptime $(cut -d" " -f1 /proc/uptime)"
echo "== xdpyinfo (starts Xwayland on demand)"; timeout 10 xdpyinfo 2>&1 | head -3
sleep 3
P=$(pgrep -n -x Xwayland); echo "== Xwayland pid ${P:-none} dri fds:"; [ -n "$P" ] && ls -l "/proc/$P/fd" 2>/dev/null | grep -oE 'dri/[a-zA-Z0-9]+' | sort | uniq -c
echo "== HWR count after glamor init: $(hwr)"
echo "== xmessage (glamor 2D)"; timeout 6 xmessage -timeout 2 "pvr test" 2>&1 | head -3
sleep 1; echo "== HWR count after xmessage: $(hwr)"
echo "== es2gears_x11 (GPU client via DRI3)"; timeout 6 es2gears_x11 2>&1 | head -6
sleep 2; echo "== HWR count after es2gears: $(hwr)"
echo "== session done"
EOF
chmod +x "$D/session.sh"

export XWL_VARIANT="$V" XWL_LOG="$LOG"
R0=$(hwr)
echo "variant=$V HWR before: $R0" | tee -a "$LOG"
# KWin itself: DDK on the render node (pvr personality), GLES compositing, no override
export LD_LIBRARY_PATH=/opt/pvr/lib LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri KWIN_COMPOSE=O2ES KWIN_FORCE_SW_CURSOR=1
unset MESA_LOADER_DRIVER_OVERRIDE
export PATH="$D/bin:$PATH"
export QT_LOGGING_TO_CONSOLE=1 QT_LOGGING_RULES="kwin_*.debug=true;kwin_*.info=true;qt.qpa.*.debug=false"
timeout -k 5 50 dbus-run-session -- kwin_wayland --virtual --xwayland --socket wayland-pvrtest \
  --no-lockscreen --no-global-shortcuts --no-kactivities --exit-with-session "$D/session.sh" > "$D/$V.kwin.log" 2>&1
echo "kwin exit=$?" | tee -a "$LOG"
R1=$(hwr)
echo "HWR after: $R1 (delta $((R1-R0)))" | tee -a "$LOG"
echo "wall clock now: $(date +%T.%N) uptime $(cut -d" " -f1 /proc/uptime)" | tee -a "$LOG"
echo "===== session log ====="; cat "$LOG"
echo "===== kwin/Xwayland log (filtered) ====="
grep -vE "^\s*$|Failed to gain real time|xkbcomp|Warning:|Using |ignoring|redefined|Errors from xkbcomp|conflicting fields" "$D/$V.kwin.log" | cut -c1-220 | head -60
echo "===== recent PVR kernel lines ====="; sudo dmesg | grep -E "PVR_K|pvrsrvkm" | tail -5 | cut -c1-200
