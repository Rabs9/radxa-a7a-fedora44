#!/bin/bash
# Mesa's own PowerVR driver is installed but reports only llvmpipe. Why?
#
# Upstream mesa's powervr driver talks to the MAINLINE kernel driver
# (drivers/gpu/drm/imagination, DRM node name "powervr"), not to Imagination's
# proprietary pvrsrvkm. This board runs pvrsrvkm, so mesa has nothing to bind.
#
# If that is the only gap, the real fix is a kernel with CONFIG_DRM_POWERVR -
# and then the GPU becomes a normal mesa device with a Wayland platform, GLX,
# zink, everything. The question is whether upstream supports THIS GPU.
set -u

echo "############ which kernel driver does mesa's powervr look for?"
strings -a /usr/lib64/libvulkan_powervr_mesa.so 2>/dev/null \
  | grep -oE '^(powervr|pvrsrvkm|pvr)$' | sort -u | sed 's/^/  drm name: /'

echo
echo "############ which GPUs does it claim to support?"
strings -a /usr/lib64/libvulkan_powervr_mesa.so 2>/dev/null \
  | grep -oE '(AXE|AXM|BXE|BXM|BXS|GX[0-9]+|Rogue|Series[0-9A-Za-z]*)[-_ A-Za-z0-9]{0,12}' \
  | sort -u | head -25 | sed 's/^/  /'

echo
echo "############ the BVNC this silicon reports"
dmesg 2>/dev/null | grep -oE '36\.[0-9]+\.[0-9]+\.[0-9]+' | head -1 | sed 's/^/  /'
strings -a /usr/lib64/libvulkan_powervr_mesa.so 2>/dev/null | grep -oE '\b[0-9]{1,2}\.[0-9]{1,3}\.[0-9]{1,4}\.[0-9]{1,3}\b' | sort -u | head -12 | sed 's/^/  supported BVNCs: /'

echo
echo "############ does this kernel have the mainline driver at all?"
zcat /proc/config.gz 2>/dev/null | grep -iE 'DRM_POWERVR|IMAGINATION' | sed 's/^/  /' || echo "  no DRM_POWERVR in this kernel config"
echo "  running kernel: $(uname -r)  (mainline drm/imagination landed in 6.8)"

echo
echo "############ what DRM nodes exist, and what would mesa need?"
for c in /sys/class/drm/card*; do
    [ -e "$c/device/driver" ] || continue
    printf "  %-8s %s\n" "$(basename $c)" "$(basename "$(readlink -f $c/device/driver)")"
done
echo "  mesa's powervr binds a node whose driver name is 'powervr'"

echo
echo "############ is the A733 supported by mainline at all?"
echo "  (linux-sunxi lists the board as Not Supported for mainline)"
grep -ic 'sun60i\|a733' /proc/config.gz 2>/dev/null || zcat /proc/config.gz 2>/dev/null | grep -c 'SUN60I' | sed 's/^/  SUN60I config symbols in this BSP kernel: /'
