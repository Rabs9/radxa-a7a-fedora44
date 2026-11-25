#!/bin/bash
# Fedora's own kernel is installed on this board (unused - it cannot boot the
# A733). If it carries CONFIG_DRM_POWERVR, then the driver is already sitting on
# the disk; the only thing missing is a kernel that can both boot this SoC and
# contain it. That reframes the work from "port a driver" to "get one driver
# into a kernel that already boots".
set -u

echo "############ kernels installed here"
rpm -qa 2>/dev/null | grep -E '^kernel(-core)?-[0-9]' | sed 's/^/  /'
echo "  running: $(uname -r)"

echo
echo "############ does FEDORA's kernel have the upstream PowerVR driver?"
for v in $(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' kernel-core 2>/dev/null); do
    C=/boot/config-$v
    if [ -f "$C" ]; then
        echo "  --- $v"
        grep -iE 'CONFIG_DRM_POWERVR|CONFIG_DRM_IMAGINATION' "$C" | sed 's/^/       /' || echo "       not set"
    else
        echo "  --- $v : no config file on disk"
    fi
done
echo "  --- is the module actually there?"
find /lib/modules -iname 'powervr*.ko*' 2>/dev/null | sed 's/^/     /' || echo "     no powervr module found"

echo
echo "############ and OUR kernel?"
zcat /proc/config.gz 2>/dev/null | grep -icE 'CONFIG_DRM_POWERVR' | sed 's/^/  DRM_POWERVR in 6.6.98-a7a: /'
zcat /proc/config.gz 2>/dev/null | grep -cE '^CONFIG_SUN60I|SUN60I' | sed 's/^/  SUN60I symbols (the A733 support Fedora lacks): /'

echo
echo "############ why Fedora'\''s kernel cannot boot this board"
ls -1 /boot/dtb-* 2>/dev/null | head -3 | sed 's/^/  /'
for v in $(rpm -q --qf '%{VERSION}-%{RELEASE}.%{ARCH}\n' kernel-core 2>/dev/null | head -1); do
    find /lib/modules/$v -maxdepth 2 -iname '*sun60i*' -o -maxdepth 2 -iname '*a733*' 2>/dev/null | head -3 | sed 's/^/  /'
    echo "  a733 device tree in Fedora'\''s kernel? $(find /boot /lib/modules/$v -iname '*a733*' -o -iname '*sun60i*' 2>/dev/null | head -1 || echo NONE)"
done
