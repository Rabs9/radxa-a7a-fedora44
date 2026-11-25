# mesa-img-a7a: Imagination's open Mesa glue for the vendor PowerVR DDK, built with the Wayland platform (TI's
# github.com/TexasInstruments/mesa, branch powervr/24.0.1, pinned; Radxa's /opt/pvr build of the same glue is X11-only),
# in /opt/pvr-wl: libEGL/libgbm/libGLESv1_CM/libGLESv2/libglapi, dri/pvr_dri.so (+ the sunxi-drm alias the DDK needs
# for card0) and libpvr_mesa_wsi.so, the Vulkan WSI that the DDK's libVK_IMG dlopens. No GLX: the DDK drop has no
# desktop-GL module. Built ON THE BOARD by tools/mesa-img/build-mesa-img.sh (meson install into a staging tarball),
# packaged on the laptop by tools/build-mesa-img-rpm.sh with --define "a7a_mesa_ver 24.0.1" --define "a7a_build
# YYYYMMDDHHMM" --define "a7a_pin <12 hex>" --define "a7a_stage <tarball>". Packaging only: nothing is compiled here.
# docs/DDK-WAYLAND-2026-09-10.md.
#
# The closed DDK (/opt/pvr, 24.2@6603887) is NOT packaged. lib/ddk/ holds links to the seven closed libraries the glue
# and the GLES modules load (all with unversioned sonames), so a library path entry can expose exactly those and not
# the rest of /opt/pvr/lib (Radxa's libvulkan.so.1 built without Wayland, libxcvt, opencv aliases, its X11-only
# libEGL/libgbm). Dangling when /opt/pvr is absent (a fresh image): nothing uses them then, and a7a-gpu-session only
# activates the stack when they resolve. Never add a VERSIONED name there: ldconfig deletes dangling versioned links.
#
# Nothing here is on any library path by itself. a7a-gpu-session.service (a7a-gpu-tools) writes the %ghost ld.so.conf.d
# entry on a boot where pvrsrvkm drives the GPU and removes it on the open-stack boot, where Fedora's Mesa and the a7a
# Vulkan driver must stay untouched; an erase removes it too (%ghost) and %postun rebuilds the loader cache.
# No automatic Provides: the package must never satisfy another package's libEGL.so.1/libgbm.so.1 dependency.
Name:           mesa-img-a7a
Version:        %{a7a_mesa_ver}
Release:        %{a7a_build}
Summary:        Imagination's Mesa %{a7a_mesa_ver} glue for the PowerVR DDK with Wayland, in /opt/pvr-wl (Radxa Cubie A7A)
License:        MIT
URL:            https://github.com/TexasInstruments/mesa/tree/powervr/%{a7a_mesa_ver}
AutoReqProv:    no
# the ELF NEEDED entries of the staged files, outside the package and the DDK (tools/mesa-img/build-mesa-img.sh stage)
Requires:       glibc libgcc libstdc++ expat zlib-ng-compat libzstd libdrm systemd-libs libwayland-client libwayland-server
Requires:       libxcb libX11-xcb libxshmfence xcb-util-keysyms
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none
%global __os_install_post %{nil}
%global ddk_libs libpvr_dri_support.so libGLESv2_PVR_MESA.so libGLESv1_CM_PVR_MESA.so libsrv_um.so libglslcompiler.so libusc.so libufwriter.so

%description
Imagination's open Mesa %{a7a_mesa_ver} glue for the closed PowerVR DDK (the DRI frontend that loads
libpvr_dri_support.so and the Vulkan WSI that libVK_IMG loads), TI's powervr/%{a7a_mesa_ver} branch at %{a7a_pin},
built with the X11, Wayland, GBM and surfaceless EGL platforms for the Radxa Cubie A7A. Needs the DDK userspace in
/opt/pvr and the kernel-a7a kernel (pvrsrvkm). a7a-gpu-session.service makes it the system EGL/GLES/GBM layer on the
DDK boot only.

%install
mkdir -p %{buildroot}
tar -C %{buildroot} -xzf "%{a7a_stage}"
mkdir -p %{buildroot}/opt/pvr-wl/lib/ddk %{buildroot}/etc/ld.so.conf.d
for l in %{ddk_libs}; do ln -s /opt/pvr/lib/$l %{buildroot}/opt/pvr-wl/lib/ddk/$l; done
touch %{buildroot}/etc/ld.so.conf.d/00-a7a-pvr-wl.conf
( cd %{buildroot} && {
    echo "%%dir /opt/pvr-wl"
    find opt/pvr-wl -mindepth 1 -type d | sed 's|^|%%dir /|'
    find opt/pvr-wl \( -type f -o -type l \) | sed 's|^|/|'
  } ) > %{_builddir}/mesa-img-a7a.files

%post -p /sbin/ldconfig
%postun -p /sbin/ldconfig

%files -f %{_builddir}/mesa-img-a7a.files
%ghost %attr(0644,root,root) /etc/ld.so.conf.d/00-a7a-pvr-wl.conf
