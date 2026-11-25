#!/bin/bash
# Two questions decide whether the backport is a weekend or a dead end:
#   1. does the mainline driver accept BVNC 36.56.104.183?
#   2. what firmware filename does it want, and do we have that blob?
#
# The module Fedora built is on this disk, so ask it directly rather than
# reading source and hoping the build matches.
set -u
M=/lib/modules/6.19.10-300.fc44.aarch64/kernel/drivers/gpu/drm/imagination/powervr.ko.xz
T=/tmp/pvrmod
rm -rf $T; mkdir -p $T

echo "############ the module Fedora ships"
ls -l "$M" 2>/dev/null | sed 's/^/  /'
modinfo "$M" 2>/dev/null | grep -iE '^(filename|license|description|author|firmware|alias|depends|vermagic)' | head -20 | sed 's/^/  /'

echo
echo "############ FIRMWARE it declares"
modinfo -F firmware "$M" 2>/dev/null | sed 's/^/  wants: /' || echo "  none declared"

echo
echo "############ decompress and look inside"
cp "$M" $T/ 2>/dev/null && xz -d $T/powervr.ko.xz 2>/dev/null
ls -l $T/powervr.ko 2>/dev/null | sed 's/^/  /'

echo
echo "############ BVNC / device table strings"
strings -a $T/powervr.ko 2>/dev/null | grep -oE '\b3[0-9]\.[0-9]{1,3}\.[0-9]{1,4}\.[0-9]{1,3}\b' | sort -u | head -20 | sed 's/^/  /'
echo "  --- firmware path patterns:"
strings -a $T/powervr.ko 2>/dev/null | grep -oE 'powervr/[a-z0-9_./]*|rogue[a-z0-9_.%]*|%s\.fw[a-z0-9._%]*' | sort -u | head -12 | sed 's/^/  /'
echo "  --- device / compatible strings:"
strings -a $T/powervr.ko 2>/dev/null | grep -oE 'img,[a-z0-9-]+|ti,[a-z0-9-]+|BXM[-0-9]*|AXE[-0-9A-Z]*' | sort -u | head -14 | sed 's/^/  /'

echo
echo "############ what firmware do WE have?"
ls -l /lib/firmware/rgx.* 2>/dev/null | sed 's/^/  /'
ls -ld /lib/firmware/powervr 2>/dev/null | sed 's/^/  /' || echo "  no /lib/firmware/powervr directory"
echo "  --- BVNC this silicon reports:"
dmesg 2>/dev/null | grep -oE 'BVNC[^ ]*|36\.[0-9]+\.[0-9]+\.[0-9]+' | sort -u | head -4 | sed 's/^/     /'

echo
echo "############ is our GPU node even the shape mainline expects?"
echo "  our card1 driver : $(basename $(readlink -f /sys/class/drm/card1/device/driver 2>/dev/null))"
echo "  our gpu compatible:"
cat /proc/device-tree/soc*/gpu*/compatible 2>/dev/null | tr '\0' '\n' | sed 's/^/     /'
rm -rf $T
