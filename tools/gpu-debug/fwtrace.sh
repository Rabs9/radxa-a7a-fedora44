#!/bin/bash
# fwtrace.sh on|dump: PowerVR firmware trace through debugfs (works on the release DDK 24.2 build).
#   on    enable all log groups on gpu00 (resets when pvrsrvkm is reloaded) and raise the HWR dump limit
#   dump  print the kicks / faults / HWR analysis lines of the current ring buffer
# The firmware log is the ONLY place that shows the real fault: the kernel's "Guilty Lockup" dump reads the
# fault registers after the reset (all zero). A real MMU fault looks like:
#   T0-HWR> MMU fault event: 0x00000210 (debug info: mmu pf)
#   T0-HWR> BIF0 page fault detected (Core 0 MMU Status: 0x...6005 Req Status: 0x0000748000596000)
# Decode Req Status with tools/gpu-debug/decode-fault.py (address, requester tag, read/write).
A=/sys/kernel/debug/pvr/gpu00/apphint
case "${1:-dump}" in
  on)
    printf "main,mts,cleanup,csw,bif,pm,rtd,spm,pow,hwr,dma,misc,debug" | sudo tee $A/EnableLogGroup >/dev/null
    printf "0x10" | sudo tee $A/HWRDebugDumpLimit >/dev/null
    echo "EnableLogGroup=$(sudo cat $A/EnableLogGroup) FirmwareLogType=$(sudo cat $A/FirmwareLogType)" ;;
  dump)
    sudo cat /sys/kernel/debug/pvr/gpu00/firmware_trace | grep -nE "Kick 3D TQ|Kick TA|Kick 3D:|TA finished|3D finished|Transfer finished|page fault detected|MMU fault|timed out|locked up|Analysis DM|USC slots|Reconstruction needed|Recovery DM3|GPU init|GPU deinit" | tail -${2:-40} | cut -c1-170 ;;
  raw) sudo cat /sys/kernel/debug/pvr/gpu00/firmware_trace ;;
  *) echo "usage: fwtrace.sh on|dump [lines]|raw"; exit 1 ;;
esac
