# mesa-pvr-a7a: a7a-fedora's build of Mesa's PowerVR Vulkan driver (the standalone ICD libvulkan_powervr_mesa.so)
# with the fixes from tools/mesa/build-pvr-vulkan.sh (scratch-buffer reuse, render-state cache, load-op program
# cache, sub-allocator free list, BVNC 36.56.104.183 conformant), installed under /usr/local so nothing of Fedora's
# Mesa is replaced. Built ON THE BOARD by tools/mesa/build-pvr-vulkan.sh (meson install into a staging tarball),
# packaged on the laptop by tools/build-mesa-rpm.sh with --define "a7a_mesa_ver 26.1.8" --define "a7a_build
# YYYYMMDDHHMM" --define "a7a_stage <tarball>". Packaging only: nothing is compiled here.
#
# Fedora's own PowerVR ICD JSON would make the Vulkan loader enumerate both drivers (Fedora's refuses this GPU without
# PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 and has none of the fixes), so %post masks it the way the board did by hand
# (rename to .disabled-by-a7a, what a7a-gpu-check expects), %postun on erase unmasks it, and the trigger re-masks it
# after every update of mesa-vulkan-drivers, which re-creates the file. `rpm -V mesa-vulkan-drivers` therefore lists
# the JSON as missing while this package is installed: intended.
Name:           mesa-pvr-a7a
Version:        %{a7a_mesa_ver}
Release:        %{a7a_build}
Summary:        Mesa PowerVR Vulkan driver %{a7a_mesa_ver} with the a7a-fedora fixes, in /usr/local (Radxa Cubie A7A)
License:        MIT
URL:            https://gitlab.freedesktop.org/mesa/mesa
AutoReqProv:    no
Requires:       vulkan-loader
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none
%global __os_install_post %{nil}
%global fedora_icd /usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json

%description
The PowerVR (BXM-4-64, drm/imagination) Vulkan driver from Mesa %{a7a_mesa_ver} with a7a-fedora's fixes for the
KWin compositor on the open GPU stack of the Radxa Cubie A7A. Needs the kernel-a7apvr kernel (open driver); the
ICD JSON in /usr/local/share/vulkan/icd.d is picked up by the Vulkan loader without any environment variable.

%install
mkdir -p %{buildroot}
tar -C %{buildroot} -xzf "%{a7a_stage}"
# everything the staging tarball carries; directories below /usr/local/{lib64,share,...} (which the filesystem
# package owns, depth 1) are ours and listed as %%dir so an erase removes them
( cd %{buildroot} && {
    find usr/local -mindepth 2 -type d | sed 's|^|%%dir /|'
    find usr/local \( -type f -o -type l \) | sed 's|^|/|'
  } ) > %{_builddir}/mesa-pvr-a7a.files

%post
[ -f %{fedora_icd} ] && mv -f %{fedora_icd} %{fedora_icd}.disabled-by-a7a || :

%postun
if [ "$1" = 0 ] && [ -f %{fedora_icd}.disabled-by-a7a ] && [ ! -f %{fedora_icd} ]; then
  mv %{fedora_icd}.disabled-by-a7a %{fedora_icd}
fi

%triggerin -- mesa-vulkan-drivers
[ -f %{fedora_icd} ] && mv -f %{fedora_icd} %{fedora_icd}.disabled-by-a7a || :

%files -f %{_builddir}/mesa-pvr-a7a.files
