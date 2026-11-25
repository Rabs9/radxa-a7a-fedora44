#!/bin/bash
sudo insmod ~/dsu_oc/dsu_oc.ko freq=1352000000 2>/dev/null
sudo cat /sys/kernel/debug/clk/clk_summary | awk "/^ *cpu_dsu /{print \"soak at \" \$5/1000000 \" MHz\"}"
( while true; do echo "$(date +%H:%M:%S) $(cat /sys/class/thermal/thermal_zone0/temp)"; sleep 10; done > ~/dsu_soak_temps.log ) &
TPID=$!
stress-ng --cpu 4 --matrix 4 --vm 2 --vm-bytes 2G --timeout 720 --metrics-brief 2>&1
kill $TPID
echo "=== post-soak clk ==="
sudo cat /sys/kernel/debug/clk/clk_summary | awk "/^ *cpu_dsu /{print \$5/1000000\" MHz\"}"
echo "=== dmesg tail ==="
sudo dmesg | tail -15
echo "=== max temp ==="
sort -k2 -n ~/dsu_soak_temps.log | tail -1
echo SOAK_DONE
