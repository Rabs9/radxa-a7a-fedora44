#!/bin/bash
# Make the images for other people out of the backup of a card that has been in use: the owner's working system,
# minus what is his. The system that is proven on the board is the one on his card (Fedora 44 with the updates of
# 2026-09-07, the a7a packages, the vendor GPU userspace); an image assembled from Fedora's download is a different
# set of packages (docs/RADXA-SYNC-2026-11-15.md, section 13).
#   tools/master-image.sh <backup image under output/backups> <standard|oc> [size_gb]
#     -> output/a7a-fedora-44-kde-<date>-<variant>.img (+ .sha256 .kernel .rpms .build .packages)
#        output/master-work/<variant>/: filter.rules (the rules that were used); the lists of what no package accounts
#        for (unowned-files.txt, unowned-dirs.txt, ghost-files.txt, unverified-files.txt, changed-files.txt,
#        rpm-verify.txt); canaries.txt (mode 600: strings of the owner's) and canary-report.txt (mode 600: where any
#        of them was seen)
# Two variants of the same system, as the owner's Debian release has them (a7a-standard and a7a-maximum there). They
# differ in the boot firmware, that is in the memory clock, and in nothing else of the system: the device trees
# (CPU operating points to 2800/3000 MHz), the kernels and the clock panel (a7a-clock) are the same.
#   oc        the card as it runs: its boot0 (the owner's recipe "lab-V": memory at 2136 MHz, VDD2 1120 mV);
#   standard  the boot0 of his standard image ("lab-E": memory at 2040 MHz): output/sd-firmware-region-standard.img,
#             the first 16 MiB of the published a7a-standard image.
# Both without the memory-training result of his board (the "bootpara" block: a card that has never booted has zeros
# there and trains at its first boot). /etc/a7a-image-variant says which image a system came from; the clock panel
# takes its memory target from it.
# How nothing personal gets in:
#   - the backup is attached through a read-only loop and mounted read-only; it is never written;
#   - a NEW ext4 is made and the system is copied into it through tools/master-filter.rules: the home directories,
#     credentials, machine identity, logs, caches and per-machine state are never read, so they cannot be in the new
#     filesystem, not even in its free space. Kernels that no package owns (the experiments of September) stay behind
#     with their boot entries;
#   - the home directory is made anew from /etc/skel, the accounts are set (not inherited), the machine-id is reset
#     (first-boot semantics), the timezone is UTC;
#   - at the end the free space of the new filesystem is trimmed out of the image file.
# How that is checked, by the build itself:
#   - tools/master-audit.py: every file, link and directory no package owns, every %ghost file, every package file
#     rpm does not checksum and every changed package file must be on the list of what is let in
#     (tools/master-allow.rules). The filter is a list of what to leave out, so by itself it lets through whatever
#     nobody thought of;
#   - tools/master-canary.py: strings taken from the card (tools/master-canaries.sh: keys, passphrases, addresses,
#     ids, command lines) are searched in the whole finished file.
# The file is built as <image>.building and gets its name only when every check has passed; whatever else happens it
# ends as <image>.rejected, which no stage takes for an image.
# What is added: the first-boot root grow (tools/board/a7a-grow-rootfs), the unit presets of the image, the current
# a7a tool packages (KWin 6.8 handling), the directories under /var that packages own and the card lacked
# (tools/board/a7a-var-dirs), GRUB_DISABLE_OS_PROBER (a grub2 update on the board would otherwise probe, and mount,
# every disk attached to it), and for each kernel the DSU clock modules and the exFAT, ISO 9660 and uinput modules its
# configuration lacks (tools/build-dsu-modules.sh). Every command inside the image runs in tools/priv.sh's sandbox.
set -euo pipefail
[ -n "${A7A_RUN:-}" ] || [ -t 0 ] || { echo "$0: refusing to run non-interactively without A7A_RUN=1"; exit 2; }
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output; PRIV="sudo -n $P/tools/priv.sh"
SRC=$(readlink -f "${1:?the backup image of the card (under output/backups)}"); VARIANT=${2:-}; SIZE_GB=${3:-16}
case "$VARIANT" in standard|oc) ;; *) echo "usage: $0 <backup under output/backups> <standard|oc> [size_gb]"
  echo "  standard: memory at 2040 MHz; oc: the card as it runs (memory at 2136 MHz); everything else is the same"; exit 2 ;; esac
IMG=$OUT/a7a-fedora-44-kde-${A7A_IMAGE_DATE:-$(date +%Y%m%d)}-$VARIANT.img; B=$IMG.building; WORK=$OUT/master-work/$VARIANT
step() { echo "== [$(date +%H:%M:%S)] $*"; }
case "$SRC" in "$OUT"/backups/*.img) ;; *) echo "the source must be a card backup under $OUT/backups, not $SRC"; exit 1 ;; esac
[ -f "$SRC" ] || { echo "no such backup: $SRC"; exit 1; }
FW=$OUT/sd-firmware-region.img; FWS=$OUT/sd-firmware-region-standard.img
[ -f "$FW" ] && [ "$(stat -c %s "$FW")" = 16777216 ] || { echo "$FW missing"; exit 1; }
[ "$VARIANT" = oc ] || { [ -f "$FWS" ] && [ "$(stat -c %s "$FWS")" = 16777216 ]; } || { echo "$FWS missing: the first 16 MiB of the owner's standard image (memory at 2040 MHz), e.g."
  echo "  curl -sL https://github.com/Rabs9/radxa-cubie-a7a-kernel/releases/download/images-20260825/a7a-standard-20260825.img.xz | xz -dc | head -c 16777216 > $FWS"; exit 1; }
rc=0; sudo -n "$P/tools/priv.sh" >/dev/null 2>&1 || rc=$?; [ "$rc" -eq 2 ] || { echo "sudo without a password is needed for tools/priv.sh (see the sudoers line in its header)"; exit 1; }
# the newest build of each tool package, as tools/assemble-image.sh picks them
# (|| true: under pipefail a pattern that matches nothing would end the script at the assignment, before the message)
newest() { ls "$OUT"/rpms/$1 2>/dev/null | sed -E 's/^(.*-([0-9]{12})\.[a-z0-9_]+\.rpm)$/\2 \1/' | LC_ALL=C sort | tail -1 | cut -d' ' -f2- || true; }
R_EXT=$(newest 'a7a-extlinux-*.noarch.rpm'); R_BOARD=$(newest 'a7a-board-tools-*.noarch.rpm'); R_TOOLS=$(newest 'a7a-gpu-tools-*.noarch.rpm')
# the DDK's Wayland Mesa too: the card carries the build of 2026-11-15, and that one loses every application to
# software rendering once KWin 6.8 arrives with the first `dnf upgrade` (edit 3 in tools/mesa-img/build-mesa-img.sh)
R_MESA=$(newest 'mesa-img-a7a-*.aarch64.rpm')
# the board's panel (clocks, power, hardware, the Assistant), compiled on this laptop by tools/build-panel.sh
R_PANEL=$(newest 'a7a-panel-*.aarch64.rpm')
# where the board's updates come from after the image, and the key they are signed with (tools/repo-key.sh, once)
R_REPO=$(newest 'a7a-repo-*.noarch.rpm')
# Fedora's own packages the a7a ones recommend and the card lacks (output/fedora-rpms: downloaded from Fedora's
# repositories, signed by Fedora; the install below runs with the repositories off, so a Recommends never lands):
# dotool, for typing by voice (2026-11-24: the first image had none). Each must carry Fedora's signature (checked in
# the image, against the keys it has) and be built for this release
FEDORA_RPMS=$(ls "$OUT"/fedora-rpms/*.fc44.aarch64.rpm 2>/dev/null || true)
grep -q '/dotool-' <<< "$FEDORA_RPMS" || { echo "no dotool in $OUT/fedora-rpms: on a Fedora 44 board, dnf download --repo=updates dotool, and copy it there"; exit 1; }
for v in R_EXT R_BOARD R_TOOLS R_MESA R_PANEL R_REPO; do [ -n "${!v}" ] || { echo "no $v package in $OUT/rpms (tools/build-tools-rpm.sh, tools/build-mesa-img-rpm.sh, tools/build-panel.sh; a7a-repo needs tools/repo-key.sh first)"; exit 1; }; done
case "$(basename "$R_MESA")" in mesa-img-a7a-*-20261115*|mesa-img-a7a-*-202609*) echo "the newest mesa-img-a7a in $OUT/rpms ($(basename "$R_MESA")) is from before the KWin 6.8 fix"; exit 1 ;; esac
# The tool packages are whatever tools/build-tools-rpm.sh last made: a board script edited after that would be
# missing from the image without a word (2026-11-16: the packages were rebuilt by hand five times in one evening).
# Every file of these packages must be, byte for byte, a file of this tree as it is now (the packages copy files,
# some under another name; none is generated). Checked here, before the half hour of copying.
TREE_SUMS=$(find "$P/tools/board" "$P/tools/a7a-extlinux" "$P/tools/repo" -type f -exec sha256sum {} + | cut -c1-64 | sort -u)
# the NPU wake spotter's data is too big for the repository: the tree holds its sums (tools/npu/npu-kws.sha256, the
# files tested on the board), and a7a-board-tools may carry exactly those (2026-11-24: the first image since they
# were packaged stopped here, the gate not knowing them)
TREE_SUMS=$(printf '%s\n%s\n' "$TREE_SUMS" "$(cut -c1-64 "$P/tools/npu/npu-kws.sha256")" | sort -u)
# a7a-panel's binary is compiled: it counts as this tree's when tools/build-panel.sh built it from tools/panel as it is now
PANEL_SRC=$(cd "$P/tools/panel" && find Cargo.toml Cargo.lock src vendor -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)
[ "$(cat "$P/output/panel/a7a-panel.sha256" 2>/dev/null)" = "$PANEL_SRC" ] || { echo "output/panel/a7a-panel was not built from tools/panel as it is now (run tools/build-panel.sh)"; exit 1; }
TREE_SUMS=$(printf '%s\n%s\n' "$TREE_SUMS" "$(sha256sum < "$P/output/panel/a7a-panel" | cut -c1-64)" | sort -u)
RPMTMP=$(mktemp -d); : > "$RPMTMP/usr.sha256"
for r in "$R_EXT" "$R_BOARD" "$R_TOOLS" "$R_PANEL" "$R_REPO"; do
  mkdir "$RPMTMP/x"; ( cd "$RPMTMP/x" && rpm2cpio "$r" | cpio -id --quiet ) || { echo "cannot unpack $r" >&2; exit 1; }
  stale=$(cd "$RPMTMP/x" && find . -type f -exec sha256sum {} + | while read -r sum f; do grep -qx "$sum" <<<"$TREE_SUMS" || echo "${f#.}"; done)
  [ -z "$stale" ] || { echo "$(basename "$r") was not built from this tree as it is now (run tools/build-tools-rpm.sh):" $stale >&2; rm -rf "$RPMTMP"; exit 1; }
  # for the look into the image after the install: the files under /usr (those under /etc are configuration the
  # image may keep its own version of)
  # (a7a-repo has none: /etc only; 2026-11-24, the first image since it was packaged stopped here)
  [ ! -d "$RPMTMP/x/usr" ] || ( cd "$RPMTMP/x" && find ./usr -type f -exec sha256sum {} + | sed 's|  \./|  /|' ) >> "$RPMTMP/usr.sha256"
  rm -rf "$RPMTMP/x"
done
mkdir -p "$WORK"; chmod 700 "$OUT/master-work" "$WORK"
# one build at a time: the two variants share the mount names and would unlink each other's files
exec 8> "$OUT/master-work/.lock"; flock -n 8 || { echo "another tools/master-image.sh is running (output/master-work/.lock)"; exit 1; }
# The image is built under a name no stage takes for an image and is renamed when every check has passed. Whatever
# ends the script before that (a failed check, a failed command, a signal) leaves it as *.rejected
REJECTED=$IMG.rejected; DONE=0; SLOOP=""; LOOP=""
reject() { echo "REJECTED: $*"; exit 1; }
finish() { set +e; for m in msrc mroot; do $PRIV umount $m 2>/dev/null; done; [ -z "$SLOOP" ] || $PRIV loop-detach "$SLOOP" 2>/dev/null; [ -z "$LOOP" ] || $PRIV loop-detach "$LOOP" 2>/dev/null
  if [ "$DONE" != 1 ] && [ -f "$B" ]; then mv -f "$B" "$REJECTED"; echo "NOT AN IMAGE TO PUBLISH: the build did not pass; the file is kept for inspection as $REJECTED"; fi; }
trap finish EXIT

step "the card's system, read-only: $(basename "$SRC")"
START=$(sfdisk -d "$SRC" 2>/dev/null | awk '$1 ~ /3$/ {for (i = 1; i < NF; i++) if ($i == "start=") {gsub(",", "", $(i + 1)); print $(i + 1)}}')
[ -n "$START" ] || { echo "no partition 3 in $SRC"; exit 1; }
SLOOP=$($PRIV loop-attach "$SRC" $((START * 512)))
S=$($PRIV mount "$SLOOP" msrc ro)
[ -f "$S/boot/extlinux/extlinux.conf" ] && [ -d "$S/usr/lib/sysimage/rpm" ] || { echo "$SRC does not hold an a7a-fedora system"; exit 1; }
DEF_LABEL=$(sed -n 's/^default  *//p' "$S/boot/extlinux/extlinux.conf" | head -1)
REL_DEF=$(awk -v l="$DEF_LABEL" '$1 == "label" {cur = $2} cur == l && $1 == "linux" {sub(".*/vmlinuz-", "", $2); print $2; exit}' "$S/boot/extlinux/extlinux.conf")
[ -n "$REL_DEF" ] || { echo "cannot read the default kernel from the card's extlinux.conf (default '$DEF_LABEL')"; exit 1; }
# kernels: the ones a package owns stay; what was installed by hand for an experiment stays behind
KEEP=""; DROP=""
for f in "$S"/boot/vmlinuz-*; do k=${f##*/vmlinuz-}
  if rpm --root "$S" -qf "/boot/vmlinuz-$k" >/dev/null 2>&1; then KEEP="$KEEP $k"; else DROP="$DROP $k"; fi
done
case " $KEEP " in *" $REL_DEF "*) ;; *) echo "the card's default kernel $REL_DEF is owned by no package: this script keeps packaged kernels only"; exit 1 ;; esac
echo "default kernel $REL_DEF; kept:$KEEP; left behind:${DROP:- none}"
# the DSU clock modules must exist for each kept kernel, built for exactly that release, and so must its set of exFAT,
# ISO 9660 and uinput modules (empty for a kernel that has the three itself; the steps that install them say why);
# looked at here, before the half hour of copying
for k in $KEEP; do
  for m in dsu_oc dsu_dyn; do
    f=$OUT/dsu-modules/$k/$m.ko
    [ -f "$f" ] || { echo "no $m.ko for kernel $k in $OUT/dsu-modules: tools/build-dsu-modules.sh $k" >&2; exit 1; }
    vm=$(modinfo -F vermagic "$f" | awk '{print $1}')
    [ "$vm" = "$k" ] || { echo "$f is built for '$vm', not for $k: it would not load" >&2; exit 1; }
  done
  [ -d "$OUT/extra-modules/$k" ] || { echo "no $OUT/extra-modules/$k (exFAT, ISO 9660, uinput): tools/build-dsu-modules.sh $k" >&2; exit 1; }
  for f in "$OUT/extra-modules/$k"/*.ko; do
    [ -e "$f" ] || continue
    vm=$(modinfo -F vermagic "$f" | awk '{print $1}')
    [ "$vm" = "$k" ] || { echo "$f is built for '$vm', not for $k: it would not load" >&2; exit 1; }
  done
done
{ cat "$P/tools/master-filter.rules"; echo
  echo "# -- appended by tools/master-image.sh: kernels, modules and device trees of $(basename "$SRC") that no package owns"
  for k in $DROP; do echo "- /boot/vmlinuz-$k"; done
  for d in "$S"/usr/lib/modules/* "$S"/usr/lib/linux-image-*; do [ -e "$d" ] || continue
    rpm --root "$S" -qf "${d#"$S"}" >/dev/null 2>&1 || echo "- ${d#"$S"}"; done
} > "$WORK/filter.rules"
echo "$(/usr/bin/grep -c '^- ' "$WORK/filter.rules") exclusion rules in $WORK/filter.rules"
# strings of the owner's and of this machine that must not turn up in the image (tools/master-canary.py). They are
# read as root inside the sandbox and go into a file only the owner can read; they are never printed
# (the collection uses these programs of the card; without one of them whole kinds would be missing without a word)
$PRIV chroot-run msrc 'for t in grep sed awk sort tr cut strings python3 find; do command -v $t > /dev/null || { echo "the card has no $t"; exit 1; }; done' || exit 1
( umask 077; $PRIV chroot-run msrc "bash -s" < "$P/tools/master-canaries.sh" > "$WORK/canaries.txt" )
echo "$(wc -l < "$WORK/canaries.txt") canary strings collected ($WORK/canaries.txt, mode 600): $(cut -f1 "$WORK/canaries.txt" | sed 's/^soft:.*/(may occur by chance)/' | sort | uniq -c | sort -rn | awk '{n = $1; $1 = ""; printf "%s%s", n, $0 "; "}')"
# a card that has booted has a machine-id and ssh host keys: a collection without them did not work, and a search
# for nothing finds nothing
for k in machine-id ssh-host-public ssh-host-private password-hash; do
  /usr/bin/grep -q "^$k	" "$WORK/canaries.txt" || { echo "the collection of the owner's strings holds no $k: it did not work, nothing is built"; exit 1; }
done

step "new image ${SIZE_GB} GB ($VARIANT): the boot firmware, the card layout, new filesystems"
rm -f "$B" "$REJECTED"; truncate -s "${SIZE_GB}G" "$B"
# The same layout as tools/assemble-image.sh (firmware region first, then our table over it; p2 and p3 EFI-typed, or
# U-Boot never scans them for extlinux.conf: docs/HARDWARE.md).
# The firmware region: output/sd-firmware-region.img is what every card of this project booted from: the vendor's
# U-Boot with the OWNER'S boot0 "lab-V" (memory at 2136 MHz) and, in the "bootpara" block (sectors 24496 and
# following), the result of the memory training of HIS board at its last boot. boot0 itself is never rewritten on the
# card (three copies of that card, months apart, differ in bootpara only). The standard image gets the firmware of
# his published standard image instead: the same U-Boot, his boot0 "lab-E" (2040 MHz). The two may differ in boot0
# (two copies: sectors 256-291 and 2064-2099) and in bootpara only, each boot0 must carry a valid checksum and the
# memory clock its variant promises, and bootpara is emptied.
BP=24496; BPN=8
case "$VARIANT" in oc) FWU=$FW; RAM=2136 ;; standard) FWU=$FWS; RAM=2040 ;; esac
rc=0; cmp -l -i 17408 -n $((16777216 - 17408)) "$FWU" "$FW" > "$WORK/firmware.cmp" || rc=$?; [ "$rc" -le 1 ] || { echo "cmp of the firmware files failed"; exit 1; }
odd=$(awk '{s = int(($1 + 17408 - 1) / 512); if (!((s >= 256 && s <= 291) || (s >= 2064 && s <= 2099) || (s >= BP && s < BP + BPN))) n++} END {print n + 0}' BP=$BP BPN=$BPN "$WORK/firmware.cmp")
[ "$odd" = 0 ] || { echo "$FWU is not the firmware of $FW with another boot0: $odd bytes differ outside boot0 and bootpara"; exit 1; }
python3 "$P/tools/boot0-check.py" "$FWU" "$RAM" || { echo "the boot0 in $FWU is not what the $VARIANT image needs"; exit 1; }
[ "$(dd if="$FWU" bs=1 skip=$((BP * 512)) count=8 status=none)" = bootpara ] || { echo "no bootpara block at sector $BP of $FWU: its layout is not the one this script knows"; exit 1; }
dd if="$FWU" of="$B" bs=1M conv=notrunc status=none
dd if=/dev/zero of="$B" bs=512 seek=$BP count=$BPN conv=notrunc status=none
[ "$(dd if="$B" bs=512 skip=$BP count=$BPN status=none | tr -d '\0' | wc -c)" = 0 ] || { echo "the bootpara block is not empty"; exit 1; }
echo "boot firmware: $(basename "$FWU") (boot0 checksums valid, memory at $RAM MHz), the memory training of the owner's board taken out"
sfdisk -q --wipe never --wipe-partitions never "$B" <<EOF
label: gpt
first-lba: 32768
start=32768,  size=32768,  type=0FC63DAF-8483-4772-8E79-3D69D8477DE4, name="config"
start=65536,  size=614400, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="boot"
start=679936, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="rootfs"
EOF
sfdisk --verify "$B" 2>&1 | tail -1
for n in 2 3; do t=$(sfdisk --part-type "$B" $n | tr a-f A-F); [ "$t" = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" ] || { echo "p$n type is $t, not EFI System"; exit 1; }; done
LOOP=$($PRIV loop-attach "$B"); sleep 1; ls "${LOOP}p1" "${LOOP}p2" "${LOOP}p3" >/dev/null
$PRIV mkfs ext4 config "${LOOP}p1"; $PRIV mkfs vfat BOOT "${LOOP}p2"; $PRIV mkfs ext4 rootfs "${LOOP}p3"
ROOT=$($PRIV mount "${LOOP}p3" mroot)

step "copy the system through the filter (the slow part)"
$PRIV copy-system msrc mroot "$WORK/filter.rules" | tr '\r' '\n' | tail -1
$PRIV umount msrc; $PRIV loop-detach "$SLOOP"; SLOOP=""
df -h "$ROOT" | tail -1

step "a machine without an identity: machine-id, timezone, host name"
$PRIV chroot-run mroot "printf 'uninitialized\n' > /etc/machine-id; chmod 444 /etc/machine-id; mkdir -p /var/lib/dbus; ln -sfn /etc/machine-id /var/lib/dbus/machine-id; ln -sfn ../usr/share/zoneinfo/UTC /etc/localtime; echo radxa-cubie-a7a > /etc/hostname; echo ok"

step "accounts: the image's login and nobody's password but that one"
# /etc/passwd and /etc/shadow come from the card as they are. Whatever the owner set, the image has user radxa with
# the documented password, root locked, no full name, no old hashes; another person's account stops the build
$PRIV chroot-run mroot 'others=$(awk -F: "\$3 >= 1000 && \$3 < 60000 && \$1 != \"radxa\" {print \$1}" /etc/passwd | tr "\n" " ")
[ -z "$others" ] || { echo "the card has accounts besides radxa: $others"; exit 1; }
id radxa > /dev/null || exit 1
echo radxa:radxa | chpasswd && passwd -l root > /dev/null && usermod -c "" radxa || exit 1
# groups: no person's group but radxa's own, nobody but radxa a member of anything, no group password
odd=$(awk -F: "(\$3 >= 1000 && \$3 < 60000 && \$1 != \"radxa\") || (\$4 != \"\" && \$4 != \"radxa\") {print \$1}" /etc/group | tr "\n" " ")
[ -z "$odd" ] || { echo "groups of the card that are a person'"'"'s or have other members than radxa: $odd"; exit 1; }
odd=$(awk -F: "\$2 != \"\" && \$2 !~ /^[!*]+\$/ {print \$1}" /etc/gshadow 2>/dev/null | tr "\n" " ")
[ -z "$odd" ] || { echo "groups with a password: $odd"; exit 1; }
[ ! -e /etc/security/opasswd ] || : > /etc/security/opasswd
# the tools above keep the file they change as <name>-: the hashes the card had
rm -f /etc/passwd- /etc/shadow- /etc/group- /etc/gshadow- /etc/subuid- /etc/subgid-
with=$(awk -F: "\$2 != \"\" && \$2 !~ /^[!*]/ {print \$1}" /etc/shadow | tr "\n" " ")
[ "$with" = "radxa " ] || { echo "accounts with a password: ${with:-none}; expected radxa alone"; exit 1; }
command -v perl > /dev/null || { echo "no perl in the image to check the password with"; exit 1; }
h=$(awk -F: "\$1 == \"radxa\" {print \$2}" /etc/shadow)
[ "$(perl -e "print crypt(\$ARGV[0], \$ARGV[1])" radxa "$h")" = "$h" ] || { echo "the password of radxa is not the documented one"; exit 1; }
# the dot files of root are made from the package'"'"'s own copies, not taken from the card
[ ! -d /usr/share/rootfiles ] || cp -f /usr/share/rootfiles/.[!.]* /root/
echo "radxa (password radxa) is the only account with a password; root is locked"' || { echo "the accounts of the image are not as they must be"; exit 1; }

step "the a7a packages of today: $(basename "$R_EXT") $(basename "$R_BOARD") $(basename "$R_TOOLS") $(basename "$R_MESA") $(basename "$R_PANEL") $(basename "$R_REPO")"
rm -rf "$OUT/image-rpms"; mkdir -p "$OUT/image-rpms/fedora"; cp "$R_EXT" "$R_BOARD" "$R_TOOLS" "$R_MESA" "$R_PANEL" "$R_REPO" "$OUT/image-rpms/"
cp $FEDORA_RPMS "$OUT/image-rpms/fedora/"
$PRIV copy-dir "$OUT/image-rpms" mroot var/tmp/a7a-rpms
# (the key must be Fedora 44's, named so in the image's keyring, not any key it holds: the a7a-fedora key is one too)
$PRIV chroot-run mroot "fk=\$(rpm -q gpg-pubkey --qf '%{VERSION} %{SUMMARY}\n' | awk '/Fedora \\(44\\)/ {print \$1; exit}'); [ -n \"\$fk\" ] || { echo 'no Fedora 44 key in the image'; exit 1; }; for f in /var/tmp/a7a-rpms/fedora/*.rpm; do rpmkeys -K \"\$f\" | grep -q ': digests signatures OK\$' && rpmkeys -Kv \"\$f\" | grep -Eqi \"[0-9a-f]*\$fk: OK\$\" || { echo \"\$f: not signed by Fedora 44's key (\$fk)\"; rpmkeys -Kv \"\$f\" | tail -4; exit 1; }; done; echo \"Fedora packages: signed by Fedora 44's key \$fk\"" || { echo "a package of output/fedora-rpms is not Fedora's"; exit 1; }
$PRIV chroot-run mroot "dnf install -y --disablerepo='*' /var/tmp/a7a-rpms/*.rpm /var/tmp/a7a-rpms/fedora/*.rpm > /var/tmp/a7a-dnf.log 2>&1 || { tail -25 /var/tmp/a7a-dnf.log; exit 1; }; rpm -q a7a-extlinux a7a-board-tools a7a-gpu-tools mesa-img-a7a a7a-panel a7a-repo dotool > /tmp/q || { cat /tmp/q; exit 1; }; rpmkeys --import /etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora || { echo 'the a7a-fedora key does not import'; exit 1; }; tr '\n' ' ' < /tmp/q; echo; grep -qa 'has no render node, using' /opt/pvr-wl/lib/libEGL.so.1 || { echo 'the Wayland Mesa in the image is without the KWin 6.8 fix'; exit 1; }"

# the image now holds the packages' files (the same list that was compared with the tree at the start)
cp "$RPMTMP/usr.sha256" "$WORK/tools.sha256"; rm -rf "$RPMTMP"
$PRIV chroot-run mroot "sha256sum -c --quiet -" < "$WORK/tools.sha256" \
  || { echo "after the install the image does not hold the files of the a7a tool packages (above)" >&2; exit 1; }
echo "the tools in the image are the files of this tree ($(wc -l < "$WORK/tools.sha256") files of the a7a packages)"

step "the card's kernels and Mesa driver, their packages without the build machine's name"
# They stay as the card has them (proven on the board), but their packages, built before 2026-11-16, name this
# machine as their build host, and the image's package database would carry that. tools/rewrap-rpm.sh made the same
# packages again; tools/master-rewrap.sh puts them in, after checking the card's files against them (2026-11-24)
BUILDER=$(uname -n)
INST=$($PRIV chroot-run mroot "rpm -qa --qf '%{NAME} %{NEVRA} %{BUILDHOST}\n' 'kernel-a7a*' 'mesa-pvr-a7a*'") || { echo "the package database of the image could not be read"; exit 1; }
rm -rf "$OUT/image-rewrap"; mkdir -p "$OUT/image-rewrap"; cp "$P/tools/master-rewrap.sh" "$OUT/image-rewrap/"; need=()
while read -r name nevra host; do
  [ -n "$name" ] && [ "$host" != a7a-fedora ] || continue
  f=$OUT/rpms/$nevra.rpm
  if [ ! -f "$f" ] || [ "$(rpm -qp --qf '%{BUILDHOST}' "$f" 2>/dev/null)" != a7a-fedora ]; then
    echo "$nevra in the image names $host as its build host, and $OUT/rpms has no re-wrapped package of it:"
    echo "  tools/rewrap-rpm.sh $f   (the package as built then, in $OUT/rpms)"; exit 1
  fi
  cp "$f" "$OUT/image-rewrap/"; need+=("/var/tmp/a7a-rewrap/$(basename "$f")")
done <<< "$INST"
$PRIV copy-dir "$OUT/image-rewrap" mroot var/tmp/a7a-rewrap
$PRIV chroot-run mroot "bash /var/tmp/a7a-rewrap/master-rewrap.sh '$BUILDER' ${need[*]}" || { echo "the re-wrapped packages could not be put in (above)"; exit 1; }
rm -rf "$OUT/image-rewrap"

step "leave out what an image for other people does without (tools/master-remove.list): the .xz must fit under 2 GiB"
# What no removal may take out, whatever dnf works out from the dependencies: the build stops instead
GUARD='^(plasma-desktop|plasma-workspace|plasma-workspace-wayland|kwin|kwin-wayland|kwin-libs|plasmalogin|plasma-login-manager|sddm|mesa-dri-drivers|mesa-libEGL|mesa-libGL|mesa-libgbm|mesa-vulkan-drivers|mesa-pvr-a7a|mesa-img-a7a|kernel-a7a|kernel-a7apvr|a7a-extlinux|a7a-board-tools|a7a-gpu-tools|NetworkManager|NetworkManager-wifi|wpa_supplicant|firefox|dnf5|sudo|openssh-server|openssh-clients|systemd|systemd-udev|dolphin|konsole|kate|kwrite|plasma-nm|plasma-pa|pipewire|wireplumber|bluez|plasma-discover|flatpak|xorg-x11-server-Xwayland|qt6-qtbase|qt6-qtdeclarative|qt6-qtwayland|glibc|glibc-all-langpacks|bash|python3|python3-pyside6|python3-pyqt6|python3-pyqt6-base|llvm-libs|polkit|udisks2|upower|firewalld|chrony|plasma-systemmonitor|ark|spectacle|gwenview|okular|kcalc|plasma-systemsettings|kscreen|powerdevil|bluedevil|xdg-desktop-portal-kde|linux-firmware|linux-firmware-whence|rpm|coreutils|util-linux|e2fsprogs|gdisk|parted|cloud-utils-growpart|tar|xz|curl|nano|vim-minimal|less|grep|sed|gawk|findutils|iproute|iputils|kde-settings|breeze-icon-theme|plasma-breeze|fontconfig|unbound-libs|rsyslog|logrotate|plocate|tuned|atheros-firmware|mt7xxx-firmware|realtek-firmware|brcmfmac-firmware)$'
: > "$WORK/removed-packages.txt"
# (Fedora's kernels are hidden from dnf on this board by excludepkgs in /etc/dnf/dnf.conf: for this step dnf must see them)
$PRIV chroot-run mroot "sed -i 's/^excludepkgs/#a7a-removal#&/' /etc/dnf/dnf.conf; echo ok" > /dev/null
# What the image is about is there on purpose, whatever brought it in: marked as installed by the user, it is never
# taken out as "a dependency nothing needs any more" (2026-11-16: Fedora's own kernel was the only package that still
# asked for linux-firmware, which holds the PowerVR firmware of this board)
$PRIV chroot-run mroot "keep=\$(rpm -qa --qf '%{NAME}\n' | grep -E '$GUARD' | LC_ALL=C sort -u | tr '\n' ' ')
dnf mark user -y --disablerepo='*' \$keep > /var/tmp/a7a-dnf-mark.log 2>&1 || { tail -5 /var/tmp/a7a-dnf-mark.log; exit 1; }
rm -f /var/tmp/a7a-dnf-mark.log; echo \"\$(echo \$keep | wc -w) packages marked as wanted\"" || { echo "dnf could not mark the packages the image is about"; exit 1; }
# (which directories are empty before anything goes: see the sweep after the removals)
$PRIV chroot-run mroot "find / -xdev -type d -empty 2>/dev/null | LC_ALL=C sort > /var/tmp/a7a-empty-before; wc -l < /var/tmp/a7a-empty-before" > /dev/null
set -f      # the list's globs are for dnf inside the image, not for this shell
while IFS='|' read -r -u 7 what pkgs; do
  case "$what" in ''|'#'*) continue ;; esac
  args=$(printf "'%s' " $pkgs)
  $PRIV chroot-run mroot "dnf remove --assumeno --disablerepo='*' $args 2>&1; true" > "$WORK/removal-preview.txt"
  awk '$2 ~ /^(aarch64|noarch)$/ {print $1}' "$WORK/removal-preview.txt" | LC_ALL=C sort -u > "$WORK/removal-would-go.txt"
  n=$(wc -l < "$WORK/removal-would-go.txt")
  [ "$n" -gt 0 ] || { echo "-- $what: nothing of it is installed"; continue; }
  hit=$(/usr/bin/grep -E "$GUARD" "$WORK/removal-would-go.txt" | tr '\n' ' ' || true)
  [ -z "$hit" ] || { echo "removing \"$what\" would take out $hit(of $n packages): tools/master-remove.list is wrong for this card"; exit 1; }
  $PRIV chroot-run mroot "dnf remove -y --disablerepo='*' $args > /var/tmp/a7a-dnf-remove.log 2>&1 || { tail -15 /var/tmp/a7a-dnf-remove.log; exit 1; }" || { echo "dnf could not remove \"$what\""; exit 1; }
  echo "-- $what: $n packages"; cat "$WORK/removal-would-go.txt" >> "$WORK/removed-packages.txt"
done 7< "$P/tools/master-remove.list"
set +f
$PRIV chroot-run mroot "sed -i 's/^#a7a-removal#excludepkgs/excludepkgs/; /^excludepkgs/ s/,kernel-headers\b//' /etc/dnf/dnf.conf; grep -c '^excludepkgs' /etc/dnf/dnf.conf; rm -f /var/tmp/a7a-dnf-remove.log
rpm -q plasma-desktop plasma-workspace kwin firefox NetworkManager-wifi openssh-server dnf5 mesa-pvr-a7a mesa-img-a7a kernel-a7a kernel-a7apvr a7a-extlinux a7a-board-tools a7a-gpu-tools a7a-panel python3-pyside6 dtc glmark2 > /tmp/q || { grep -v '^[a-zA-Z0-9]' /tmp/q; cat /tmp/q | grep 'not installed'; exit 1; }
echo \"\$(rpm -qa | wc -l) packages stay\"" || { echo "after the removals the image lacks a package it is about"; exit 1; }
# rpm removes a package's files and the directories that package owns; directories no package owns stay behind,
# empty (/usr/lib64/libreoffice/..., /usr/lib/clang/22/..., qt6/plugins/pim6/...: 82 of them, which the audit then
# cannot account for). Swept here, innermost first: empty now, not empty before the removals, owned by no package
$PRIV chroot-run mroot 'rules=""; for d in /lib /lib64 /bin /sbin /usr/sbin; do [ -L "$d" ] && rules="$rules;s#^$d/#$(readlink -f "$d")/#"; done
rpm -qa --qf "[%{FILENAMES}\n]" | sed -E "${rules#;}" | LC_ALL=C sort -u > /tmp/owned; n=0
while :; do find / -xdev -type d -empty 2>/dev/null | LC_ALL=C sort | LC_ALL=C comm -23 - /var/tmp/a7a-empty-before | LC_ALL=C comm -23 - /tmp/owned > /tmp/left
  [ -s /tmp/left ] || break; n=$((n + $(wc -l < /tmp/left))); xargs -d "\n" rmdir < /tmp/left || exit 1; done
rm -f /var/tmp/a7a-empty-before; echo "$n directories that the removed packages left behind, empty, swept"' || { echo "the sweep of empty directories failed"; exit 1; }
echo "$(wc -l < "$WORK/removed-packages.txt") packages removed ($WORK/removed-packages.txt)"

step "boot menu: only entries whose kernel is in the image"
$PRIV chroot-run mroot '/usr/local/bin/a7a-extlinux list | while read -r a b c d e; do
  if [ "$a" = "*" ]; then label=$b; linux=$d; else label=$a; linux=$c; fi
  [ -e "$linux" ] || /usr/local/bin/a7a-extlinux remove "$label"
done; echo "--"; /usr/local/bin/a7a-extlinux list'
$PRIV chroot-run mroot '/usr/local/bin/a7a-extlinux list | while read -r a b c d e; do if [ "$a" = "*" ]; then linux=$d; else linux=$c; fi; [ -e "$linux" ] || echo "$linux"; done' > "$WORK/menu-missing.txt"
[ ! -s "$WORK/menu-missing.txt" ] || { echo "boot entries name kernels that are not in the image: $(tr '\n' ' ' < "$WORK/menu-missing.txt")"; exit 1; }
[ "$(sed -n 's/^default  *//p' "$ROOT/boot/extlinux/extlinux.conf")" = "fedora-$REL_DEF" ] || { echo "the boot menu does not default to fedora-$REL_DEF"; exit 1; }

step "a new user's desktop: no on-screen keyboard at login"
# Fedora's default (kde-settings) makes KWin start plasma-keyboard as the input method of every session. It cannot get
# an OpenGL context through the vendor GPU driver and aborts, five times, at each login: five crash reports on the
# first desktop of the first card made from this image (2026-11-16). The owner's own home has "InputMethod=" (none),
# which is why his card never showed it; a fresh home must start the same way. (kde-settings' profile outranks
# /etc/xdg, so the default goes where new homes come from. Turning the keyboard on again: System Settings, Keyboard,
# Virtual Keyboard.)
printf '[Wayland]\nInputMethod=\n' | $PRIV write-file mroot etc/skel/.config/kwinrc

step "the DSU clock modules, one pair for each kernel of the image"
# `a7a-clock dsu`, the four profiles and the panel's DSU buttons load dsu_oc.ko or dsu_dyn.ko (tools/dsu/). This
# kernel has no CONFIG_MODVERSIONS: a module loads only under the release it was built for. Every image until
# 2026-11-16 carried the Debian card's pair ("6.6.98+") in /home/radxa, so none of those controls could work, and
# nobody had pressed one. tools/build-dsu-modules.sh builds the pair for a packaged kernel.
rm -rf "$WORK/dsu"; mkdir -p "$WORK/dsu"
for k in $KEEP; do
  for m in dsu_oc dsu_dyn; do
    f=$OUT/dsu-modules/$k/$m.ko
    [ -f "$f" ] || { echo "no $m.ko for kernel $k in $OUT/dsu-modules: tools/build-dsu-modules.sh $k"; exit 1; }
    vm=$(modinfo -F vermagic "$f" | awk '{print $1}')
    [ "$vm" = "$k" ] || { echo "$f is built for '$vm', not for $k: it would not load"; exit 1; }
    install -D -m 644 "$f" "$WORK/dsu/$k/$m.ko"
  done
done
$PRIV copy-dir "$WORK/dsu" mroot usr/local/lib/a7a/dsu
$PRIV chroot-run mroot "chown -R root:root /usr/local/lib/a7a && chmod -R u=rwX,go=rX /usr/local/lib/a7a && for k in $KEEP; do test -f /usr/local/lib/a7a/dsu/\$k/dsu_oc.ko && test -f /usr/local/lib/a7a/dsu/\$k/dsu_dyn.ko || exit 1; done; echo 'DSU modules for:$KEEP'" \
  || { echo "the DSU modules could not be installed"; exit 1; }

step "exFAT, ISO 9660 and uinput for each kernel of the image whose configuration leaves them out"
# Found on the board 2026-11-18: a 64 GB stick (exFAT, as most sticks of that size and SDXC cards come) and an
# installer stick (ISO 9660) did not open at all, and bluetoothd could not make the input device for the buttons of
# a Bluetooth speaker or headset ("failed to create uinput"): the BSP's kernel configuration has none of the three.
# tools/build-dsu-modules.sh builds them from the kernel's own code. In /usr/lib/modules/<release>/extra/a7a, indexed
# by depmod, they load as a packaged module does: a mount asks for "fs-exfat" or "fs-iso9660", and /dev/uinput is
# made at boot (modules.devname) and loads uinput when bluetoothd opens it. Every kernel of the image must then find
# all three, from there or from its own package, built for it (depmod's index and each module's vermagic).
rm -rf "$WORK/extra"
for k in $KEEP; do
  mkdir -p "$WORK/extra/$k"
  for f in "$OUT/extra-modules/$k"/*.ko; do [ ! -e "$f" ] || install -m 644 "$f" "$WORK/extra/$k/"; done
  [ -z "$(ls -A "$WORK/extra/$k")" ] || $PRIV copy-dir "$WORK/extra/$k" mroot "usr/lib/modules/$k/extra/a7a"
done
$PRIV chroot-run mroot "for k in $KEEP; do
  [ ! -d /usr/lib/modules/\$k/extra/a7a ] || { chown -R root:root /usr/lib/modules/\$k/extra/a7a && chmod -R u=rwX,go=rX /usr/lib/modules/\$k/extra/a7a; } || exit 1
  depmod -a \$k || exit 1
  for m in fs-exfat fs-iso9660 uinput; do modprobe -S \$k -n -q \$m || { echo \"kernel \$k finds no \$m\"; exit 1; }; done
  for m in exfat isofs uinput; do v=\$(modinfo -k \$k -F vermagic \$m 2>/dev/null | awk '{print \$1}'); [ -z \"\$v\" ] || [ \"\$v\" = \$k ] || { echo \"kernel \$k: \$m is built for \$v\"; exit 1; }; done
done; echo 'exFAT, ISO 9660 and uinput for:$KEEP'" || { echo "exFAT, ISO 9660 and uinput could not be installed"; exit 1; }

step "a fresh home for user radxa, with the vendor's NPU SDK as the assembled image carries it"
# (the SDK is a clone made on the board. Its .git directory is half of its size and holds the log of when it was
# cloned: the files are what the image needs, the history is at https://github.com/ZIFENG278/ai-sdk)
[ ! -d "$OUT/ddk/ai-sdk" ] || $PRIV copy-dir "$OUT/ddk/ai-sdk" mroot home/radxa/ai-sdk
$PRIV chroot-run mroot "rm -rf /home/radxa/ai-sdk/.git; cp -an /etc/skel/. /home/radxa/ 2>/dev/null; chown -R radxa:radxa /home/radxa && chmod 700 /home/radxa && test -f /home/radxa/.bashrc || exit 1; ls -A /home/radxa | tr '\n' ' '; echo" \
  || { echo "the home directory could not be made"; exit 1; }

step "which image this is: /etc/a7a-image-variant (the clock panel takes its memory target from it)"
printf 'variant=%s\ncpu=A76 3000 MHz, A55 2800 MHz\nram=%s MHz\ngpu=1008 MHz\nethernet=RGMII tx-delay 9\n' "$VARIANT" "$RAM" | $PRIV write-file mroot etc/a7a-image-variant

step "first boot: grow the root filesystem to the card, the image's unit presets, no first-run wizard"
# (the marker of the wizard is written anew: the card's holds the time of the owner's first setup. Fedora's user timer
# that tells GRUB a boot succeeded is masked: there is no GRUB environment on this board for it to write)
$PRIV write-file mroot usr/local/sbin/a7a-grow-rootfs < "$P/tools/board/a7a-grow-rootfs"
$PRIV write-file mroot etc/systemd/system/a7a-grow-rootfs.service < "$P/tools/image-files/a7a-grow-rootfs.service"
$PRIV write-file mroot etc/systemd/system-preset/00-a7a.preset < "$P/tools/image-files/00-a7a.preset"
$PRIV chroot-run mroot "chmod +x /usr/local/sbin/a7a-grow-rootfs; systemctl enable a7a-grow-rootfs.service sshd.service serial-getty@ttyAS0.service a7a-firstboot.service >/dev/null 2>&1; : > /etc/plasma-setup-done; systemctl disable plasma-setup.service >/dev/null 2>&1; systemctl --global mask grub-boot-success.timer >/dev/null 2>&1; chmod 555 /boot; test -L /etc/systemd/user/grub-boot-success.timer && test ! -s /etc/plasma-setup-done && test -x /usr/local/sbin/a7a-grow-rootfs && test -e /etc/systemd/system/multi-user.target.wants/a7a-grow-rootfs.service && test -e /etc/systemd/system/multi-user.target.wants/sshd.service && test ! -e /etc/systemd/system/multi-user.target.wants/plasma-setup.service && test ! -e /var/lib/a7a/rootfs-grown && echo ok" \
  || { echo "the first-boot units are not as they must be in the image"; exit 1; }

step "the directories under /var that packages own and the card lacked (tools/board/a7a-var-dirs)"
$PRIV chroot-run mroot "bash -s" < "$P/tools/board/a7a-var-dirs" | tail -2

step "two units that failed at the first boot on a board (2026-11-16): logrotate, lm_sensors"
# psacct's logrotate rule has no "missingok", and its /var/account/pacct is a %ghost file that the card, made without
# Fedora's /var, never had: logrotate.service failed on it as soon as its timer fired (not within an emulator run).
# lm_sensors.service fails where no sensor chip is configured; this board's temperatures are thermal zones
$PRIV chroot-run mroot "if [ -d /var/account ] && [ ! -e /var/account/pacct ]; then install -m 600 /dev/null /var/account/pacct; fi
systemctl disable lm_sensors.service > /dev/null 2>&1; test ! -e /etc/systemd/system/multi-user.target.wants/lm_sensors.service && { [ ! -d /var/account ] || test -f /var/account/pacct; } && echo ok" \
  || { echo "the logrotate and lm_sensors fixes did not apply"; exit 1; }

step "KDE's power slider: the a7a tuned profiles, starting on Balanced"
# Fedora KDE's slider (tuned-ppd) switched tuned between Fedora's profiles: "Performance" held the CPUs at the top all
# the time (the card was on it, so every image until 2026-11-17 started that way), and "Power Save" did the same as
# Balanced on this chip. The a7a-* profiles of a7a-board-tools set what each position means here (a7a-clock power),
# and the clock panel shows the position. Proven on the board 2026-11-17: all three positions read back.
$PRIV chroot-run mroot "f=/etc/tuned/ppd.conf; test -f \$f && test -d /usr/lib/tuned/profiles/a7a-balanced \
  && sed -i '/^\[profiles\]/,/^\[/{s/^power-saver=.*/power-saver=a7a-powersave/; s/^balanced=.*/balanced=a7a-balanced/; s/^performance=.*/performance=a7a-performance/}' \$f \
  && grep -qx 'power-saver=a7a-powersave' \$f && grep -qx 'balanced=a7a-balanced' \$f && grep -qx 'performance=a7a-performance' \$f \
  && echo balanced > /etc/tuned/ppd_base_profile && echo a7a-balanced > /etc/tuned/active_profile && echo ok" \
  || { echo "KDE's power slider could not be pointed at the a7a profiles"; exit 1; }

step "stay on this Fedora release: Discover offers the next one, and nothing of it has run on this board"
# tools/board/dnf/versionlock.toml (proven on the board 2026-11-17): updates within the release resolve, a release
# upgrade fails at dnf's dependency resolution, before anything is downloaded or changed
$PRIV write-file mroot etc/dnf/versionlock.toml < "$P/tools/board/dnf/versionlock.toml" > /dev/null
$PRIV chroot-run mroot "chmod 644 /etc/dnf/versionlock.toml && dnf versionlock list 2>&1 | grep -q 'evr < 45' && echo ok" \
  || { echo "the release lock is not in effect"; exit 1; }

step "the clock at power-on: the last time the board ran, not systemd's build date"
# The board's clock chip (hym8563, with a battery plug, U13) has come up with no valid time at every boot recorded so
# far, warm reboots included ("hym8563: no valid clock/calendar values": its voltage-low flag; 2026-11-18, cause open),
# and the system started at systemd's build date (2026-10-18) until chrony reached the network, half a minute later; rpm refused package signatures "not live until" their date in that window. chronyd -s sets the clock
# from the drift file's time (saved at every shutdown and hourly) when the clock chip has nothing.
$PRIV chroot-run mroot "f=/etc/sysconfig/chronyd; grep -q '^OPTIONS=.* -s' \$f || sed -i 's/^OPTIONS=\"\\(.*\\)\"/OPTIONS=\"\\1 -s\"/' \$f; grep -q '^OPTIONS=\".* -s\"' \$f && echo ok" \
  || { echo "chronyd -s could not be set"; exit 1; }

step "no firmware feed: nothing on this board takes firmware from LVFS"
# Discover asks fwupd for the LVFS catalogue and reports "Failed to download metadata for lvfs" whenever that
# download fails (the owner saw it on the board during the update test, 2026-11-16). This board's boot firmware is
# on the card and is not updated through fwupd.
$PRIV chroot-run mroot "f=/etc/fwupd/remotes.d/lvfs.conf; [ ! -f \$f ] || { sed -i 's/^Enabled=true/Enabled=false/' \$f; grep -q '^Enabled=false' \$f; }" \
  || { echo "the LVFS remote could not be switched off"; exit 1; }

step "grub2 must not probe the board's disks at its next update"
$PRIV chroot-run mroot "mkdir -p /etc/default; touch /etc/default/grub; sed -i '/^GRUB_DISABLE_OS_PROBER=/d' /etc/default/grub; echo 'GRUB_DISABLE_OS_PROBER=true' >> /etc/default/grub; grep -c '^GRUB_DISABLE_OS_PROBER=true' /etc/default/grub"

step "tidy, then what must not be in the image"
# dnf's history starts with the owner of the image: the one the card had stayed behind, and the entry this build made
# by installing the tool packages goes too (dnf must still work without one: asked right here)
$PRIV chroot-run mroot "dnf clean all > /dev/null 2>&1; rm -rf /var/cache/libdnf5/* /var/log/dnf5.log* /var/log/hawkey.log /var/tmp/a7a-* /root/.cache /root/.config /root/.local /usr/lib/sysimage/libdnf5/transaction_history.sqlite*
dnf -q --disablerepo='*' list --installed a7a-extlinux > /dev/null 2>&1 || { echo 'dnf does not work without its history'; exit 1; }
rm -rf /usr/lib/sysimage/libdnf5/transaction_history.sqlite* /var/cache/libdnf5/* /var/log/dnf5.log*; echo tidied" || { echo "tidying failed"; exit 1; }
# leftovers of the experiments on the card, none of them part of a package: backups of files (they name the old
# kernels and, in one boot menu label, the owner), the boot menu's own backup written a moment ago, the kernels'
# 'build' links (they point into the build tree on the build machine, a path with the owner's name in it), and the
# hand-installed helpers in /usr/local/bin (what the assembled image puts there stays)
$PRIV chroot-run mroot 'rm -f /boot/extlinux/extlinux.conf.prev /boot/extlinux/extlinux.conf.new
find /etc /usr/local /boot /opt -xdev \( -name "*.bak-*" -o -name "*.bak" -o -name "*.rpmsave" -o -name "*.rpmorig" \) -print -delete 2>/dev/null | sed "s/^/removed /"
for l in /usr/lib/modules/*/build /usr/lib/modules/*/source; do if [ -L "$l" ] && [ ! -e "$l" ] && ! rpm -qf "$l" > /dev/null 2>&1; then echo "removed the dangling link $l (it pointed into the build tree)"; rm -f "$l"; fi; done
for f in /usr/local/bin/*; do [ -f "$f" ] || continue; rpm -qf "$f" > /dev/null 2>&1 && continue
  case "${f##*/}" in a7a-firstboot|a7a-grow-rootfs) ;; *) rm -f "$f"; echo "removed $f (no package owns it)" ;; esac; done; true'
step "owners: nothing outside /home belongs to a person"
# what was copied onto the card by hand as user radxa (/boot, /opt, firmware directories) kept his user and a group id
# that does not exist in the image
$PRIV chroot-run mroot 'find / -xdev ! -path "/home/*" \( \( -uid +999 -uid -60000 \) -o \( -gid +999 -gid -60000 \) \) -print 2>/dev/null | while IFS= read -r f; do
  echo "root:root now, was $(stat -c %u:%g "$f"): $f"; chown -h root:root "$f"; done | { n=0; while IFS= read -r l; do n=$((n + 1)); [ $n -gt 6 ] || echo "$l"; done; [ $n -le 6 ] || echo "... and $((n - 6)) more"; }
left=$(find / -xdev ! -path "/home/*" \( \( -uid +999 -uid -60000 \) -o \( -gid +999 -gid -60000 \) \) -print 2>/dev/null | head -5 | tr "\n" " "); [ -z "$left" ] || { echo "still a person'"'"'s outside /home: $left"; exit 1; }
left=$(find / -xdev \( -nouser -o -nogroup \) -print 2>/dev/null | head -5 | tr "\n" " "); [ -z "$left" ] || { echo "owned by a user or group the image does not have: $left"; exit 1; }; echo ok' || { echo "the owners in the image are not as they must be"; exit 1; }

# files older than /etc/machine-id (written right after the copy) came from the card; younger ones are this build's
$PRIV chroot-run mroot 'bad=0; chk() { if [ -n "$2" ]; then echo "LEFT IN THE IMAGE: $1: $2"; bad=1; fi; }
chk "ssh host keys" "$(ls /etc/ssh/ssh_host_* 2>/dev/null | tr "\n" " ")"
chk "network connections" "$(ls -A /etc/NetworkManager/system-connections 2>/dev/null | tr "\n" " ")"
chk "authorized_keys or private keys" "$(find /home /root /etc/ssh -xdev \( -name authorized_keys -o -name "id_rsa*" -o -name "id_ed25519*" -o -name "id_ecdsa*" \) 2>/dev/null | tr "\n" " ")"
chk "shell or other histories" "$(find / -xdev \( -name ".*_history" -o -name ".lesshst" -o -name ".viminfo" \) 2>/dev/null | tr "\n" " ")"
chk "files of the card in /var/log, /var/cache, /var/tmp, /var/spool" "$(find /var/log /var/cache /var/tmp /var/spool -xdev -type f ! -newer /etc/machine-id 2>/dev/null | head -8 | tr "\n" " ")"
chk "a machine-id" "$([ "$(cat /etc/machine-id)" = uninitialized ] || cat /etc/machine-id)"
chk "a random seed or credential secret" "$(ls /var/lib/systemd/random-seed /var/lib/systemd/credential.secret 2>/dev/null | tr "\n" " ")"
chk "more in the home of root than the five files of rootfiles" "$(ls -A /root | grep -vxE "\.bashrc|\.bash_profile|\.bash_logout|\.cshrc|\.tcshrc" | tr "\n" " ")"
chk "the grow marker" "$(ls /var/lib/a7a/rootfs-grown 2>/dev/null)"
chk "state of the login screen" "$(ls -A /var/lib/plasmalogin 2>/dev/null | tr "\n" " ")"
chk "another user with a home" "$(awk -F: "\$3 >= 1000 && \$3 < 60000 && \$1 != \"radxa\" {print \$1}" /etc/passwd | tr "\n" " ")"
chk "in the home directory something that is neither from /etc/skel nor an SDK" "$(cd /home/radxa && find . -xdev ! -type d ! -path "./ai-sdk/*" ! -path "./dsu_*/*" 2>/dev/null | while read -r f; do [ -e "/etc/skel/$f" ] || [ -L "/etc/skel/$f" ] || echo "$f"; done | head -8 | tr "\n" " ")"
chk "another directory in /home" "$(ls -A /home | grep -vx radxa | tr "\n" " ")"
chk "an earlier version of the account files (hashes)" "$(ls /etc/passwd- /etc/shadow- /etc/group- /etc/gshadow- /etc/*.old /etc/shadow.* 2>/dev/null | tr "\n" " ")"
chk "a history of dnf commands" "$(ls /usr/lib/sysimage/libdnf5/transaction_history.sqlite* 2>/dev/null | tr "\n" " ")"
chk "a GRUB menu or environment made on the card" "$(ls /boot/grub2/grub.cfg /boot/grub2/grubenv /usr/lib/efi/grub2/*/EFI/fedora/grub.cfg 2>/dev/null | tr "\n" " ")"
chk "an index of file names (plocate, mlocate) or the status of logrotate" "$(ls /var/lib/plocate/*.db /var/lib/mlocate/* /var/lib/logrotate/* 2>/dev/null | tr "\n" " ")"
chk "a host name that is not the image'"'"'s" "$([ "$(cat /etc/hostname)" = radxa-cubie-a7a ] || cat /etc/hostname)"
chk "entries in /etc/hosts besides localhost" "$(grep -vE "^[[:space:]]*(#|$)" /etc/hosts | grep -vE "^(127\.0\.0\.1|::1)[[:space:]]" | head -3 | tr "\n" " ")"
chk "mounts in /etc/fstab that are not the image'"'"'s own partitions" "$(grep -vE "^[[:space:]]*(#|$)" /etc/fstab | grep -vE "^/dev/mmcblk0p[0-9]+[[:space:]]" | head -3 | tr "\n" " ")"
chk "a language or keyboard that is not the image'"'"'s default (en_US.UTF-8, us)" "$(grep -hE "^(LANG|KEYMAP|XKBLAYOUT)=" /etc/locale.conf /etc/vconsole.conf 2>/dev/null | grep -vxE "LANG=\"?en_US\.UTF-8\"?|KEYMAP=\"?us\"?|XKBLAYOUT=\"?us\"?" | tr "\n" " ")"
chk "text in /etc/motd, /etc/exports, /etc/crypttab or /etc/machine-info" "$(for f in /etc/motd /etc/exports /etc/crypttab /etc/machine-info; do [ -s "$f" ] && echo "$f"; done | tr "\n" " ")"
chk "a swap file" "$(ls /swapfile /swap.img /var/swap 2>/dev/null | tr "\n" " ")"
chk "a file name with a control character in it (a line break hides a file from the lists below)" "$(find / -xdev -name "*[[:cntrl:]]*" 2>/dev/null | head -3 | tr "\n\r\t" "   ")"
chk "a printer, a print server or a default printer" "$(grep -lE "^(ServerName|Default)" /etc/cups/client.conf /etc/cups/lpoptions 2>/dev/null | tr "\n" " ")"
chk "extended attributes in the user namespace" "$(command -v getfattr > /dev/null && find / -xdev \( -type f -o -type d \) -print0 2>/dev/null | xargs -0 getfattr -h --absolute-names -m "^user\." 2>/dev/null | grep "^# file: " | head -3 | tr "\n" " ")"
[ $bad = 0 ] && echo "none of it found"; exit $bad' || { echo "the image holds something it must not: see above"; exit 1; }
[ ! -e "$ROOT/etc/resolv.conf" ] && [ ! -L "$ROOT/etc/resolv.conf" ] || { echo "the image has an /etc/resolv.conf: $(ls -la "$ROOT/etc/resolv.conf")"; exit 1; }
[ "$(readlink "$ROOT/etc/localtime")" = ../usr/share/zoneinfo/UTC ] || { echo "the timezone is not UTC"; exit 1; }
# What no package accounts for must be on the list of what is let in (tools/master-allow.rules): three listings from
# inside the image, judged out here by tools/master-audit.py.
# (packages name some files through /lib, /lib64, /bin, /sbin and /usr/sbin, which are links into /usr here)
step "audit: every path that no package accounts for (rpm -Va reads every packaged file: minutes)"
$PRIV chroot-run mroot 'rules=""; for d in /lib /lib64 /bin /sbin /usr/sbin; do [ -L "$d" ] && rules="$rules;s#^([0-9]+ [0-9]+) $d/#\\1 $(readlink -f "$d")/#"; done
rpm -qa --qf "[%{FILEFLAGS} %{FILEVERIFYFLAGS} %{FILENAMES}\n]" > /tmp/o || exit 1; sed -E "${rules#;}" /tmp/o; echo "#done"' > "$WORK/owned.txt"
$PRIV chroot-run mroot 'find / -xdev -printf "%y %n %p\n" > /tmp/p 2>/dev/null; grep -vx "f 1 /etc/resolv.conf" /tmp/p; echo "#done"' > "$WORK/present.txt"
# (rpm -Va ends with a status that says "something differs", which something always does; a status of 128 or more is rpm
# itself dying, and then the listing is not complete)
$PRIV chroot-run mroot 'rc=0; rpm -Va 2>&1 || rc=$?; [ $rc -ge 128 ] || echo "#done"' > "$WORK/rpm-verify.txt"
echo "$(wc -l < "$WORK/owned.txt") paths that packages name, $(wc -l < "$WORK/present.txt") paths in the image, $(wc -l < "$WORK/rpm-verify.txt") lines from rpm -Va"
AUDIT=0; python3 "$P/tools/master-audit.py" --allow "$P/tools/master-allow.rules" --owned "$WORK/owned.txt" --present "$WORK/present.txt" --verify "$WORK/rpm-verify.txt" \
  --lists "$WORK" --complete --min-paths 100000 || AUDIT=$?
$PRIV chroot-run mroot "rpm -qa --qf '%{NAME} %{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\n' > /tmp/q || exit 1; LC_ALL=C sort /tmp/q" > "$WORK/packages.txt"
[ "$(wc -l < "$WORK/packages.txt")" -gt 1500 ] || { echo "the package list of the image is implausibly short"; exit 1; }
df -h "$ROOT" | tail -1
$PRIV trim mroot | tail -1

step "close"
$PRIV umount mroot; $PRIV loop-detach "$LOOP"; LOOP=""
FSOFF=$((679936 * 512))
# ext4 notes in its superblock where it was mounted last: here that is a path on the build machine, with the owner's
# name in it. The board writes "/" there at its first mount; so does this
"$P/tools/ext4-last-mounted.sh" "$B" "$FSOFF" || reject "the last-mounted path in the image's superblock"

step "search the whole image file for the owner's strings"
# A string found only inside a package's own file that rpm has just verified is the package's text
# (pristine-files.txt, written by the audit). The build machine's user and host name are shown where they are and do
# not fail the build: compiled into the kernels and their modules, recorded in the package database
# (docs/RADXA-SYNC-2026-11-15.md 15)
CAN=0; python3 "$P/tools/master-canary.py" "$B" --canaries "$WORK/canaries.txt" --fs-offset "$FSOFF" --pristine "$WORK/pristine-files.txt" \
  --report "$WORK/canary-report.txt" --note "/home/$(id -un)" --note "$(id -un)@" --note "$(uname -n)" || CAN=$?
[ "$AUDIT" = 0 ] || echo "FAILED: paths in the image that nothing accounts for (the audit above)"
[ "$CAN" = 0 ] || echo "FAILED: strings of the owner's are in the image ($WORK/canary-report.txt says where; exit $CAN)"
[ "$AUDIT" = 0 ] && [ "$CAN" = 0 ] || reject "the checks named above"

step "every check passed: the image gets its name"
# sidecars: the default kernel's Image for the emulator, the a7a packages for the emulator's gates, what this build is
KRPM=$(ls "$OUT"/rpms/kernel-a7a*-"${REL_DEF//-/_}"-*.aarch64.rpm 2>/dev/null | tail -1)
mkdir -p "$OUT/kernels"
if [ ! -s "$OUT/kernels/Image-$REL_DEF" ]; then
  [ -n "$KRPM" ] || { echo "no kernel package for $REL_DEF in $OUT/rpms: the emulator needs its Image"; exit 1; }
  rpm2cpio "$KRPM" | cpio -i --quiet --to-stdout "./boot/vmlinuz-$REL_DEF" > "$OUT/kernels/Image-$REL_DEF"
fi
rm -f "$IMG" "$IMG.sha256" "$IMG.kernel" "$IMG.rpms" "$IMG.build" "$IMG.packages"
mv "$B" "$IMG"
cp "$WORK/packages.txt" "$IMG.packages"
echo "$REL_DEF" > "$IMG.kernel"
/usr/bin/grep -E '^(a7a-|mesa-pvr-a7a |mesa-img-a7a |kernel-a7a)' "$IMG.packages" | awk '{v = $2; sub(/^[0-9]+:/, "", v); print $1 "-" v ".rpm"}' > "$IMG.rpms"
case "$REL_DEF" in *-a7apvr.*) STACK=open ;; *) STACK=ddk ;; esac
printf 'default_stack=%s\nssh_keys=0\nvariant=%s\nram_mhz=%s\nsource=%s\n' "$STACK" "$VARIANT" "$RAM" "$(basename "$SRC")" > "$IMG.build"
# the checksum names the file, not the directory it was made in
( cd "$OUT" && sha256sum "$(basename "$IMG")" ) > "$IMG.sha256"
DONE=1
step "done: $IMG ($VARIANT; $(du -h --apparent-size "$IMG" | cut -f1) apparent, $(du -h "$IMG" | cut -f1) on disk); default kernel $REL_DEF"
