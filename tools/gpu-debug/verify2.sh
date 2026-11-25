#!/bin/bash
# The issue claims "publish one blob and the driver can use it". That is only
# true if the driver builds the firmware filename from the BVNC at runtime AND
# recognises 36.56.104.183 as a device it knows.
#
# If instead it carries a fixed BVNC table, a small kernel patch is needed too -
# which must be said in the issue rather than discovered by them.
set -u
M=$(find /lib/modules -name 'powervr.ko*' 2>/dev/null | head -1)
xz -dc "$M" > /tmp/p.ko 2>/dev/null || cp "$M" /tmp/p.ko

echo "############ is the firmware name built at runtime?"
strings -a /tmp/p.ko | grep -nE 'powervr/rogue|%d\.%d|_v1\.fw|%u\.%u' | sort -u | sed 's/^/  /'

echo
echo "############ every BVNC-shaped string in the module"
strings -a /tmp/p.ko | grep -oE '\b[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}\.[0-9]{1,4}\b' | sort -u | sed 's/^/  /'

echo
echo "############ does it carry a per-BVNC feature table?"
strings -a /tmp/p.ko | grep -oiE 'pvr_device_info[a-z0-9_]*|pvr_device_features[a-z0-9_]*|quirks?_[0-9]+|enhancements?_[0-9]+' | sort -u | head -20 | sed 's/^/  /'
echo "  --- symbols that look like a device table:"
nm -a /tmp/p.ko 2>/dev/null | grep -iE 'device_info|features|quirk|enhancement|bvnc' | awk '{print $NF}' | sort -u | head -25 | sed 's/^/  /'

echo
echo "############ how does it report an unknown device?"
strings -a /tmp/p.ko | grep -iE 'unsupported|unknown|no match|not supported|BVNC' | sort -u | head -15 | sed 's/^/  /'

echo
echo "############ our two DDK files, for the record"
ls -l /lib/firmware/rgx.fw.36.56.104.183 /lib/firmware/rgx.sh.36.56.104.183 2>/dev/null | awk '{printf "  %-40s %10s bytes\n",$9,$5}'
ls -l /lib/firmware/powervr/ 2>/dev/null | awk 'NR>1{printf "  %-40s %10s bytes\n",$9,$5}'
rm -f /tmp/p.ko
