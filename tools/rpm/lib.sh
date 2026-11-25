# Shared by tools/build-rpm.sh, tools/build-tools-rpm.sh and tools/build-mesa-rpm.sh (source it; needs P and OUT set).
#   a7a_rpmbuild <spec> <target|noarch> <name-glob> [--define ...]  -> prints the package path in $OUT/rpms
# Packaging only: every spec copies files that were compiled elsewhere (the laptop cross-compiles the kernel, the
# board builds the Mesa driver), so `rpmbuild --target aarch64-linux` on x86 is fine and no post-install scan runs.
#   a7a_board_ssh / a7a_board_scp: the board from a7a-fedora.json (board_user, board_host, ssh_identity), overridable
#   with A7A_BOARD_HOST / A7A_SSH_KEY. a7a_src_date <paths...>: YYYYMMDD of the newest commit touching them (today
#   when uncommitted or outside git), the Version of the script packages.
a7a_rpmbuild() {
  local spec=$1 target=$2 glob=$3; shift 3
  local top; mkdir -p "$OUT/rpms"; top=$(mktemp -d "$OUT/rpmbuild.XXXXXX") || { echo "cannot create a build directory under $OUT" >&2; return 1; }
  mkdir -p "$top"/{BUILD,RPMS,SOURCES,SPECS,SRPMS,BUILDROOT}
  local targ=(); [ "$target" = noarch ] || targ=(--target "$target")
  # _buildhost: rpm writes the build machine's host name into every package, and from there into the package database
  # of every system that installs it; the project's name stands there instead (2026-11-16)
  if ! rpmbuild --define "_topdir $top" --define "_binary_payload w2T.xzdio" --define "_buildhost a7a-fedora" "${targ[@]}" "$@" -bb "$spec" > "$top/rpmbuild.log" 2>&1; then
    grep -E "error|Error|Bad|not found" "$top/rpmbuild.log" | head -20 >&2
    echo "rpmbuild failed for $spec (log kept: $top/rpmbuild.log)" >&2; return 1
  fi
  local rpm; rpm=$(find "$top/RPMS" -name "$glob" | head -1)
  [ -n "$rpm" ] || { echo "rpmbuild produced no $glob (log kept: $top/rpmbuild.log)" >&2; return 1; }
  cp "$rpm" "$OUT/rpms/"; rm -rf "$top"
  echo "$OUT/rpms/$(basename "$rpm")"
}

a7a_rpm_summary() {   # one line: name, version, release, arch, size, file count
  rpm -qip "$1" 2>/dev/null | grep -E "^(Name|Version|Release|Architecture|Size)" | awk -F': *' '{printf "%s=%s ", $1, $2}'
  echo "files=$(rpm -qlp "$1" 2>/dev/null | wc -l)"
}

a7a_src_date() {
  local d
  if git -C "$P" rev-parse --is-inside-work-tree >/dev/null 2>&1 && git -C "$P" diff --quiet HEAD -- "$@" 2>/dev/null; then
    d=$(git -C "$P" log -1 --format=%cd --date=format:%Y%m%d -- "$@" 2>/dev/null)
  fi
  echo "${d:-$(date +%Y%m%d)}"
}

_a7a_board_cfg() { python3 -c "import json,sys; c=json.load(open(sys.argv[1])); print(c.get(sys.argv[2]) or '')" "$P/a7a-fedora.json" "$1" 2>/dev/null; }
a7a_board_host() { echo "${A7A_BOARD_HOST:-$(_a7a_board_cfg board_host)}"; }
a7a_board_user() { echo "${A7A_BOARD_USER:-$(_a7a_board_cfg board_user)}"; }
a7a_board_key()  { echo "${A7A_SSH_KEY:-$(_a7a_board_cfg ssh_identity)}"; }
a7a_ssh_opts() {
  local key; key=$(a7a_board_key)
  echo -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8 -o LogLevel=ERROR ${key:+-i "$key"}
}
a7a_board_ssh() { ssh $(a7a_ssh_opts) "$(a7a_board_user)@$(a7a_board_host)" "$@"; }
a7a_board_scp_from() { scp $(a7a_ssh_opts) "$(a7a_board_user)@$(a7a_board_host):$1" "$2"; }
