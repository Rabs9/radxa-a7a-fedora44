#!/bin/bash
# Ours is MIPS, mainline's 36.53 is RISC-V. Does mainline support MIPS firmware
# processors at all? That decides the ask entirely:
#   - if yes: the only gap is one missing blob (small, concrete request)
#   - if no : the driver itself needs work for this GPU generation (much bigger)
set -u

echo "############ firmware processor architecture of each blob"
for f in /lib/firmware/powervr/*.xz; do
    b=$(basename "$f" .xz)
    xz -dc "$f" > /tmp/x.fw 2>/dev/null
    m=$(readelf -h /tmp/x.fw 2>/dev/null | grep -i 'Machine:' | sed 's/.*Machine: *//')
    printf "  %-34s %s\n" "$b" "$m"
done
rm -f /tmp/x.fw
printf "  %-34s %s\n" "OURS rgx.fw.36.56.104.183" \
  "$(readelf -h /lib/firmware/rgx.fw.36.56.104.183 2>/dev/null | grep -i 'Machine:' | sed 's/.*Machine: *//')"

echo
echo "############ what firmware processors does the mainline driver implement?"
M=/lib/modules/6.19.10-300.fc44.aarch64/kernel/drivers/gpu/drm/imagination/powervr.ko.xz
cp "$M" /tmp/powervr.ko.xz 2>/dev/null
xz -df /tmp/powervr.ko.xz 2>/dev/null
ls -l /tmp/powervr.ko 2>/dev/null | sed 's/^/  /'
echo "  --- processor-specific symbols in the driver:"
nm /tmp/powervr.ko 2>/dev/null | grep -oiE 'pvr_fw_(mips|riscv|meta)[a-z_]*' | sort -u | head -12 | sed 's/^/     /'
echo "  --- and strings:"
strings -a /tmp/powervr.ko 2>/dev/null | grep -oiE '\bmips\b|\briscv\b|\bmeta\b' | sort -u | sed 's/^/     /'
rm -f /tmp/powervr.ko
