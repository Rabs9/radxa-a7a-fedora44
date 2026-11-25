#!/bin/bash
# The firmware is not the blocker after all. The blocker is a kernel that both
# boots the A733 AND contains drm/imagination.
#
# Backporting the driver to our 6.6.98 BSP is only feasible if its DRM core
# dependencies are reachable. The driver needs drm_gpuvm (6.7), drm_exec (6.8)
# and a modern drm_sched. Check what 6.6.98 already has.
set -u

echo "############ our kernel"
echo "  running: $(uname -r)"
echo "  --- DRM core options in the BSP config:"
zcat /proc/config.gz 2>/dev/null | grep -E '^CONFIG_DRM(_|=)' | grep -viE 'panel|bridge|sun4i|sunxi|dp_aux|display_helper' | head -30 | sed 's/^/     /'

echo
echo "############ does 6.6.98 have the helpers the driver needs?"
for s in drm_gpuvm drm_exec drm_sched drm_gem_shmem drm_syncobj; do
    n=$(grep -c "^$s" /proc/kallsyms 2>/dev/null)
    m=$(find /lib/modules/$(uname -r)/kernel/drivers/gpu/drm -maxdepth 1 -name "${s}*" 2>/dev/null | head -1)
    printf "  %-16s kallsyms:%-5s module:%s\n" "$s" "${n:-0}" "${m:-none}"
done
echo "  --- built-in symbol check (any prefix):"
for s in drm_gpuvm drm_exec_ drm_sched_ ; do
    printf "  %-14s %s\n" "$s" "$(grep -c " $s" /proc/kallsyms 2>/dev/null)"
done

echo
echo "############ what DRM version does 6.6.98 expose?"
grep -m1 -r 'DRM_IOCTL_VERSION' /dev/null 2>/dev/null
for c in /sys/class/drm/card0 /sys/class/drm/card1; do
    [ -e "$c" ] || continue
    printf "  %-8s %s\n" "$(basename $c)" "$(cat $c/device/uevent 2>/dev/null | tr '\n' ' ' | cut -c1-90)"
done

echo
echo "############ Fedora's 6.19 module - how big is the thing we would backport?"
M=/lib/modules/6.19.10-300.fc44.aarch64/kernel/drivers/gpu/drm/imagination/powervr.ko.xz
ls -l "$M" 2>/dev/null | awk '{printf "  compressed  %s bytes\n",$5}'
xz -dc "$M" 2>/dev/null | wc -c | sed 's/^/  uncompressed /'
echo "  --- what it depends on:"
modinfo -F depends "$M" 2>/dev/null | sed 's/^/     /'
echo "  --- undefined symbols it needs from the DRM core:"
xz -dc "$M" > /tmp/p5.ko 2>/dev/null
nm -u /tmp/p5.ko 2>/dev/null | awk '{print $2}' | grep -E '^drm_' | sed 's/^/     /' | head -60
echo "  --- total undefined drm_ symbols: $(nm -u /tmp/p5.ko 2>/dev/null | awk '{print $2}' | grep -cE '^drm_')"
echo "  --- of those, MISSING from our running 6.6.98 kernel:"
miss=0
for s in $(nm -u /tmp/p5.ko 2>/dev/null | awk '{print $2}' | grep -E '^drm_'); do
    if ! grep -q " $s\$" /proc/kallsyms 2>/dev/null; then
        echo "        $s"; miss=$((miss+1))
    fi
done
echo "     MISSING COUNT: $miss"
rm -f /tmp/p5.ko
