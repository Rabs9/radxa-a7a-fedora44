#!/bin/bash
# Builds a7a-panel (tools/panel) for the board: aarch64, on this x86_64 laptop, with nothing installed system-wide.
#   - Rust: a toolchain of its own under work/rust (rustup, pinned to the version the crate was written with), with
#     the aarch64 standard library; Fedora's rustc has no aarch64-linux target.
#   - Linking: the cross gcc (gcc-aarch64-linux-gnu, a kernel compiler without a C library) against a sysroot made of
#     Fedora 44's own aarch64 glibc, libgcc and kernel headers (work/sysroot-aarch64). The panel needs only libc,
#     libm and libgcc_s; Wayland, X11 and the GPU libraries are opened at run time.
# Output: output/panel/a7a-panel (aarch64), output/panel/a7a-panel.sha256 (of tools/panel's sources) and the package
# output/rpms/a7a-panel-<YYYYMMDD>-<build>.aarch64.rpm (tools/rpm/a7a-panel.spec; A7A_RPM_BUILD sets the Release).
# Usage: tools/build-panel.sh [--test]   --test also runs the crate's tests on this machine first
set -euo pipefail
P=$(cd "$(dirname "$0")/.." && pwd)
W=$P/work
RUST_VERSION=1.98.0
TARGET=aarch64-unknown-linux-gnu
SR=$W/sysroot-aarch64
export RUSTUP_HOME=$W/rust/rustup
RUSTUP_BIN=$W/rust/cargo/bin
PANEL_OUT=$P/output/panel

if [ ! -x "$RUSTUP_BIN/cargo" ] || ! "$RUSTUP_BIN/rustup" target list --installed --toolchain "$RUST_VERSION" 2>/dev/null | grep -qx "$TARGET"; then
  echo "== Rust $RUST_VERSION with $TARGET into work/rust"
  mkdir -p "$W/rust/dl"
  ( cd "$W/rust/dl"
    curl -sSfO https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init
    curl -sSfO https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init.sha256
    sed 's| \*\./| |' rustup-init.sha256 | sha256sum -c --quiet
    chmod +x rustup-init )
  CARGO_HOME=$W/rust/cargo "$W/rust/dl/rustup-init" -y --no-modify-path --profile minimal \
    --default-toolchain "$RUST_VERSION" --target "$TARGET" > /dev/null
fi

if [ ! -e "$SR/usr/lib64/libgcc_s.so" ]; then
  echo "== aarch64 sysroot from Fedora's packages into work/sysroot-aarch64"
  rm -rf "$SR"
  mkdir -p "$SR" "$W/sysroot-aarch64-rpms"
  for pkg in glibc glibc-devel libgcc kernel-headers; do
    ls "$W/sysroot-aarch64-rpms/$pkg"-[0-9]*.aarch64.rpm > /dev/null 2>&1 && continue
    # stdin closed: a repository whose signing key is not imported yet asks, and nobody would answer
    timeout 600 dnf download --forcearch=aarch64 --arch=aarch64 --destdir "$W/sysroot-aarch64-rpms" "$pkg" < /dev/null > /dev/null \
      || { echo "dnf download --forcearch=aarch64 $pkg failed; put the aarch64 $pkg RPM of Fedora 44 into work/sysroot-aarch64-rpms"; exit 1; }
  done
  for r in "$W"/sysroot-aarch64-rpms/*.rpm; do
    rpm2cpio "$r" | (cd "$SR" && cpio -idm --quiet 2> /dev/null)
  done
  # the merged /usr layout of a Fedora system (the filesystem package is not unpacked)
  if [ -d "$SR/lib64" ] && [ ! -L "$SR/lib64" ]; then mv "$SR"/lib64/* "$SR/usr/lib64/"; rmdir "$SR/lib64"; fi
  ln -sfn usr/lib64 "$SR/lib64"
  ln -sfn usr/lib "$SR/lib"
  ln -sfn libgcc_s.so.1 "$SR/usr/lib64/libgcc_s.so"
fi

if [ "${1:-}" = "--test" ]; then
  echo "== tests (on this machine)"
  (cd "$P/tools/panel" && cargo test --release --quiet 2>&1 | grep -E "^test result|FAILED|panicked")
fi

echo "== a7a-panel for $TARGET"
# the sources' fingerprint, taken BEFORE the build and checked after it: a file edited during the build would
# otherwise leave a binary of the old sources under the new fingerprint (review, 2026-11-17)
src_sum() { (cd "$P/tools/panel" && find Cargo.toml Cargo.lock src vendor -type f | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1); }
SUM=$(src_sum)
# the toolchain's own cargo and rustc first in PATH: cargo runs `rustc` from PATH, and Fedora's has no aarch64 std
TC=$(ls -d "$RUSTUP_HOME/toolchains/$RUST_VERSION"-x86_64-unknown-linux-gnu)
export PATH=$TC/bin:$PATH
export CARGO_HOME=${CARGO_HOME:-$HOME/.cargo}                  # the crate cache this laptop already has
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
# the builder's paths out of the binary: panic messages name each crate's source file, under CARGO_HOME (in the home
# folder) and this repository; remapped they read /cargo/... and /a7a-fedora/... (review 2026-11-24: 620 home paths)
export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-C link-arg=--sysroot=$SR --remap-path-prefix=$CARGO_HOME=/cargo --remap-path-prefix=$P=/a7a-fedora --remap-path-prefix=$RUSTUP_HOME=/rustup"
export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc       # ring's C and assembly
export CFLAGS_aarch64_unknown_linux_gnu="--sysroot=$SR"
# no GPU drawing on the board (Cargo.toml, feature gpu): software rendering with tiny-skia
(cd "$P/tools/panel" && cargo build --release --target "$TARGET" --no-default-features --quiet)
BIN=$P/tools/panel/target/$TARGET/release/a7a-panel
file "$BIN" | grep -q "ARM aarch64" || { echo "not an aarch64 binary: $(file "$BIN")"; exit 1; }
# and nothing of the builder's home left in it (the remapping above missed one: a new dependency's build script?)
! grep -aqF "$HOME/" "$BIN" || { echo "the binary still holds $HOME paths: $(grep -aoF "$HOME/" "$BIN" | wc -l)"; exit 1; }
needed=$(aarch64-linux-gnu-objdump -p "$BIN" | awk '/NEEDED/{print $2}' | sort | tr '\n' ' ')
for n in $needed; do
  case $n in ld-linux-aarch64.so.1|libc.so.6|libgcc_s.so.1|libm.so.6) ;; *) echo "unexpected library: $n"; exit 1 ;; esac
done
mkdir -p "$PANEL_OUT"
install -m 755 "$BIN" "$PANEL_OUT/a7a-panel"
# what the binary was built from, so the image build can tell a stale binary from a current one
[ "$(src_sum)" = "$SUM" ] || { echo "tools/panel changed during the build: run tools/build-panel.sh again"; exit 1; }
echo "$SUM" > "$PANEL_OUT/a7a-panel.sha256"
echo "built $PANEL_OUT/a7a-panel ($(du -h "$PANEL_OUT/a7a-panel" | cut -f1), needs: $needed)"

OUT=${A7A_OUTPUT:-$P/output}
. "$P/tools/rpm/lib.sh"
VER=$(a7a_src_date tools/panel tools/rpm/a7a-panel.spec tools/board/a7a-panel.desktop tools/board/a7a-assistant.desktop)
RPM=$(a7a_rpmbuild "$P/tools/rpm/a7a-panel.spec" aarch64-linux "a7a-panel-*.rpm" --define "a7a_src $P" \
  --define "a7a_bin $PANEL_OUT/a7a-panel" --define "a7a_ver $VER" --define "a7a_build ${A7A_RPM_BUILD:-$(date +%Y%m%d%H%M)}")
echo "packaged $RPM"
