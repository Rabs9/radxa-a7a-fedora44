#!/bin/bash
# speed-bench.sh: repeatable GPU/compositor speed measurement on the board (Item E).
#
# Runs inside the logged-in Wayland session (found from kwin_wayland like a7a-gpu-check does), so run it over
# ssh while the desktop is up. Every mode prints one "RESULT" line per repeat plus a median line, so runs can be
# pasted straight into docs/SPEED-2026-09-10.md.
#
#   speed-bench.sh client [reps] [sizes...]   glmark2 build scene per size: client fps + KWin ms CPU per frame
#   speed-bench.sh full [--all] [reps] [size] glmark2-es2-wayland: build / texture / score. Default = the build and
#                                             texture scenes only (~30 s/run); --all runs the whole suite (~5 min/run,
#                                             and its Score is the all-scene one, not comparable with the default).
#   speed-bench.sh threads [pid]              where a process's threads ran last (default kwin_wayland)
#   speed-bench.sh idle                       what the board is doing right now (load, clocks, temps, GPU power state)
#
# Defaults: 3 repeats, sizes "640x480 1000x560", full at 640x480. Do NOT measure at 1024x600: that is exactly the
# screen size, so KWin promotes the window to a scanout plane on some runs and composites it on others, and the
# result is bimodal (~41 fps / 7.5 ms scanout vs ~35 fps / 12 ms composited) - useless for A/B. 1000x560 is the
# same work through the compositor and is stable to +-1 fps. A warm-up run is done first and discarded
# (the GPU cold-boots from runtime suspend and Mesa compiles shaders on the first client): without it the first
# number is ~15 fps instead of ~270.
set -u

K=$(pgrep -x kwin_wayland | head -1)
[ -z "$K" ] && { echo "no kwin_wayland: log in first"; exit 1; }
export XDG_RUNTIME_DIR=/run/user/$(stat -c %u /proc/$K) WAYLAND_DISPLAY=wayland-0

# median of the numbers on stdin (integer or decimal), printed with one decimal
median() { sort -n | awk '{v[NR]=$1} END{if(NR==0){print "-";exit} m=(NR%2)?v[(NR+1)/2]:(v[NR/2]+v[NR/2+1])/2; printf "%.1f\n", m}'; }

kwin_ticks() { awk '{print $14+$15}' /proc/$K/stat 2>/dev/null || echo 0; }

# one glmark2 build-scene run: echoes "<fps> <kwin_ms_per_frame>"
run_client() {
  local size=$1 dur=$2 c0 c1 fps out
  c0=$(kwin_ticks)
  out=$(timeout 60 glmark2-es2-wayland -s "$size" -b build:duration="$dur" 2>&1 | grep -oE 'FPS: [0-9]+')
  c1=$(kwin_ticks)
  fps=${out#FPS: }; fps=${fps:-0}
  # ticks are 10 ms each; ms of KWin CPU per client frame
  awk -v f="$fps" -v t="$((c1 - c0))" -v d="$dur" 'BEGIN{ printf "%d %.1f\n", f, (f>0? t*10/(f*d) : 0) }'
}

mode=${1:-client}; shift || true

case "$mode" in
  client)
    reps=${1:-3}; shift || true
    sizes=${*:-"640x480 1000x560"}
    for size in $sizes; do
      run_client "$size" 2 >/dev/null            # warm-up, discarded
      fpsl=""; msl=""
      for i in $(seq 1 "$reps"); do
        read -r fps ms < <(run_client "$size" 3)
        echo "RESULT client $size rep$i fps=$fps kwin_ms=$ms"
        fpsl="$fpsl$fps"$'\n'; msl="$msl$ms"$'\n'
      done
      echo "MEDIAN client $size fps=$(printf '%s' "$fpsl" | median) kwin_ms=$(printf '%s' "$msl" | median)"
    done
    ;;
  full)
    # build + texture scenes only (~30 s/run) unless --all is given (whole suite, ~5 min/run).
    all=0; [ "${1:-}" = --all ] && { all=1; shift; }
    reps=${1:-3}; size=${2:-640x480}
    scenes=(-b build -b texture); [ $all = 1 ] && scenes=()
    fps_of() { printf '%s' "$1" | grep -E "^\[$2\]" | grep -oE 'FPS: [0-9]+' | head -1 | sed 's/FPS: //'; }
    timeout 400 glmark2-es2-wayland -s "$size" "${scenes[@]}" >/dev/null 2>&1   # warm-up, discarded
    bl=""; tl=""; sl=""
    for i in $(seq 1 "$reps"); do
      o=$(timeout 400 glmark2-es2-wayland -s "$size" "${scenes[@]}" 2>&1)
      b=$(fps_of "$o" build); t=$(fps_of "$o" texture)
      s=$(printf '%s' "$o" | awk '/glmark2 Score/{print $NF}')
      echo "RESULT full$([ $all = 1 ] && echo -all) $size rep$i build=${b:--} texture=${t:--} score=${s:--}"
      bl="$bl$b"$'\n'; tl="$tl$t"$'\n'; sl="$sl$s"$'\n'
    done
    echo "MEDIAN full$([ $all = 1 ] && echo -all) $size build=$(printf '%s' "$bl" | median) texture=$(printf '%s' "$tl" | median) score=$(printf '%s' "$sl" | median)"
    ;;
  idle)
    echo "uptime: $(uptime | tr -s ' ')"
    echo "kwin pid $K threads: $(ps -L -o psr= -p "$K" | tr -d ' ' | sort | uniq -c | tr '\n' ' ')"
    echo "gpu power: $(cat /sys/bus/platform/devices/1800000.gpu/power/control 2>/dev/null) $(cat /sys/bus/platform/devices/1800000.gpu/power/runtime_status 2>/dev/null)"
    echo "gpu clk: $(sudo awk '/^ +gpu0 /{printf "%d", $5/1000000}' /sys/kernel/debug/clk/clk_summary 2>/dev/null) MHz, dcdc4 $(for r in /sys/class/regulator/regulator.*; do case $(cat "$r"/name 2>/dev/null) in *dcdc4*) echo $(( $(cat "$r"/microvolts) / 1000 ));; esac; done | head -1) mV"
    echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)/$(cat /sys/devices/system/cpu/cpu6/cpufreq/scaling_governor 2>/dev/null) freq: $(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)/1000 ))/$(( $(cat /sys/devices/system/cpu/cpu6/cpufreq/scaling_cur_freq 2>/dev/null || echo 0)/1000 )) MHz"
    echo "irq484: $(cat /proc/irq/484/smp_affinity_list 2>/dev/null)"
    printf 'temps:'; for z in /sys/class/thermal/thermal_zone*/; do printf ' %s=%s' "$(cat "$z"/type)" "$(( $(cat "$z"/temp) / 1000 ))"; done; echo
    ;;
  threads)
    # where the threads of a process ran last, busiest first (default: kwin_wayland)
    p=${1:-$K}
    echo "-- $(ps -o comm= -p "$p") pid $p (psr = last CPU; 0-5 little, 6-7 big)"
    ps -L -o psr,tid,pcpu,comm -p "$p" | sort -k3 -rn | head -12
    ;;
  *) echo "usage: speed-bench.sh {client|full|idle|threads} ..."; exit 2 ;;
esac
