#!/bin/bash
# pvr-module-build.sh: rebuild only pvrsrvkm.ko from the rogue_km tree (same invocation as tools/build-extras.sh),
# for kernel-module experiments. Prints errors and the resulting .ko. Load on the board with
#   sudo rmmod pvrsrvkm && sudo insmod /tmp/pvrsrvkm-dbg.ko        (only when `lsmod` shows 0 users)
# and go back to the installed module with  sudo rmmod pvrsrvkm && sudo modprobe pvrsrvkm  (or reboot).
# Firmware-log apphints (tools/gpu-debug/fwtrace.sh on) reset at every reload.
set -uo pipefail
P=$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd); W=$P/work; K=$W/kernel-6.6
GPU=$W/allwinner-bsp-1.4.8/modules/gpu/img-bxm/linux/rogue_km
LOG=${LOG:-/tmp/pvr-module-build.log}
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
sed -i 's/^.NOTINTERMEDIATE:/#.NOTINTERMEDIATE:/' "$K/scripts/Kbuild.include"
( cd "$GPU" && make PVR_BUILD_DIR=sunxi_linux BUILD=release KERNELDIR="$K" KERNEL_CROSS_COMPILE=aarch64-linux-gnu- \
    KERNEL_CC=aarch64-linux-gnu-gcc KERNEL_LD=aarch64-linux-gnu-ld KERNEL_NM=aarch64-linux-gnu-nm KERNEL_AR=aarch64-linux-gnu-ar \
    KERNEL_OBJCOPY=aarch64-linux-gnu-objcopy CROSS_COMPILE=aarch64-linux-gnu- ARCH=arm64 -j16 ) > "$LOG" 2>&1
rc=$?
sed -i 's/^#.NOTINTERMEDIATE:/.NOTINTERMEDIATE:/' "$K/scripts/Kbuild.include"
grep -E "error|undefined" "$LOG" | head -20
KO=$(find "$GPU" -name pvrsrvkm.ko | head -1)
echo "rc=$rc ko=$KO $( [ -n "$KO" ] && ls -la "$KO" | awk '{print $5, $6, $7, $8}')"
