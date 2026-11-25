#!/bin/bash
# Package the Wayland-capable DDK Mesa as mesa-img-a7a-<mesa version>-<build>.aarch64.rpm (tools/rpm/mesa-img-a7a.spec):
# Imagination's open Mesa glue for the vendor DDK (TI's powervr/24.0.1 branch), everything under /opt/pvr-wl. It is
# BUILT ON THE BOARD by tools/mesa-img/build-mesa-img.sh, which leaves a staging tarball
# /home/radxa/mesa-img/mesa-img-a7a-<mesa version>-<12-hex pin>-<stamp>.tar.gz (opt/pvr-wl/..., relative paths).
#   tools/build-mesa-img-rpm.sh              fetch the newest tarball from the board (a7a-fedora.json), package it
#   tools/build-mesa-img-rpm.sh <tarball>    package a tarball already on this machine
# The RPM's Release is the tarball's stamp, so the package names the build it came from. Nothing is compiled here.
# docs/DDK-WAYLAND-2026-09-10.md.
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}
. "$P/tools/rpm/lib.sh"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
TAR=${1:-}
if [ -z "$TAR" ]; then
  mkdir -p "$OUT/mesa"
  NEWEST=$(a7a_board_ssh 'ls -t /home/radxa/mesa-img/mesa-img-a7a-*.tar.gz 2>/dev/null | head -1')
  [ -n "$NEWEST" ] || { echo "no mesa-img-a7a-*.tar.gz on the board: run tools/mesa-img/build-mesa-img.sh there first"; exit 1; }
  step "fetching $NEWEST from $(a7a_board_host)"
  a7a_board_scp_from "$NEWEST" "$OUT/mesa/"; TAR=$OUT/mesa/$(basename "$NEWEST")
fi
[ -f "$TAR" ] || { echo "no tarball $TAR"; exit 1; }
TAR=$(readlink -f "$TAR")   # rpmbuild runs %install elsewhere
B=$(basename "$TAR" .tar.gz)
[[ "$B" =~ ^mesa-img-a7a-([0-9][0-9.]*[0-9])-([0-9a-f]{12})-([0-9]{12})$ ]] \
  || { echo "tarball name must be mesa-img-a7a-<mesa version>-<12-hex pin>-<YYYYMMDDHHMM>.tar.gz, got $B"; exit 1; }
MESA=${BASH_REMATCH[1]}; PIN=${BASH_REMATCH[2]}; BUILD=${A7A_RPM_BUILD:-${BASH_REMATCH[3]}}
LIST=$(tar -tzf "$TAR")
for f in libEGL.so.1 libgbm.so.1 libglapi.so.0 libGLESv2.so.2 libpvr_mesa_wsi.so dri/pvr_dri.so dri/sunxi-drm_dri.so a7a/wl-render-node-shim.so; do
  grep -qx "opt/pvr-wl/lib/$f" <<<"$LIST" || { echo "$TAR has no opt/pvr-wl/lib/$f"; exit 1; }
done
STRAY=$(grep -vE "^opt/$|^opt/pvr-wl(/|$)" <<<"$LIST" || true)
[ -z "$STRAY" ] || { echo "$TAR carries files outside opt/pvr-wl:"; echo "$STRAY"; exit 1; }
# a GLX build (-Dglx=dri): the DDK has no desktop-GL module, and a libGL.so.1 in the loader cache would shadow glvnd's GLX
! grep -qE "^opt/pvr-wl/lib/libGL\.so" <<<"$LIST" || { echo "$TAR carries libGL (a GLX build): rebuild with -Dglx=disabled"; exit 1; }
step "mesa-img-a7a $MESA-$BUILD (TI powervr/$MESA @ $PIN) from $TAR ($(wc -l <<<"$LIST") entries)"
RPM=$(a7a_rpmbuild "$P/tools/rpm/mesa-img-a7a.spec" aarch64-linux "mesa-img-a7a-*.rpm" \
  --define "a7a_mesa_ver $MESA" --define "a7a_build $BUILD" --define "a7a_pin $PIN" --define "a7a_stage $TAR")
step "done: $RPM ($(du -h "$RPM" | cut -f1))"
a7a_rpm_summary "$RPM"
