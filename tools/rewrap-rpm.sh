#!/bin/bash
# rewrap-rpm: a package built before 2026-11-16 again, its files untouched, without the build machine's name.
#   tools/rewrap-rpm.sh <package.rpm>...      kernel-a7a, kernel-a7apvr, mesa-pvr-a7a
# rpm writes the build machine's host name into every package (BUILDHOST), and from there into the package database
# of every system that installs it. Since 2026-11-16 tools/rpm/lib.sh writes "a7a-fedora" there; the kernels and the
# Mesa driver the owner's card runs (September builds, proven on the board) still say the laptop's name, and the
# images are made from that card. Recompiling them would make new, untested binaries, so the SAME files are packaged
# again: the package's own payload is put back in the shape its builder takes (tools/build-rpm.sh: a kernel payload;
# tools/build-mesa-rpm.sh: a staging tarball) and built with the same Name, Version and Release.
# The new package must be the old one but for the build host and time and the files' times: every file (path, size,
# digest, mode, owner, group, flags, link), every dependency, script and trigger, the summary, description, licence
# and URL. Anything else differing stops it, and output/rpms keeps the old package. Otherwise the old one is moved to
# output/rpms-replaced/ (never deleted) and the new one takes its name in output/rpms; tools/master-image.sh then
# puts it over the card's copy, checking the files there against it first.
#   A7A_OUTPUT=<dir>  another output directory than output/ (the tests)
set -euo pipefail
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}
[ $# -gt 0 ] || { echo "usage: $0 <package.rpm>... (kernel-a7a, kernel-a7apvr, mesa-pvr-a7a)"; exit 2; }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
same() {   # old new: the comparison above; prints what differs
  local o=$1 n=$2 bad=0 q
  # rpm --dump: path size mtime digest mode owner group config doc rdev link; the mtime (3rd) is left out
  diff <(rpm -qp --dump "$o" | awk '{$3=""; print}' | LC_ALL=C sort) <(rpm -qp --dump "$n" | awk '{$3=""; print}' | LC_ALL=C sort) > "$WORK/d" \
    || { echo "  files differ:"; head -5 "$WORK/d" | sed 's/^/    /'; bad=1; }
  for q in requires provides conflicts obsoletes recommends suggests scripts triggers; do
    diff <(rpm -qp --"$q" "$o") <(rpm -qp --"$q" "$n") > "$WORK/d" || { echo "  $q differ:"; head -5 "$WORK/d" | sed 's/^/    /'; bad=1; }
  done
  local f='%{NAME}|%{EPOCHNUM}|%{VERSION}|%{RELEASE}|%{ARCH}|%{SUMMARY}|%{LICENSE}|%{URL}|%{DESCRIPTION}'
  [ "$(rpm -qp --qf "$f" "$o")" = "$(rpm -qp --qf "$f" "$n")" ] || { echo "  name, version, summary, licence, URL or description differ"; bad=1; }
  return $bad
}
rc=0
for OLD in "$@"; do
  OLD=$(readlink -f "$OLD"); B=$(basename "$OLD")
  [ -f "$OLD" ] || { echo "$B: no such package"; rc=1; continue; }
  # only a package of output/rpms: it is moved aside, and the new one takes its place there
  [ "$(dirname "$OLD")" = "$(readlink -f "$OUT/rpms")" ] || { echo "$B: not in $OUT/rpms"; rc=1; continue; }
  NAME=$(rpm -qp --qf '%{NAME}' "$OLD" 2>/dev/null) || { echo "$B: not a package"; rc=1; continue; }
  HOST=$(rpm -qp --qf '%{BUILDHOST}' "$OLD"); REL=$(rpm -qp --qf '%{RELEASE}' "$OLD")
  [ "$HOST" != a7a-fedora ] || { echo "$B: built as a7a-fedora already"; continue; }
  R=$WORK/$NAME; rm -rf "$R"; mkdir -p "$R/root" "$R/out"
  ( cd "$R/root" && rpm2cpio "$OLD" | cpio -idm --quiet )
  case "$NAME" in
    kernel-a7a|kernel-a7apvr)
      K=$(rpm -qp --provides "$OLD" | sed -n 's/^kernel-uname-r = //p')
      [ -n "$K" ] && [ -f "$R/root/boot/vmlinuz-$K" ] || { echo "$B: no kernel-uname-r, or no /boot/vmlinuz-$K in it"; rc=1; continue; }
      PAY=$R/payload; mkdir -p "$PAY/boot/dtb/allwinner" "$PAY/lib/modules" "$PAY/lib/firmware"; echo "$K" > "$PAY/kernelrelease"
      cp -a "$R/root/boot/vmlinuz-$K" "$PAY/boot/Image-$K"
      cp -a "$R/root/usr/lib/linux-image-$K"/allwinner/*.dtb "$PAY/boot/dtb/allwinner/"
      cp -a "$R/root/usr/lib/modules/$K" "$PAY/lib/modules/"
      cp -a "$R/root/usr/lib/firmware/aic8800D80" "$PAY/lib/firmware/"
      [ ! -d "$R/root/usr/lib/firmware/powervr" ] || cp -a "$R/root/usr/lib/firmware/powervr" "$PAY/lib/firmware/"
      A7A_PAYLOAD=$PAY A7A_RPM_BUILD=$REL A7A_OUTPUT=$R/out "$P/tools/build-rpm.sh" > "$R/build.log" 2>&1 \
        || { echo "$B: the build failed:"; tail -5 "$R/build.log"; rc=1; continue; } ;;
    mesa-pvr-a7a)
      T=$R/mesa-pvr-a7a-$(rpm -qp --qf '%{VERSION}' "$OLD")-$REL.tar.gz
      ( cd "$R/root" && tar --owner=0 --group=0 -czf "$T" usr )
      A7A_OUTPUT=$R/out "$P/tools/build-mesa-rpm.sh" "$T" > "$R/build.log" 2>&1 \
        || { echo "$B: the build failed:"; tail -5 "$R/build.log"; rc=1; continue; } ;;
    *) echo "$B: $NAME is not a package this re-wraps (kernel-a7a, kernel-a7apvr, mesa-pvr-a7a)"; rc=1; continue ;;
  esac
  NEW=$R/out/rpms/$B
  [ -f "$NEW" ] || { echo "$B: the build made another name: $(ls "$R/out/rpms")"; rc=1; continue; }
  if ! same "$OLD" "$NEW"; then echo "$B: NOT the same package again (above): left as it was"; rc=1; continue; fi
  [ "$(rpm -qp --qf '%{BUILDHOST}' "$NEW")" = a7a-fedora ] || { echo "$B: the new package is not built as a7a-fedora"; rc=1; continue; }
  mkdir -p "$OUT/rpms-replaced" "$OUT/rpms"
  # the new one is copied in beside the old under a temporary name first: the old is moved aside only once it is there
  cp "$NEW" "$OUT/rpms/.$B.new" && mv -f "$OLD" "$OUT/rpms-replaced/$B" && mv -f "$OUT/rpms/.$B.new" "$OUT/rpms/$B"
  echo "$B: re-wrapped (build host $HOST -> a7a-fedora; $(rpm -qp --dump "$NEW" | wc -l) files, digests as before); the old package: $OUT/rpms-replaced/$B"
done
exit $rc
