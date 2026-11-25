#!/bin/bash
# master-release.sh <backup image under output/backups>: the two published images from one backup, start to finish:
#   tools/master-image.sh <backup> standard|oc    (every gate of that script)
#   python3 -m src.cli emulate --image <name>     (the image must reach a login prompt; its checksum is registered,
#                                                  and the flash stage writes nothing that has not passed here)
#   tools/master-compress.sh <image>              (.xz under the release limit)
# and output/release/SHA256SUMS for the two .xz files. Stops at the first step that fails.
# Both images carry the date of the moment this script started, also when the second one is built after midnight.
# The emulator stage takes the image's NAME (it looks in output/): given a path on 2026-11-16 it looked for
# output/output/<name>, recorded "failed" and the pipeline went on to compress an untested image.
set -euo pipefail
[ -n "${A7A_RUN:-}" ] || [ -t 0 ] || { echo "$0: refusing to run non-interactively without A7A_RUN=1"; exit 2; }
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output
SRC=${1:?the backup image of the card (under output/backups)}
export A7A_RUN=1 A7A_IMAGE_DATE=${A7A_IMAGE_DATE:-$(date +%Y%m%d)}
cd "$P"
for v in standard oc; do
  name=a7a-fedora-44-kde-$A7A_IMAGE_DATE-$v.img
  echo "######## $v $(date +%T)"
  tools/master-image.sh "$SRC" "$v"
  [ -f "$OUT/$name" ] || { echo "tools/master-image.sh ended well but $OUT/$name is not there"; exit 1; }
  echo "-- emulator $(date +%T)"
  python3 -m src.cli emulate --image "$name" 2>&1 | tee "$OUT/logs/emulate-$name.log" | grep -E "^PASS|^FAIL|VERDICT|^emulate:" || true
  grep -q "^emulate: passed" "$OUT/logs/emulate-$name.log" || { echo "the emulator did not pass $name ($OUT/logs/emulate-$name.log)"; exit 1; }
  echo "-- compress $(date +%T)"
  tools/master-compress.sh "$OUT/$name"
done
mkdir -p "$OUT/release"
( cd "$OUT" && sha256sum "a7a-fedora-44-kde-$A7A_IMAGE_DATE-standard.img.xz" "a7a-fedora-44-kde-$A7A_IMAGE_DATE-oc.img.xz" ) > "$OUT/release/SHA256SUMS"
echo "######## done $(date +%T)"; cat "$OUT/release/SHA256SUMS"
