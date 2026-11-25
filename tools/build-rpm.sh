#!/bin/bash
# Package a kernel payload as an RPM a running a7a-fedora board can install with dnf (tools/rpm/kernel-a7a.spec):
#   kernel-a7a-<version>-<build>.aarch64.rpm     vendor GPU stack flavour (pvrsrvkm), release 6.6.98-a7a.<stamp>
#   kernel-a7apvr-<version>-<build>.aarch64.rpm  open GPU stack flavour (drm/imagination), release 6.6.98-a7apvr.<stamp>
# The flavour comes from the payload's release string. Both are install-only packages (dnf keeps versions side by
# side), both Require a7a-extlinux (tools/build-tools-rpm.sh) and add their extlinux entry in %post (A7A_DEFAULT=1
# in dnf's environment makes it the default). Built on x86 with --target aarch64: nothing is compiled.
#   A7A_PAYLOAD=<dir>   another payload than output/payload (tools/payload-from-board.sh makes one from the board)
#   A7A_RPM_BUILD=<n>   the Release field (default: now, YYYYMMDDHHMM)
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}; PAY=${A7A_PAYLOAD:-$OUT/payload}
. "$P/tools/rpm/lib.sh"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
[ -f "$PAY/kernelrelease" ] || { echo "no kernel payload in $PAY (tools/build-kernel.sh, or tools/payload-from-board.sh <release>)"; exit 1; }
# rpmbuild runs %install elsewhere: the payload path must be absolute
PAY=$(readlink -f "$PAY")
REL=$(cat "$PAY/kernelrelease"); VER=${REL//+/.plus}; VER=${VER//-/_}; BUILD=${A7A_RPM_BUILD:-$(date +%Y%m%d%H%M)}
case "$REL" in *-a7apvr.*) FLAV=a7apvr ;; *) FLAV=a7a ;; esac
NAME=kernel-$FLAV
for f in "boot/Image-$REL" "lib/modules/$REL" "lib/firmware/aic8800D80"; do [ -e "$PAY/$f" ] || { echo "payload $PAY lacks $f"; exit 1; }; done
ls "$PAY"/boot/dtb/allwinner/*.dtb >/dev/null 2>&1 || { echo "payload $PAY has no DTB under boot/dtb/allwinner"; exit 1; }
if [ "$FLAV" = a7apvr ]; then
  FW=lib/firmware/powervr/rogue_36.56.104.183_v1.fw
  if [ ! -f "$PAY/$FW" ]; then
    # build-kernel.sh could not download it: the repo carries the same file (sha256 1db1c399..., the board's copy)
    mkdir -p "$PAY/lib/firmware/powervr"; cp "$P/powervr-backport/rogue_36.56.104.183_v1.fw" "$PAY/$FW"
    step "firmware: $FW taken from the repo copy"
  fi
else
  [ -f "$PAY/lib/modules/$REL/extra/pvrsrvkm.ko" ] || { echo "vendor flavour $REL without extra/pvrsrvkm.ko: run tools/build-extras.sh first"; exit 1; }
fi
step "$NAME $VER-$BUILD from $PAY (release string $REL, $(find "$PAY/lib/modules/$REL" -name '*.ko*' | wc -l) modules)"
RPM=$(a7a_rpmbuild "$P/tools/rpm/kernel-a7a.spec" aarch64-linux "$NAME-*.rpm" \
  --define "a7a_name $NAME" --define "a7a_flavour $FLAV" --define "a7a_rel $REL" --define "a7a_ver $VER" \
  --define "a7a_build $BUILD" --define "a7a_payload $PAY")
step "done: $RPM ($(du -h "$RPM" | cut -f1))"
a7a_rpm_summary "$RPM"
