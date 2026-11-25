#!/bin/bash
# master-rewrap: run INSIDE the image by tools/master-image.sh (chroot-run, in its sandbox), from the folder that holds
# the re-wrapped packages (tools/rewrap-rpm.sh): each is put over the card's copy of the same package.
#   bash master-rewrap.sh <the build machine's host name> <package.rpm>...
# The card's kernels and Mesa driver stay as they are (proven on the board); only their packages named the build
# machine. A re-wrapped package is the same Name, Version and Release with the same files, so it goes in with rpm
# --replacepkgs and no scripts (there is nothing to run: nothing changes on disk). Before that, for each:
#   - the card's files are what the installed package says (rpm -V of the files alone, their times aside;
#     the dependencies are the audit's);
#   - the re-wrapped package says the same of them (path, size, digest, mode, owner, group, flags, link);
# and after it: rpm -V is clean against the new package, which names a7a-fedora. Then the package database is
# rebuilt (the old headers would stay in its free pages), and no package of the image names the build machine.
#   A7A_RPM_ROOT=<dir>  rpm --root <dir> (the tests: a package database of their own)
set -u
HOST=${1:?the host name of the build machine}; shift
rpm() { command rpm ${A7A_RPM_ROOT:+--root "$A7A_RPM_ROOT"} "$@"; }
rpmdb() { command rpmdb ${A7A_RPM_ROOT:+--root "$A7A_RPM_ROOT"} "$@"; }
rc=0
for f in "$@"; do
  n=$(rpm -qp --qf '%{NEVRA}' "$f" 2>/dev/null) || { echo "$f: not a package"; rc=1; continue; }
  rpm -q "$n" > /dev/null 2>&1 || { echo "$n: not installed in the image"; rc=1; continue; }
  if ! out=$(rpm -V --nodeps --noscripts --nomtime "$n" 2>&1); then echo "$n: the card's files are not as its package says:"; echo "$out" | head -8; rc=1; continue; fi
  if ! diff <(rpm -q --dump "$n" | awk '{$3=""; print}' | LC_ALL=C sort) <(rpm -qp --dump "$f" | awk '{$3=""; print}' | LC_ALL=C sort) > /tmp/a7a-rewrap.diff; then
    echo "$n: the re-wrapped package describes other files than the card's:"; head -5 /tmp/a7a-rewrap.diff; rc=1; continue
  fi
  # (a test root has no a7a-extlinux nor kmod, and runs on x86: there only)
  rpm -i --replacepkgs --noscripts --notriggers ${A7A_RPM_ROOT:+--nodeps --ignorearch} "$f" || { echo "$n: rpm could not put it in"; rc=1; continue; }
  [ "$(rpm -q --qf '%{BUILDHOST}' "$n")" = a7a-fedora ] || { echo "$n: the database still holds the old package"; rc=1; continue; }
  if ! out=$(rpm -V --nodeps --noscripts "$n" 2>&1); then echo "$n: after the change, rpm -V says:"; echo "$out" | head -8; rc=1; continue; fi
  echo "$n: the re-wrapped package is in (its files as they were)"
done
rm -f /tmp/a7a-rewrap.diff
[ "$rc" = 0 ] || exit 1
rpmdb --rebuilddb || { echo "rpmdb --rebuilddb failed"; exit 1; }
left=$(rpm -qa --qf '%{NEVRA} %{BUILDHOST}\n' | awk -v h="$HOST" '$2 == h {print $1}' | tr '\n' ' ')
[ -z "$left" ] || { echo "packages of the image that still name the build machine ($HOST): $left"; exit 1; }
echo "no package of the image names the build machine"
