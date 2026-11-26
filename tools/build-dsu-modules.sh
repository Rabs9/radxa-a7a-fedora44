#!/bin/bash
# build-dsu-modules.sh <kernel release> ...: the modules an image adds to kernels that are packaged already.
#   dsu_oc.ko, dsu_dyn.ko (tools/dsu/: the owner's modules behind `a7a-clock dsu`, the four profiles and the panel's
#     DSU buttons)  -> output/dsu-modules/<release>/   (not under output/ddk: tools/stage-extras.sh empties that)
#   exfat.ko, isofs.ko, uinput.ko: the kernel's own exFAT, ISO 9660 and uinput, for a kernel whose configuration
#     leaves them out  -> output/extra-modules/<release>/   (the step of tools/master-image.sh that installs them
#     says why they are needed)
# Why the DSU pair is built here: the modules in every image until 2026-11-16 were the Debian card's builds (vermagic "6.6.98+").
# This kernel has no CONFIG_MODVERSIONS, so a module loads only under the exact release it was built for: under
# 6.6.98-a7a.<stamp> every one of those controls answered "Invalid module format", and nobody had pressed one.
# A kernel's modules must be built against that kernel's configuration. The packaged kernel carries its
# configuration (CONFIG_IKCONFIG), so this script takes it out of the kernel RPM's vmlinuz and refuses when the
# tree in work/kernel-6.6 is configured differently, except for the three GPU-driver options that separate the two
# flavours and are modules themselves (DRM_EXEC, DRM_GPUVM, DRM_POWERVR: no shared structure changes with them), and
# for the six lines that the exFAT, ISO 9660 and uinput options of tools/build-kernel.sh (2026-11-18) bring with them
# (EXFAT_FS, EXFAT_DEFAULT_IOCHARSET, ISO9660_FS, JOLIET, ZISOFS, INPUT_UINPUT): leaf options too, what they select
# (BUFFER_HEAD, NLS, LEGACY_DIRECT_IO, ZLIB_INFLATE) is =y in both and still compared. Without them, a tree built with
# the three would refuse every kernel packaged before.
# The build runs in a reflink copy of the tree (work/kmod-tree), where only the release string is set to the
# packaged kernel's: work/kernel-6.6 itself is not touched.
# A kernel built from now on should carry the DSU pair in its own package (tools/build-extras.sh): open. It has the
# other three itself (tools/build-kernel.sh, 2026-11-18), and for such a kernel none of them is built here.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: $0 <kernel release> ...   (the releases an image keeps: its .kernel sidecar, and the other flavour)"; exit 2; }
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
W=$P/work; OUT=$P/output; K=$W/kernel-6.6; T=$W/kmod-tree
export ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- KBUILD_BUILD_USER="${A7A_KBUILD_USER:-$(id -un)}" KBUILD_BUILD_HOST=a7a-fedora
export CCACHE_DIR=$P/work/ccache
# the make arguments of tools/build-kernel.sh: the BSP's Kconfig files are found through BSP_TOP, and the compiler
# is named the way the kernel's own build named it
MK=(BSP_TOP=bsp/ CC="ccache aarch64-linux-gnu-gcc")
step() { echo "== [$(date +%H:%M:%S)] $*"; }
[ -f "$K/.config" ] && [ -f "$K/Module.symvers" ] || { echo "no built kernel tree in $K (python3 -m src.cli kernel)"; exit 1; }
BASE=$(make -s -C "$K" "${MK[@]}" kernelversion)
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# a module carries the paths of its sources (__FILE__ in its warnings, its debug information): without this random
# directory in them the same source gives the same bytes on every run, so the module tested on the board is the one
# an image gets (2026-11-18: a rerun had made new DSU modules that differed only in that directory's name)
MODFLAGS=(KCFLAGS="-ffile-prefix-map=$TMP/=")
# and without debug information, like the kernel package's own modules (tools/build-kernel.sh: INSTALL_MOD_STRIP=1)
strip_ko() { aarch64-linux-gnu-strip --strip-debug "$1"; }

for REL in "$@"; do
  case "$REL" in "$BASE"-*) ;; *) echo "$REL is not a $BASE kernel: the tree in $K cannot build for it"; exit 1 ;; esac
  RPM=$(ls "$OUT"/rpms/kernel-*-"${REL//-/_}"-*.aarch64.rpm 2>/dev/null | tail -1 || true)
  [ -n "$RPM" ] || { echo "no kernel package for $REL in $OUT/rpms"; exit 1; }
  step "$REL: its configuration, out of $(basename "$RPM")"
  mkdir -p "$TMP/$REL"; ( cd "$TMP/$REL" && rpm2cpio "$RPM" | ( cpio -id --quiet "./boot/vmlinuz-$REL"; s=$?; cat > /dev/null; exit $s ) )
  bash "$K/scripts/extract-ikconfig" "$TMP/$REL/boot/vmlinuz-$REL" > "$TMP/$REL/config" 2>/dev/null \
    || { echo "the kernel in $RPM carries no configuration (CONFIG_IKCONFIG)"; exit 1; }
  flavour='^(# )?CONFIG_(DRM_(EXEC|GPUVM|POWERVR)|EXFAT_FS|EXFAT_DEFAULT_IOCHARSET|ISO9660_FS|JOLIET|ZISOFS|INPUT_UINPUT)[= ]'
  if ! diff <(grep -vE "$flavour" "$TMP/$REL/config") <(grep -vE "$flavour" "$K/.config") > "$TMP/$REL/config.diff"; then
    echo "the tree in $K is configured differently from $REL: a module built here may not fit that kernel"
    head -20 "$TMP/$REL/config.diff"; exit 1
  fi
  echo "  same configuration as the tree ($(wc -l < "$TMP/$REL/config") lines; GPU-driver and file-system/uinput module options aside)"

  step "$REL: a copy of the tree with that release string"
  rm -rf "$T"; cp -a --reflink=always "$K" "$T"
  LOCALVERSION="${REL#"$BASE"}" make -s -C "$T" "${MK[@]}" modules_prepare > "$TMP/$REL/prepare.log" 2>&1 || { tail -20 "$TMP/$REL/prepare.log"; exit 1; }
  [ "$(cat "$T/include/config/kernel.release")" = "$REL" ] || { echo "the copy calls itself $(cat "$T/include/config/kernel.release"), not $REL"; exit 1; }
  cmp -s "$K/.config" "$T/.config" || { echo "preparing the copy changed its configuration:"; diff "$K/.config" "$T/.config" | head -10; exit 1; }

  mkdir -p "$OUT/dsu-modules/$REL"
  for m in dsu_oc dsu_dyn; do
    step "$REL: $m"
    B=$TMP/$REL/$m; mkdir -p "$B"; cp "$P/tools/dsu/$m/$m.c" "$B/"; echo "obj-m := $m.o" > "$B/Makefile"
    LOCALVERSION="${REL#"$BASE"}" make -s -C "$T" "${MK[@]}" "${MODFLAGS[@]}" M="$B" modules > "$B/build.log" 2>&1 || { tail -30 "$B/build.log"; exit 1; }
    ! grep -qi "warning" "$B/build.log" || { echo "  compiler warnings:"; grep -i -A3 "warning" "$B/build.log" | head -20; }
    strip_ko "$B/$m.ko"
    VM=$(modinfo -F vermagic "$B/$m.ko"); [ "${VM%% *}" = "$REL" ] || { echo "$m.ko says '$VM', not $REL"; exit 1; }
    install -m 644 "$B/$m.ko" "$OUT/dsu-modules/$REL/$m.ko"
    echo "  $OUT/dsu-modules/$REL/$m.ko  ($VM)  sha256 $(sha256sum < "$B/$m.ko" | cut -c1-16)"
  done

  # the kernel's own exFAT, ISO 9660 and uinput where this kernel's configuration leaves them out: each from a copy of
  # its directory in the tree (exFAT and ISO 9660 with the tree's Makefile switched to build it, uinput with a Kbuild
  # of its own) and the C definitions Kconfig would have made; modpost stops a module that needs a symbol this kernel
  # does not export. The set is made in the temporary directory and replaces output/extra-modules/<release> only
  # when all three are made: an existing directory is always a whole set, an empty one a kernel that has all three
  X=$TMP/$REL/extra-modules; mkdir -p "$X"
  for m in exfat isofs uinput; do
    case $m in exfat) opt=EXFAT_FS ;; isofs) opt=ISO9660_FS ;; uinput) opt=INPUT_UINPUT ;; esac
    if grep -qE "^CONFIG_$opt=[ym]$" "$TMP/$REL/config"; then step "$REL: $m is in the kernel itself (CONFIG_$opt)"; continue; fi
    step "$REL: $m (CONFIG_$opt is not set in this kernel)"
    B=$TMP/$REL/$m; mkdir -p "$B"
    case $m in
      exfat)  for f in "$T"/fs/exfat/*.[ch]; do case "$f" in *.mod.c) ;; *) cp "$f" "$B/" ;; esac; done
              { sed 's/^obj-$(CONFIG_EXFAT_FS)/obj-m/' "$T/fs/exfat/Makefile"
                echo 'ccflags-y += -DCONFIG_EXFAT_DEFAULT_IOCHARSET=\"utf8\"'; } > "$B/Kbuild" ;;
      # Joliet (the long names on most ISO images) on; zisofs (transparently compressed CDs) stays off
      isofs)  for f in "$T"/fs/isofs/*.[ch]; do case "$f" in *.mod.c) ;; *) cp "$f" "$B/" ;; esac; done
              { sed 's/^obj-$(CONFIG_ISO9660_FS)/obj-m/; s/-$(CONFIG_JOLIET)/-y/' "$T/fs/isofs/Makefile"
                echo 'ccflags-y += -DCONFIG_JOLIET=1'; } > "$B/Kbuild" ;;
      # uinput.c includes "../input-compat.h"
      uinput) mkdir -p "$B/misc"; cp "$T/drivers/input/input-compat.h" "$B/"; cp "$T/drivers/input/misc/uinput.c" "$B/misc/"
              B=$B/misc; echo 'obj-m := uinput.o' > "$B/Kbuild" ;;
    esac
    grep -q "^obj-m.*\b$m\.o" "$B/Kbuild" || { echo "  the tree's Makefile for $m no longer reads as this script expects:"; cat "$B/Kbuild"; exit 1; }
    LOCALVERSION="${REL#"$BASE"}" make -s -C "$T" "${MK[@]}" "${MODFLAGS[@]}" M="$B" modules > "$B/build.log" 2>&1 || { tail -30 "$B/build.log"; exit 1; }
    ! grep -qi "warning" "$B/build.log" || { echo "  compiler warnings:"; grep -i -A3 "warning" "$B/build.log" | head -20; }
    strip_ko "$B/$m.ko"
    VM=$(modinfo -F vermagic "$B/$m.ko"); [ "${VM%% *}" = "$REL" ] || { echo "$m.ko says '$VM', not $REL"; exit 1; }
    install -m 644 "$B/$m.ko" "$X/$m.ko"
    echo "  $OUT/extra-modules/$REL/$m.ko  ($VM)  sha256 $(sha256sum < "$B/$m.ko" | cut -c1-16)"
  done
  mkdir -p "$OUT/extra-modules"; rm -rf "$OUT/extra-modules/$REL"; cp -a "$X" "$OUT/extra-modules/$REL"
done
rm -rf "$T"
step "done"
