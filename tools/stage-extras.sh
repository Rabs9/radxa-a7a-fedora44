#!/bin/bash
# Stage the closed GPU userspace (Imagination DDK 24.2), its firmware, the DSU modules and the NPU SDK from a
# mounted Debian card (or its backup) into output/ddk, where assemble-image.sh picks them up.
#   tools/stage-extras.sh <mounted debian root>      e.g. output/mnt/debian after:
#   sudo tools/priv.sh loop-attach output/backups/deb-13-a7a.img $((679936*512)); sudo tools/priv.sh mount /dev/loopN debian ro,noload
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=$P/output; M=${1:?mounted debian root}; D=$OUT/ddk
[ -f "$M/usr/lib/libsrv_um.so" ] || { echo "$M does not look like a Debian card with the DDK (no /usr/lib/libsrv_um.so)"; exit 1; }
rm -rf "$D"; mkdir -p "$D"/{lib,usrlib2,firmware,icd,dsu}
cp -a "$M/usr/local/lib/." "$D/lib/"                                   # EGL/GLES/gbm/glapi/dri of the DDK, plus the NPU libs
python3 - "$M" "$D" <<'PY'
import pathlib, sys
m, d = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
owned = set()
for lst in (m / "var/lib/dpkg/info").glob("*.list"):
    owned.update(lst.read_text(errors="replace").splitlines())
import shutil
n = 0
for f in (m / "usr/lib").iterdir():
    if f.name.startswith("lib") and ".so" in f.name and f"/usr/lib/{f.name}" not in owned:
        shutil.copy2(f, d / "usrlib2" / f.name, follow_symlinks=False); n += 1
print(f"{n} DDK libraries from /usr/lib (not owned by a Debian package)")
PY
cp -a "$M"/lib/firmware/rgx.* "$D/firmware/"
sed 's|/usr/lib/libVK_IMG.so.1|/opt/pvr/lib/libVK_IMG.so.1|' "$M/usr/share/vulkan/icd.d/powervr_icd.aarch64.json" > "$D/icd/powervr_icd.json"
cp "$M/home/radxa/dsu_oc/dsu_oc.ko" "$M/home/radxa/dsu_dyn/dsu_dyn.ko" "$D/dsu/" 2>/dev/null || echo "no DSU modules on this card"
[ -d "$M/home/radxa/ai-sdk" ] && { rsync -a "$M/home/radxa/ai-sdk/" "$D/ai-sdk/"; echo "NPU SDK: $(du -sh "$D/ai-sdk" | cut -f1)"; }
echo "staged in $D: $(ls "$D/lib" | wc -l) + $(ls "$D/usrlib2" | wc -l) libraries, firmware $(ls "$D/firmware" | tr '\n' ' ')"
