#!/bin/bash
# Re-confirm, on hardware, the two claims the GitHub issue rests on:
#   1. our firmware blob is the same processor family as an already-published one
#   2. mesa's powervr driver names this exact GPU
# Anything that does not reproduce here does not go in the issue.
set -u

echo "############ 1. firmware processor of every blob"
for f in /lib/firmware/powervr/*; do
    b=$(basename "$f"); b=${b%.xz}
    case "$f" in
      *.xz) xz -dc "$f" > /tmp/vx.fw 2>/dev/null ;;
      *)    cp "$f" /tmp/vx.fw 2>/dev/null ;;
    esac
    m=$(readelf -h /tmp/vx.fw 2>/dev/null | sed -n 's/.*Machine: *//p')
    e=$(readelf -h /tmp/vx.fw 2>/dev/null | sed -n 's/.*Entry point address: *//p')
    printf "  %-32s %-24s entry %s\n" "$b" "${m:-NOT-AN-ELF}" "${e:-?}"
done
rm -f /tmp/vx.fw
for f in /lib/firmware/rgx.fw.36.56.104.183; do
    m=$(readelf -h "$f" 2>/dev/null | sed -n 's/.*Machine: *//p')
    e=$(readelf -h "$f" 2>/dev/null | sed -n 's/.*Entry point address: *//p')
    printf "  %-32s %-24s entry %s   <== OURS\n" "$(basename $f)" "${m:-NOT-AN-ELF}" "${e:-?}"
done

echo
echo "############ 2. does mesa's powervr driver name our GPU?"
L=/usr/lib64/libvulkan_powervr_mesa.so
ls -l "$L" 2>/dev/null | sed 's/^/  /'
echo "  --- BXM / BXE / AXE strings:"
strings -a "$L" 2>/dev/null | grep -oE 'B?[ABG]X[EMS][-_ ]?[0-9]+[-_ ]?[0-9]*[-_ ]?(MC[0-9])?' | sort -u | sed 's/^/     /'
echo "  --- exact BXM-4-64 hit:"
strings -a "$L" 2>/dev/null | grep -c 'BXM-4-64' | sed 's/^/     occurrences: /'

echo
echo "############ 3. firmware processors the in-tree driver implements"
M=$(find /lib/modules -name 'powervr.ko*' 2>/dev/null | head -1)
echo "  module: ${M:-NOT FOUND}"
if [ -n "${M:-}" ]; then
    case "$M" in
      *.xz) xz -dc "$M" > /tmp/vpvr.ko 2>/dev/null ;;
      *)    cp "$M" /tmp/vpvr.ko ;;
    esac
    echo "  --- symbols:"
    nm /tmp/vpvr.ko 2>/dev/null | grep -oiE 'pvr_fw_(mips|riscv|meta)[a-z0-9_]*' | sort -u | sed 's/^/     /'
    echo "  --- processor words in strings:"
    strings -a /tmp/vpvr.ko 2>/dev/null | grep -oiE '\bmips\b|\briscv\b|\bmeta\b' | sort -u | sed 's/^/     /'
    echo "  --- firmware path format string:"
    strings -a /tmp/vpvr.ko 2>/dev/null | grep -iE 'powervr/|rogue_' | sort -u | head -6 | sed 's/^/     /'
    echo "  --- MODULE_FIRMWARE entries:"
    modinfo -F firmware /tmp/vpvr.ko 2>/dev/null | sed 's/^/     /' || echo "     (none declared)"
    echo "  --- compatible strings it binds:"
    strings -a /tmp/vpvr.ko 2>/dev/null | grep -oE 'img,[a-z0-9-]+|ti,[a-z0-9-]+' | sort -u | sed 's/^/     /'
    rm -f /tmp/vpvr.ko
fi

echo
echo "############ 4. this board, for the Environment block"
echo "  kernel     : $(uname -r)"
echo "  DRM nodes  :"
for c in /sys/class/drm/card*; do
    [ -e "$c/device/driver" ] || continue
    printf "     %-8s %s\n" "$(basename $c)" "$(basename "$(readlink -f $c/device/driver)")"
done
echo "  GPU compatible:"
cat /proc/device-tree/soc*/gpu*/compatible 2>/dev/null | tr '\0' '\n' | sed 's/^/     /'
echo "  BVNC reported:"
dmesg 2>/dev/null | grep -oE '36\.[0-9]+\.[0-9]+\.[0-9]+' | sort -u | head -3 | sed 's/^/     /'
echo "  DRM_POWERVR in this kernel:"
(zcat /proc/config.gz 2>/dev/null | grep -E 'DRM_POWERVR' || echo "     not set") | sed 's/^/     /'
