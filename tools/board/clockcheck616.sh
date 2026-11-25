#!/bin/bash
# clockcheck616.sh: run ON THE BOARD right after booting the open-stack kernel (fedora-6.6.98-pvr+).
# The upstream drm/imagination driver only clk_prepare_enable()s the clocks the DT hands it ("core", "sys", "mem")
# and never touches the GPU regulator, while the DDK sets the core clock from the DT's clk_rate property and the
# dcdc4 voltage from its DVFS table. So under the open stack the GPU runs at whatever the clock controller and PMIC
# defaulted to at boot, which nobody has measured; the DDK's "computed-looking" faults at 1200 MHz / 800 mV were
# exactly a clock/voltage margin problem (docs/GPU-TQ-FAULT-2026-09-07.md). Validated pairs: 600 @ 800 mV,
# 1008 @ 800 mV (clock-ceiling.sh, 4 rounds x 16 cases, 0 resets).
set -u
S() { sudo "$@" 2>/dev/null; }
echo "############ kernel / driver"
echo "  kernel : $(uname -r), up $(uptime -p)"
echo "  powervr: loaded=$(lsmod | grep -c '^powervr')  $(S dmesg | grep -a 'Initialized powervr' | tr -cd '[:print:]\n' | tail -1)"
for c in /sys/class/drm/card*; do [ -e "$c/device/driver" ] && printf "  %-8s %s\n" "$(basename $c)" "$(basename "$(readlink -f $c/device/driver)")"; done
echo
echo "############ GPU clock tree (rate column is the programmed rate even while gated)"
S grep -E "gpu|pll-gpu" /sys/kernel/debug/clk/clk_summary | cut -c1-105
echo
echo "############ GPU rail"
for r in /sys/class/regulator/regulator.*; do n=$(cat $r/name 2>/dev/null); case $n in *dcdc4*) echo "  $n: $(cat $r/microvolts) uV, state $(cat $r/state), consumers: $(ls $r | grep -c '^[a-z0-9]*\.[a-z]*$')";; esac; done
echo
echo "############ what the DT says (ignored by the upstream driver, honoured by the DDK)"
echo "  clk_rate     : $(od -An -tu4 --endian=big /proc/device-tree/soc@3000000/gpu@1800000/clk_rate 2>/dev/null | awk '{print $1/1000000" MHz"}')"
echo "  assigned-clks: $(ls /proc/device-tree/soc@3000000/gpu@1800000/ | grep -c assigned-clock) property(ies)"
echo "  clock-names  : $(tr '\0' ' ' < /proc/device-tree/soc@3000000/gpu@1800000/clock-names)"
echo
echo "############ RAM layout (for the mem= test)"
echo "  $(grep MemTotal /proc/meminfo)   $(S cat /proc/iomem | grep -i 'system ram' | tr '\n' ' ')"
echo "  cmdline: $(cat /proc/cmdline)"
echo
echo "############ GPU messages so far"
S dmesg | grep -aiE 'powervr|pvr|imagination' | grep -aivE 'Initialized powervr' | tail -12 | tr -cd '[:print:]\n' | cut -c1-150
