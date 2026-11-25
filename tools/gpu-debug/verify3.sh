#!/bin/bash
# The driver builds the filename from the BVNC at runtime (%s_%d.%d.%d.%d_v%d.fw)
# and takes its feature/quirk config FROM the firmware image. So there is no
# device table to patch - the blob alone configures the driver.
#
# Which raises a much better question than "please publish it":
# mainline's blob is an ELF with the SAME entry point as ours (0xbfc00001).
# If mainline's container is structurally the DDK's rgx.fw plus the shader
# payload, then the file we already have could potentially be repacked locally.
# Compare the two ELFs section by section.
set -u
T=/tmp/cmp; rm -rf $T; mkdir -p $T
xz -dc /lib/firmware/powervr/rogue_36.52.104.182_v1.fw.xz > $T/main.fw 2>/dev/null
cp /lib/firmware/rgx.fw.36.56.104.183 $T/ours.fw
cp /lib/firmware/rgx.sh.36.56.104.183 $T/ours.sh 2>/dev/null

echo "############ sizes"
printf "  %-26s %8s bytes\n" "mainline 36.52 (v1.fw)" "$(stat -c %s $T/main.fw)"
printf "  %-26s %8s bytes\n" "ours rgx.fw"             "$(stat -c %s $T/ours.fw)"
printf "  %-26s %8s bytes\n" "ours rgx.sh"             "$(stat -c %s $T/ours.sh 2>/dev/null || echo 0)"
printf "  %-26s %8s bytes\n" "ours fw+sh"              "$(( $(stat -c %s $T/ours.fw) + $(stat -c %s $T/ours.sh 2>/dev/null || echo 0) ))"

echo
echo "############ ELF headers side by side"
for f in main.fw ours.fw; do
    echo "  --- $f"
    readelf -h $T/$f 2>/dev/null | grep -E 'Class|Data|Type|Machine|Entry|program headers|section headers|Number of' | sed 's/^ */     /'
done

echo
echo "############ section names"
for f in main.fw ours.fw; do
    echo "  --- $f"
    readelf -SW $T/$f 2>/dev/null | awk '$1 ~ /^\[/ {print}' | sed 's/^/     /' | head -30
done

echo
echo "############ is the shader payload inside mainline's blob?"
echo "  --- mainline strings (first 20):"
strings -a $T/main.fw 2>/dev/null | head -20 | sed 's/^/     /'
echo "  --- ours rgx.fw strings (first 20):"
strings -a $T/ours.fw 2>/dev/null | head -20 | sed 's/^/     /'
echo "  --- ours rgx.sh: is it an ELF too?"
readelf -h $T/ours.sh 2>/dev/null | grep -E 'Machine|Type|Entry' | sed 's/^ */     /' || echo "     not an ELF: $(od -An -tx1 -N8 $T/ours.sh | tr -s ' ')"

echo
echo "############ fw-info structure the driver validates"
echo "  (driver: pvr_fw_validate_init_device_info, 'Unsupported fw info version')"
for f in main.fw ours.fw; do
    printf "  %-10s last 64 bytes: %s\n" "$f" "$(tail -c 64 $T/$f | od -An -tx1 | tr -s ' \n' ' ')"
done
rm -rf $T
