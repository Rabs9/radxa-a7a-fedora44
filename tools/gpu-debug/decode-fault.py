#!/usr/bin/env python3
"""decode-fault.py <MMU Status hex> <Req Status hex>: decode a PowerVR BIF fault from the firmware trace.

Register layout from hwdefs/rogue/km/rgx_cr_defs_km.h (RGX_CR_BIF_FAULT_BANK0_*), requester tags from
hwdefs/rogue/km/rgxmhdefs_km.h (the XE memory hierarchy table applies to the BXM-4-64: its feature mask
0x000002181d51bf35 has XE_MEMORY_HIERARCHY set).
  ./decode-fault.py 0x6005 0x0000748000596000
  -> FAULT, PM/META-RO violation, data-level, address 0x8000596000, requester PBE0 (write)
"""
import sys

XE_TAGS = {0x00: "MMU", 0x01: "CPU MMU", 0x02: "CPU IFU", 0x03: "CPU LSU", 0x04: "MIPS",
           0x05: "CDM stage 0", 0x06: "CDM stage 1", 0x07: "CDM stage 2", 0x08: "CDM stage 3",
           0x09: "VDM stage 0", 0x0a: "VDM stage 1", 0x0b: "VDM stage 2", 0x0c: "VDM stage 3", 0x0d: "VDM stage 4",
           0x0e: "PDS req 0", 0x0f: "PDS req 1", 0x10: "MCU USCA", 0x11: "MCU USCB", 0x12: "MCU USCC", 0x13: "MCU USCD",
           0x14: "MCU PDS USCA", 0x15: "MCU PDS USCB", 0x16: "MCU PDS USCC", 0x17: "MCU PDS USCD", 0x18: "MCU PDSRW",
           0x19: "TC 0", 0x1a: "TC 1", 0x1b: "FBCDC 0", 0x1c: "FBCDC 1", 0x1d: "FBCDC 2", 0x1e: "FBCDC 3", 0x1f: "SC",
           0x20: "ISP ZLS", 0x21: "ISP DS", 0x22: "TPF", 0x23: "TPF PBCDBIAS", 0x24: "TPF SPF", 0x25: "IPF CREQ",
           0x26: "IPF others", 0x27: "VDM stage 5", 0x28: "TA PPP", 0x29: "TA TPWRTC", 0x2a: "TA TEACRTC",
           0x2b: "TA PSGRTC", 0x2c: "TA PSGREGION", 0x2d: "TA PSGSTREAM", 0x2e: "TA TPW", 0x2f: "TA TPC",
           0x30: "PM alloc", 0x31: "PM dealloc", 0x32: "TDM DMA", 0x33: "TDM CTL",
           0x34: "PBE0", 0x35: "PBE1", 0x36: "PBE2", 0x37: "PBE3"}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    mmu = int(sys.argv[1], 16)
    req = int(sys.argv[2], 16)
    fault = mmu & 1
    pm_meta_ro = (mmu >> 2) & 1
    fault_ro = (mmu >> 4) & 1
    data_type = (mmu >> 5) & 3
    page_size = (mmu >> 8) & 7
    cat_base = (mmu >> 12) & 0xf
    addr = ((req >> 4) & ((1 << 40) - 1)) << 4
    tag_xe = (req >> 40) & 0x3f
    sb_xe = (req >> 46) & 0x3f
    rnw_xe = (req >> 52) & 1
    print(f"MMU status 0x{mmu:x}: fault={fault} PM/META-RO violation={pm_meta_ro} RO violation={fault_ro} "
          f"data_type={data_type} (0 = data access, PTE was valid) page_size_code={page_size} catalogue_base(reg set)={cat_base}")
    print(f"Req status 0x{req:x}: address 0x{addr:x}, requester tag 0x{tag_xe:x} = {XE_TAGS.get(tag_xe, '?')}, "
          f"sideband 0x{sb_xe:x}, {'read' if rnw_xe else 'WRITE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
