# kernel-a7a / kernel-a7apvr: one a7a-fedora kernel payload as an RPM. Fed by tools/build-rpm.sh with
#   --define "a7a_name kernel-a7apvr" --define "a7a_flavour a7apvr" --define "a7a_rel 6.6.98-a7apvr.202609091821"
#   --define "a7a_ver 6.6.98_a7apvr.202609091821" --define "a7a_build 202609092010" --define "a7a_payload <dir>"
# Packaging only (rpmbuild --target aarch64 on the laptop): the payload was compiled by tools/build-kernel.sh.
#
# Install-only, like Fedora's kernels: `Provides: installonlypkg(kernel)` makes dnf keep versions side by side
# (installonly_limit, default 3, erases the oldest and never the running one) instead of upgrading, so the previous
# kernel really stays as the extlinux fallback. %post adds the extlinux entry (A7A_DEFAULT=1 in dnf's environment makes
# it the default and moves the previous default to entry 2); %postun drops the erased kernel's entry, or, when it was
# the default, falls back to the previous kernel first. Nothing here prunes: the update stage does that explicitly.
Name:           %{a7a_name}
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        Radxa Cubie A7A kernel %{a7a_rel} (Allwinner A733 BSP 6.6 + a7a-fedora patches, %{a7a_flavour} flavour)
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-cubie-a7a-kernel
AutoReqProv:    no
Requires:       a7a-extlinux
Requires:       kmod
Provides:       %{a7a_name}-%{a7a_rel}
Provides:       installonlypkg(kernel)
Provides:       kernel-uname-r = %{a7a_rel}
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none
%undefine __brp_mangle_shebangs
# the payload is aarch64 code packaged on x86: none of the post-install scans apply
%global __os_install_post %{nil}
%global a7a_shared_mtime 1767225600
%global depmod_files modules.alias modules.alias.bin modules.builtin.alias.bin modules.builtin.bin modules.dep modules.dep.bin modules.devname modules.softdep modules.symbols modules.symbols.bin modules.weakdep

%description
The a7a-fedora kernel payload for release %{a7a_rel}: Image, device trees, modules, firmware.
%if "%{a7a_flavour}" == "a7apvr"
Open GPU stack flavour: the upstream drm/imagination driver (tools/pvr-graft.sh) with its firmware
rogue_36.56.104.183_v1.fw; no pvrsrvkm. Pair it with mesa-pvr-a7a for the userspace driver.
%else
Vendor GPU stack flavour: pvrsrvkm (fdinfo NULL-guard + dma-buf importer fix) for the DDK under /opt/pvr.
%endif
Installing adds the extlinux entry fedora-%{a7a_rel}; A7A_DEFAULT=1 in the environment of dnf/rpm makes it
the default (the previous default becomes entry 2, the fallback). Erasing removes the entry again.

%install
mkdir -p %{buildroot}/boot %{buildroot}/usr/lib/linux-image-%{a7a_rel}/allwinner %{buildroot}/usr/lib/modules %{buildroot}/usr/lib/firmware
cp "%{a7a_payload}/boot/Image-%{a7a_rel}" %{buildroot}/boot/vmlinuz-%{a7a_rel}
cp "%{a7a_payload}"/boot/dtb/allwinner/*.dtb %{buildroot}/usr/lib/linux-image-%{a7a_rel}/allwinner/
cp -a "%{a7a_payload}/lib/modules/%{a7a_rel}" %{buildroot}/usr/lib/modules/
# the build symlink points into the laptop's source tree: useless on the board
rm -f %{buildroot}/usr/lib/modules/%{a7a_rel}/build %{buildroot}/usr/lib/modules/%{a7a_rel}/source
# depmod's output is generated on the board by %%post and owned as %%ghost (as Fedora's kernel-core does), so
# `rpm -V` stays clean and an erase removes it
for f in %{depmod_files}; do rm -f "%{buildroot}/usr/lib/modules/%{a7a_rel}/$f"; done
cp -a "%{a7a_payload}/lib/firmware/aic8800D80" %{buildroot}/usr/lib/firmware/
%if "%{a7a_flavour}" == "a7apvr"
mkdir -p %{buildroot}/usr/lib/firmware/powervr
cp "%{a7a_payload}/lib/firmware/powervr/rogue_36.56.104.183_v1.fw" %{buildroot}/usr/lib/firmware/powervr/
%else
mkdir -p %{buildroot}/etc/a7a
echo "pvrsrvkm from a7a-fedora build-extras: tools/patches/0001-pvr_drm-fdinfo-null-connection.patch and 0005-pvr_dmabuf-map-for-importer.patch applied (pvr-desktop keys on this marker)" > %{buildroot}/etc/a7a/pvrsrvkm-fdinfo-fixed
%endif
# Files SHARED between kernel packages (the firmware: both flavours and every version; the DDK marker: every
# kernel-a7a) must carry the same mtime in every package, or the last install re-stamps them and `rpm -V` of the
# other packages reports `.......T.` (seen 2026-09-10 after installing a second kernel-a7apvr). Content is identical
# by construction; the mtime is pinned to a constant (2026-01-01 00:00:00 UTC) instead of the copy or build time.
find %{buildroot} \( -path "%{buildroot}/usr/lib/firmware/*" -o -path "%{buildroot}/etc/a7a/*" \) -type f -exec touch -d @%{a7a_shared_mtime} {} +

%post
depmod %{a7a_rel} || :
if [ -f /boot/extlinux/extlinux.conf ] && [ -x /usr/local/bin/a7a-extlinux ]; then
  if [ "${A7A_DEFAULT:-0}" = 1 ]; then
    /usr/local/bin/a7a-extlinux --check-files add %{a7a_rel} --default
  else
    /usr/local/bin/a7a-extlinux --check-files add %{a7a_rel}
  fi
fi

%postun
# Runs for every erase of this version, also when dnf's installonly_limit retires it while newer versions stay.
# Two builds of the SAME release (same payload, another Release stamp) can be installed side by side; rpm keeps
# the files while one of them remains, so the entry is dropped only once the kernel image is really gone.
if [ ! -e /boot/vmlinuz-%{a7a_rel} ] && [ -f /boot/extlinux/extlinux.conf ] && [ -x /usr/local/bin/a7a-extlinux ]; then
  L=fedora-%{a7a_rel}
  if /usr/local/bin/a7a-extlinux list | awk -v l="$L" '$1 == "*" && $2 == l {f = 1} END {exit !f}'; then
    /usr/local/bin/a7a-extlinux --check-files fallback || echo "a7a: no other bootable kernel entry: /boot/extlinux/extlinux.conf still defaults to $L, whose kernel is gone" >&2
  fi
  /usr/local/bin/a7a-extlinux remove "$L" || :
fi

%files
/boot/vmlinuz-%{a7a_rel}
/usr/lib/linux-image-%{a7a_rel}
/usr/lib/modules/%{a7a_rel}
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.alias
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.alias.bin
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.builtin.alias.bin
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.builtin.bin
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.dep
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.dep.bin
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.devname
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.softdep
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.symbols
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.symbols.bin
%ghost %attr(0644,root,root) /usr/lib/modules/%{a7a_rel}/modules.weakdep
/usr/lib/firmware/aic8800D80
%if "%{a7a_flavour}" == "a7apvr"
/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw
%else
%dir /etc/a7a
/etc/a7a/pvrsrvkm-fdinfo-fixed
%endif
