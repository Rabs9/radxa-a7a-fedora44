#!/bin/bash
# run-matrix.sh "<variant w h>" ...: run glamor-repro cases on the DDK and report the GPU hardware-recovery
# delta (from /sys/kernel/debug/pvr/status) and the readback pixels of each case. Copy to the board as
# /tmp/run-matrix.sh next to /tmp/glamor-repro (see glamor-repro.c for the build line).
#   /tmp/run-matrix.sh "tex 24 24" "bo 24 24" "bo+tqfirst 24 24" "sample 24 24" "bo+scissor 1024 768"
# HWR +N means N GPU resets happened during the case (each ~45-60 ms glFinish); pixels tell whether the
# clear actually landed (red 255 0 0 255, then blue 0 0 255 255).
NODE=${NODE:-/dev/dri/renderD128}
h() { v=$(sudo grep -oE "HWR Event Count: +[0-9]+" /sys/kernel/debug/pvr/status 2>/dev/null | grep -oE "[0-9]+$"); echo "${v:-0}"; }
for args in "$@"; do
  H0=$(h); set -- $args
  out=$(LD_LIBRARY_PATH=/opt/pvr/lib LIBGL_DRIVERS_PATH=/opt/pvr/lib/dri timeout 25 /tmp/glamor-repro "$NODE" $1 $2 $3 2>&1 \
        | grep -vE "EGL extensions" | grep -E "glFinish|pixel\(|RESULT|error|FAIL|map|warm-up|early" \
        | sed -E "s/\[ *[0-9.]+ ms\] //; s/glFinish after //; s/pixel\(0,0\) = /px=/" | tr "\n" "|" | cut -c1-170)
  H1=$(h); printf "%-22s HWR +%s  %s\n" "$args" "$((H1-H0))" "$out"
done
