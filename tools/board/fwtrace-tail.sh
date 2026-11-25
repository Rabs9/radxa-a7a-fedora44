#!/bin/bash
# fwtrace-tail.sh <seconds>: ON THE BOARD as root. Append NEW firmware trace lines (both FW threads) to
# /home/radxa/pvr-logs/fwtrace-live-<stamp>.txt. "New" = not present in the previous read of the ring (keyed on the
# whole line), which survives firmware reboots (the FW timer restarts at ~0, so timestamp ordering cannot be used).
# Every 5 s a "# host time, timeouts, resets" line is added.
T=${1:-900}; END=$((SECONDS+T)); M=$(for m in /sys/kernel/debug/dri/*; do [ -d "$m/pvr_fw" ] && basename "$m" && break; done)
OUT=/home/radxa/pvr-logs/fwtrace-live-$(date +%Y%m%d-%H%M%S).txt; mkdir -p /home/radxa/pvr-logs
# firmware trace mask knob: the upstream-master driver has pvr_fw/trace_mask (group mask; the TRACE bit is implied and
# rejected), the v6.16 graft had pvr_params/fw_trace_mask (raw log type: groups | TRACE bit 0x1). Groups 0x67a =
# MAIN|CLEANUP|CSW|BIF|PM|POW|HWR.
fwmask_knob() { if [ -e "/sys/kernel/debug/dri/$1/pvr_fw/trace_mask" ]; then echo "/sys/kernel/debug/dri/$1/pvr_fw/trace_mask 0x67a"; else echo "/sys/kernel/debug/dri/$1/pvr_params/fw_trace_mask 0x67b"; fi; }
read -r MASKF MASKV <<<"$(fwmask_knob "$M")"; printf "%s" "$MASKV" > "$MASKF" 2>/dev/null
echo "# start $(date +%T) minor $M mask $(cat "$MASKF")" > "$OUT"
: > /tmp/fwprev-0.txt; : > /tmp/fwprev-1.txt; n=0
while [ $SECONDS -lt $END ]; do
  for t in 0 1; do
    cat "/sys/kernel/debug/dri/$M/pvr_fw/trace_$t" 2>/dev/null > /tmp/fwcur-$t.txt
    # lines of the current ring that were not in the previous ring, in ring order
    awk -v T="$t" 'NR==FNR {seen[$0]++; next} { if (seen[$0]-- > 0) next; print "T" T " " $0 }' /tmp/fwprev-$t.txt /tmp/fwcur-$t.txt >> "$OUT"
    mv /tmp/fwcur-$t.txt /tmp/fwprev-$t.txt
  done
  n=$((n+1)); if [ $((n % 10)) -eq 0 ]; then echo "# $(date +%T) timeouts=$(dmesg | grep -ac 'Job timeout') resets=$(dmesg | grep -ac 'context reset notification') runtime=$(cat /sys/bus/platform/devices/1800000.gpu/power/runtime_status)" >> "$OUT"; fi
  sleep 0.5
done
echo "# end $(date +%T) lines=$(wc -l < "$OUT")" >> "$OUT"; echo "$OUT"
