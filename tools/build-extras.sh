#!/bin/bash
# The out-of-tree modules from BUILDING.md: the PowerVR BXM-4-64 GPU module and the AIC8800 WiFi
# driver (from BSP 1.4.6, the 1.4.8 one has bugs). Both land in output/payload/lib/modules/<rel>/extra.
set -uo pipefail
# a bare non-interactive invocation (a pipe, a stray call) must not rebuild or delete anything: the CLI sets A7A_RUN=1
[ -n "${A7A_RUN:-}" ] || [ -t 0 ] || { echo "$0: refusing to run non-interactively without A7A_RUN=1 (use python3 -m src.cli)"; exit 2; }
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); W=$P/work; K=$W/kernel-6.6; PAY=$P/output/payload
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu-
REL=$(cat $PAY/kernelrelease); EXTRA=$PAY/lib/modules/$REL/extra; mkdir -p "$EXTRA"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
step "WiFi driver from BSP 1.4.6"
[ -d $W/allwinner-bsp-1.4.6 ] || git clone --branch cubie-aiot-v1.4.6 --depth 1 https://github.com/radxa/allwinner-bsp.git $W/allwinner-bsp-1.4.6
( cd $K && make BSP_TOP=bsp/ M=$W/allwinner-bsp-1.4.6/drivers/net/wireless/aic8800/usb -j16 ) 2>&1 | tail -3
find $W/allwinner-bsp-1.4.6/drivers/net/wireless/aic8800/usb -name "*.ko" -exec cp {} "$EXTRA/" \; ; ls "$EXTRA"
if [ "${A7A_SKIP_PVRSRVKM:-0}" = 1 ]; then step "PowerVR DDK module skipped (A7A_SKIP_PVRSRVKM=1: open-stack flavour, the upstream powervr driver owns the GPU)"; else
step "PowerVR GPU module (rogue_km)"
GPU=$W/allwinner-bsp-1.4.8/modules/gpu/img-bxm/linux/rogue_km
# a7a-fedora's fixes to the module (idempotent: patch -N skips what is already applied)
for pf in "$P"/tools/patches/*pvr*.patch; do [ -f "$pf" ] && { patch -p1 -N -s -d "$GPU" < "$pf" 2>/dev/null && echo "applied $(basename "$pf")" || echo "$(basename "$pf"): already applied"; }; done
sed -i 's/^.NOTINTERMEDIATE:/#.NOTINTERMEDIATE:/' $K/scripts/Kbuild.include
( cd $GPU && make PVR_BUILD_DIR=sunxi_linux BUILD=release KERNELDIR=$K KERNEL_CROSS_COMPILE=aarch64-linux-gnu- KERNEL_CC=aarch64-linux-gnu-gcc KERNEL_LD=aarch64-linux-gnu-ld KERNEL_NM=aarch64-linux-gnu-nm KERNEL_AR=aarch64-linux-gnu-ar KERNEL_OBJCOPY=aarch64-linux-gnu-objcopy CROSS_COMPILE=aarch64-linux-gnu- ARCH=arm64 -j16 ) 2>&1 | tail -4
sed -i 's/^#.NOTINTERMEDIATE:/.NOTINTERMEDIATE:/' $K/scripts/Kbuild.include
KO=$(find $GPU -name pvrsrvkm.ko | head -1); [ -n "$KO" ] && cp "$KO" "$EXTRA/" && echo "pvrsrvkm.ko: $(du -h "$KO" | cut -f1)" || echo "pvrsrvkm.ko NOT built"
fi
step "depmod for $REL"; depmod -b "$PAY" "$REL" 2>&1 | tail -1; ls "$EXTRA"
step "done"
