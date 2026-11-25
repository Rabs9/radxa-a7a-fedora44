# a7a-extlinux: the extlinux.conf editor the kernel packages, the update stage and the board scripts use.
# Its own package on purpose: a file shipped by several kernel packages becomes an rpm file conflict the first time
# its content changes. Fed by tools/build-tools-rpm.sh with --define "a7a_src <repo>/tools" --define "a7a_ver YYYYMMDD"
# --define "a7a_build YYYYMMDDHHMM". /usr/local/bin is /usr/local/sbin on Fedora 44 (sbin merge, symlink).
Name:           a7a-extlinux
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        Edit /boot/extlinux/extlinux.conf on the Radxa Cubie A7A without breaking the boot chain
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-a7a-fedora44
BuildArch:      noarch
AutoReqProv:    no
Requires:       python3
# The 2026-09-06/07 kernel-a7a packages shipped this file themselves, as /usr/local/sbin/a7a-extlinux (and the WiFi
# firmware as /lib/firmware/...): other rpm path strings for the same inodes through the sbin and /lib symlinks, so
# rpm would neither see a conflict nor protect the files when that package is erased. Refuse to coexist with exactly
# those two builds (release-qualified: a re-packaging of the same payload with the current spec is fine); the owner's
# board forgets its copy with `rpm -e --justdb` first (docs/PACKAGING-2026-09-09.md).
Conflicts:      kernel-a7a = 6.6.98_a7a.202609072057-202609072103
Conflicts:      kernel-a7a = 6.6.98.plus-202609062352
%undefine __brp_mangle_shebangs

%description
a7a-extlinux list | add <release> [--default] | fallback | default <label> | prune [--keep N] | remove <label>.
Used by the kernel-a7a and kernel-a7apvr scriptlets, by the a7a-fedora update stage and by the board scripts.

%install
install -D -m 755 "%{a7a_src}/a7a-extlinux" %{buildroot}/usr/local/bin/a7a-extlinux

%files
/usr/local/bin/a7a-extlinux
