#!/bin/bash
# master-compress.sh <image> ...: the file people download. <image>.xz (xz -9, 8 threads) with its checksum, and the
# limit that decides whether it can be one file of a GitHub release: under 2 GiB, with room to spare.
# The image must have passed the emulator stage first (its sha256 in the registry is what flash compares; this script
# only refuses an image whose own .sha256 does not match the file).
set -euo pipefail
LIMIT_MIB=${A7A_XZ_LIMIT_MIB:-1950}
[ $# -ge 1 ] || { echo "usage: $0 <image> ..."; exit 2; }
for IMG in "$@"; do
  [ -f "$IMG" ] && [ -f "$IMG.sha256" ] || { echo "$IMG or its .sha256 is missing"; exit 1; }
  ( cd "$(dirname "$IMG")" && sha256sum -c --quiet "$(basename "$IMG").sha256" ) || { echo "$IMG is not the file its .sha256 names"; exit 1; }
  rm -f "$IMG.xz" "$IMG.xz.sha256"
  xz -T8 -9 -c "$IMG" > "$IMG.xz.part"
  mib=$(( $(stat -c %s "$IMG.xz.part") / 1048576 ))
  if [ "$mib" -ge "$LIMIT_MIB" ]; then
    mv -f "$IMG.xz.part" "$IMG.xz.too-big"
    echo "$(basename "$IMG").xz is $mib MiB: over the limit of $LIMIT_MIB MiB (a GitHub release file must be under 2048); kept as $(basename "$IMG").xz.too-big"; exit 1
  fi
  mv -f "$IMG.xz.part" "$IMG.xz"
  ( cd "$(dirname "$IMG")" && sha256sum "$(basename "$IMG").xz" ) > "$IMG.xz.sha256"
  echo "$(basename "$IMG").xz: $mib MiB (limit $LIMIT_MIB), sha256 $(cut -c1-16 "$IMG.xz.sha256")..."
done
