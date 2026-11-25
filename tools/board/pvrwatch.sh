#!/bin/bash
# pvrwatch.sh <seconds> [interval]: run ON THE BOARD (sudo) on the open-stack kernel. Every interval: health sample
# (job timeouts, context resets decoded by pvr_dump, CCB WARNs, D-state kworkers, load), a snapshot of the firmware
# trace rings (/sys/kernel/debug/dri/<pvr minor>/pvr_fw/trace_*), and, when a kworker is in D state, its kernel
# stack. Everything goes under /home/radxa/pvr-logs/<stamp>/ (NOT /tmp: tmpfs, lost at reboot). Prints only changes.
T=${1:-900}; I=${2:-5}; END=$((SECONDS+T))
D=/home/radxa/pvr-logs/$(date +%Y%m%d-%H%M%S); mkdir -p "$D"; chown radxa:radxa /home/radxa/pvr-logs "$D" 2>/dev/null
M=$(for m in /sys/kernel/debug/dri/*; do [ -d "$m/pvr_fw" ] && basename "$m" && break; done | head -1)
# firmware trace mask knob: the upstream-master driver has pvr_fw/trace_mask (group mask; the TRACE bit is implied and
# rejected), the v6.16 graft had pvr_params/fw_trace_mask (raw log type: groups | TRACE bit 0x1). Groups 0x67a =
# MAIN|CLEANUP|CSW|BIF|PM|POW|HWR.
fwmask_knob() { if [ -e "/sys/kernel/debug/dri/$1/pvr_fw/trace_mask" ]; then echo "/sys/kernel/debug/dri/$1/pvr_fw/trace_mask 0x67a"; else echo "/sys/kernel/debug/dri/$1/pvr_params/fw_trace_mask 0x67b"; fi; }
# the v6.16 graft's fw_trace_mask module parameter did not arm the trace at boot (ring stayed empty); writing the mask
# through debugfs does, so re-arm it here (master loads init_fw_trace_mask at init, the write is then a no-op)
if [ -n "$M" ]; then read -r MASKF MASKV <<<"$(fwmask_knob "$M")"; printf "%s" "$MASKV" > "$MASKF" 2>/dev/null; fi
echo "logs: $D  pvr debugfs minor: ${M:-none}  fw trace mask: $(cat "${MASKF:-/dev/null}" 2>/dev/null)"
prev=""; n=0
sample() {
  local d; d=$(dmesg 2>/dev/null)
  local jt=$(grep -ac "Job timeout" <<<"$d") rs=$(grep -acE "context reset notification" <<<"$d")
  local uf=$(grep -acE "unknown FWCCB" <<<"$d") wa=$(grep -acE "WARNING:.*pvr_" <<<"$d")
  local ds=$(ps -eo stat,comm | awk '$1 ~ /^D/ && $2 ~ /kworker/ {n++} END{print n+0}')
  local kw=$(pgrep -u 1000 -x kwin_wayland | head -1)
  echo "timeouts=$jt resets=$rs unknownFWCCB=$uf pvrWARN=$wa Dkworkers=$ds load=$(cut -d" " -f1 /proc/loadavg) session_kwin=${kw:-none}"
}
snap() {
  n=$((n+1)); local s=$(printf "%04d" $n)
  for t in 0 1; do [ -r "/sys/kernel/debug/dri/$M/pvr_fw/trace_$t" ] && cat "/sys/kernel/debug/dri/$M/pvr_fw/trace_$t" > "$D/fwtrace${t}-$s.txt" 2>/dev/null; done
  for p in $(ps -eo pid,stat,comm | awk '$2 ~ /^D/ && $3 ~ /kworker/ {print $1}'); do echo "== pid $p $(cat /proc/$p/comm)"; cat /proc/$p/stack; done > "$D/dstacks-$s.txt" 2>/dev/null
  dmesg > "$D/dmesg-$s.txt" 2>/dev/null
  # keep only the last 12 snapshots of the rings/dmesg (the last one before the wedge is what matters), all d-stacks
  ls -1t "$D"/fwtrace0-*.txt 2>/dev/null | tail -n +13 | xargs -r rm -f
  ls -1t "$D"/fwtrace1-*.txt 2>/dev/null | tail -n +13 | xargs -r rm -f
  ls -1t "$D"/dmesg-*.txt 2>/dev/null | tail -n +13 | xargs -r rm -f
  [ -s "$D/dstacks-$s.txt" ] || rm -f "$D/dstacks-$s.txt"
}
echo "$(date +%T) start: $(sample)" | tee -a "$D/samples.txt"
while [ $SECONDS -lt $END ]; do
  sleep "$I"; snap
  cur=$(sample | sed 's/ load=[^ ]*//')
  if [ "$cur" != "$prev" ]; then echo "$(date +%T) $(sample)" | tee -a "$D/samples.txt"; prev=$cur; fi
done
echo "$(date +%T) end: $(sample)" | tee -a "$D/samples.txt"
dmesg | grep -aE "context reset|Reset reason|Data Master|Job ref|fault address|Job timeout" | tail -20 > "$D/resets.txt"
echo "--- decoded resets (last 20 lines): $(wc -l < "$D/resets.txt")"; cut -c1-150 "$D/resets.txt"
