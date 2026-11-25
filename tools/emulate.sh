#!/bin/bash
# Boot an image in qemu-system-aarch64 -M virt and watch the serial console for milestones.
#   tools/emulate.sh stock    <fedora.raw>          # Fedora's own kernel via EDK2: proves the rootfs
#   tools/emulate.sh kernel   <a7a image.img>       # the image's default kernel (virtio built in), root=/dev/vda3: the Image
#                                                   # comes from <image>.kernel + output/kernels/ (written by assemble-image.sh),
#                                                   # or from output/payload for an image without that sidecar
#   tools/emulate.sh interactive <image> [stock]    # leaves the console attached, ssh on 127.0.0.1:2222
# Output: output/emulate-<mode>-<time>.log (the whole serial transcript) and a verdict line.
set -uo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output; PAY=$OUT/payload
mode=${1:?mode}; img=${2:?image}; shift 2
TIMEOUT=${TIMEOUT:-600}
stamp=$(date +%H%M%S); log=$OUT/emulate-$mode-$stamp.log
EFI=/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw
cp -f "$img" /dev/null 2>/dev/null; [ -f "$img" ] || { echo "no image $img"; exit 2; }
# -snapshot: QEMU writes to a temporary file, the image itself is never modified by a test boot
# virtio-gpu + a local VNC display so sddm/Plasma have a DRM device (view with: vncviewer 127.0.0.1:5)
# 8 vCPUs = the A733's eight cores: the image pins KWin to cpu6-7 (plasma-kwin_wayland.service CPUAffinity=6-7, Item
# E), and with fewer CPUs systemd refuses to start it (215/CPUAFFINITY) and every Plasma client dumps core
SMP=${A7A_EMU_SMP:-8}
# Offline = restrict=on: the guest gets an address and nothing else (no gateway, no DNS, nothing routed out, the host
# unreachable), ssh from here still works. The backup of a card that has been in use is a copy of a real machine and
# must not go online as that machine: an image under output/backups boots offline by itself, and only
# A7A_EMU_OFFLINE=0 changes that. An image test boot keeps its network unless A7A_EMU_OFFLINE=1
case "${A7A_EMU_OFFLINE-auto}" in
  1) offline=1 ;; 0) offline=0 ;;
  auto) case "$(readlink -f "$img")" in "$OUT"/backups/*) offline=1 ;; *) offline=0 ;; esac ;;
  *) echo "A7A_EMU_OFFLINE must be 0 or 1 (unset: offline for an image under output/backups), not '$A7A_EMU_OFFLINE'"; exit 2 ;;
esac
if [ "$offline" = 1 ]; then NET=user,id=net0,restrict=on; netmode="offline: the guest reaches nothing outside"; else NET=user,id=net0; netmode="network open"; fi
common=(-M virt -cpu cortex-a72 -smp "$SMP" -m 3G -nographic -snapshot -device virtio-gpu-pci -vnc 127.0.0.1:5
        -drive "file=$img,format=raw,if=none,id=hd0" -device virtio-blk-pci,drive=hd0
        -netdev "$NET,hostfwd=tcp:127.0.0.1:2222-:22" -device virtio-net-pci,netdev=net0
        -device virtio-rng-pci -rtc base=utc)
case "$mode" in
  stock|interactive-stock)
    vars=$OUT/emulate-vars-$stamp.fd; cp /usr/share/edk2/aarch64/vars-template-pflash.raw "$vars" 2>/dev/null || truncate -s 64M "$vars"
    args=(-drive "if=pflash,format=raw,readonly=on,file=$EFI" -drive "if=pflash,format=raw,file=$vars") ;;
  kernel|interactive)
    if [ -f "$img.kernel" ]; then REL=$(cat "$img.kernel"); KIMG=$OUT/kernels/Image-$REL
    else REL=$(cat "$PAY/kernelrelease"); KIMG=$PAY/boot/Image-$REL; fi
    [ -f "$KIMG" ] || { echo "no kernel image $KIMG for release $REL (assemble-image.sh writes <image>.kernel + output/kernels/)"; exit 2; }
    echo "kernel $REL from $KIMG"
    args=(-kernel "$KIMG" -append "root=/dev/vda3 rootwait rootfstype=ext4 console=ttyAMA0,115200 loglevel=7 selinux=0 systemd.unified_cgroup_hierarchy=1") ;;
  *) echo "mode?"; exit 2 ;;
esac
SSHPASS=$(command -v sshpass || echo /home/linuxbrew/.linuxbrew/bin/sshpass)
# password auth only: the image now carries the builder's public keys, and ssh offering a passphrase-protected private key
# would sit at "Enter passphrase" forever (sshpass only answers password prompts)
SSH="$SSHPASS -p radxa ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o LogLevel=ERROR -o PubkeyAuthentication=no -o PreferredAuthentications=password -o NumberOfPasswordPrompts=1 radxa@127.0.0.1"
checks() {   # after the login prompt: what the userspace can do, over ssh (user radxa / radxa)
  local i; for i in $(seq 1 24); do $SSH true >/dev/null 2>&1 && break; sleep 5; done
  if ! $SSH true >/dev/null 2>&1; then echo "  check: sshd = unreachable after 120s"; return; fi
  echo "  check: sshd = listening"
  echo "  check: uname = $($SSH uname -r 2>/dev/null)"
  echo "  check: model = $($SSH 'cat /proc/device-tree/model 2>/dev/null | tr -d "\0"')"
  echo "  check: root = $($SSH 'findmnt -n -o SOURCE,FSTYPE /' 2>/dev/null)"
  echo "  check: dnf = $($SSH 'dnf --version 2>/dev/null | head -1')"
  echo "  check: plasma = $($SSH 'rpm -q plasma-desktop 2>/dev/null')"
  echo "  check: modules_loaded = $($SSH 'lsmod | tail -n +2 | wc -l' 2>/dev/null)"
  # logrotate runs from a timer that does not fire within a test boot: on the board it failed on the first day
  # (2026-11-16, a file its rules expect was missing) while every emulator run had shown 0 failed units. Run it once,
  # before the failed units are counted
  echo "  check: logrotate = $($SSH 'sudo systemctl start logrotate.service > /dev/null 2>&1; systemctl is-failed logrotate.service; sudo journalctl -b -u logrotate.service --no-pager -o cat 2>/dev/null | grep -m1 -i "^error" | cut -c1-120' 2>/dev/null | tr "\n" " ")"
  echo "  check: failed_units = $($SSH 'systemctl --failed --no-legend | wc -l' 2>/dev/null)"
  $SSH 'systemctl --failed --no-legend' 2>/dev/null | sed 's/^/    failed: /'
  echo "  check: graphical_target = $($SSH 'systemctl is-active graphical.target' 2>/dev/null)"
  echo "  check: display_manager = $($SSH 'systemctl is-active display-manager 2>/dev/null; systemctl show -p Id --value display-manager 2>/dev/null' | tr "\\n" " ")"
  echo "  check: drm = $($SSH 'ls /dev/dri 2>/dev/null | tr "\n" " "')"
  echo "  check: sleep_masked = $($SSH 'systemctl is-enabled sleep.target suspend.target 2>&1 | tr "\n" " "')"
  echo "  check: repart_masked = $($SSH 'systemctl is-enabled systemd-repart.service 2>&1')"   # unmasked, it partitions the boot firmware area
  echo "  check: nftables = $($SSH 'sudo nft list tables 2>&1 | head -2 | tr "\n" ";"')"
  echo "  check: firstboot = $($SSH 'head -1 /var/log/a7a-firstboot.log 2>/dev/null')"
  # the first-boot grow step (tools/board/a7a-grow-rootfs): its marker, and how big / is (an image booted at its own
  # size has nothing to grow; boot a copy enlarged with truncate to see it grow)
  echo "  check: rootfs_grow = $($SSH 'cut -d" " -f2- /var/lib/a7a/rootfs-grown 2>/dev/null; systemctl is-failed a7a-grow-rootfs.service 2>/dev/null | grep -x failed')"
  echo "  check: root_size = $($SSH 'findmnt -n -o SIZE /' 2>/dev/null)"
  # who has a session on the seat at the first boot: the greeter (plasmalogin) is expected; a plasma-setup user here is
  # KDE's initial-setup wizard sitting in front of the login screen (docs/NEXT-SESSION-PROMPT.md, open since 2026-09-10)
  echo "  check: sessions = $($SSH 'loginctl list-sessions --no-legend 2>/dev/null | awk "{print \$3 \":\" \$4 \":\" \$6}" | sort -u | tr "\n" " "')"
  echo "  check: setup_wizard = $($SSH 'rpm -q plasma-setup 2>/dev/null | head -1; systemctl is-enabled plasma-setup.service 2>&1 | head -1; ls /etc/plasma-setup-done /var/lib/plasma-setup 2>/dev/null | tr "\n" " "')"
  # the a7a-fedora packages (docs/PACKAGING-2026-09-09.md): all of them installed, the image's default kernel (open
  # stack or vendor: its .kernel sidecar) the extlinux default with the other one as entry 2, the whole menu in order
  # (an image built with earlier kernels has them at 3 and 4), Fedora's PowerVR ICD masked in favour of /usr/local's,
  # the dnf kernel guard
  echo "  check: a7a_packages = $($SSH 'rpm -q --qf "%{NAME}-%{VERSION} " a7a-extlinux a7a-board-tools a7a-gpu-tools mesa-pvr-a7a mesa-img-a7a kernel-a7a kernel-a7apvr a7a-panel a7a-repo 2>&1 | tr "\n" " "')"
  echo "  check: extlinux_default = $($SSH 'sudo /usr/local/bin/a7a-extlinux list 2>/dev/null | awk "\$1 == \"*\" {print \$2}"')"
  echo "  check: extlinux_entry2 = $($SSH 'sudo /usr/local/bin/a7a-extlinux list 2>/dev/null | sed -n 2p | awk "{print \$1}"')"
  echo "  check: extlinux_entries = $($SSH 'sudo /usr/local/bin/a7a-extlinux list 2>/dev/null | awk "{print (\$1 == \"*\") ? \$2 : \$1}" | tr "\n" " "')"
  # the device tree each entry boots: the default one's path first (the standard image's ends in -standard/), then any
  # entry whose device tree is not there (U-Boot would stop at such an entry; the emulator never loads one)
  echo "  check: boot_dtb = $($SSH 'l=$(sudo /usr/local/bin/a7a-extlinux list 2>/dev/null); echo "$l" | awk "\$1 == \"*\" {print \$5 \"allwinner/sun60i-a733-cubie-a7a.dtb\"}"; echo "$l" | awk "{print (\$1 == \"*\") ? \$5 : \$4}" | while read -r d; do [ -f "${d}allwinner/sun60i-a733-cubie-a7a.dtb" ] || echo "MISSING:$d"; done' | tr "\n" " ")"
  echo "  check: pvr_icd = $($SSH 'ls /usr/local/share/vulkan/icd.d/ 2>/dev/null | tr "\n" " "; ls /usr/share/vulkan/icd.d/ 2>/dev/null | grep powervr')"
  echo "  check: dnf_kernel_guard = $($SSH 'grep -c "^excludepkgs=kernel" /etc/dnf/dnf.conf 2>/dev/null')"
  # directories under /var that packages own and that are not there, "N missing of M, T tested: the first four".
  # Fedora's raw image keeps /var in a subvolume of its own; an image assembled without it boots all the same (tmpfiles
  # rebuilds the skeleton), and then logrotate, plocate and package scriptlets (/var/lib/rpm-state) have nowhere to
  # write (2026-11-15). The existence test runs as root and says how many paths it saw: T must be M, or it did not run
  echo "  check: var_dirs = $($SSH 't=$(rpm -qa --qf "[%{FILEMODES:perms} %{FILEFLAGS} %{FILENAMES}\n]" 2>/dev/null | awk "\$1 ~ /^d/ && \$2 < 64 && \$3 ~ /^\/var\// && \$3 !~ /^\/var\/(run|lock)(\/|\$)/ {print \$3}" | sort -u); r=$(printf "%s\n" "$t" | sudo sh -c "n=0; while read -r d; do n=\$((n+1)); [ -e \"\$d\" ] || echo \"\$d\"; done; echo \"tested \$n\""); miss=$(printf "%s\n" "$r" | grep -v "^tested "); echo "$(printf "%s\n" "$miss" | grep -c .) missing of $(printf "%s\n" "$t" | grep -c .), $(printf "%s\n" "$r" | sed -n "s/^tested //p" | tail -1) tested:" $(printf "%s\n" "$miss" | head -4)' 2>/dev/null)"
  echo "  check: gpu_check = $($SSH 'test -x /usr/local/bin/a7a-gpu-check && echo present')"
  # a7a-gpu-session.service ran before the display manager and left the config matching the kernel (no GPU device in
  # QEMU: the release string decides, -a7apvr. = open); "match: yes" is required for a packaged image
  echo "  check: gpu_session_unit = $($SSH 'systemctl show -p UnitFileState,ActiveState,Result --value a7a-gpu-session.service 2>/dev/null | tr "\n" " "')"
  echo "  check: gpu_session = $($SSH 'sudo /usr/local/bin/a7a-gpu-session status 2>/dev/null | sed -n "s/^match: //p; s/^config: //p" | tr "\n" " "')"
  # mesa-img-a7a's loader entry exists only on the DDK boot (docs/DDK-WAYLAND-2026-09-10.md): here it must be absent
  echo "  check: pvr_wl = $($SSH 'sudo /usr/local/bin/a7a-gpu-session status 2>/dev/null | sed -n "s/^pvr-wl: //p"')"
  # is the login screen really there? A login prompt on the serial console, plasmalogin "active" and a greeter session
  # in logind were all true on 2026-11-16 in front of a black screen (KWin 6.8 without its QPainter backend had exited).
  # The compositor process of the greeter, the core dumps of this boot, what a7a-gpu-session says about the greeter,
  # and at the very end (the greeter has had the whole check run to draw) the picture itself, saved beside the transcript
  echo "  check: greeter_compositor = $($SSH 'n=$(pgrep -c -u plasmalogin -x kwin_wayland 2>/dev/null); if [ "${n:-0}" -ge 1 ]; then echo "running ($n kwin_wayland of plasmalogin)"; else echo "not running"; fi' 2>/dev/null)"
  echo "  check: greeter = $($SSH 'sudo /usr/local/bin/a7a-gpu-session status 2>/dev/null | sed -n "s/^greeter-kwin: //p"')"
  echo "  check: coredumps = $($SSH 'l=$(sudo coredumpctl list --no-legend -q --since="$(uptime -s)" 2>/dev/null | awk "{for (i = 1; i <= NF; i++) if (\$i ~ /^\//) { print \$i; break }}" | sed "s|.*/||"); n=$(printf "%s\n" "$l" | grep -c .); echo "$n${l:+: }$(printf "%s\n" "$l" | tr "\n" " ")"' 2>/dev/null)"
  echo "  check: screen = $(python3 "$P/tools/vnc-shot.py" --display 5 --out "$log.png" 2>&1 | tail -1)"
  # distinct error lines of the boot, most frequent first; 8 once hid the CPUAFFINITY line under 55 identical Qt messages
  $SSH 'sudo journalctl -p err -b --no-pager -o cat 2>/dev/null | cut -c1-160 | sort | uniq -c | sort -rn | head -40' | sed 's/^/    err: /'
}
if [[ "$mode" == interactive* ]]; then
  echo "console attached; Ctrl-a x quits; ssh -p 2222 radxa@127.0.0.1; $netmode"; exec qemu-system-aarch64 "${common[@]}" "${args[@]}"
fi
echo "== emulate $mode: $img (timeout ${TIMEOUT}s, $netmode), transcript $log"
timeout --foreground "$TIMEOUT" qemu-system-aarch64 "${common[@]}" "${args[@]}" -no-reboot > "$log" 2>&1 < /dev/null &
qpid=$!
milestones=("Linux version" "Booting Linux on physical CPU" "systemd\[1\]: .*(Welcome|Detected)" "Reached target.*(Multi-User|multi-user)" "Reached target.*(Graphical|graphical)" "login:" "a7a-firstboot:")
seen=""
while kill -0 $qpid 2>/dev/null; do
  for m in "${milestones[@]}"; do
    if [[ "$seen" != *"|$m|"* ]] && grep -qE "$m" "$log" 2>/dev/null; then seen="$seen|$m|"; echo "  [$(date +%H:%M:%S)] milestone: $m"; fi
  done
  if grep -qE "Kernel panic|end Kernel panic|Failed to start|Timed out waiting for device" "$log"; then echo "  [$(date +%H:%M:%S)] FAILURE line seen"; fi
  if [[ "$seen" == *"|login:|"* ]] || [[ "$seen" == *"a7a-firstboot:"* ]]; then
    echo "  [$(date +%H:%M:%S)] login prompt: running the ssh checks"; checks 2>&1 | tee -a "$log.checks"; kill $qpid 2>/dev/null; break; fi
  sleep 5
done
wait $qpid 2>/dev/null
if [[ "$seen" == *"|login:|"* ]] || [[ "$seen" == *"a7a-firstboot:"* ]]; then echo "VERDICT: PASS - reached a login prompt ($mode)"; exit 0; fi
if grep -qE "Kernel panic" "$log"; then echo "VERDICT: FAIL - kernel panic; see $log"; grep -m3 -E "Kernel panic|Unable to mount|VFS:" "$log"; exit 1; fi
echo "VERDICT: FAIL - timed out; milestones reached: ${seen//|/ }; see $log"; tail -5 "$log" | cut -c1-120; exit 1
