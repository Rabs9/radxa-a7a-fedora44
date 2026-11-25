#!/bin/bash
# prepare616.sh [--no-reboot] [--force]: boot the open GPU stack next. Makes the newest packaged open-stack kernel entry
# (kernel-a7apvr, fedora-<version>-a7apvr.<stamp>) the extlinux default and reboots after 3 s.
# The session config is no longer touched here: a7a-gpu-session.service (package a7a-gpu-tools) reads which driver
# is bound at boot and applies the matching config before the display manager (docs/PACKAGING-2026-09-09.md), so a
# login before the reboot is safe. Nothing is removed; backtoddk.sh returns to the DDK the same way.
set -u
S() { sudo -n "$@"; }
REBOOT=1; FORCE=0
for a in "$@"; do case "$a" in --no-reboot) REBOOT=0 ;; --force) FORCE=1 ;; *) echo "usage: prepare616.sh [--no-reboot] [--force]"; exit 2 ;; esac; done

# the session config follows the kernel only through a7a-gpu-session.service: without it, KWin would load the DDK libraries from a stale drop-in on the open kernel, or run without cap_sys_nice.
# Checked BEFORE the default entry changes; --force is for experiments with the manual marker and a hand-made config.
if [ "$(systemctl is-enabled a7a-gpu-session.service 2>/dev/null)" != enabled ]; then
  echo "a7a-gpu-session.service is not enabled: the session config would not follow the kernel at boot"
  echo "  fix: sudo systemctl enable a7a-gpu-session.service (the unit ships in a7a-gpu-tools >= 20260910), then run this again"
  [ $FORCE = 1 ] && echo "  --force: going on anyway" || { echo "  nothing changed (--force overrides)"; exit 1; }
fi

# the target entry: the newest packaged open-stack kernel, else the 2026-09-08 hand-installed backport entry
ENTRIES=$(S a7a-extlinux list 2>/dev/null)
TARGET=$(grep -oE "fedora-[0-9][0-9.]*-a7apvr\.[0-9]+" <<<"$ENTRIES" | awk -F. '{print $NF, $0}' | sort -n | tail -1 | cut -d' ' -f2)
[ -n "$TARGET" ] || { grep -qE "^. fedora-6.6.98-pvr\+ " <<<"$ENTRIES" && TARGET=fedora-6.6.98-pvr+; }
[ -n "$TARGET" ] || { echo "no open-stack kernel entry (fedora-*-a7apvr.<stamp> or fedora-6.6.98-pvr+) in extlinux.conf: nothing changed"; exit 1; }

# --check-files: the entry's kernel and fdtdir must exist, or extlinux.conf is left alone
if ! OUT=$(S a7a-extlinux --check-files default "$TARGET"); then
  echo "$OUT"; echo "could not make $TARGET the default: nothing changed, not rebooting"; exit 1
fi
echo "$OUT"
echo "default: $(grep -m1 '^default' /boot/extlinux/extlinux.conf)"
if [ $REBOOT = 1 ]; then
  echo "rebooting in 3 s onto $TARGET (the screen goes blank; the session config is applied at boot)"
  sync; S systemd-run --on-active=3 --timer-property=AccuracySec=100ms /sbin/reboot
else
  echo "not rebooting (--no-reboot): 'sudo systemctl reboot' when ready"
fi
