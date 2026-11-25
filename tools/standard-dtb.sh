#!/bin/bash
# standard-dtb.sh <kernel release> ...: the device trees of the "standard" image (the vendor's clocks), one per kernel.
#   -> output/dtb-standard/linux-image-<release>-standard/allwinner/sun60i-a733-cubie-a7a.dtb
#      output/dtb-standard/vendor-reference.dtb (compiled here from the vendor's unmodified sources)
# The kernel packages ship the owner's overclocked device tree. tools/standard-dtb.py takes that very file out of the
# package in output/rpms and removes what goes beyond the vendor's tables; this script gives it the reference: the
# vendor's board file and SoC files as they are in work/ (allwinner-device, allwinner-bsp), run through cpp and dtc
# the way the kernel's build does, without touching the kernel tree (tools/build-kernel.sh copies the overclocked
# files over the kernel tree's own, so the tree itself is no reference).
# tools/master-image.sh puts the result into the standard image beside the package's device tree and points the boot
# menu at it; tools/a7a-extlinux keeps a later kernel's entry on the same kind of tree.
# No root, nothing outside output/. Tests: tests/test_standard_dtb.py.
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output; W=$P/work; D=$OUT/dtb-standard
[ $# -ge 1 ] || { echo "usage: $0 <kernel release> ..."; exit 2; }
BSP=$(ls -d "$W"/allwinner-bsp-* 2>/dev/null | LC_ALL=C sort -V | tail -1); DEV=$(ls -d "$W"/allwinner-device-* 2>/dev/null | LC_ALL=C sort -V | tail -1); K=$W/kernel-6.6
BOARD=$DEV/configs/cubie_a7a/linux-6.6/board.dts
for f in "$BOARD" "$BSP/configs/linux-6.6/sun60iw2p1.dtsi" "$BSP/configs/linux-6.6/sun60iw2p1-cpu-vf.dtsi" "$BSP/include/dt-bindings/clock/sun60iw2-ccu.h" "$K/include/dt-bindings/gpio/gpio.h"; do
  [ -f "$f" ] || { echo "the vendor's sources are not in work/ ($f): run the sources stage"; exit 1; }
done
mkdir -p "$D"; T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
# the three files the overclocked build replaces, as the vendor has them; everything they include comes from the BSP
# and the kernel tree
cp "$BOARD" "$T/sun60i-a733-cubie-a7a.dts"; cp "$BSP/configs/linux-6.6/sun60iw2p1.dtsi" "$BSP/configs/linux-6.6/sun60iw2p1-cpu-vf.dtsi" "$T/"
( cd "$T" && cpp -nostdinc -I "$BSP/include" -I "$K/include" -I "$K/arch/arm64/boot/dts/allwinner" -I "$K/arch/arm64/boot/dts" -I "$K/scripts/dtc/include-prefixes" \
    -undef -D__DTS__ -x assembler-with-cpp sun60i-a733-cubie-a7a.dts | dtc -q -I dts -O dtb -o "$D/vendor-reference.dtb" - )
echo "vendor reference: $D/vendor-reference.dtb ($(basename "$DEV"), $(basename "$BSP"); sha256 $(sha256sum "$D/vendor-reference.dtb" | cut -c1-16)...)"
for REL in "$@"; do
  RPM=$(ls "$OUT"/rpms/kernel-a7a*-"${REL//-/_}"-*.aarch64.rpm 2>/dev/null | tail -1)
  [ -n "$RPM" ] || { echo "no kernel package for $REL in $OUT/rpms"; exit 1; }
  rpm2cpio "$RPM" | cpio -i --quiet --to-stdout "./usr/lib/linux-image-$REL/allwinner/sun60i-a733-cubie-a7a.dtb" > "$T/$REL.dtb"
  [ -s "$T/$REL.dtb" ] || { echo "$(basename "$RPM") holds no device tree for the Cubie A7A"; exit 1; }
  O=$D/linux-image-$REL-standard/allwinner; mkdir -p "$O"
  echo "== $REL (from $(basename "$RPM"))"
  python3 "$P/tools/standard-dtb.py" "$T/$REL.dtb" "$D/vendor-reference.dtb" "$O/sun60i-a733-cubie-a7a.dtb"
  echo "   $O/sun60i-a733-cubie-a7a.dtb  sha256 $(sha256sum "$O/sun60i-a733-cubie-a7a.dtb" | cut -c1-16)..."
done
