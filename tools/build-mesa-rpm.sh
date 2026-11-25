#!/bin/bash
# Package the a7a Mesa PowerVR Vulkan driver as mesa-pvr-a7a-<mesa version>-<build>.aarch64.rpm
# (tools/rpm/mesa-pvr-a7a.spec). The driver is BUILT ON THE BOARD by tools/mesa/build-pvr-vulkan.sh, which leaves
# a staging tarball /home/radxa/mesa-build/mesa-pvr-a7a-<mesa version>-<stamp>.tar.gz (usr/local/lib64/
# libvulkan_powervr_mesa.so + usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json, relative paths).
#   tools/build-mesa-rpm.sh                 fetch the newest tarball from the board (a7a-fedora.json), package it
#   tools/build-mesa-rpm.sh <tarball>       package a tarball already on this machine
# The RPM's Release is the tarball's stamp, so the package names the build it came from. Nothing is compiled here.
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}
. "$P/tools/rpm/lib.sh"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
TAR=${1:-}
if [ -z "$TAR" ]; then
  mkdir -p "$OUT/mesa"
  NEWEST=$(a7a_board_ssh 'ls -t /home/radxa/mesa-build/mesa-pvr-a7a-*.tar.gz 2>/dev/null | head -1')
  [ -n "$NEWEST" ] || { echo "no mesa-pvr-a7a-*.tar.gz on the board: run tools/mesa/build-pvr-vulkan.sh there first"; exit 1; }
  step "fetching $NEWEST from $(a7a_board_host)"
  a7a_board_scp_from "$NEWEST" "$OUT/mesa/"; TAR=$OUT/mesa/$(basename "$NEWEST")
fi
[ -f "$TAR" ] || { echo "no tarball $TAR"; exit 1; }
TAR=$(readlink -f "$TAR")   # rpmbuild runs %install elsewhere
B=$(basename "$TAR" .tar.gz)
[[ "$B" =~ ^mesa-pvr-a7a-([0-9][0-9.]*[0-9])-([0-9]{12})$ ]] || { echo "tarball name must be mesa-pvr-a7a-<mesa version>-<YYYYMMDDHHMM>.tar.gz, got $B"; exit 1; }
MESA=${BASH_REMATCH[1]}; BUILD=${A7A_RPM_BUILD:-${BASH_REMATCH[2]}}
LIST=$(tar -tzf "$TAR")
grep -qx "usr/local/lib64/libvulkan_powervr_mesa.so" <<<"$LIST" || { echo "$TAR has no usr/local/lib64/libvulkan_powervr_mesa.so"; exit 1; }
ICD=$(grep -E "^usr/local/share/vulkan/icd.d/powervr_.*\.json$" <<<"$LIST" | head -1)
[ -n "$ICD" ] || { echo "$TAR has no ICD JSON under usr/local/share/vulkan/icd.d"; exit 1; }
tar -xzOf "$TAR" "$ICD" | grep -q '"library_path": *"/usr/local/lib64/libvulkan_powervr_mesa.so"' || { echo "$ICD does not point at /usr/local/lib64/libvulkan_powervr_mesa.so"; exit 1; }
STRAY=$(grep -vE "^usr/(local/)?$|^usr/local/(lib64|share)(/|$)" <<<"$LIST" || true)
[ -z "$STRAY" ] || { echo "$TAR carries files outside usr/local/lib64 and usr/local/share:"; echo "$STRAY"; exit 1; }
step "mesa-pvr-a7a $MESA-$BUILD from $TAR ($(wc -l <<<"$LIST") entries)"
RPM=$(a7a_rpmbuild "$P/tools/rpm/mesa-pvr-a7a.spec" aarch64-linux "mesa-pvr-a7a-*.rpm" \
  --define "a7a_mesa_ver $MESA" --define "a7a_build $BUILD" --define "a7a_stage $TAR")
step "done: $RPM ($(du -h "$RPM" | cut -f1))"
a7a_rpm_summary "$RPM"
