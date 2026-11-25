#!/bin/bash
# 19 sections, same names, same order, same load addresses, same entry point.
# That is not "a similar format" - that looks like the SAME BUILD SYSTEM.
# If .comment (the toolchain string) and the program headers also match, then
# linux-firmware's rogue_*_v1.fw is simply a DDK rgx.fw under a new name -
# and the file we already have on this board may be usable as-is.
set -u
T=/tmp/cmp2; rm -rf $T; mkdir -p $T
xz -dc /lib/firmware/powervr/rogue_36.52.104.182_v1.fw.xz > $T/main.fw
xz -dc /lib/firmware/powervr/rogue_33.15.11.3_v1.fw.xz   > $T/old.fw
cp /lib/firmware/rgx.fw.36.56.104.183 $T/ours.fw

echo "############ .comment - which toolchain built each"
for f in main.fw old.fw ours.fw; do
    printf "  %-9s %s\n" "$f" "$(readelf -p .comment $T/$f 2>/dev/null | sed -n 's/.*\]  *//p' | tr '\n' ' ')"
done

echo
echo "############ program headers"
for f in main.fw ours.fw; do
    echo "  --- $f"
    readelf -lW $T/$f 2>/dev/null | awk '/^  LOAD|^  [A-Z]/ {print}' | sed 's/^/     /'
done

echo
echo "############ .gnu.attributes"
for f in main.fw ours.fw; do
    printf "  %-9s %s\n" "$f" "$(readelf -A $T/$f 2>/dev/null | tr '\n' ' ' | tr -s ' ')"
done

echo
echo "############ does the in-tree driver load a SECOND (shader) file?"
M=$(find /lib/modules -name 'powervr.ko*' | head -1)
xz -dc "$M" > $T/p.ko 2>/dev/null || cp "$M" $T/p.ko
echo "  --- every request_firmware-ish format string:"
strings -a $T/p.ko | grep -E '\.fw|\.sh|firmware' | sort -u | head -12 | sed 's/^/     /'
echo "  --- section names the driver looks for inside the blob:"
strings -a $T/p.ko | grep -oE '\.[a-z_]{3,20}' | sort -u | grep -E 'text|data|rodata|bss|boot|exc' | sed 's/^/     /'

echo
echo "############ what IS rgx.sh then?"
od -An -tx1 -N16 /lib/firmware/rgx.sh.36.56.104.183 | tr -s ' ' | sed 's/^/  magic: /'
file /lib/firmware/rgx.sh.36.56.104.183 2>/dev/null | sed 's/^/  /'
strings -a /lib/firmware/rgx.sh.36.56.104.183 2>/dev/null | head -6 | sed 's/^/  /'
rm -rf $T
