#!/bin/bash
# Substituting 36.52/36.53 firmware is out - BVNC identifies the silicon, not a
# release. But we DO hold firmware for 36.56.104.183; it is the DDK's two-file
# packaging rather than mainline's single _v1.fw container.
#
# If mainline's container is just a header plus the same fw and shader payloads,
# converting is arithmetic. If it is a different build of the microcode
# entirely, it is not. Look at the actual bytes.
set -u
T=/tmp/fwx; rm -rf $T; mkdir -p $T

echo "############ what we hold (DDK packaging)"
ls -l /lib/firmware/rgx.fw.36.56.104.183 /lib/firmware/rgx.sh.36.56.104.183 2>/dev/null \
  | awk '{printf "  %-44s %s bytes\n", $9, $5}'
echo "  --- headers:"
for f in /lib/firmware/rgx.fw.36.56.104.183 /lib/firmware/rgx.sh.36.56.104.183; do
    printf "     %-40s %s\n" "$(basename $f)" "$(od -An -tx1 -N16 "$f" | tr -s ' ')"
done
echo "  --- any BVNC text inside?"
strings -a /lib/firmware/rgx.fw.36.56.104.183 2>/dev/null | grep -oE '36\.[0-9]+\.[0-9]+\.[0-9]+|RGX|BVNC' | sort -u | head -6 | sed 's/^/     /'

echo
echo "############ what mainline wants (its own container)"
for f in /lib/firmware/powervr/*.xz; do
    b=$(basename "$f" .xz)
    xz -dc "$f" > "$T/$b" 2>/dev/null
    printf "  %-34s %s bytes uncompressed\n" "$b" "$(stat -c %s "$T/$b" 2>/dev/null)"
done
echo "  --- header of one:"
H=$(ls $T/rogue_36.53* 2>/dev/null | head -1)
[ -n "$H" ] && od -An -tx1 -N32 "$H" | tr -s ' ' | sed 's/^/     /'
echo "  --- readable strings in it:"
[ -n "$H" ] && strings -a "$H" 2>/dev/null | head -8 | sed 's/^/     /'
echo "  --- does it look like a container with sections?"
[ -n "$H" ] && strings -a "$H" 2>/dev/null | grep -oiE 'rgx|shader|fw|meta|riscv|mips' | sort -u | head -8 | sed 's/^/     /'

echo
echo "############ size comparison - is mainline fw = DDK fw + sh?"
A=$(stat -c %s /lib/firmware/rgx.fw.36.56.104.183 2>/dev/null)
B=$(stat -c %s /lib/firmware/rgx.sh.36.56.104.183 2>/dev/null)
C=$(stat -c %s "$H" 2>/dev/null)
echo "  DDK fw + sh   : $A + $B = $((A+B))"
echo "  mainline _v1  : $C"
echo "  (if these are the same order of magnitude, the payloads may be the same"
echo "   microcode with a different wrapper; if not, they are different builds)"

rm -rf $T
