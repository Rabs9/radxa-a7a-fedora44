#!/bin/bash
# Cross-compile the Rabs9 A7A kernel (radxa 6.6 BSP + Rabs9 patches + overclocked DTS) on this laptop,
# with virtio enabled so the same binary boots under qemu-system-aarch64 -M virt. Follows
# mirror/docs/BUILDING.md step for step. Everything goes to output/kernel-build.log.
set -euo pipefail
# a bare non-interactive invocation (a pipe, a stray call) must not rebuild or delete anything: the CLI sets A7A_RUN=1
[ -n "${A7A_RUN:-}" ] || [ -t 0 ] || { echo "$0: refusing to run non-interactively without A7A_RUN=1 (use python3 -m src.cli)"; exit 2; }
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
W=$P/work; M=$P/mirror; OUT=$P/output; PAY=$OUT/payload
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- KBUILD_BUILD_USER="${A7A_KBUILD_USER:-$(id -un)}" KBUILD_BUILD_HOST=a7a-fedora
# every build gets its own release string (6.6.98-a7a.<stamp>): kernel, DTB dir and modules install side by side,
# a7a-extlinux/the RPM keep the previous kernel as the fallback entry (a same-release build overwrote it in place).
# Setting LOCALVERSION in the environment also stops setlocalversion appending "+". A7A_LOCALVERSION overrides.
# A7A_PVR=1 builds the OPEN GPU stack flavour: the upstream drm/imagination driver (v6.16, tools/pvr-graft.sh)
# instead of the vendor DDK's pvrsrvkm; release suffix -a7apvr.<stamp>, DT node gets "img,img-rogue" + the
# core/sys/mem clock names (docs/GPU-ROADMAP-2026-09-09.md route A, docs/OPEN-STACK-WEDGE-2026-09-09.md).
A7A_PVR=${A7A_PVR:-0}
export LOCALVERSION=${A7A_LOCALVERSION:--a7a$([ "$A7A_PVR" = 1 ] && echo pvr).$(date +%Y%m%d%H%M)}
# GPU clock the shipped DTB boots at (MHz). The mirror's board tree says 1200, which is not stable on this silicon
# (docs/GPU-TQ-FAULT-2026-09-07.md); 1008 validated 2026-09-09 (clock-ceiling.sh: 4 rounds x 16 cases, 0 resets, plus two days of desktop use), 600 proven; 1200 @ 920 mV untested.
A7A_GPU_MHZ=${A7A_GPU_MHZ:-1008}
export CCACHE_DIR=$P/work/ccache; mkdir -p "$CCACHE_DIR"
T0=$(date +%s); step() { echo "== [$(date +%H:%M:%S)] $*"; }
cd "$W"
# apply-patches.sh is not idempotent (two of its seds append a line on every run), so it runs once per checkout;
# `rm work/.patches-applied` (or a fresh clone) makes it run again
if [ -f "$W/.patches-applied" ]; then step "patches: already applied (rm work/.patches-applied to redo)"
else step "patches"; bash "$M/scripts/apply-patches.sh" allwinner-bsp-1.4.8 kernel-6.6 allwinner-device-1.4.8 && touch "$W/.patches-applied"; fi
# a7a-fedora's own BSP fixes (sunxi-iommu fault-storm protection, sunxi-drm buffer trace); idempotent like build-extras.sh
for pf in "$P"/tools/patches/*sunxi*.patch; do
  [ -f "$pf" ] || continue
  if patch -p1 -N -s --dry-run -d "$W/allwinner-bsp-1.4.8" < "$pf" >/dev/null 2>&1; then
    patch -p1 -N -s -d "$W/allwinner-bsp-1.4.8" < "$pf" && step "patch: applied $(basename "$pf")"
  else step "patch: $(basename "$pf") already applied"; fi
done
# fixes for the kernel tree itself (tools/patches/kernel/, Radxa's commits made after the manifest's pin); a patch that
# neither applies nor reverses stops the build instead of passing as "already applied". The loop runs before
# tools/pvr-graft.sh, whose un-graft does a git checkout of drivers/gpu/drm/{Kconfig,Makefile,drm_gem.c,
# scheduler/sched_main.c} and include/drm/{drm_gem.h,gpu_scheduler.h}: a patch to one of those belongs in
# tools/patches/pvr-core/, not here.
for pf in "$P"/tools/patches/kernel/*.patch; do
  [ -f "$pf" ] || continue
  if patch -p1 -N -s --dry-run -d "$W/kernel-6.6" < "$pf" >/dev/null 2>&1; then
    patch -p1 -N -s -d "$W/kernel-6.6" < "$pf"
    step "patch: applied kernel/$(basename "$pf")"
  elif patch -p1 -R -s --dry-run -d "$W/kernel-6.6" < "$pf" >/dev/null 2>&1; then step "patch: kernel/$(basename "$pf") already applied"
  else echo "patch kernel/$(basename "$pf") neither applies to work/kernel-6.6 nor is applied there"; exit 1; fi
done
# the two Radxa fixes must be in the trees whatever the loops above printed (a patch file missing from the checkout
# makes its loop a silent no-op)
grep -q 'buf--;' "$W/allwinner-bsp-1.4.8/drivers/drm/sunxi_device/hardware/lowlevel_hdmi20/dw_i2cm.c" || { echo "bsp: the HDMI DDC retry fix (tools/patches/0006) is not in dw_i2cm.c"; exit 1; }
grep -qF 'kfree_rcu(rcu_dereference_protected(inet->inet_opt, 1), rcu)' "$W/kernel-6.6/net/ipv4/af_inet.c" \
  && grep -qF 'kfree_rcu(rcu_dereference_protected(inet_rsk(req)->ireq_opt, 1), rcu)' "$W/kernel-6.6/net/ipv4/tcp_ipv4.c" \
  || { echo "kernel: the ipv4 inet_opt/ireq_opt fix (tools/patches/kernel/0001) is not in net/ipv4"; exit 1; }
step "overclocked device trees and defconfig from configs/"
DTS=kernel-6.6/arch/arm64/boot/dts/allwinner
cp "$M/configs/sun60iw2p1-gpu-overclocked.dtsi"    "$DTS/sun60iw2p1.dtsi"
cp "$M/configs/sun60iw2p1-cpu-vf-overclocked.dtsi" "$DTS/sun60iw2p1-cpu-vf.dtsi"
cp "$M/configs/board-overclocked.dts"              "$DTS/sun60i-a733-cubie-a7a.dts"
grep -q 'clk_rate = <1200000000>;' "$DTS/sun60i-a733-cubie-a7a.dts" || { echo "board dts: expected the GPU clk_rate = <1200000000> line"; exit 1; }
sed -i "s/clk_rate = <1200000000>;/clk_rate = <${A7A_GPU_MHZ}000000>;/" "$DTS/sun60i-a733-cubie-a7a.dts"
step "GPU clock in the board DTB: $A7A_GPU_MHZ MHz (A7A_GPU_MHZ), release suffix $LOCALVERSION"
# The mirror's SoC tree has the two CPU zones on each other's sensor: ths 0 is the little cluster and ths 3 the big one
# (the idle zones' ths 5/6 read the same two sensors the right way round), so each cluster's 80 degC trip watched the
# other cluster. Radxa's fix is allwinner-bsp 6b877c3c5a; measured on the board 2026-11-15: six little cores loaded
# -> ths 0 65.0 degC / ths 3 61.1, the two big cores -> 52.9 / 54.3. No-op once the mirror carries the fix.
zone_sensor() { sed -n "/$1: $1 {/,/thermal-sensors/ s/.*<&\(ths [0-9]*\)>.*/\1/p" "$DTS/sun60iw2p1.dtsi"; }
sed -i -e '/cpul_thermal_zone: cpul_thermal_zone {/,/thermal-sensors/ s/<&ths 3>/<\&ths 0>/' \
       -e '/cpub_thermal_zone: cpub_thermal_zone {/,/thermal-sensors/ s/<&ths 0>/<\&ths 3>/' "$DTS/sun60iw2p1.dtsi"
[ "$(zone_sensor cpul_thermal_zone)" = "ths 0" ] && [ "$(zone_sensor cpub_thermal_zone)" = "ths 3" ] || { echo "soc dtsi: expected cpul_thermal_zone on ths 0 and cpub_thermal_zone on ths 3"; exit 1; }
step "CPU thermal zones: little cluster on ths 0, big cluster on ths 3"
if [ "$A7A_PVR" = 1 ]; then
  bash "$P/tools/pvr-graft.sh"
  step "open stack: gpu node binds the upstream driver (img,img-rogue; core/sys/mem = CLK_GPU0/CLK_BUS_GPU0/CLK_PLL_GPU0)"
  # The DDK looks its clocks up by name (clk_parent, clk, clk_bus, clk_800..clk_200) and the upstream driver wants
  # core (+ optional sys/mem): append duplicate phandles under the upstream names, keep the DDK's list intact, list
  # both compatibles. One DTB serves either driver (powervr-backport/patch6.py, proven 2026-09-08).
  python3 - "$DTS/sun60i-a733-cubie-a7a.dts" "$A7A_GPU_MHZ" <<'EOF'
import sys
p=sys.argv[1]; mhz=sys.argv[2]; s=open(p).read()
if "img,img-rogue" in s: print("  = board dts already carries img,img-rogue"); sys.exit(0)
old="&gpu  {\n\tgpu-supply = <&reg_dcdc4>;\n"
assert s.count(old)==1, "gpu override block not found"
new=old+"""\tcompatible = "img,gpu", "img,img-rogue";
\tclocks = <&ccu CLK_PLL_GPU0>, <&ccu CLK_GPU0>, <&ccu CLK_BUS_GPU0>,
\t\t <&ccu CLK_PLL_PERI0_800M>, <&ccu CLK_PLL_PERI0_600M>, <&ccu CLK_PLL_PERI0_400M>,
\t\t <&ccu CLK_PLL_PERI0_300M>, <&ccu CLK_PLL_PERI0_200M>,
\t\t <&ccu CLK_GPU0>, <&ccu CLK_BUS_GPU0>, <&ccu CLK_PLL_GPU0>;
\tclock-names = "clk_parent", "clk", "clk_bus", "clk_800", "clk_600", "clk_400", "clk_300", "clk_200",
\t\t      "core", "sys", "mem";
\t/* the upstream driver never sets a rate (the DDK uses clk_rate): pin the validated clock at probe. Above
\t * 800 MHz the DDK programs the parent PLL (gpu0 = PLL_GPU0 / 1), so pin the PLL and the divider clock */
\tassigned-clocks = <&ccu CLK_PLL_GPU0>, <&ccu CLK_GPU0>;
\tassigned-clock-rates = <MHZ000000>, <MHZ000000>;
"""
new=new.replace("MHZ", mhz)
open(p,'w').write(s.replace(old,new,1)); print("  + gpu node: img,img-rogue + core/sys/mem")
EOF
fi
cp "$M/configs/cubie_a7a_defconfig" kernel-6.6/arch/arm64/configs/cubie_a7a_defconfig
cd kernel-6.6
ln -sfn ../allwinner-bsp-1.4.8 bsp
step "defconfig"; make BSP_TOP=bsp/ cubie_a7a_defconfig
step "virtio for the emulator"
scripts/config --enable PCI --enable PCI_HOST_GENERIC --enable PCI_MSI \
  --enable VIRTIO --enable VIRTIO_MENU --enable VIRTIO_PCI --enable VIRTIO_MMIO --enable VIRTIO_BLK --enable VIRTIO_NET \
  --enable VIRTIO_CONSOLE --enable HW_RANDOM_VIRTIO --enable VIRTIO_INPUT --enable VIRTIO_BALLOON \
  --enable SERIAL_AMBA_PL011 --enable SERIAL_AMBA_PL011_CONSOLE --enable RTC_DRV_PL031 --enable ARM_AMBA \
  --module DRM_VIRTIO_GPU --enable NET_9P --enable NET_9P_VIRTIO --enable 9P_FS --enable 9P_FS_POSIX_ACL \
  --enable EFI --enable EFI_STUB --enable DMI
step "netfilter for Fedora: firewalld uses the nftables backend and the BSP defconfig has nf_tables off (firewalld failed, 75 s boot delay)"
scripts/config --module NF_TABLES --enable NF_TABLES_INET --enable NF_TABLES_NETDEV --enable NF_TABLES_IPV4 --enable NF_TABLES_IPV6 --enable NF_TABLES_ARP \
  --module NF_TABLES_BRIDGE --module NFT_BRIDGE_META --module NFT_BRIDGE_REJECT \
  --module NFT_CT --module NFT_LOG --module NFT_LIMIT --module NFT_MASQ --module NFT_REDIR --module NFT_NAT --module NFT_REJECT --module NFT_REJECT_INET \
  --module NFT_REJECT_IPV4 --module NFT_REJECT_IPV6 --module NFT_REJECT_NETDEV --module NFT_COMPAT --module NFT_HASH --module NFT_FIB --module NFT_FIB_INET \
  --module NFT_FIB_IPV4 --module NFT_FIB_IPV6 --module NFT_FIB_NETDEV --module NFT_QUOTA --module NFT_NUMGEN --module NFT_QUEUE --module NFT_SOCKET \
  --module NFT_TPROXY --module NFT_SYNPROXY --module NFT_XFRM --module NFT_TUNNEL --module NFT_CONNLIMIT --module NFT_DUP_IPV4 --module NFT_DUP_IPV6 \
  --module NFT_DUP_NETDEV --module NFT_FWD_NETDEV --module NFT_FLOW_OFFLOAD --module NFT_OSF --module NF_CONNTRACK_FTP --module NF_CONNTRACK_TFTP
step "security xattrs for Fedora: file capabilities (setcap) and SELinux labels live in security.* xattrs; the BSP config has EXT4_FS_SECURITY off, so every rpm carrying file capabilities (shadow-utils, gstreamer, ...) failed to install: cap_set_file: Operation not supported (2026-09-07 dnf update aborted with a mixed Plasma)"
scripts/config --enable EXT4_FS_SECURITY --enable SECURITY_NETWORK --enable F2FS_FS_SECURITY
step "sticks, cards and Bluetooth buttons: the BSP config has no exFAT (most sticks of 64 GB and up, SDXC cards), no ISO 9660 (installer sticks, .iso files) and no uinput (bluetoothd: \"failed to create uinput\" for a speaker's or headset's buttons); on the board 2026-11-18. Kernels built before this get the three from tools/build-dsu-modules.sh"
scripts/config --module EXFAT_FS --set-str EXFAT_DEFAULT_IOCHARSET utf8 --module ISO9660_FS --enable JOLIET --module INPUT_UINPUT
if [ "$A7A_PVR" = 1 ]; then
  step "open stack: CONFIG_DRM_POWERVR=m (selects DRM_GPUVM/DRM_EXEC/DRM_SCHED/DRM_GEM_SHMEM_HELPER); pvrsrvkm is not built for this flavour"
  scripts/config --module DRM_POWERVR --module DRM_GPUVM --module DRM_EXEC --module DRM_SCHED --module DRM_GEM_SHMEM_HELPER
fi
# nouveau (a module in the BSP config, useless on this SoC) is the only user of 6.6's drm_gpuva_mgr, which the v6.16
# drm_gpuvm graft replaces in drm.ko (tools/pvr-graft.sh); it fails modpost once the tree is grafted, in both flavours
scripts/config --disable DRM_NOUVEAU
# the committed defconfig leaves CONFIG_EXTRA_FIRMWARE empty, so a verbatim build ships no regulatory db (5 GHz WiFi dead)
scripts/config --set-str EXTRA_FIRMWARE "regulatory.db regulatory.db.p7s" --set-str EXTRA_FIRMWARE_DIR "/lib/firmware"
make BSP_TOP=bsp/ olddefconfig
grep -E "^CONFIG_(VIRTIO_PCI|VIRTIO_BLK|VIRTIO_NET|SERIAL_AMBA_PL011|PCI_HOST_GENERIC|DRM_VIRTIO_GPU|EFI_STUB|NF_TABLES|NFT_CT|NFT_MASQ|NFT_FIB_INET|EXT4_FS_SECURITY|SECURITY_NETWORK)=" .config
grep -q "^CONFIG_EXT4_FS_SECURITY=y" .config || { echo "EXT4_FS_SECURITY did not stick"; exit 1; }
for o in EXFAT_FS=m ISO9660_FS=m JOLIET=y INPUT_UINPUT=m; do grep -q "^CONFIG_$o$" .config || { echo "CONFIG_$o did not stick"; exit 1; }; done
[ "$A7A_PVR" != 1 ] || grep -q "^CONFIG_DRM_POWERVR=m" .config || { echo "DRM_POWERVR did not stick (Kconfig graft missing?)"; exit 1; }
step "compile (make -j16, ccache)"
make BSP_TOP=bsp/ CC="ccache aarch64-linux-gnu-gcc" -j16 Image modules
# the board tree is not on the allwinner dtb list: build it as an explicit target, with the BSP's
# dt-bindings on the include path (the stock path lacks dt-bindings/clock/sun60iw2-ccu.h)
make BSP_TOP=bsp/ DTC_INCLUDE="$(pwd)/scripts/dtc/include-prefixes $(pwd)/bsp/include" allwinner/sun60i-a733-cubie-a7a.dtb
REL=$(make -s BSP_TOP=bsp/ kernelrelease)
step "payload for release $REL"
rm -rf "$PAY"; mkdir -p "$PAY/boot/dtb/allwinner"
cp arch/arm64/boot/Image "$PAY/boot/Image-$REL"
cp arch/arm64/boot/dts/allwinner/sun60i-a733-cubie-a7a.dtb "$PAY/boot/dtb/allwinner/"
make BSP_TOP=bsp/ INSTALL_MOD_PATH="$PAY" INSTALL_MOD_STRIP=1 modules_install
mkdir -p "$PAY/lib/firmware/aic8800D80"
cp "$W/allwinner-target/debian/cubie_a7a/overlay/lib/firmware/aic8800D80/"* "$PAY/lib/firmware/aic8800D80/" 2>/dev/null || echo "no aic8800D80 firmware found in allwinner-target"
echo "$REL" > "$PAY/kernelrelease"
if [ "$A7A_PVR" = 1 ]; then
  # the open driver's firmware (linux-firmware, same BVNC as the DDK's rgx.fw.36.56.104.183 but the upstream ABI)
  FW=rogue_36.56.104.183_v1.fw; mkdir -p "$PAY/lib/firmware/powervr" "$W/pvr-extra"
  [ -s "$W/pvr-extra/$FW" ] || curl -sfL --max-time 180 -o "$W/pvr-extra/$FW" "https://gitlab.com/kernel-firmware/linux-firmware/-/raw/main/powervr/$FW" || true
  [ -s "$W/pvr-extra/$FW" ] && cp "$W/pvr-extra/$FW" "$PAY/lib/firmware/powervr/" && step "firmware: $FW ($(du -h "$W/pvr-extra/$FW" | cut -f1)) in the payload" || step "firmware $FW NOT fetched: the board keeps its own copy in /lib/firmware/powervr"
fi
step "check: tx-delay and OPP in the board DTB"
dtc -I dtb -O dts "$PAY/boot/dtb/allwinner/sun60i-a733-cubie-a7a.dtb" 2>/dev/null | grep -E 'tx-delay|opp-hz|clk_rate' | tail -4
step "check: regulatory db embedded"; grep -qaP '\x52\x47\x44\x42' arch/arm64/boot/Image && echo "RGDB present" || echo "RGDB MISSING"
step "done in $(( ($(date +%s)-T0)/60 )) min: $(du -sh "$PAY" | cut -f1) payload, $(find "$PAY/lib/modules" -name '*.ko*' | wc -l) modules"
