# a7a-repo: where a board gets the a7a-fedora packages from after the image (tools/repo/a7a-fedora.repo) and the key
# they are signed with (tools/repo/RPM-GPG-KEY-a7a-fedora, made once by tools/repo-key.sh). Fed by
# tools/build-tools-rpm.sh with --define "a7a_src <repo>/tools/repo" --define "a7a_key <key file>" --define
# "a7a_ver YYYYMMDD" --define "a7a_build YYYYMMDDHHMM". tools/master-image.sh installs it and imports the key into the
# image's rpm database, so Discover never has to ask about it. A new key comes with a new build of this package,
# signed with the old one.
Name:           a7a-repo
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        The package repository and signing key of a7a-fedora (Radxa Cubie A7A)
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-a7a-fedora44
BuildArch:      noarch
AutoReqProv:    no

%description
/etc/yum.repos.d/a7a-fedora.repo (the board packages: Mesa, the A7A Panel, the board tools; kernels come with a new image) and
/etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora, the public key they are signed with.

%install
install -D -m 644 "%{a7a_src}/a7a-fedora.repo" %{buildroot}/etc/yum.repos.d/a7a-fedora.repo
install -D -m 644 "%{a7a_key}" %{buildroot}/etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora

%files
%config(noreplace) /etc/yum.repos.d/a7a-fedora.repo
/etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora
