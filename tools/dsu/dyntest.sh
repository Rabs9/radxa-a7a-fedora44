#!/bin/bash
# dsu_dyn behavioral test driver — run one step per invocation, results to stdout.
snap() {
  echo "L=$(cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq) B=$(cat /sys/devices/system/cpu/cpufreq/policy6/scaling_cur_freq) DSU=$(sudo grep -m1 ' cpu_dsu ' /sys/kernel/debug/clk/clk_summary | awk '{print $5}') T=$(cat /sys/class/thermal/thermal_zone0/temp)"
}
case "$1" in
idle)
  for i in $(seq 1 8); do sleep 1; snap; done
  ;;
allcore)
  sysbench cpu --cpu-max-prime=20000 --threads=8 --time=12 run > /tmp/dyn_cpu.log 2>&1 &
  P=$!
  for i in $(seq 1 5); do sleep 2; snap; done
  wait $P
  grep "events per second" /tmp/dyn_cpu.log
  ;;
bigonly)
  taskset -c 6,7 sysbench cpu --cpu-max-prime=20000 --threads=2 --time=10 run > /tmp/dyn_big.log 2>&1 &
  P=$!
  for i in $(seq 1 4); do sleep 2; snap; done
  wait $P
  grep "events per second" /tmp/dyn_big.log
  ;;
mem)
  sysbench memory --memory-block-size=1M --memory-total-size=16G --threads=8 run 2>/dev/null | grep transferred
  snap
  ;;
storm)
  echo "MARK dyn storm" | sudo tee /dev/kmsg > /dev/null
  for i in $(seq 1 20); do
    sysbench cpu --cpu-max-prime=5000 --threads=8 --time=1 run > /dev/null 2>&1
    sleep 1
  done
  snap
  echo "errs=$(sudo dmesg | awk '/MARK dyn storm/{s=1} s&&/Failed to change|error|WARN/{c++} END{print c+0}')"
  ;;
esac
