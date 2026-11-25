#!/bin/bash
# backtoddk.sh [--no-reboot] [--force]: boot the vendor DDK stack next. Makes the newest packaged vendor-flavour kernel entry
# (kernel-a7a, fedora-<version>-a7a.<stamp>; the 2026-09-09 build from the grafted tree was boot-tested with 0 HWR)
# the extlinux default, else the owner's hand-made g_1008_800 (the 2026-09-07 kernel), and reboots after 3 s.
# The session config is no longer touched here: a7a-gpu-session.service (package a7a-gpu-tools) reads which driver
# is bound at boot and applies the DDK config (`pvr-desktop on`, cap stripped, nocap units) before the display
# manager (docs/PACKAGING-2026-09-09.md). Everything for the open stack stays installed; prepare616.sh returns to it.
set -u
S() { sudo -n "$@"; }
REBOOT=1; FORCE=0
for a in "$@"; do case "$a" in --no-reboot) REBOOT=0 ;; --force) FORCE=1 ;; *) echo "usage: backtoddk.sh [--no-reboot] [--force]"; exit 2 ;; esac; done

# the session config follows the kernel only through a7a-gpu-session.service: without it, the DDK kernel boots with the open-stack session config = KWin on llvmpipe = black screen at login.
# Checked BEFORE the default entry changes; --force is for experiments with the manual marker and a hand-made config.
if [ "$(systemctl is-enabled a7a-gpu-session.service 2>/dev/null)" != enabled ]; then
  echo "a7a-gpu-session.service is not enabled: the session config would not follow the kernel at boot"
  echo "  fix: sudo systemctl enable a7a-gpu-session.service (the unit ships in a7a-gpu-tools >= 20260910), then run this again"
  [ $FORCE = 1 ] && echo "  --force: going on anyway" || { echo "  nothing changed (--force overrides)"; exit 1; }
fi

ENTRIES=$(S a7a-extlinux list 2>/dev/null)
TARGET=$(grep -oE "fedora-[0-9][0-9.]*-a7a\.[0-9]+" <<<"$ENTRIES" | awk -F. '{print $NF, $0}' | sort -n | tail -1 | cut -d' ' -f2)
[ -n "$TARGET" ] || { grep -qE "^. g_1008_800 " <<<"$ENTRIES" && TARGET=g_1008_800; }
[ -n "$TARGET" ] || { echo "no vendor-DDK kernel entry (fedora-*-a7a.<stamp> or g_1008_800) in extlinux.conf: nothing changed, not rebooting"; exit 1; }

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
