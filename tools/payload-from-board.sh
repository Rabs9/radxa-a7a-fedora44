#!/bin/bash
# Rebuild a kernel payload from the files a board already runs, so tools/build-rpm.sh packages the exact binaries
# that were tested there instead of a rebuild with a new stamp:
#   tools/payload-from-board.sh <release>      -> output/payload-<release>/ (same layout as output/payload)
# then: A7A_PAYLOAD=output/payload-<release> tools/build-rpm.sh
# Takes /boot/vmlinuz-<release>, /usr/lib/linux-image-<release>/allwinner/*.dtb, /usr/lib/modules/<release>,
# /usr/lib/firmware/aic8800D80 and, for the open-stack flavour, /usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw,
# and checks the kernel image's sha256 against the board afterwards. Board from a7a-fedora.json (A7A_BOARD_HOST,
# A7A_SSH_KEY override).
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}
. "$P/tools/rpm/lib.sh"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
REL=${1:?release, e.g. 6.6.98-a7apvr.202609091821}
[[ "$REL" =~ ^[A-Za-z0-9.+_-]+$ ]] || { echo "odd release string: $REL"; exit 1; }
PAY=$OUT/payload-$REL
PATHS="boot/vmlinuz-$REL usr/lib/linux-image-$REL usr/lib/modules/$REL usr/lib/firmware/aic8800D80"
case "$REL" in *-a7apvr.*) PATHS="$PATHS usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw" ;; esac
step "checking $REL on $(a7a_board_host)"
a7a_board_ssh "cd / && ls -d $PATHS" >/dev/null || { echo "the board lacks some of: $PATHS"; exit 1; }
step "fetching (tar over ssh) into $PAY"
rm -rf "$PAY" "$PAY.stage"; mkdir -p "$PAY.stage" "$PAY/boot/dtb/allwinner" "$PAY/lib/modules" "$PAY/lib/firmware"
a7a_board_ssh "cd / && tar -czf - $PATHS" | tar -xzf - -C "$PAY.stage"
mv "$PAY.stage/boot/vmlinuz-$REL" "$PAY/boot/Image-$REL"
cp "$PAY.stage/usr/lib/linux-image-$REL/allwinner/"*.dtb "$PAY/boot/dtb/allwinner/"
mv "$PAY.stage/usr/lib/modules/$REL" "$PAY/lib/modules/$REL"
mv "$PAY.stage/usr/lib/firmware/aic8800D80" "$PAY/lib/firmware/aic8800D80"
[ -d "$PAY.stage/usr/lib/firmware/powervr" ] && mv "$PAY.stage/usr/lib/firmware/powervr" "$PAY/lib/firmware/powervr"
rm -rf "$PAY.stage"
echo "$REL" > "$PAY/kernelrelease"
step "verify: kernel image sha256 against the board"
HERE=$(sha256sum "$PAY/boot/Image-$REL" | cut -d' ' -f1); THERE=$(a7a_board_ssh "sha256sum /boot/vmlinuz-$REL" | cut -d' ' -f1)
[ "$HERE" = "$THERE" ] || { echo "sha256 mismatch: here $HERE, board $THERE"; exit 1; }
step "done: $PAY ($(du -sh "$PAY" | cut -f1), $(find "$PAY/lib/modules/$REL" -name '*.ko*' | wc -l) modules, dtb $(ls "$PAY/boot/dtb/allwinner"))"
