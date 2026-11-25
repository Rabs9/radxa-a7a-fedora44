#!/bin/bash
# stk-bench.sh [--label L] [--screenshot FILE [--shot-after SECONDS]]: SuperTuxKart --benchmark on the board, inside
# the logged-in desktop (docs/SPEED-2026-09-10.md: the black_forest replay, a fixed 762-frame workload, so wall time
# is the metric; 2026-09-10 baseline on the open stack 4.13 fps). Times the run, reads from STK's own log which
# renderer it chose (the fixed-function fallback when GL/GLES is too old, the modern one otherwise), counts the frames
# in its profiler CSV, and with --screenshot grabs the screen mid-run with spectacle, so "the screen is not black" is
# a file, not a claim. Only this run's log and CSV count (newer than a marker taken before the start); nothing of
# STK's is deleted. Prints one JSON line last. Uses the caller's WAYLAND_DISPLAY/XDG_RUNTIME_DIR when set, else the
# session KWin's (--socket from its command line). STK_CONFIG_DIR overrides ~/.config/supertuxkart/config-0.10.
# Run it over ssh on the board while the owner is logged in and the desktop is idle.
set -u
LABEL=run; SHOT=; AFTER=60
while [ $# -gt 0 ]; do
  case "$1" in
    --label) LABEL=$2; shift 2 ;;
    --screenshot) SHOT=$2; shift 2 ;;
    --shot-after) AFTER=$2; shift 2 ;;
    *) echo "usage: stk-bench.sh [--label L] [--screenshot FILE [--shot-after SECONDS]]" >&2; exit 2 ;;
  esac
done
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
  K=$(pgrep -x kwin_wayland | head -1)
  [ -z "$K" ] && { echo "no kwin_wayland: log in first" >&2; exit 1; }
  export XDG_RUNTIME_DIR=/run/user/$(stat -c %u /proc/"$K")
  SOCK=$(tr '\0' '\n' < /proc/"$K"/cmdline | sed -n '/^--socket$/{n;p;}')
  export WAYLAND_DISPLAY=${SOCK:-wayland-0}
fi
export DBUS_SESSION_BUS_ADDRESS=${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}
CFG=${STK_CONFIG_DIR:-$HOME/.config/supertuxkart/config-0.10}
W=$(mktemp -d)
MARK=$W/start
touch "$MARK"
T0=$(date +%s.%N)
supertuxkart --benchmark > "$W/console" 2>&1 &
PID=$!
if [ -n "$SHOT" ]; then
  # only while the game still runs: a shot after it ended would show the desktop
  ( sleep "$AFTER"; kill -0 "$PID" 2>/dev/null && spectacle -b -n -f -o "$SHOT" > "$W/spectacle" 2>&1 ) &
  SHOTPID=$!
fi
wait "$PID"
RC=$?
T1=$(date +%s.%N)
[ -n "$SHOT" ] && wait "$SHOTPID" 2>/dev/null
CSV=$(find "$CFG" -maxdepth 1 -name 'stdout.log.profile-*-cpu-0.csv' -newer "$MARK" 2>/dev/null | head -1)
LOGF=
[ -f "$CFG/stdout.log" ] && [ "$CFG/stdout.log" -nt "$MARK" ] && LOGF=$CFG/stdout.log
python3 - "$LABEL" "$RC" "$T0" "$T1" "$LOGF" "$CSV" "$SHOT" "$W/console" <<'PY'
import json, os, sys
label, rc, t0, t1, logf, csv, shot, console = sys.argv[1:9]
log = open(logf, errors="replace").read() if logf else ""

def after(key):
    for line in log.splitlines():
        if key in line:
            return line.split(key, 1)[1].strip()
    return None

frames = None
if csv:
    with open(csv) as f:
        frames = max(0, sum(1 for _ in f) - 1)
seconds = round(float(t1) - float(t0), 3)          # fps from the value printed, so the JSON agrees with itself
fixed = "Using the fixed pipeline" in log
res = {"label": label, "rc": int(rc), "seconds": seconds, "frames": frames,
       "fps": round(frames / seconds, 3) if frames and seconds > 0 else None,
       "log": logf or None, "profile_csv": csv or None,
       "gl_renderer": after("OpenGL renderer:"), "gl_version": after("IrrDriver: OpenGL version:"),
       "using_renderer": after("Using renderer:"), "version_too_old": "OpenGL version is too old" in log,
       "renderer_path": "fixed-function" if fixed else ("modern" if "Using renderer:" in log else "unknown"),
       "screenshot": shot if shot and os.path.exists(shot) else None}
if not logf:
    res["error"] = "no stdout.log from this run (console tail: %s)" % open(console, errors="replace").read()[-300:]
print(json.dumps(res))
PY
