#!/bin/bash
# The ONLY thing a7a-fedora runs with sudo. A fixed set of operations, each checked, each logged.
# Intended sudoers line (one script, no password, so builds can run unattended):
#   <your user> ALL=(root) NOPASSWD: <the repository>/tools/priv.sh
# Every operation refuses paths outside the project's output/ and mount root, and refuses to
# write any block device that is not a removable card it has been told about explicitly.
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
OUT=$P/output
MNT=$P/output/mnt
LOG=$OUT/priv.log
op=${1:-}; shift || true
log() { echo "[$(date +%H:%M:%S)] priv $op $*" >> "$LOG"; }
die() { echo "priv: $*" >&2; log "REFUSED: $*"; exit 2; }
under_out() { case "$(readlink -f "$1")" in "$OUT"/*) ;; *) die "$1 is not under $OUT";; esac; }
# inside <path> <mounted image>: the path must still be in the image after the image's own links are followed. These
# writes happen out here, as root, not in the sandbox: on a card whose /home or /var/tmp is an absolute link
# (/home -> /var/home) "home/radxa" under the mount would be THIS machine's /var/home/radxa
inside() { local real; real=$(realpath -m -- "$1") || die "cannot resolve $1"; case "$real/" in "$2"/*) ;; *) die "$1 leads out of the image (to $real): nothing written";; esac; }
[ "$(id -u)" = 0 ] || die "must run as root (via sudo)"
log "$*"
case "$op" in
  loop-attach)      # loop-attach <image> [offset-bytes]  -> prints /dev/loopN (partitions scanned; with an offset: that
                    #  partition only, read-only - for a backup that stops at the end of its last partition)
    under_out "$1"; if [ -n "${2:-}" ]; then [[ "$2" =~ ^[0-9]+$ ]] || die "offset must be a number"; losetup --find --show --read-only --offset "$2" "$1"
    # a read-only flag set on a whole loop device (mount ... ro of an image without partitions) outlives its detach
    else L=$(losetup --find --show --partscan "$1"); blockdev --setrw "$L" 2>/dev/null || true; echo "$L"; fi ;;
  loop-detach)      # loop-detach /dev/loopN
    [[ "$1" =~ ^/dev/loop[0-9]+$ ]] || die "not a loop device: $1"; losetup -d "$1" ;;
  mkfs)             # mkfs <ext4|vfat> <label> /dev/loopNpM
    fs=$1; label=$2; dev=$3; [[ "$dev" =~ ^/dev/loop[0-9]+p[0-9]+$ ]] || die "not a loop partition: $dev"
    case "$fs" in ext4) mkfs.ext4 -q -F -L "$label" "$dev";; vfat) mkfs.vfat -F 32 -n "$label" "$dev";; *) die "fs $fs";; esac ;;
  mount)            # mount <dev> <name> [options]  -> mounts at output/mnt/<name>
                    #  With "ro" among the options (ro, ro=fs, ...) the DEVICE is made read-only first, until umount.
                    #  "ro" alone is about the mount: an ext4 that was not unmounted cleanly is still written to when its
                    #  device is writable (the journal replay; with noload the orphan cleanup WITHOUT the replay; after a
                    #  failed mount an error record in the superblock). 2026-11-15: "ro,noload" damaged the working card
                    #  that way. The flag is what ext4, btrfs, xfs and vfat ask before they write, not a barrier in the
                    #  block layer, and it covers this helper's mounts only (not a file manager, not a plain mount)
    dev=$1; name=$2; opts=${3:-}
    if [[ "$dev" =~ ^/dev/(sd[a-z])([0-9]+)$ ]]; then disk=${BASH_REMATCH[1]}; [ "$(cat /sys/block/$disk/removable 2>/dev/null)" = 1 ] || die "$dev is not on a removable card"
    elif [[ "$dev" =~ ^/dev/(mmcblk[0-9]+)p[0-9]+$ ]]; then disk=${BASH_REMATCH[1]}; [ "$(cat /sys/block/$disk/removable 2>/dev/null)" = 1 ] || die "$dev is not on a removable card"
    else [[ "$dev" =~ ^/dev/loop[0-9]+(p[0-9]+)?$ ]] || die "not a loop device/partition or a removable card partition: $dev"; fi
    [[ "$name" =~ ^[a-z0-9_-]+$ ]] || die "bad mount name"
    case ",$opts," in *,remount,*) die "remount is not offered: umount, then mount again" ;; esac
    # one mount or umount at a time: an umount elsewhere hands back every device that nothing has mounted, and a
    # device whose mount is still on its way is exactly that
    mkdir -p "$MNT"; exec 9>>"$MNT/.lock"; flock -w 120 9 || die "another mount or umount of this helper has been running for two minutes"
    ro=0; guard=0; marker=$MNT/.ro-${dev##*/}
    case ",$opts," in *,ro,*|*,ro=*) ro=1
      state=$(blockdev --getro "$dev") || die "cannot read the read-only state of $dev: not mounting it"
      case "$state" in
        1) ;;      # read-only already: a write-protected card, a read-only loop, an earlier ro mount of the same device
        0) m=0; findmnt -n -S "$dev" >/dev/null 2>&1 || m=$?
           [ "$m" != 0 ] || die "$dev is mounted already, and writable: a read-only mount of it would not be a read-only look"
           [ "$m" = 1 ] || die "cannot tell whether $dev is mounted (findmnt exit $m): not mounting it"
           echo "$dev" > "$marker" || die "cannot write $marker: not mounting $dev"      # first the note for umount, then the flag
           log "setro $dev"
           blockdev --setro "$dev" && [ "$(blockdev --getro "$dev")" = 1 ] || { rm -f "$marker"; die "could not make $dev read-only: not mounting it"; }
           guard=1 ;;
        *) die "blockdev --getro $dev answered '$state', not 0 or 1: not mounting it" ;;
      esac ;;
    esac
    mkdir -p "$MNT/$name"; rc=0; lost=0
    if [ -n "$opts" ]; then mount -o "$opts" "$dev" "$MNT/$name" || rc=$?; else mount "$dev" "$MNT/$name" || rc=$?; fi
    # the flag must still stand now that the filesystem is up (a partition table re-read in between resets it)
    if [ "$rc" = 0 ] && [ "$ro" = 1 ] && [ "$(blockdev --getro "$dev" 2>/dev/null)" != 1 ]; then lost=1; rc=1; umount "$MNT/$name" || true; fi
    if [ "$rc" != 0 ]; then
      if [ "$guard" = 1 ]; then log "setrw $dev (not mounted)"; blockdev --setrw "$dev" || true; rm -f "$marker"; fi
      if [ "$lost" = 1 ]; then echo "priv: $dev was WRITABLE again when its filesystem came up (a partition table re-read?): unmounted. It may have been written to; read the kernel log (journalctl -k) before trusting it" >&2
      elif [ "$guard" = 1 ]; then
        hint=" An ext4 that was not unmounted cleanly (a card pulled from a running board) mounts only with ro,noload here; repairing it (e2fsck) belongs on a copy first."
        case ",$opts," in *,noload,*) hint="" ;; esac
        echo "priv: $dev was read-only for this attempt: the mount wrote nothing to it.$hint The kernel log (journalctl -k) has the reason" >&2; fi
      rmdir "$MNT/$name" 2>/dev/null || true; exit "$rc"
    fi
    echo "$MNT/$name" ;;
  umount)           # umount <name>   (a device that mount made read-only is writable again once nothing has it mounted)
    [[ "$1" =~ ^[a-z0-9_-]+$ ]] || die "bad mount name"
    mkdir -p "$MNT"; exec 9>>"$MNT/.lock"; flock -w 120 9 || die "another mount or umount of this helper has been running for two minutes"
    rc=0; umount "$MNT/$1" || rc=$?; rmdir "$MNT/$1" 2>/dev/null || true
    # also after an umount that failed (the mount went away without this helper): what nothing has mounted is handed back
    for f in "$MNT"/.ro-*; do [ -f "$f" ] || continue; dev=/dev/${f##*/.ro-}
      [[ "$dev" =~ ^/dev/(sd[a-z][0-9]+|mmcblk[0-9]+p[0-9]+|loop[0-9]+(p[0-9]+)?)$ ]] || { rm -f "$f"; continue; }
      m=0; findmnt -n -S "$dev" >/dev/null 2>&1 || m=$?
      [ "$m" = 1 ] || continue      # 0: still mounted under another name; anything else: findmnt cannot tell, the guard stays
      log "setrw $dev"; blockdev --setrw "$dev" 2>/dev/null || true; rm -f "$f"; done
    exit "$rc" ;;
  copy-root)        # copy-root <from-mount-name> <to-mount-name>   (rsync, preserving everything incl. xattrs/SELinux)
    from=$MNT/$1; to=$MNT/$2; [ -d "$from" ] && [ -d "$to" ] || die "mounts missing"
    rsync -aAXH --numeric-ids --info=progress2 --exclude='/boot/*' --exclude='/proc/*' --exclude='/sys/*' --exclude='/dev/*' --exclude='/run/*' "$from/" "$to/" ;;
  copy-system)      # copy-system <from-mount-name> <to-mount-name> <rules file>   (the whole system of a card that has been
                    #  in use into a NEW filesystem, minus what the rsync filter rules name: tools/master-filter.rules plus
                    #  what tools/master-image.sh appends. A path the rules exclude is never read. The source must be a
                    #  read-only mount: the copy must not be able to change the backup it reads)
    [[ "${1:-}" =~ ^[a-z0-9_-]+$ ]] && [[ "${2:-}" =~ ^[a-z0-9_-]+$ ]] || die "bad mount name"
    from=$MNT/$1; to=$MNT/$2; [ -n "${3:-}" ] || die "no rules file given"
    rules=$(readlink -f "$3"); case "$rules" in "$P"/tools/*|"$OUT"/*) ;; *) die "the rules file $3 is not under $P/tools or $OUT";; esac
    [ -f "$rules" ] || die "no such rules file: $rules"
    mountpoint -q "$from" && mountpoint -q "$to" || die "mounts missing ($from, $to)"
    findmnt -n -o OPTIONS "$from" | grep -qE '(^|,)ro(,|$)' || die "$from is not mounted read-only"
    [ -z "$(ls -A "$to" | grep -vx 'lost+found')" ] || die "$to is not empty: the copy goes into a new filesystem"
    rsync -aAXHx --numeric-ids --info=progress2 --filter="merge $rules" "$from/" "$to/" ;;
  trim)             # trim <name>   (fstrim: what is free in the filesystem goes back to the image file as holes, so
                    #  nothing that was written and deleted there stays readable in the image)
    [[ "${1:-}" =~ ^[a-z0-9_-]+$ ]] || die "bad mount name"; mountpoint -q "$MNT/$1" || die "mount missing: $MNT/$1"
    fstrim -v "$MNT/$1" ;;
  install-payload)  # install-payload <to-mount-name>   (kernel Image, dtb, modules, firmware from output/payload)
    to=$MNT/$1; [ -d "$to" ] || die "mount missing"; PAY=$OUT/payload; REL=$(cat "$PAY/kernelrelease")
    for d in boot/extlinux "usr/lib/linux-image-$REL/allwinner" lib/modules lib/firmware; do inside "$to/$d" "$to"; done
    mkdir -p "$to/boot/extlinux" "$to/usr/lib/linux-image-$REL/allwinner" "$to/lib/modules" "$to/lib/firmware"
    cp "$PAY/boot/Image-$REL" "$to/boot/vmlinuz-$REL"
    cp "$PAY"/boot/dtb/allwinner/*.dtb "$to/usr/lib/linux-image-$REL/allwinner/"
    cp -a "$PAY/lib/modules/$REL" "$to/lib/modules/"
    cp -a "$PAY/lib/firmware/." "$to/lib/firmware/"
    echo "$REL" ;;
  copy-dir)         # copy-dir <dir under output/> <to-mount-name> <relative dest>   (rsync -a; for the DDK, firmware, SDKs)
    src=$1; to=$MNT/$2; rel=$3; under_out "$src"; [ -d "$src" ] || die "not a directory: $src"; [ -d "$to" ] || die "mount missing"
    case "$rel" in /*|*..*) die "bad relative path";; esac; inside "$to/$rel" "$to"; mkdir -p "$to/$rel"; rsync -a "$src/" "$to/$rel/"; echo "copied $(basename "$src") -> $rel" ;;
  write-file)       # write-file <to-mount-name> <relative path> < content on stdin   (etc files, extlinux.conf, fstab, services)
    to=$MNT/$1; rel=$2; [ -d "$to" ] || die "mount missing"; case "$rel" in /*|*..*) die "bad relative path";; esac
    inside "$to/$rel" "$to"; mkdir -p "$(dirname "$to/$rel")"; cat > "$to/$rel"; echo "wrote $rel" ;;
  chroot-run)       # chroot-run <to-mount-name> [--cache <dir under output/>] <command...>   (arm64 via binfmt; for systemctl
                    #  mask, useradd, passwd, ldconfig, dnf). The command runs inside the mounted image in a sandbox
                    #  (bubblewrap): a /dev, /proc, /sys, /run and /tmp of its own, its own process tree, no right to
                    #  mount or to make device nodes. Until 2026-11-16 it was a bare chroot with THIS machine's /dev, /sys
                    #  and /proc bound in, and a `dnf upgrade` in the image ran grub2's os-prober over every disk of the
                    #  laptop: the owner's USB stick mounted read-write twice, the Fedora source image modified, his SD
                    #  card read. Nothing is mounted outside the sandbox, so nothing can be left mounted either.
                    #  --cache: that directory is dnf's package cache for the command (2 GiB of downloads neither fill
                    #  the image nor are fetched twice)
    name=${1:-}; [[ "$name" =~ ^[a-z0-9_-]+$ ]] || die "bad mount name: '$name'"
    to=$MNT/$name; shift; [ -d "$to" ] || die "mount missing: $to"
    mountpoint -q "$to" || die "$to is not a mounted filesystem (mount missing, or the leftover of a failed mount)"
    cache=""
    if [ "${1:-}" = --cache ]; then
      [ -n "${2:-}" ] || die "--cache needs a directory under $OUT"
      under_out "$2"; cache=$(readlink -f "$2"); shift 2
      case "$cache" in "$MNT"|"$MNT"/*) die "the cache $cache lies in the mount root $MNT: it would be the image itself";; esac
      [ -d "$cache" ] || die "no such cache directory: $cache"
    fi
    [ $# -ge 1 ] || die "no command given"
    command -v bwrap >/dev/null 2>&1 || die "bubblewrap (bwrap) is not installed: without it a command in the image would see this machine's disks"
    # name resolution for the time of the command only, and only where the system has none. Fedora's image has no
    # /etc/resolv.conf (tmpfiles links it to systemd-resolved's at the first boot); the copy this helper used to leave
    # is a plain file that NetworkManager then manages instead, and every image until 2026-11-15 carried the build
    # machine's ("search home"). A system's own file (a card that has been in use) was overwritten the same way
    res=""; bp=""
    tidy() { [ -z "$res" ] || rm -f "$res"; res=""; }
    # a signal ends the command (the sandbox dies with bwrap) and the copy still goes; bash runs a trap only between
    # commands, so bwrap is waited for, not run in the foreground
    stop() { [ -z "$bp" ] || { kill -TERM "$bp" 2>/dev/null; wait "$bp" 2>/dev/null; }; tidy; exit 143; }
    trap tidy EXIT; trap stop INT TERM HUP
    if [ ! -e "$to/etc/resolv.conf" ] && [ ! -L "$to/etc/resolv.conf" ]; then cp -L /etc/resolv.conf "$to/etc/resolv.conf" 2>/dev/null && res=$to/etc/resolv.conf || true; fi
    box=(--bind "$to" / --dev /dev --proc /proc --tmpfs /sys --tmpfs /run --tmpfs /tmp
         --unshare-pid --unshare-ipc --unshare-uts --hostname a7a-image --die-with-parent --new-session
         --cap-drop CAP_SYS_ADMIN --cap-drop CAP_MKNOD --cap-drop CAP_SYS_RAWIO --cap-drop CAP_SYS_MODULE --cap-drop CAP_SYS_BOOT --cap-drop CAP_SYS_TIME
         --chdir / --clearenv --setenv PATH /usr/sbin:/usr/bin:/sbin:/bin --setenv HOME /root --setenv LANG C.UTF-8 --setenv TERM dumb)
    if [ -n "$cache" ]; then mkdir -p "$to/var/cache/libdnf5"; box+=(--bind "$cache" /var/cache/libdnf5); fi
    # the command may fail (the build's checks are meant to be able to): its exit code is the helper's
    rc=0; bwrap "${box[@]}" /bin/bash -c "$*" <&0 & bp=$!
    wait "$bp" || rc=$?
    bp=""; tidy
    # the cache goes back to the owner of output/, who must be able to look into it and empty it
    [ -z "$cache" ] || chown -R "$(stat -c %U:%G "$OUT")" "$cache" 2>/dev/null || true
    exit $rc ;;
  dd-firmware)      # dd-firmware <image>   (the 16 MB boot firmware region at offset 0, not touching the rest)
    under_out "$1"; dd if="$OUT/sd-firmware-region.img" of="$1" bs=1M conv=notrunc status=none; echo "firmware written" ;;
  inspect)          # inspect <device>   (read-only: what is on a card - partition table, filesystems, the GPT signature)
    dev=$1; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    echo "model: $(cat /sys/block/$(basename "$dev")/device/model 2>/dev/null) removable: $(cat /sys/block/$(basename "$dev")/removable) size: $(blockdev --getsize64 "$dev") bytes"
    sfdisk -l "$dev" 2>&1 | head -20; blkid "$dev"* 2>/dev/null
    for p in "$dev"*[0-9]; do [ "$(blkid -o value -s TYPE "$p" 2>/dev/null)" = ext4 ] && echo "$p ext4 filesystem: $(( $(dumpe2fs -h "$p" 2>/dev/null | awk -F: '/^Block count/{c=$2} /^Block size/{b=$2} END{print c*b}') / 1000000000 )) GB in a $(( $(blockdev --getsize64 "$p") / 1000000000 )) GB partition"; done
    echo "sector 0 (MBR) tail:"; dd if="$dev" bs=512 count=1 status=none | tail -c 66 | xxd | head -5
    echo "sector 1 (GPT header):"; dd if="$dev" bs=512 skip=1 count=1 status=none | xxd | head -3
    for n in 2 3; do t=$(sfdisk --part-type "$dev" $n 2>/dev/null | tr a-f A-F); [ "$t" = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" ] && echo "p$n type: EFI System (U-Boot scans it)" || echo "WARNING: p$n type is $t, not EFI System: U-Boot will not scan it for extlinux.conf (priv.sh boot-type $dev fixes it)"; done
    echo "GPT entries (LBA 2..33):"; dd if="$dev" bs=512 skip=2 count=32 status=none | python3 -c '
import sys,struct
d=sys.stdin.buffer.read()
for i in range(0,len(d),128):
    e=d[i:i+128]
    if len(e)<128 or e[:16]==bytes(16): continue
    first,last=struct.unpack_from("<QQ",e,32); name=e[56:128].decode("utf-16-le","replace").split("\0")[0]
    print(f"  p{i//128+1}: sectors {first}-{last} ({(last-first+1)*512/1e9:.1f} GB) {name!r}")'
    echo "GPT header says the disk ends at LBA $(dd if="$dev" bs=512 skip=1 count=1 status=none | python3 -c 'import sys,struct;print(struct.unpack_from("<Q",sys.stdin.buffer.read(),32)[0]+1)') ($(dd if="$dev" bs=512 skip=1 count=1 status=none | python3 -c 'import sys,struct;print(round((struct.unpack_from("<Q",sys.stdin.buffer.read(),32)[0]+1)*512/1e9,1))') GB); this reader exposes $(blockdev --getsize64 "$dev") bytes"
    echo "read at 20 GB: $(dd if="$dev" bs=1M skip=20480 count=1 status=none 2>&1 | wc -c) bytes; read at 21 GB: $(dd if="$dev" bs=1M skip=21504 count=1 status=none 2>&1 | wc -c) bytes" ;;
  restore-firmware) # restore-firmware <device>   (rewrite the 16 MB vendor boot firmware region (boot0 at 128 KB, U-Boot) from
                    #  output/sd-firmware-region.img, keeping the card's own GPT (sectors 0-33); reads it back)
    dev=$1; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    rm=$(cat "/sys/block/$(basename "$dev")/removable" 2>/dev/null || echo 0); [ "$rm" = 1 ] || die "$dev is not removable"
    grep -q "^$dev" /proc/mounts && die "$dev has mounted partitions"; [ -f "$OUT/sd-firmware-region.img" ] || die "no sd-firmware-region.img"
    # any partition that was put onto the firmware area (systemd-repart does this to "free" space) goes first
    for n in $(sfdisk -d "$dev" 2>/dev/null | awk '/^\/dev\// { if (match($0, /start= *[0-9]+/)) { st=substr($0, RSTART+6, RLENGTH-6)+0; if (st < 32768) { n=$1; sub(/^.*[^0-9]/, "", n); print n } } }'); do
      sfdisk -q --no-reread --delete "$dev" "$n" && echo "deleted partition $n (it sat on the boot firmware region)"; done
    partprobe "$dev" 2>/dev/null || true
    dd if="$OUT/sd-firmware-region.img" of="$dev" bs=512 skip=34 seek=34 count=$((32768-34)) conv=fsync,notrunc status=none
    cmp -i 17408 -n $((16777216-17408)) "$OUT/sd-firmware-region.img" "$dev" && echo "firmware region restored and verified (sectors 34-32767)" || die "verification failed" ;;
  fsck-rootfs)      # fsck-rootfs <device>   (e2fsck -f -y on partition 3 after a crash; the card must not be mounted)
    dev=$1; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    grep -q "^$dev" /proc/mounts && die "$dev has mounted partitions"; part="${dev}3"; [[ "$dev" == /dev/mmcblk* ]] && part="${dev}p3"
    e2fsck -f -y "$part" 2>&1 | tail -4; echo "e2fsck exit ${PIPESTATUS[0]}" ;;
  read-card)        # read-card <device> <out.img> [bytes]   (back up a removable card into output/, before flashing over it;
                    #  bytes = how much to read, normally the end of the last partition; default the whole card)
    dev=$1; out=$2; limit=${3:-}; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    rm=$(cat "/sys/block/$(basename "$dev")/removable" 2>/dev/null || echo 0); [ "$rm" = 1 ] || die "$dev is not removable"
    case "$(readlink -f "$(dirname "$out")")" in "$OUT"|"$OUT"/*) ;; *) die "$out is not under $OUT";; esac
    if [ -n "$limit" ]; then [[ "$limit" =~ ^[0-9]+$ ]] || die "bytes must be a number"; dd if="$dev" of="$out" bs=4M count=$(( (limit + 4194303) / 4194304 )) status=progress conv=fsync
    else dd if="$dev" of="$out" bs=4M status=progress conv=fsync; fi
    chown "$(stat -c %U:%G "$OUT")" "$out"; echo "read $(stat -c %s "$out") bytes" ;;
  boot-type)        # boot-type <device>   (HARDWARE.md: partition 3 must be GPT type EF00 or U-Boot never scans it for extlinux.conf;
                    #  checks p2 and p3 and sets them to EFI System when they are not; metadata only, no data touched)
    dev=$1; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    rm=$(cat "/sys/block/$(basename "$dev")/removable" 2>/dev/null || echo 0); [ "$rm" = 1 ] || die "$dev is not removable"
    grep -q "^$dev" /proc/mounts && die "$dev has mounted partitions"
    for n in 2 3; do t=$(sfdisk --part-type "$dev" $n 2>/dev/null | tr a-f A-F)
      if [ "$t" = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" ]; then echo "p$n type EFI System (ok)"
      else sfdisk -q --no-reread --part-type "$dev" $n C12A7328-F81F-11D2-BA4B-00A0C93EC93B || die "could not set the type of p$n"; echo "p$n type was $t: set to EFI System"; fi; done
    partprobe "$dev" 2>/dev/null || true; sfdisk -l "$dev" 2>/dev/null | grep -E "^/dev" ;;
  grow-rootfs)      # grow-rootfs <device>   (after a flash: partition 3 to the end of the card, then the ext4 inside it)
    dev=$1; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"; [ -b "$dev" ] || die "no such device"
    rm=$(cat "/sys/block/$(basename "$dev")/removable" 2>/dev/null || echo 0); [ "$rm" = 1 ] || die "$dev is not removable"
    grep -q "^$dev" /proc/mounts && die "$dev has mounted partitions"
    part="${dev}3"; [[ "$dev" == /dev/mmcblk* ]] && part="${dev}p3"; [ -b "$part" ] || die "no partition 3 on $dev"
    sfdisk --relocate gpt-bak-std "$dev" >/dev/null 2>&1 || true
    echo ", +" | sfdisk -q --no-reread --wipe never --wipe-partitions never -N 3 "$dev" || die "sfdisk could not grow partition 3"
    partprobe "$dev" 2>/dev/null || true; sleep 1
    e2fsck -f -p "$part" >/dev/null 2>&1 || e2fsck -f -y "$part" >/dev/null 2>&1 || die "e2fsck failed on $part"
    resize2fs "$part" 2>&1 | grep -E "resiz|Nothing|blocks long" | tail -1
    echo "rootfs: $(blockdev --getsize64 "$part") bytes ($(( $(blockdev --getsize64 "$part") / 1000000000 )) GB)"; sfdisk -l "$dev" 2>/dev/null | grep -E "^/dev" ;;
  flash)            # flash <image> <device>   (refuses non-removable, mounted, or system disks; verifies after writing)
    img=$1; dev=$2; under_out "$img"; [[ "$dev" =~ ^/dev/(sd[a-z]|mmcblk[0-9]+)$ ]] || die "not a whole disk: $dev"
    [ -b "$dev" ] || die "no such device"; rm=$(cat "/sys/block/$(basename "$dev")/removable" 2>/dev/null || echo 0)
    [ "$rm" = 1 ] || die "$dev is not removable"; grep -q "^$dev" /proc/mounts && die "$dev has mounted partitions"
    findmnt -n -o SOURCE / | grep -q "$(basename "$dev")" && die "$dev holds the root filesystem"
    size=$(blockdev --getsize64 "$dev"); isz=$(stat -c %s "$img"); [ "$size" -ge "$isz" ] || die "device smaller than image"
    # erase what the image will not overwrite of the old layout: the backup GPT at the end of the card
    dd if=/dev/zero of="$dev" bs=1M seek=$(( size / 1048576 - 1 )) count=1 conv=fsync status=none; echo "old backup GPT at the end of the card zeroed"
    dd if="$img" of="$dev" bs=4M status=progress conv=fsync; sync
    cmp -n "$isz" "$img" "$dev" && echo "verified $isz bytes" || die "verification failed"
    # the image's backup GPT sits at the image's end; move it to the card's end so the table matches the card (and p3 can be grown)
    if sfdisk --relocate gpt-bak-std "$dev" >/dev/null 2>&1; then echo "backup GPT relocated to the end of the card"; else echo "backup GPT not relocated (sfdisk --relocate unsupported)"; fi
    for n in 2 3; do t=$(sfdisk --part-type "$dev" $n 2>/dev/null | tr a-f A-F); [ "$t" = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B" ] || { sfdisk -q --no-reread --part-type "$dev" $n C12A7328-F81F-11D2-BA4B-00A0C93EC93B && echo "p$n type was $t: set to EFI System (U-Boot only scans EFI-typed partitions)"; }; done
    partprobe "$dev" 2>/dev/null || true; sfdisk -l "$dev" 2>/dev/null | grep -E "^/dev|^Disk /dev" ;;
  *) die "unknown operation '$op' (loop-attach loop-detach mkfs mount umount copy-root copy-system trim install-payload copy-dir write-file chroot-run dd-firmware inspect read-card restore-firmware fsck-rootfs flash grow-rootfs boot-type)" ;;
esac
