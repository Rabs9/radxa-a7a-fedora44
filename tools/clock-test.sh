#!/bin/bash
# clock-test.sh: ON THE BOARD, as radxa, with the desktop logged in. "The clocks work" for an image means two things
# for each clock domain (CPU, GPU, memory, NPU, DSU/L3): the board boots at what the image promises, and the panel's
# controls really change it. `a7a-clock check` answers the first (read-only). This script does the second: it uses
# every runtime control of a7a-clock, reads the result back from the hardware's own files, measures where a number
# alone proves nothing (CPU loop time, memory copy time), and puts everything back the way it found it.
#   clock-test.sh [--no-session]   everything that needs no reboot (about 4 minutes). The GPU stress test draws
#                                  through the desktop: without a logged-in session the script stops, or with
#                                  --no-session skips it and ends "INCOMPLETE" (exit 3)
#   clock-test.sh gpu-set          makes a GPU boot entry at another clock and leaves it as the default: reboot,
#   clock-test.sh gpu-verify       LOG IN FIRST; checks that the GPU runs at that clock and survives a soak, and puts
#                                  the boot menu back byte for byte: reboot again
# exit 0 = every step read back what it set; 1 = something failed; 2 = not started (nothing was changed);
# 3 = nothing failed but a part was skipped.
# The memory clock is switched while the system runs, at every rate the boot firmware lists, some of them for the
# first time on this board: that step comes last, the card is synced before each switch, and a rate that hangs the
# board is a finding (the panel offers it). On a test card.
# A lost ssh connection must not stop this half-way with the boot menu or the clocks changed: HUP and PIPE are
# ignored (tools/board-session.sh runs it as a unit of its own anyway).
# Not measured here: the NPU's speed at its three rates (the control and its read-back are; an inference timed at
# 492 and at 1008 MHz would be the proof).
set -u
trap '' HUP PIPE
DMC=/sys/class/devfreq/a020000.dmcfreq
NPU=/sys/class/devfreq/3600000.npu
POL0=/sys/devices/system/cpu/cpufreq/policy0
POL6=/sys/devices/system/cpu/cpufreq/policy6
EXT=/boot/extlinux/extlinux.conf
STATE=/var/tmp/clock-test.state
BAK=/var/tmp/clock-test.extlinux.conf
FAILS=0; PASSES=0; SKIPPED=0
ok()   { printf "  PASS  %s\n" "$*"; PASSES=$((PASSES+1)); }
bad()  { printf "  FAIL  %s\n" "$*"; FAILS=$((FAILS+1)); }
info() { printf "  info  %s\n" "$*"; }
skip() { printf "  SKIP  %s\n" "$*"; SKIPPED=$((SKIPPED+1)); }
same() { if [ "$2" = "$3" ]; then ok "$1: $2"; else bad "$1: read back '$2', expected '$3'"; fi; }   # what, got, want
mhz()  { echo $(( $(cat "$1" 2>/dev/null || echo 0) / 1000000 )); }
npu_mhz() { echo $(( $(cat $NPU/target_freq 2>/dev/null || cat $NPU/cur_freq 2>/dev/null || echo 0) / 1000000 )); }
dsu_mhz() { sudo grep -m1 ' cpu_dsu ' /sys/kernel/debug/clk/clk_summary 2>/dev/null | awk '{print int($5/1000000)}'; }
gpu_mhz() { sudo grep -E "^ +gpu0 " /sys/kernel/debug/clk/clk_summary 2>/dev/null | awk '{print int($5/1000000)}' | head -1; }
dsu_mod() { lsmod | awk '/^dsu_(oc|dyn)/{print $1}' | paste -sd, -; }
hwr()  { sudo awk -F: '/HWR Event Count/ {gsub(/ /,"",$2); print $2}' /sys/kernel/debug/pvr/status 2>/dev/null | head -1; }
session() { pgrep -x plasmashell > /dev/null; }
# the operating point a cap becomes: the kernel stores the highest point of this chip's table at or under the request
# (eco asks for 1512 MHz on the big cluster; a board of the 26 MHz bin has 1508)
opp_under() { tr ' ' '\n' < "$1/scaling_available_frequencies" | awk -v m="$2" 'NF && $1 <= m' | sort -n | tail -1; }
# kernel trouble since a moment (unix time), from the journal: the kernel's ring buffer wraps within minutes here
ktrouble() { sudo journalctl -k -b --since "@$1" --no-pager 2>/dev/null | grep -cE "Unable to handle kernel|Internal error: |Oops: |BUG: |Call trace:|EXT4-fs error|I/O error, dev |PVR_K.*(Fatal|Device experienced error)" || true; }
# seconds for a fixed piece of work on one core / for copying 64 MiB eight times
cpu_time() { taskset -c "$1" python3 -c 'import time
t=time.perf_counter(); x=0
for i in range(2500000): x+=i*i
print("%.3f" % (time.perf_counter()-t))'; }
mem_time() { taskset -c "$BIGCPU" python3 -c 'import time
b=bytearray(64<<20); t=time.perf_counter()
for _ in range(8): c=bytes(b)
print("%.3f" % (time.perf_counter()-t))'; }
ratio_ok() { awk -v a="$1" -v b="$2" -v lo="$3" 'BEGIN { exit !(b > 0 && a / b >= lo) }'; }    # a/b >= lo
LITTLECPU=$(tr ' ' '\n' < $POL0/related_cpus 2>/dev/null | grep -v '^$' | tail -1); LITTLECPU=${LITTLECPU:-1}
BIGCPU=$(tr ' ' '\n' < $POL6/related_cpus 2>/dev/null | grep -v '^$' | tail -1); BIGCPU=${BIGCPU:-7}

# ---- the GPU boot entry of the test: a clock the PLL makes exactly (a multiple of 24 MHz), near the validated 840,
# whose label and device-tree directory nobody uses yet. The boot menu is copied before and put back as that copy.
GPU_PENDING=0; GCLK=; GDIR=
gpu_pick() {
  local c
  for c in 840 864 816; do
    grep -q "^label g_${c}_800\$" "$EXT" 2>/dev/null && continue
    [ -e "/usr/lib/linux-image-gpu${c}v800" ] && continue
    GCLK=$c; GDIR=/usr/lib/linux-image-gpu${c}v800; return 0
  done
  return 1
}
ext_save() {     # the menu as it is, and whether it is immutable
  local immut=0; lsattr "$EXT" 2>/dev/null | cut -d' ' -f1 | grep -q i && immut=1
  sudo cp -p "$EXT" "$BAK" && sync && printf 'clk=%s\ndir=%s\nimmut=%s\nsum=%s\n' "$GCLK" "$GDIR" "$immut" "$(sha256sum < "$EXT" | cut -c1-64)" | sudo tee "$STATE" > /dev/null
}
ext_restore() {  # the menu byte for byte as ext_save found it; the test's device tree gone
  [ -f "$STATE" ] && sudo test -f "$BAK" || return 1
  local immut dir; immut=$(sed -n 's/^immut=//p' "$STATE"); dir=$(sed -n 's/^dir=//p' "$STATE")
  sudo chattr -i "$EXT" 2>/dev/null
  # a new file, then a rename: written in place, a power cut between the truncation and the write would leave an
  # empty boot menu and a card that does not boot
  sudo cp -p "$BAK" "$EXT.clock-test" && sync && sudo mv -f "$EXT.clock-test" "$EXT" || return 1
  [ "$immut" = 1 ] && sudo chattr +i "$EXT"
  case "$dir" in /usr/lib/linux-image-gpu*v800) sudo rm -rf "$dir" ;; esac
  sync
  [ "$(sha256sum < "$EXT" | cut -c1-64)" = "$(sed -n 's/^sum=//p' "$STATE")" ] || return 1
  sudo rm -f "$STATE" "$BAK"; GPU_PENDING=0
}

case "${1:-}" in
gpu-set)
  echo "== GPU boot entry at another clock"
  [ ! -f "$STATE" ] || { echo "an earlier run left $STATE: 'clock-test.sh gpu-verify' finishes it (or puts the menu back)"; exit 2; }
  gpu_pick || { echo "the labels g_840_800, g_864_800 and g_816_800 are all in use on this board: nothing to test with"; exit 2; }
  ext_save || { echo "could not copy $EXT to $BAK"; exit 2; }
  GPU_PENDING=1; trap '[ $GPU_PENDING = 1 ] && [ $FAILS -gt 0 ] && ext_restore' EXIT
  trap 'FAILS=$((FAILS+1)); exit 1' INT TERM
  OLD_DEF=$(awk '/^default/{print $2}' "$EXT"); OLD_KERNEL=$(awk -v l="$OLD_DEF" '$1=="label"{c=$2} $1=="linux"&&c==l{print $2; exit}' "$EXT")
  if OUT=$(a7a-clock gpu "$GCLK" 2>&1); then
    echo "$OUT" | sed 's/^/    /'
    same "default boot entry" "$(awk '/^default/{print $2}' "$EXT")" "g_${GCLK}_800"
    same "clk_rate in the entry's device tree" "$(fdtget -t i "$GDIR/allwinner/sun60i-a733-cubie-a7a.dtb" /soc@3000000/gpu@1800000 clk_rate 2>/dev/null)" "$(( GCLK * 1000000 ))"
    grep -A4 "^label g_${GCLK}_800\$" "$EXT" | grep -q "fdtdir $GDIR/" && ok "the entry boots that device tree" || bad "the entry does not name $GDIR/"
    same "the entry's kernel (that of $OLD_DEF, the default until now)" "$(awk -v l="g_${GCLK}_800" '$1=="label"{c=$2} $1=="linux"&&c==l{print $2; exit}' "$EXT")" "$OLD_KERNEL"
  else bad "a7a-clock gpu $GCLK failed: $(echo "$OUT" | tail -1)"; fi
  if [ $FAILS -gt 0 ]; then echo "== $PASSES passed, $FAILS failed: the boot menu is put back, nothing to reboot for"; exit 1; fi
  echo "== $PASSES passed. Reboot, LOG IN, then: clock-test.sh gpu-verify"; exit 0 ;;
gpu-verify)
  echo "== GPU after the reboot"
  [ -f "$STATE" ] || { echo "no $STATE: run clock-test.sh gpu-set and reboot first"; exit 2; }
  # the entry is used up by this step (the menu goes back): without a desktop there is no load to put on the GPU
  session || { echo "nobody is logged in on the desktop: log in at the board (radxa), then run gpu-verify again. Nothing was changed."; exit 2; }
  GCLK=$(sed -n 's/^clk=//p' "$STATE"); T0=$(date +%s)
  same "GPU clock (MHz)" "$(gpu_mhz)" "$GCLK"
  OUT=$(a7a-clock soak 1 2>&1); echo "$OUT" | sed 's/^/    /'
  echo "$OUT" | grep -q "VERDICT: STABLE" && ok "one-minute soak at $GCLK MHz: stable" || bad "the soak at $GCLK MHz did not end with VERDICT: STABLE"
  H=$(hwr); same "hardware recoveries since boot" "${H:-0}" 0
  same "kernel trouble since the check began" "$(ktrouble "$T0")" 0
  if ext_restore; then ok "boot menu put back byte for byte"; else bad "the boot menu could not be put back as it was: compare $EXT with $BAK"; fi
  echo "== $PASSES passed, $FAILS failed. Reboot once more: the GPU is back on $(awk '/^default/{print $2}' "$EXT")"
  [ $FAILS -eq 0 ] || exit 1; exit 0 ;;
""|--no-session) ;;
*) echo "usage: clock-test.sh [--no-session | gpu-set | gpu-verify]"; exit 2 ;;
esac

if ! session && [ "${1:-}" != --no-session ]; then
  echo "nobody is logged in on the desktop: the GPU stress test draws through the session."
  echo "Log in (radxa) and run this again, or: clock-test.sh --no-session   (skips that part, ends INCOMPLETE)"; exit 2
fi
[ ! -f "$STATE" ] || { echo "an unfinished GPU entry test is on this board ($STATE): 'clock-test.sh gpu-verify' first"; exit 2; }

# ---- what is there now, and how to get back to it
T_START=$(date +%s)
G0=$(cat $POL0/scaling_governor); G6=$(cat $POL6/scaling_governor)
C0=$(cat $POL0/scaling_max_freq); C6=$(cat $POL6/scaling_max_freq)
DG=$(cat $DMC/governor); DM=$(cat $DMC/max_freq); DC=$(cat $DMC/cur_freq)
NG=$(cat $NPU/governor 2>/dev/null); NT=$(cat $NPU/target_freq 2>/dev/null)
M0=$(dsu_mod); D0=$(dsu_mhz); H0=$(hwr); H0=${H0:-0}
restore() {
  [ $GPU_PENDING = 1 ] && ext_restore
  echo "$G0" | sudo tee $POL0/scaling_governor > /dev/null; echo "$G6" | sudo tee $POL6/scaling_governor > /dev/null
  echo "$C0" | sudo tee $POL0/scaling_max_freq > /dev/null; echo "$C6" | sudo tee $POL6/scaling_max_freq > /dev/null
  # the memory controller: the limit first (a rate above a lower limit would be clamped), then rate and governor
  tr ' ' '\n' < $DMC/available_frequencies | sort -n | tail -1 | sudo tee $DMC/max_freq > /dev/null 2>&1
  echo userspace | sudo tee $DMC/governor > /dev/null; echo "$DC" | sudo tee $DMC/userspace/set_freq > /dev/null 2>&1
  echo "$DG" | sudo tee $DMC/governor > /dev/null; echo "$DM" | sudo tee $DMC/max_freq > /dev/null 2>&1
  if [ -n "$NG" ]; then
    echo userspace | sudo tee $NPU/governor > /dev/null 2>&1; [ -z "$NT" ] || echo "$NT" | sudo tee $NPU/userspace/set_freq > /dev/null 2>&1
    echo "$NG" | sudo tee $NPU/governor > /dev/null 2>&1
  fi
  # the DSU as it was: no module, the dynamic one, or the fixed one at the clock it held
  sudo rmmod dsu_dyn 2>/dev/null; sudo rmmod dsu_oc 2>/dev/null
  case "$M0" in dsu_dyn) a7a-clock dsu dyn > /dev/null 2>&1 ;; dsu_oc) a7a-clock dsu "${D0:-1352}" > /dev/null 2>&1 ;; esac
  return 0
}
trap restore EXIT
trap 'echo; echo "interrupted: putting everything back"; exit 1' INT TERM

echo "== 1. what the image promises (a7a-clock check)"
if a7a-clock check; then ok "a7a-clock check: no failure"; else bad "a7a-clock check reports failures (above)"; fi

echo "== 2. CPU governor"
for g in $(cat $POL0/scaling_available_governors); do
  case "$g" in performance|schedutil|ondemand|powersave) ;; *) continue ;; esac
  a7a-clock cpu "$g" > /dev/null
  same "governor $g on both clusters" "$(cat $POL0/scaling_governor) $(cat $POL6/scaling_governor)" "$g $g"
done
a7a-clock cpu nonsense > /dev/null 2>&1 && bad "a7a-clock cpu accepted a governor that does not exist" || ok "a governor that does not exist is refused"
a7a-clock cpu performance > /dev/null

echo "== 3. CPU clock caps, with the time a fixed loop takes (cores $LITTLECPU and $BIGCPU)"
TOP0=$(( $(cat $POL0/cpuinfo_max_freq) / 1000 )); TOP6=$(( $(cat $POL6/cpuinfo_max_freq) / 1000 )); [ "$TOP6" -gt 3000 ] && TOP6=3000
# a low operating point of each cluster: the first one at or above 1000 MHz
LOW0=$(tr ' ' '\n' < $POL0/scaling_available_frequencies | sort -n | awk '$1 >= 1000000 {print int($1/1000); exit}')
LOW6=$(tr ' ' '\n' < $POL6/scaling_available_frequencies | sort -n | awk '$1 >= 1000000 {print int($1/1000); exit}')
a7a-clock cpumax max max > /dev/null; sleep 1
same "caps at the top (MHz)" "$(( $(cat $POL0/scaling_max_freq)/1000 )) $(( $(cat $POL6/scaling_max_freq)/1000 ))" "$TOP0 $TOP6"
same "clocks at the top under 'performance' (MHz)" "$(( $(cat $POL0/scaling_cur_freq)/1000 )) $(( $(cat $POL6/scaling_cur_freq)/1000 ))" "$TOP0 $TOP6"
FAST0=$(cpu_time "$LITTLECPU"); FAST6=$(cpu_time "$BIGCPU")
a7a-clock cpumax "$LOW0" "$LOW6" > /dev/null; sleep 1
same "caps lowered (MHz)" "$(( $(cat $POL0/scaling_max_freq)/1000 )) $(( $(cat $POL6/scaling_max_freq)/1000 ))" "$LOW0 $LOW6"
SLOW0=$(cpu_time "$LITTLECPU"); SLOW6=$(cpu_time "$BIGCPU")
a7a-clock cpumax max max > /dev/null; sleep 1
# the loop must slow down by most of the clock ratio: 0.6 of it. Caches and memory do not scale with the core clock,
# and the A76 feels that most (the L3 runs at the DSU's 780 MHz here): measured on the board 2026-11-17, a 3000 ->
# 1014 MHz cap made the A76 loop 2.15x slower (0.73 of the clock ratio), the A55 2800 -> 1014 MHz 2.48x (0.90). A cap
# that does not take shows as 1.0x.
W0=$(awk -v t="$TOP0" -v l="$LOW0" 'BEGIN { printf "%.2f", 0.6 * t / l }'); W6=$(awk -v t="$TOP6" -v l="$LOW6" 'BEGIN { printf "%.2f", 0.6 * t / l }')
ratio_ok "$SLOW0" "$FAST0" "$W0" && ok "A55: the loop takes ${FAST0}s at $TOP0 MHz and ${SLOW0}s at $LOW0 MHz" || bad "A55: ${FAST0}s at $TOP0 MHz, ${SLOW0}s at $LOW0 MHz: the clock did not change as asked (expected at least ${W0}x)"
ratio_ok "$SLOW6" "$FAST6" "$W6" && ok "A76: the loop takes ${FAST6}s at $TOP6 MHz and ${SLOW6}s at $LOW6 MHz" || bad "A76: ${FAST6}s at $TOP6 MHz, ${SLOW6}s at $LOW6 MHz: the clock did not change as asked (expected at least ${W6}x)"

echo "== 4. NPU clock (read at devfreq's target: the module clock in cur_freq never moves)"
if [ -d $NPU ]; then
  for r in $(tr ' ' '\n' < $NPU/available_frequencies | sort -n | awk 'NF {print int($1/1000000)}'); do
    if OUT=$(a7a-clock npu "$r" 2>&1); then same "a7a-clock npu $r" "$(npu_mhz)" "$r"; else bad "a7a-clock npu $r failed: $(echo "$OUT" | tail -1)"; fi
  done
  info "the NPU's speed at these rates is not measured here"
else bad "no NPU devfreq device ($NPU)"; fi

echo "== 5. DSU/L3 (CPU caps at the top: the fixed rung needs the A55 rail up)"
if OUT=$(a7a-clock dsu stock 2>&1); then
  sleep 1; S=$(dsu_mhz); same "stock: no module, the boot clock (MHz)" "$(dsu_mod):$S" ":780"
  if OUT=$(a7a-clock dsu 1352 2>&1); then sleep 1; same "forced 1352 MHz" "$(dsu_mod):$(dsu_mhz)" "dsu_oc:1352"; else bad "a7a-clock dsu 1352: $(echo "$OUT" | tail -1)"; fi
  if OUT=$(a7a-clock dsu dyn 2>&1); then
    sleep 1
    ( for c in $(tr ' ' '\n' < $POL0/related_cpus | grep -v '^0$') $(cat $POL6/related_cpus); do taskset -c "$c" timeout 3 sh -c 'while :; do :; done' & done; sleep 2; dsu_mhz > /tmp/clock-test.dsu; wait ) 2>/dev/null
    D=$(cat /tmp/clock-test.dsu 2>/dev/null); rm -f /tmp/clock-test.dsu
    [ "$(dsu_mod)" = dsu_dyn ] && [ "${D:-0}" -gt "${S:-0}" ] && ok "dynamic: $D MHz under load (stock $S)" || bad "dynamic: module '$(dsu_mod)', $D MHz under load (stock $S): it did not rise"
  else bad "a7a-clock dsu dyn: $(echo "$OUT" | tail -1)"; fi
  a7a-clock dsu stock > /dev/null 2>&1; sleep 1; same "back to stock" "$(dsu_mod):$(dsu_mhz)" ":780"
  a7a-clock dsu 99999 > /dev/null 2>&1 && bad "a7a-clock dsu 99999 was accepted" || same "a DSU clock out of range is refused, nothing loaded" "$(dsu_mod)" ""
else bad "a7a-clock dsu stock: $(echo "$OUT" | tail -1)"; fi

echo "== 6. profiles"
TOPR=$(tr ' ' '\n' < $DMC/available_frequencies | sort -n | awk 'NF {print int($1/1000000)}' | tail -1)
if OUT=$(a7a-clock profile eco 2>&1); then
  sleep 1
  same "eco: CPU caps (kHz, the table's points under 1196 and 1512 MHz), memory limit (MHz), DSU module" \
    "$(cat $POL0/scaling_max_freq) $(cat $POL6/scaling_max_freq) $(mhz $DMC/max_freq) $(dsu_mod)" "$(opp_under $POL0 1196000) $(opp_under $POL6 1512000) 854 dsu_dyn"
  [ "$(mhz $DMC/cur_freq)" -le 854 ] && ok "eco: memory at $(mhz $DMC/cur_freq) MHz" || bad "eco: memory still at $(mhz $DMC/cur_freq) MHz"
  # the limit eco leaves must not clamp a later request (it did, without a word, until 2026-11-16)
  if OUT=$(a7a-clock ram "$TOPR" 2>&1); then same "after eco, a7a-clock ram $TOPR" "$(mhz $DMC/cur_freq)" "$TOPR"; else bad "after eco, a7a-clock ram $TOPR: $(echo "$OUT" | tail -1)"; fi
else bad "a7a-clock profile eco: $(echo "$OUT" | tail -1)"; fi
for pr in balanced oc dynoc; do
  if OUT=$(a7a-clock profile $pr 2>&1); then
    sleep 1
    case $pr in oc) wm=dsu_oc ;; *) wm=dsu_dyn ;; esac
    same "$pr: CPU caps (MHz), memory (MHz), DSU module" "$(( $(cat $POL0/scaling_max_freq)/1000 )) $(( $(cat $POL6/scaling_max_freq)/1000 )) $(mhz $DMC/cur_freq) $(dsu_mod)" "$TOP0 $TOP6 $TOPR $wm"
    [ $pr != oc ] || same "oc: DSU clock (MHz)" "$(dsu_mhz)" 1352
  else bad "a7a-clock profile $pr: $(echo "$OUT" | tail -1)"; fi
done
a7a-clock dsu stock > /dev/null 2>&1

echo "== 7. GPU boot entry (made, checked, put back; the clock itself needs the reboot: clock-test.sh gpu-set)"
if gpu_pick && ext_save; then
  GPU_PENDING=1
  if OUT=$(a7a-clock gpu "$GCLK" 2>&1); then
    same "default boot entry" "$(awk '/^default/{print $2}' "$EXT")" "g_${GCLK}_800"
    same "clk_rate in the entry's device tree" "$(fdtget -t i "$GDIR/allwinner/sun60i-a733-cubie-a7a.dtb" /soc@3000000/gpu@1800000 clk_rate 2>/dev/null)" "$(( GCLK * 1000000 ))"
  else bad "a7a-clock gpu $GCLK failed: $(echo "$OUT" | tail -1)"; fi
  # what is not a clock must be refused. Inside the window in which the menu has a copy: an a7a-clock that takes
  # one of these makes it the default entry, and the copy is what undoes that
  for wrong in abc 0 500 1500 01300x; do
    SUM=$(sha256sum < "$EXT")
    if a7a-clock gpu $wrong > /dev/null 2>&1; then bad "a7a-clock gpu $wrong was accepted"
    elif [ "$(sha256sum < "$EXT")" = "$SUM" ]; then ok "a7a-clock gpu $wrong is refused, the boot menu untouched"
    else bad "a7a-clock gpu $wrong was refused but changed the boot menu"; fi
  done
  if ext_restore; then ok "boot menu put back byte for byte"; else bad "the boot menu could not be put back as it was: compare $EXT with $BAK"; fi
else bad "no free test label (g_840_800, g_864_800, g_816_800), or $EXT could not be copied"; fi

echo "== 8. GPU stress test (1 minute, at the running clock: $(gpu_mhz) MHz)"
if session; then
  OUT=$(a7a-clock soak 1 2>&1); echo "$OUT" | sed 's/^/    /'
  echo "$OUT" | grep -q "VERDICT: STABLE" && ok "soak: stable" || bad "soak did not end with VERDICT: STABLE"
else skip "nobody is logged in: the soak draws through the desktop session"; fi

echo "== 9. memory clock: every rate the boot firmware offers, with the time a 512 MiB copy takes (LAST: a rate this"
echo "      board has never run could hang it; the card is synced before each switch)"
RATES=$(tr ' ' '\n' < $DMC/available_frequencies | sort -n | awk 'NF {print int($1/1000000)}')
LOWR=$(echo "$RATES" | head -1); TLOW=; TTOP=
for r in $(echo "$RATES" | sort -rn); do      # from the top down: the rates in daily use first
  echo "  ....  switching the memory to $r MHz"; sync
  if OUT=$(a7a-clock ram "$r" 2>&1); then
    got=$(mhz $DMC/cur_freq); t=$(mem_time)
    [ "$got" = "$r" ] && ok "a7a-clock ram $r: memory at $got MHz, copy ${t}s" || bad "a7a-clock ram $r said yes, the memory controller reads $got MHz"
    [ "$r" = "$LOWR" ] && TLOW=$t; [ "$r" = "$TOPR" ] && TTOP=$t
  else bad "a7a-clock ram $r failed: $(echo "$OUT" | tail -1)"; fi
done
a7a-clock ram "$TOPR" > /dev/null 2>&1
if [ -n "$TLOW" ] && [ -n "$TTOP" ] && [ "$LOWR" != "$TOPR" ]; then
  ratio_ok "$TLOW" "$TTOP" 1.2 && ok "the copy is slower at $LOWR MHz (${TLOW}s) than at $TOPR MHz (${TTOP}s): the memory clock really moves" \
    || bad "the copy takes ${TLOW}s at $LOWR MHz and ${TTOP}s at $TOPR MHz: no difference, the memory clock did not move"
else bad "the memory copy could not be timed at the lowest and the highest rate"; fi
a7a-clock ram 1 > /dev/null 2>&1 && bad "a7a-clock ram accepted a rate the firmware does not offer" || ok "a rate the firmware does not offer is refused"

echo "== 10. everything back"
restore; trap - EXIT; sleep 1
same "CPU governors" "$(cat $POL0/scaling_governor) $(cat $POL6/scaling_governor)" "$G0 $G6"
same "CPU caps" "$(cat $POL0/scaling_max_freq) $(cat $POL6/scaling_max_freq)" "$C0 $C6"
same "memory clock (MHz), governor, limit (MHz)" "$(mhz $DMC/cur_freq) $(cat $DMC/governor) $(mhz $DMC/max_freq)" "$(( DC / 1000000 )) $DG $(( DM / 1000000 ))"
[ -z "$NG" ] || same "NPU rate (MHz) and governor" "$(npu_mhz) $(cat $NPU/governor)" "$(( ${NT:-0} / 1000000 )) $NG"
same "DSU module" "$(dsu_mod)" "$M0"
H1=$(hwr); same "GPU hardware recoveries during the test" "$(( ${H1:-0} - H0 ))" 0
same "kernel trouble during the test" "$(ktrouble "$T_START")" 0
if OUT=$(a7a-clock check 2>&1); then ok "a7a-clock check passes again"; else bad "a7a-clock check fails after the test: $(echo "$OUT" | grep FAIL | head -3 | tr '\n' ';')"; fi
if [ $FAILS -gt 0 ]; then echo "== $PASSES passed, $FAILS failed, $SKIPPED skipped"; exit 1; fi
if [ $SKIPPED -gt 0 ]; then echo "== $PASSES passed, 0 failed, $SKIPPED skipped: INCOMPLETE"; exit 3; fi
echo "== $PASSES passed, 0 failed"; exit 0
