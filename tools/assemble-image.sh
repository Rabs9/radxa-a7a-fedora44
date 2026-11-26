#!/bin/bash
# Assemble the A7A card image: a Radxa card's boot firmware region + the card layout U-Boot expects
# (from mirror/scripts/flash-image.sh and docs/HARDWARE.md) + Fedora 44 KDE's root filesystem
# copied onto ext4 + the a7a-fedora packages (both kernels, the a7a Mesa PowerVR driver, the board tools) installed
# with dnf inside the image + extlinux. Privileged steps go through tools/priv.sh.
#   tools/assemble-image.sh [size_gb]     -> output/a7a-fedora-44-kde-<date>.img (+ .sha256, .kernel, .rpms)
# Packages (the newest of each in output/rpms, docs/PACKAGING-2026-09-09.md): a7a-extlinux, a7a-board-tools,
# a7a-gpu-tools, mesa-pvr-a7a, mesa-img-a7a (the DDK's Mesa with Wayland), kernel-a7a (vendor DDK flavour), kernel-a7apvr
# (open GPU stack). A7A_IMAGE_DEFAULT=open|ddk (default open) says which of the two kernels boots by default; the
# other one is entry 2. With ddk the image also carries /etc/a7a/pvr-wl.enabled.
set -euo pipefail
# a bare non-interactive invocation (a pipe, a stray call) must not rebuild or delete anything: the CLI sets A7A_RUN=1
[ -n "${A7A_RUN:-}" ] || [ -t 0 ] || { echo "$0: refusing to run non-interactively without A7A_RUN=1 (use python3 -m src.cli)"; exit 2; }
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output; PRIV="sudo -n $P/tools/priv.sh"
SIZE_GB=${1:-12}
FEDORA_RAW=$OUT/Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw
IMG=$OUT/a7a-fedora-44-kde-$(date +%Y%m%d).img
step() { echo "== [$(date +%H:%M:%S)] $*"; }
[ -f "$FEDORA_RAW" ] || { echo "extract the Fedora image first"; exit 1; }
[ -f "$OUT/sd-firmware-region.img" ] || { echo "sd-firmware-region.img missing"; exit 1; }
rc=0; sudo -n "$P/tools/priv.sh" >/dev/null 2>&1 || rc=$?; [ "$rc" -eq 2 ] || { echo "sudo without a password is needed for tools/priv.sh (see the sudoers line in its header)"; exit 1; }
# the newest package of each kind = the newest BUILD: every package's Release is a fixed-width YYYYMMDDHHMM stamp
# (the Version is not fixed width: 6.6.98 < 6.6.100, 26.1.8 < 26.1.10 would sort wrong as text); same rule as
# src/stages/image.py image_rpms()
newest() { ls "$OUT"/rpms/$1 2>/dev/null | sed -E 's/^(.*-([0-9]{12})\.[a-z0-9_]+\.rpm)$/\2 \1/' | LC_ALL=C sort | tail -1 | cut -d' ' -f2- || true; }
R_EXT=$(newest 'a7a-extlinux-*.noarch.rpm'); R_BOARD=$(newest 'a7a-board-tools-*.noarch.rpm'); R_TOOLS=$(newest 'a7a-gpu-tools-*.noarch.rpm')
R_MESA=$(newest 'mesa-pvr-a7a-*.aarch64.rpm'); R_DDK=$(newest 'kernel-a7a-*.aarch64.rpm'); R_PVR=$(newest 'kernel-a7apvr-*.aarch64.rpm')
R_IMG=$(newest 'mesa-img-a7a-*.aarch64.rpm')
for v in R_EXT R_BOARD R_TOOLS R_MESA R_IMG R_DDK R_PVR; do [ -n "${!v}" ] || { echo "no $v package in $OUT/rpms (tools/build-tools-rpm.sh, build-mesa-rpm.sh, build-mesa-img-rpm.sh, build-rpm.sh for both flavours)"; exit 1; }; done
relof() { rpm -qp --provides "$1" 2>/dev/null | sed -n 's/^kernel-uname-r = //p' | head -1; }
REL=$(relof "$R_PVR"); REL_DDK=$(relof "$R_DDK")
[ -n "$REL" ] && [ -n "$REL_DDK" ] || { echo "the kernel packages carry no kernel-uname-r provide (old build-rpm.sh?): $R_PVR $R_DDK"; exit 1; }
echo "packages: $(basename "$R_EXT") $(basename "$R_BOARD") $(basename "$R_TOOLS") $(basename "$R_MESA") $(basename "$R_IMG") $(basename "$R_DDK") $(basename "$R_PVR")"
# A7A_IMAGE_PREVIOUS_KERNELS: earlier kernels ride along as boot entries 3 (vendor) and 4 (open). For the first boot of
# newly built kernels on a board: an image whose two kernels have both never run on hardware has no way back, and the
# owner has one card (2026-11-15). Not for an image that is published.
#   1                      the newest build of ANOTHER release of each flavour in output/rpms: "previous", which is the
#                          proven pair only as long as nothing was built in between (the line below names them)
#   <vendor rel>,<open rel>  exactly these releases (the ones the board is known to run)
previous() { ls "$OUT"/rpms/$1 2>/dev/null | sed -E 's/^(.*-([0-9]{12})\.[a-z0-9_]+\.rpm)$/\2 \1/' | LC_ALL=C sort | cut -d' ' -f2- | while read -r f; do [ "$(relof "$f")" = "$2" ] || echo "$f"; done | tail -1; }
ofrelease() { ls "$OUT"/rpms/$1 2>/dev/null | sed -E 's/^(.*-([0-9]{12})\.[a-z0-9_]+\.rpm)$/\2 \1/' | LC_ALL=C sort | cut -d' ' -f2- | while read -r f; do [ "$(relof "$f")" != "$2" ] || echo "$f"; done | tail -1; }
R_DDK_OLD=""; R_PVR_OLD=""; REL_DDK_OLD=""; REL_OLD=""; PREV=()
case "${A7A_IMAGE_PREVIOUS_KERNELS:-0}" in
  0) ;;
  1) R_DDK_OLD=$(previous 'kernel-a7a-*.aarch64.rpm' "$REL_DDK"); R_PVR_OLD=$(previous 'kernel-a7apvr-*.aarch64.rpm' "$REL")
     [ -n "$R_DDK_OLD" ] && [ -n "$R_PVR_OLD" ] || { echo "A7A_IMAGE_PREVIOUS_KERNELS=1, but $OUT/rpms holds no earlier release of both kernel flavours"; exit 1; } ;;
  *,*) want_ddk=${A7A_IMAGE_PREVIOUS_KERNELS%%,*}; want_pvr=${A7A_IMAGE_PREVIOUS_KERNELS#*,}
     [ "$want_ddk" != "$REL_DDK" ] && [ "$want_pvr" != "$REL" ] || { echo "A7A_IMAGE_PREVIOUS_KERNELS names a kernel this image has anyway ($REL_DDK, $REL)"; exit 1; }
     R_DDK_OLD=$(ofrelease 'kernel-a7a-*.aarch64.rpm' "$want_ddk"); R_PVR_OLD=$(ofrelease 'kernel-a7apvr-*.aarch64.rpm' "$want_pvr")
     [ -n "$R_DDK_OLD" ] && [ -n "$R_PVR_OLD" ] || { echo "A7A_IMAGE_PREVIOUS_KERNELS=$A7A_IMAGE_PREVIOUS_KERNELS: $OUT/rpms has no kernel-a7a of release '$want_ddk' and kernel-a7apvr of release '$want_pvr' (vendor release first, then the open one)"; exit 1; } ;;
  *) echo "A7A_IMAGE_PREVIOUS_KERNELS must be 0, 1 or <vendor release>,<open release>, not '$A7A_IMAGE_PREVIOUS_KERNELS'"; exit 1 ;;
esac
if [ -n "$R_DDK_OLD" ]; then
  REL_DDK_OLD=$(relof "$R_DDK_OLD"); REL_OLD=$(relof "$R_PVR_OLD"); PREV=("$R_DDK_OLD" "$R_PVR_OLD")
  [ -n "$REL_DDK_OLD" ] && [ -n "$REL_OLD" ] || { echo "the previous kernel packages carry no kernel-uname-r provide: $R_DDK_OLD $R_PVR_OLD"; exit 1; }
  echo "previous kernels as entries 3 and 4: $REL_DDK_OLD ($(basename "$R_DDK_OLD")), $REL_OLD ($(basename "$R_PVR_OLD"))"
fi
# which kernel boots by default: open (drm/imagination + the a7a Mesa driver; every image until 2026-11-15) or ddk
# (the vendor driver, with mesa-img-a7a switched on for the whole desktop: docs/RADXA-SYNC-2026-11-15.md, section 5)
DEFAULT=${A7A_IMAGE_DEFAULT:-open}
case "$DEFAULT" in
  open) REL_DEF=$REL; R_DEF=$R_PVR; REL_FB=$REL_DDK ;;
  ddk) REL_DEF=$REL_DDK; R_DEF=$R_DDK; REL_FB=$REL
       [ -d "$OUT/ddk/lib" ] || { echo "a ddk-default image needs the DDK userspace in output/ddk (tools/stage-extras.sh)"; exit 1; } ;;
  *) echo "A7A_IMAGE_DEFAULT must be open or ddk, not '$DEFAULT'"; exit 1 ;;
esac
echo "kernels: default $REL_DEF ($DEFAULT stack), fallback $REL_FB"
# exFAT, ISO 9660 and uinput: a set for each kernel of the image, built for exactly that release (an empty set: the
# kernel has all three itself); the step that installs them says why. Looked at here, before the image is made.
KERNELS="$REL_DDK $REL ${REL_DDK_OLD:-} ${REL_OLD:-}"
for k in $KERNELS; do
  [ -d "$OUT/extra-modules/$k" ] || { echo "no $OUT/extra-modules/$k (exFAT, ISO 9660, uinput): tools/build-dsu-modules.sh $k"; exit 1; }
  for f in "$OUT/extra-modules/$k"/*.ko; do
    [ -e "$f" ] || continue
    vm=$(modinfo -F vermagic "$f" | awk '{print $1}')
    [ "$vm" = "$k" ] || { echo "$f is built for '$vm', not for $k: it would not load"; exit 1; }
  done
done

step "sparse image ${SIZE_GB} GB, the vendor firmware region, then the card layout"
rm -f "$IMG"; truncate -s "${SIZE_GB}G" "$IMG"
# the firmware region (16 MB from a Radxa card) carries that card's own GPT in sectors 0-33: write it FIRST, then our
# table over it (boot0/U-Boot live beyond sector 33, the Radxa image itself keeps a GPT there). --wipe never: touch
# nothing but the table. sectors from flash-image.sh: p1 config 32768-65535, p2 boot 65536-679935 (EF00), p3 rootfs
# 679936-end. HARDWARE.md: p3 must be EFI-typed too, or U-Boot never scans it for extlinux.conf.
$PRIV dd-firmware "$IMG"
sfdisk -q --wipe never --wipe-partitions never "$IMG" <<EOF
label: gpt
first-lba: 32768
start=32768,  size=32768,  type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="config"
start=65536,  size=614400, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="boot"
start=679936, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="rootfs"
EOF
sfdisk -l "$IMG" | grep -E "^/|mismatch|corrupt" ; sfdisk --verify "$IMG" 2>&1 | tail -1
for n in 2 3; do t=$(sfdisk --part-type "$IMG" $n | tr a-f A-F); [ "$t" = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" ] || { echo "p$n type is $t, not EFI System: U-Boot would ignore it (HARDWARE.md)"; exit 1; }; done
echo "p2 and p3 are EFI System: U-Boot will scan them for extlinux.conf"

step "loop devices"
LOOP=$($PRIV loop-attach "$IMG"); echo "image on $LOOP"
# Fedora's image is only read: a read-only loop device over its partition 3 alone (the file is opened read-only, which
# no mount can get around), detached as soon as the copy is done. As a writable loop device that stayed attached for
# the whole build it was mounted and changed by a package script inside the image on 2026-11-16 (docs/RADXA-SYNC-2026-11-15.md 14)
FSTART=$(sfdisk -d "$FEDORA_RAW" 2>/dev/null | awk '$1 ~ /3$/ {for (i = 1; i < NF; i++) if ($i == "start=") {gsub(",", "", $(i + 1)); print $(i + 1)}}')
[[ "$FSTART" =~ ^[0-9]+$ ]] || { echo "cannot find partition 3 of $FEDORA_RAW"; exit 1; }
FLOOP=$($PRIV loop-attach "$FEDORA_RAW" $((FSTART * 512))); echo "fedora (partition 3, read-only) on $FLOOP"
cleanup() { set +e; for m in root froot fhome fvar; do $PRIV umount $m 2>/dev/null; done; $PRIV loop-detach "$LOOP"; [ -z "$FLOOP" ] || $PRIV loop-detach "$FLOOP"; }
trap cleanup EXIT
sleep 1; ls ${LOOP}p1 ${LOOP}p2 ${LOOP}p3 >/dev/null
[ "$(cat "/sys/block/${FLOOP##*/}/ro")" = 1 ] || { echo "$FLOOP (Fedora's image) is not read-only"; exit 1; }

step "filesystems"
$PRIV mkfs ext4 config "${LOOP}p1"; $PRIV mkfs vfat BOOT "${LOOP}p2"; $PRIV mkfs ext4 rootfs "${LOOP}p3"

step "copy Fedora's root subvolume onto ext4 (this is the slow part)"
ROOT=$($PRIV mount "${LOOP}p3" root)
$PRIV mount "$FLOOP" froot "ro,subvol=root" >/dev/null
$PRIV copy-root froot root
$PRIV mount "$FLOOP" fhome "ro,subvol=home" >/dev/null && $PRIV copy-root fhome root/home 2>/dev/null || true
# Fedora keeps /var in a subvolume of its own (its image has three: root, home, var), and the root subvolume has an
# empty /var. Until 2026-11-15 that one was not copied and every image started with an empty /var: tmpfiles rebuilt
# the skeleton at boot, but nothing rebuilt what only packages create: /var/lib/logrotate, /var/lib/plocate and
# /var/lib/gssproxy (the three units that had failed on the board and in the emulator since September; the working
# card's /var/log/messages was never rotated, 822 MB), /var/lib/rpm-state, /var/lib/alternatives, /var/lib/selinux
# (what package scriptlets of later updates write to). An empty /var here must be filled
if [ -z "$(ls -A "$ROOT/var" 2>/dev/null)" ]; then
  $PRIV mount "$FLOOP" fvar "ro,subvol=var" >/dev/null; $PRIV copy-root fvar root/var; $PRIV umount fvar
  [ -d "$ROOT/var/lib/rpm-state" ] || { echo "Fedora's /var subvolume was copied, but /var/lib/rpm-state is not in the image"; exit 1; }
  echo "Fedora's /var subvolume copied: $(ls "$ROOT/var/lib" | wc -l) entries in /var/lib"
else echo "Fedora's root subvolume brought its own /var ($(ls -A "$ROOT/var" | wc -l) entries): no var subvolume copied"; fi
$PRIV umount fhome 2>/dev/null || true; $PRIV umount froot
$PRIV loop-detach "$FLOOP"; FLOOP=""

step "boot configuration: extlinux (root by device path, no initrd, as on the Debian card)"
# timeout is in tenths of a second in Allwinner's U-Boot (Radxa's Debian cards use 50): a 5 s window in which
# the serial stage can type 2 for the fallback entry; prompt 0 = boot the default unless a key arrives.
# Only the vendor-DDK kernel's entry and the recovery entry are written here, as the template the kernel packages
# clone (append line = the image's boot policy); kernel-a7apvr's %post then adds the open-stack entry as the default
# and moves this one to entry 2, and re-points recovery at the default kernel.
$PRIV write-file root boot/extlinux/extlinux.conf <<EOF
timeout 50
prompt 0
default fedora-$REL_DDK
menu title a7a-fedora

label fedora-$REL_DDK
    menu label Fedora 44 KDE, Linux $REL_DDK (overclocked DTB)
    linux /boot/vmlinuz-$REL_DDK
    fdtdir /usr/lib/linux-image-$REL_DDK/
    append root=/dev/mmcblk0p3 rootwait rootfstype=ext4 console=ttyAS0,115200 loglevel=4 cma=128M selinux=0 systemd.unified_cgroup_hierarchy=1

label fedora-recovery
    menu label Fedora 44 KDE, Linux $REL_DDK, single user, no desktop
    linux /boot/vmlinuz-$REL_DDK
    fdtdir /usr/lib/linux-image-$REL_DDK/
    append root=/dev/mmcblk0p3 rootwait rootfstype=ext4 console=ttyAS0,115200 loglevel=7 cma=128M selinux=0 systemd.unit=multi-user.target
EOF
$PRIV write-file root etc/fstab <<EOF
/dev/mmcblk0p3  /      ext4  defaults,noatime  0 1
/dev/mmcblk0p2  /boot/efi  vfat  defaults,noauto  0 0
EOF
$PRIV write-file root etc/hostname <<< "radxa-cubie-a7a"
$PRIV write-file root etc/modules-load.d/a7a.conf <<EOF
# WiFi (AIC8800D80): firmware loader first, then the driver
aic_load_fw
aic8800_fdrv
EOF

step "board policy inside the image: no suspend ever, serial console, first-boot checklist"
# systemd-repart is Fedora's own root-growing service for Fedora's layout (on this layout it once partitioned the vendor
# boot firmware area): masked. The root partition is grown by the flash stage or, at the first boot, by a7a-grow-rootfs
$PRIV chroot-run root "systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target systemd-repart.service >/dev/null 2>&1; systemctl enable sshd serial-getty@ttyAS0.service >/dev/null 2>&1; echo ok"
$PRIV write-file root usr/local/sbin/a7a-firstboot <<'EOF'
#!/bin/bash
# a7a-fedora first-boot checklist: written to /var/log/a7a-firstboot.log and the serial console
{
echo "a7a-firstboot: $(date -Is) kernel $(uname -r) on $(cat /proc/device-tree/model 2>/dev/null)"
echo "cpu max: $(cat /sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq 2>/dev/null) / $(cat /sys/devices/system/cpu/cpufreq/policy6/cpuinfo_max_freq 2>/dev/null)"
echo "root: $(findmnt -n -o SOURCE,FSTYPE /)"; echo "net: $(ip -br link | tr '\n' ';')"
echo "gpu node: $(ls /dev/dri 2>/dev/null | tr '\n' ' ')  npu: $(ls /dev/vipcore 2>/dev/null)"
echo "modules: $(lsmod | wc -l)  failed units: $(systemctl --failed --no-legend | wc -l)"
} | tee -a /var/log/a7a-firstboot.log > /dev/console 2>/dev/null || true
EOF
$PRIV write-file root etc/systemd/system/a7a-firstboot.service <<EOF
[Unit]
Description=a7a-fedora first-boot checklist
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/bash /usr/local/sbin/a7a-firstboot
[Install]
WantedBy=multi-user.target
EOF
$PRIV chroot-run root "chmod +x /usr/local/sbin/a7a-firstboot; systemctl enable a7a-firstboot.service >/dev/null 2>&1; echo ok"
# the root partition and its ext4 grow to the end of the card at the first boot (tools/board/a7a-grow-rootfs), so the
# image fills the card whatever wrote it; a card written by our flash stage is already grown and only gets the marker
$PRIV write-file root usr/local/sbin/a7a-grow-rootfs < "$P/tools/board/a7a-grow-rootfs"
$PRIV write-file root etc/systemd/system/a7a-grow-rootfs.service <<EOF
[Unit]
Description=a7a-fedora: grow the root filesystem to the end of the card (first boot)
ConditionPathExists=!/var/lib/a7a/rootfs-grown
After=systemd-remount-fs.service local-fs.target
Before=a7a-firstboot.service
[Service]
Type=oneshot
ExecStart=/bin/bash /usr/local/sbin/a7a-grow-rootfs
[Install]
WantedBy=multi-user.target
EOF
# systemd applies the unit presets at the first boot (the image's machine-id is empty). Fedora's policy disables sshd,
# the serial console and our two first-boot units and enables KDE's first-run wizard; so far that only failed to
# take effect because / is still read-only at that moment ("Failed to preset all unit ...: Read-only file system").
# With this file the presets ask for what the image has (reviewer, 2026-11-15).
$PRIV write-file root etc/systemd/system-preset/00-a7a.preset <<EOF
# a7a-fedora: what the image enables and disables itself (tools/assemble-image.sh)
enable sshd.service
enable serial-getty@ttyAS0.service
enable a7a-firstboot.service
enable a7a-grow-rootfs.service
disable plasma-setup.service
EOF
$PRIV chroot-run root "chmod +x /usr/local/sbin/a7a-grow-rootfs; systemctl enable a7a-grow-rootfs.service >/dev/null 2>&1; test -x /usr/local/sbin/a7a-grow-rootfs && test -e /etc/systemd/system/multi-user.target.wants/a7a-grow-rootfs.service && echo ok" || { echo "a7a-grow-rootfs is not installed and enabled in the image"; exit 1; }

step "dnf: Fedora's own kernels are useless on this board (extlinux boots ours) and would only cost space"
$PRIV chroot-run root "grep -q '^excludepkgs' /etc/dnf/dnf.conf || echo 'excludepkgs=kernel,kernel-core,kernel-modules*,kernel-devel*,kernel-tools*,kernel-uki-virt*,kernel-debug*' >> /etc/dnf/dnf.conf; echo ok"

# grub2-mkconfig (run by grub2's package scripts at an update) calls os-prober, which mounts every disk it can see to
# look for other systems. This board does not boot through GRUB, and nobody's disks are mounted by a package update:
# neither the build machine's (tools/priv.sh chroot-run hides them besides) nor, later, the ones plugged into the board
$PRIV chroot-run root "mkdir -p /etc/default; touch /etc/default/grub; sed -i '/^GRUB_DISABLE_OS_PROBER=/d' /etc/default/grub; echo 'GRUB_DISABLE_OS_PROBER=true' >> /etc/default/grub; grep -c '^GRUB_DISABLE_OS_PROBER=true' /etc/default/grub" | grep -qx 1 || { echo "GRUB_DISABLE_OS_PROBER=true is not in the image's /etc/default/grub"; exit 1; }

UPDATED=""
if [ "${A7A_IMAGE_UPDATE:-0}" = 1 ]; then
  # Fedora's raw image is the release as it was in April (kwin 6.6, Qt 6.10, Mesa 26.0). The board has only ever run
  # the GPU desktop with the updates of 2026-09-07 on top (kwin 6.7, Qt 6.11, Mesa 26.1), and a freshly flashed image
  # met its first `dnf upgrade` with a thousand packages. With this the image is the release plus its updates as of
  # the build day, and says so (.build, .packages). Online, and slow under emulation; before our own packages, as it
  # was on the board. The log stays readable from outside while it runs: output/mnt/root/var/tmp/a7a-dnf-upgrade.log.
  # dnf's cache is output/dnf-cache, not the image: 2 GiB of packages inside a 12 GB image left the transaction 600 MB
  # short (2026-11-16), and the next build finds them there instead of downloading them again (keepcache)
  step "Fedora's updates: dnf upgrade inside the image (A7A_IMAGE_UPDATE; online)"
  mkdir -p "$OUT/dnf-cache"
  $PRIV chroot-run root --cache "$OUT/dnf-cache" "df -h / | tail -1; dnf upgrade -y --refresh --setopt=keepcache=1 > /var/tmp/a7a-dnf-upgrade.log 2>&1 || { tail -40 /var/tmp/a7a-dnf-upgrade.log; exit 1; }; grep -aE '^(Transaction Summary|Complete|Nothing to do)| packages?\$' /var/tmp/a7a-dnf-upgrade.log | tail -8; df -h / | tail -1"
  $PRIV chroot-run root "rpm -q kwin qt6-qtbase mesa-dri-drivers systemd"
  # the whole log beside the image (the tidy step removes it from the image): which scripts ran and what they said
  cp "$ROOT/var/tmp/a7a-dnf-upgrade.log" "$IMG.upgrade.log"; echo "the upgrade's log: $IMG.upgrade.log ($(wc -l < "$IMG.upgrade.log") lines)"
  UPDATED=$(date +%F)
fi

step "a7a-fedora packages: a7a-extlinux, a7a-board-tools, a7a-gpu-tools, mesa-pvr-a7a, mesa-img-a7a, kernel-a7a $REL_DDK (fallback entry)"
rm -rf "$OUT/image-rpms"; mkdir -p "$OUT/image-rpms"; cp "$R_EXT" "$R_BOARD" "$R_TOOLS" "$R_MESA" "$R_IMG" "$R_DDK" "$R_PVR" "${PREV[@]}" "$OUT/image-rpms/"
$PRIV copy-dir "$OUT/image-rpms" root var/tmp/a7a-rpms
# repos off: the image is offline here, every dependency (python3, kmod, sudo, dtc, NetworkManager, vulkan-loader) is in
# Fedora KDE already; a7a-board-tools' Recommends (dtc, alsa-utils, tuned) are weak
$PRIV chroot-run root "dnf install -y --disablerepo='*' /var/tmp/a7a-rpms/$(basename "$R_EXT") /var/tmp/a7a-rpms/$(basename "$R_BOARD") /var/tmp/a7a-rpms/$(basename "$R_TOOLS") /var/tmp/a7a-rpms/$(basename "$R_MESA") /var/tmp/a7a-rpms/$(basename "$R_IMG") /var/tmp/a7a-rpms/$(basename "$R_DDK") > /var/tmp/a7a-dnf-1.log 2>&1 || { tail -25 /var/tmp/a7a-dnf-1.log; exit 1; }; grep -E '^(Installing|Upgrading|Error|error| [a-z0-9-]+ +(noarch|aarch64))' /var/tmp/a7a-dnf-1.log | head -14"
if [ -n "$R_DDK_OLD" ]; then
  # before the open-stack kernel of this image: every entry added without --default lands at position 2, so the last
  # one installed (below) ends in front of these two, and the vendor one in front of the open one
  step "previous kernels $REL_OLD and $REL_DDK_OLD as extra entries (A7A_IMAGE_PREVIOUS_KERNELS)"
  n=0; for r in "$R_PVR_OLD" "$R_DDK_OLD"; do n=$((n + 1))
    $PRIV chroot-run root "dnf install -y --disablerepo='*' /var/tmp/a7a-rpms/$(basename "$r") > /var/tmp/a7a-dnf-prev$n.log 2>&1 || { tail -25 /var/tmp/a7a-dnf-prev$n.log; exit 1; }; grep -E '^(Installing|Upgrading|Error|error| [a-z0-9-]+ +(noarch|aarch64))' /var/tmp/a7a-dnf-prev$n.log | head -6"
  done
fi
# kernel-a7apvr's %post makes its entry the default only with A7A_DEFAULT=1; without it the entry lands at position
# 2 behind the template's (the DDK kernel), which is the ddk-default layout
if [ "$DEFAULT" = open ]; then PVR_ENV="A7A_DEFAULT=1 "; step "kernel-a7apvr $REL (open GPU stack) as the default entry"
else PVR_ENV=""; step "kernel-a7apvr $REL (open GPU stack) as the fallback entry"; fi
$PRIV chroot-run root "${PVR_ENV}dnf install -y --disablerepo='*' /var/tmp/a7a-rpms/$(basename "$R_PVR") > /var/tmp/a7a-dnf-2.log 2>&1 || { tail -25 /var/tmp/a7a-dnf-2.log; exit 1; }; grep -E '^(Installing|Upgrading|Error|error| [a-z0-9-]+ +(noarch|aarch64))' /var/tmp/a7a-dnf-2.log | head -6"
$PRIV chroot-run root "rpm -q a7a-extlinux a7a-board-tools a7a-gpu-tools mesa-pvr-a7a mesa-img-a7a kernel-a7a kernel-a7apvr; echo '-- extlinux:'; /usr/local/bin/a7a-extlinux list; echo '-- ICD:'; ls /usr/share/vulkan/icd.d/ | grep powervr; ls /usr/local/share/vulkan/icd.d/"
$PRIV chroot-run root "/usr/local/bin/a7a-extlinux list | grep -q '^\* fedora-$REL_DEF ' && grep -q '^default fedora-$REL_DEF\$' /boot/extlinux/extlinux.conf && /usr/local/bin/a7a-extlinux list | sed -n 2p | grep -q ' fedora-$REL_FB '" || { echo "the extlinux entries are not: default fedora-$REL_DEF ($DEFAULT stack), entry 2 fedora-$REL_FB"; exit 1; }
if [ -n "$R_DDK_OLD" ]; then
  $PRIV chroot-run root "/usr/local/bin/a7a-extlinux list | sed -n 3p | grep -q ' fedora-$REL_DDK_OLD ' && /usr/local/bin/a7a-extlinux list | sed -n 4p | grep -q ' fedora-$REL_OLD ' && for f in /boot/vmlinuz-$REL_DDK_OLD /boot/vmlinuz-$REL_OLD /usr/lib/modules/$REL_DDK_OLD/modules.dep /usr/lib/modules/$REL_OLD/modules.dep /usr/lib/linux-image-$REL_DDK_OLD/allwinner /usr/lib/linux-image-$REL_OLD/allwinner; do test -e \$f || { echo \"missing in the image: \$f\"; exit 1; }; done" || { echo "the previous kernels are not entries 3 (fedora-$REL_DDK_OLD) and 4 (fedora-$REL_OLD) with their files in the image"; exit 1; }
fi
$PRIV chroot-run root "test -f /usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json.disabled-by-a7a && test ! -f /usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json" || { echo "Fedora's PowerVR ICD is not masked (mesa-pvr-a7a %post)"; exit 1; }
# mesa-img-a7a (docs/DDK-WAYLAND-2026-09-10.md): its lib/ddk links (dangling until /opt/pvr is copied below) and NO
# loader entry in the image: a7a-gpu-session writes it at boot, and only when pvrsrvkm drives the GPU
$PRIV chroot-run root "test -e /opt/pvr-wl/lib/libEGL.so.1 && test -L /opt/pvr-wl/lib/ddk/libsrv_um.so && test ! -e /etc/ld.so.conf.d/00-a7a-pvr-wl.conf" || { echo "mesa-img-a7a: files or lib/ddk links missing, or a loader entry present in the image"; exit 1; }
# the depmod outputs are %ghost in the kernel packages: modules.dep exists only if %post's depmod worked in the chroot
# ... and the session unit must be enabled by its %post (preset) in the chroot: the DDK fallback entry depends on it
$PRIV chroot-run root "for f in /boot/vmlinuz-$REL /boot/vmlinuz-$REL_DDK /usr/lib/modules/$REL/modules.dep /usr/lib/modules/$REL_DDK/modules.dep /usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw /usr/local/lib64/libvulkan_powervr_mesa.so /usr/local/bin/a7a-gpu-check /usr/local/bin/backtoddk.sh /usr/local/bin/pvr-desktop /usr/local/bin/a7a-clock /etc/modprobe.d/powervr-trace.conf /etc/a7a/pvrsrvkm-fdinfo-fixed /usr/lib/systemd/system/a7a-gpu-session.service /usr/lib/systemd/system/a7a-kwin-nocap.path /etc/systemd/system/multi-user.target.wants/a7a-gpu-session.service /etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf; do test -e \$f || { echo \"missing in the image: \$f\"; tail -n 30 /var/tmp/a7a-dnf-*.log; exit 1; }; done; echo 'files: ok'; rm -rf /var/tmp/a7a-rpms /var/tmp/a7a-dnf-*.log"
# sidecars: the default kernel's Image for the emulator (tools/emulate.sh), the package set for the registry
mkdir -p "$OUT/kernels"; rpm2cpio "$R_DEF" | ( cpio -i --quiet --to-stdout "./boot/vmlinuz-$REL_DEF"; s=$?; cat > /dev/null; exit $s ) > "$OUT/kernels/Image-$REL_DEF"
[ -s "$OUT/kernels/Image-$REL_DEF" ] || { echo "could not extract /boot/vmlinuz-$REL_DEF from $R_DEF"; exit 1; }
echo "$REL_DEF" > "$IMG.kernel"; for r in "$R_EXT" "$R_BOARD" "$R_TOOLS" "$R_MESA" "$R_IMG" "$R_DDK" "$R_PVR" "${PREV[@]}"; do basename "$r"; done > "$IMG.rpms"
# (the board tools, the greeter drop-in and the session units come from a7a-board-tools / a7a-gpu-tools since 2026-09-10)

step "GPU userspace and NPU SDK from output/ddk (best effort: present only after tools/stage-extras.sh), the DSU modules, exFAT, ISO 9660 and uinput"
if [ -d "$OUT/ddk/lib" ]; then
  $PRIV copy-dir "$OUT/ddk/lib" root opt/pvr/lib; $PRIV copy-dir "$OUT/ddk/usrlib2" root opt/pvr/lib 2>/dev/null || true
  $PRIV copy-dir "$OUT/ddk/firmware" root lib/firmware; $PRIV copy-dir "$OUT/ddk/icd" root opt/pvr/icd
  $PRIV chroot-run root "ln -sfn /opt/pvr/lib/dri /usr/local/lib/dri; echo ok"
  echo "PowerVR DDK installed under /opt/pvr (pvr-run for one app, pvr-desktop on for the whole desktop)"
else echo "no output/ddk: the image gets software rendering (tools/stage-extras.sh takes the DDK from a Debian card backup)"; fi
if [ "$DEFAULT" = ddk ]; then
  # the owner's opt-in marker: on the DDK boot a7a-gpu-session then makes mesa-img-a7a the EGL/GLES layer of every
  # program (Plasma and its apps on the PowerVR); on the open-stack fallback entry the unit takes it away again
  $PRIV chroot-run root "mkdir -p /etc/a7a && touch /etc/a7a/pvr-wl.enabled && test -e /opt/pvr/lib/libpvr_dri_support.so && echo 'pvr-wl enabled for the DDK boot (/etc/a7a/pvr-wl.enabled)'" || { echo "could not enable pvr-wl in the image (no /opt/pvr/lib/libpvr_dri_support.so?)"; exit 1; }
fi
# the DSU clock modules: one pair per kernel release, built by tools/build-dsu-modules.sh (a module loads only under
# the release it was built for; the Debian card's pair that used to go to /home/radxa could not load here)
[ -d "$OUT/dsu-modules" ] && { $PRIV copy-dir "$OUT/dsu-modules" root usr/local/lib/a7a/dsu; $PRIV chroot-run root "chown -R root:root /usr/local/lib/a7a; chmod -R u=rwX,go=rX /usr/local/lib/a7a; ls /usr/local/lib/a7a/dsu | tr '\n' ' '; echo"; }
# exFAT, ISO 9660 and uinput for each kernel of the image whose configuration leaves them out (built by the same
# script; tools/master-image.sh says why): into the kernel's module tree, indexed by depmod. Every kernel of the image
# must then find all three, from there or from its own package, built for it.
for k in $KERNELS; do
  if ls "$OUT/extra-modules/$k"/*.ko > /dev/null 2>&1; then $PRIV copy-dir "$OUT/extra-modules/$k" root "usr/lib/modules/$k/extra/a7a"; fi
done
$PRIV chroot-run root "for k in $KERNELS; do
  [ ! -d /usr/lib/modules/\$k/extra/a7a ] || { chown -R root:root /usr/lib/modules/\$k/extra/a7a && chmod -R u=rwX,go=rX /usr/lib/modules/\$k/extra/a7a; } || exit 1
  depmod -a \$k || exit 1
  for m in fs-exfat fs-iso9660 uinput; do modprobe -S \$k -n -q \$m || { echo \"kernel \$k finds no \$m (not in its package, none in output/extra-modules/\$k)\"; exit 1; }; done
  for m in exfat isofs uinput; do v=\$(modinfo -k \$k -F vermagic \$m 2>/dev/null | awk '{print \$1}'); [ -z \"\$v\" ] || [ \"\$v\" = \$k ] || { echo \"kernel \$k: \$m is built for \$v\"; exit 1; }; done
done; echo 'exFAT, ISO 9660 and uinput for: $KERNELS'" || { echo "exFAT, ISO 9660 and uinput are missing from the image"; exit 1; }
[ -d "$OUT/ddk/ai-sdk" ] && { $PRIV copy-dir "$OUT/ddk/ai-sdk" root home/radxa/ai-sdk; echo "NPU SDK (VIPLite) under /home/radxa/ai-sdk"; }

step "user radxa (password radxa, sudo), no first-boot wizard"
$PRIV chroot-run root "id radxa >/dev/null 2>&1 || useradd -m -G wheel -s /bin/bash radxa; echo radxa:radxa | chpasswd; echo 'radxa ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/radxa; chmod 440 /etc/sudoers.d/radxa; systemctl disable initial-setup.service initial-setup-text.service initial-setup-graphical.service >/dev/null 2>&1; rm -f /etc/sysconfig/initial-setup; echo ok"
# the home directory exists before useradd (the DSU modules and the NPU SDK are copied first), so useradd copies nothing
# from /etc/skel: the shell start files come from here (-n: never over a file that is already there)
$PRIV chroot-run root "cp -an /etc/skel/. /home/radxa/ 2>/dev/null; chown -R radxa:radxa /home/radxa 2>/dev/null; test -f /home/radxa/.bashrc && echo ok"
# KDE's first-run wizard (plasma-setup.service, enabled by Fedora's preset, Before=display-manager, runs while
# /etc/plasma-setup-done is missing) would take the seat at the first boot to create a user; the image has its user
$PRIV chroot-run root "touch /etc/plasma-setup-done; systemctl disable plasma-setup.service >/dev/null 2>&1; test -e /etc/plasma-setup-done && test ! -e /etc/systemd/system/multi-user.target.wants/plasma-setup.service && echo 'first-run wizard off (plasma-setup)'" || { echo "could not switch KDE's first-run wizard off in the image"; exit 1; }

step "the builder's ssh keys for user radxa (a7a_key.pub, id_ed25519.pub when present; A7A_IMAGE_KEYS=0 = none, for an image that leaves the house)"
KEYS=""; [ "${A7A_IMAGE_KEYS:-1}" = 0 ] || for k in "$HOME"/.ssh/a7a_key.pub "$HOME"/.ssh/id_ed25519.pub; do [ -f "$k" ] && KEYS="$KEYS$(cat "$k")
"; done
if [ -n "$KEYS" ]; then printf '%s' "$KEYS" | $PRIV write-file root home/radxa/.ssh/authorized_keys
  $PRIV chroot-run root "chown -R radxa:radxa /home/radxa/.ssh; chmod 700 /home/radxa/.ssh; chmod 600 /home/radxa/.ssh/authorized_keys; echo ok"
else echo "no public keys in the image; password login only"; fi
# what this build is, for the registry and the release note (src/stages/image.py)
printf 'default_stack=%s\nssh_keys=%s\n' "$DEFAULT" "$(printf '%s' "$KEYS" | grep -c .)" > "$IMG.build"
[ -z "$R_DDK_OLD" ] || echo "previous_kernels=$REL_DDK_OLD,$REL_OLD" >> "$IMG.build"
[ -z "$UPDATED" ] || echo "updated=$UPDATED" >> "$IMG.build"
# an image without keys must not carry any from an earlier step either
[ "${A7A_IMAGE_KEYS:-1}" != 0 ] || $PRIV chroot-run root "test ! -e /home/radxa/.ssh/authorized_keys && test ! -e /root/.ssh/authorized_keys && echo 'no authorized_keys in the image'" || { echo "A7A_IMAGE_KEYS=0 but the image has an authorized_keys file"; exit 1; }

step "tidy: nothing of the build stays in the image"
# dnf's caches and logs (Fedora's compose logs, and the build's own, which names this machine's kernel), what dnf left
# in /run (a tmpfs on the board), our scratch files. /etc/resolv.conf must be absent as in Fedora's own image: tmpfiles
# links it to systemd-resolved's at the first boot (tools/priv.sh chroot-run lends the build machine's per command)
# What a package script may have made of the build: a GRUB menu (grub2-mkconfig; the one of 2026-11-16 listed the systems
# on the build machine's disks), os-prober's notes, a random seed (every flashed card would start from the same one)
$PRIV chroot-run root "dnf clean all > /dev/null 2>&1; rm -rf /run/* /var/log/dnf5.log* /var/cache/libdnf5/* /var/tmp/a7a-* /boot/grub2/grub.cfg /var/lib/os-prober /var/lib/systemd/random-seed; echo tidied"
[ -z "$(ls -A "$ROOT/run")" ] && [ ! -e "$ROOT/etc/resolv.conf" ] && [ ! -L "$ROOT/etc/resolv.conf" ] || { echo "build leftovers in the image: /run holds '$(ls -A "$ROOT/run" | tr '\n' ' ')', /etc/resolv.conf: $(ls -la "$ROOT/etc/resolv.conf" 2>&1)"; exit 1; }
[ ! -e "$ROOT/boot/grub2/grub.cfg" ] && [ ! -e "$ROOT/var/lib/os-prober" ] || { echo "a GRUB menu or os-prober's state is in the image: a package script probed for other systems"; exit 1; }
# every package of the image with its version, beside the image: what is in it without mounting it
$PRIV chroot-run root "rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' | LC_ALL=C sort" > "$IMG.packages"
[ "$(wc -l < "$IMG.packages")" -gt 1500 ] || { echo "the package list of the image is implausibly short ($(wc -l < "$IMG.packages") lines)"; exit 1; }
echo "$(wc -l < "$IMG.packages") packages; $(grep -E '^(kwin|qt6-qtbase|mesa-dri-drivers|systemd) ' "$IMG.packages" | tr '\n' ' ')"

step "close"
$PRIV umount root; trap - EXIT; $PRIV loop-detach "$LOOP"
# ext4 notes where it was mounted last: the build machine's path, in every copy of the image
debugfs -w -R "ssv last_mounted /" "$IMG?offset=$((679936 * 512))" > /dev/null 2>&1
[ "$(debugfs -R "show_super_stats -h" "$IMG?offset=$((679936 * 512))" 2>/dev/null | sed -n 's/^Last mounted on: *//p')" = / ] || { echo "could not reset the last-mounted path in the image's superblock"; exit 1; }
sha256sum "$IMG" > "$IMG.sha256"
step "done: $IMG ($(du -h --apparent-size "$IMG" | cut -f1) apparent, $(du -h "$IMG" | cut -f1) on disk); default kernel $REL_DEF ($DEFAULT stack), fallback $REL_FB"
