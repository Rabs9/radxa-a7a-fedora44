#!/bin/bash
# clock-ceiling.sh [repeats]: run the whole glamor-repro matrix on the board's GPU N times (default 3) and report the
# GPU hardware-recovery count per case, at whatever GPU clock the board booted with. Run from the laptop:
#     tools/gpu-debug/clock-ceiling.sh 3
# Procedure for a candidate clock (docs/GPU-TQ-FAULT-2026-09-07.md, item 2 of the plan):
#     ssh board 'sudo a7a-clock gpu 1008'        # boot entry g_1008_800 cloned from the default kernel, DTB patched
#     ssh board 'sudo systemctl reboot'          # screen blanks; stay on the login screen (the GPU must be idle)
#     tools/gpu-debug/clock-ceiling.sh 3         # all cases x 3, HWR must stay +0 everywhere
# The matrix covers what failed at 1200 MHz / 800 mV: small bo clears (transfer path), EGLImage sampling, uploads,
# the coherent VBO draw, dmabuf import, partial (scissor) clears, the 256-px upload sizes.
set -euo pipefail
P=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)
BOARD=${BOARD:-radxa@radxa-cubie-a7a.local}; KEY=${KEY:-$HOME/.ssh/a7a_key}; N=${1:-3}
SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=8 $BOARD"
scp -q -i "$KEY" "$P/tools/gpu-debug/glamor-repro.c" "$P/tools/gpu-debug/run-matrix.sh" "$BOARD:/tmp/"
$SSH 'cd /tmp && gcc -O1 -w -o glamor-repro glamor-repro.c -lgbm -lEGL -lGLESv2 && chmod +x run-matrix.sh && echo "built on $(uname -r)"'
echo "GPU clock (DTB): $($SSH 'od -An -tu4 --endian=big /proc/device-tree/soc@3000000/gpu@1800000/clk_rate' | awk '{print $1/1000000}') MHz, boot entry: $($SSH 'grep ^default /boot/extlinux/extlinux.conf | cut -d" " -f2'), GPU temp: $($SSH 'cat /sys/class/thermal/thermal_zone*/type | grep -n gpu | cut -d: -f1 | head -1 | xargs -I{} sh -c "cat /sys/class/thermal/thermal_zone\$(({}-1))/temp"' 2>/dev/null | awk '{print $1/1000}')C"
CASES=("tex 24 24" "bo 1 1" "bo 24 24" "bo 128 128" "bo 256 256" "bo 1024 768" "bo+scissor 1024 768" "bo+tqfirst 24 24" "bomod 24 24" "dmabuf 24 24" "draw 64 64" "draw+vbo 64 64" "botex 64 64" "sample 24 24" "tex+warm 64 64" "mods 24 24")
H0=$($SSH 'sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status | grep -oE "[0-9]+$"')
for r in $(seq 1 "$N"); do
  echo "=== round $r/$N"
  $SSH "cd /tmp && sudo NODE=/dev/dri/renderD128 ./run-matrix.sh $(printf '"%s" ' "${CASES[@]}")" | sed 's/^/  /'
done
H1=$($SSH 'sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status | grep -oE "[0-9]+$"')
echo "=== total GPU hardware recoveries during the run: $((H1-H0)) (HWR $H0 -> $H1); GPU temp now: $($SSH 'cat /sys/class/thermal/thermal_zone*/type | grep -n gpu | cut -d: -f1 | head -1 | xargs -I{} sh -c "cat /sys/class/thermal/thermal_zone\$(({}-1))/temp"' 2>/dev/null | awk '{print $1/1000}')C"
