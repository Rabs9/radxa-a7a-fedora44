#!/bin/bash
# build-mesa-img.sh: ON THE BOARD. Build Imagination's open Mesa glue for the vendor DDK with the Wayland platform:
# TI's publication of it (github.com/TexasInstruments/mesa, branch powervr/24.0.1 = the Mesa version Radxa built
# /opt/pvr from), pinned, for prefix /opt/pvr-wl. /opt/pvr (the closed DDK 24.2@6603887 and Radxa's X11-only Mesa
# build) is never touched: the new libEGL/libgbm/libGLES*/libglapi, dri/pvr_dri.so (+ the sunxi-drm alias) and
# libpvr_mesa_wsi.so (the Vulkan WSI libVK_IMG dlopens) load the closed libraries from /opt/pvr/lib at run time.
# docs/DDK-WAYLAND-2026-09-10.md has the why, the interface check and the results.
# Options match Radxa's build where it had one (checked on the board 2026-09-10: Mesa 24.0.1, libdir lib, no LLVM
# = softpipe for swrast, GLES1+GLES2 over a shared glapi, GBM, no GLX, no glvnd, the sunxi-drm alias) plus the Wayland
# platform (Radxa: x11 only). Two choices measured on the board, not Radxa's defaults by accident:
#   -Dglx=disabled         the DDK drop has no desktop-GL module (libGL_PVR_MESA.so), so a GLX libGL.so.1 fails every
#                          context (X_GLXCreateNewContext BadValue, 2026-09-10) and in the loader cache it would shadow
#                          glvnd's GLX, which runs those apps on llvmpipe today
#   -Dplatforms=x11,wayland  x11 first = EGL's default native platform, as in Fedora's Mesa: an eglGetDisplay(<X11
#                          Display*>) without EGL_PLATFORM must not be taken for a wl_display (Wayland is detected)
# The result is a staging tarball $W/mesa-img-a7a-24.0.1-<pin>-<stamp>.tar.gz (files under opt/pvr-wl) for the
# mesa-img-a7a RPM (tools/build-mesa-img-rpm.sh), which is the only install path: the package adds the lib/ddk links
# that a system-wide activation needs (a7a-gpu-session refuses a tree without them).
# usage: build-mesa-img.sh          (W=/home/radxa/mesa-img unless MESA_IMG_WORK is set)
set -euo pipefail
W=${MESA_IMG_WORK:-/home/radxa/mesa-img}
REPO=https://github.com/TexasInstruments/mesa.git
BRANCH=powervr/24.0.1
PIN=7c82c1eebc67f5a62a347a84d42fe795cf7f523b   # 2026-03-13 "c11/threads: fix build on c23"
PREFIX=/opt/pvr-wl
SRC=$W/mesa-24.0.1
mkdir -p "$W"; cd "$W"
rm -f "$W/last-build.status"
trap 'echo "fail: line $LINENO" > "$W/last-build.status"' ERR
step() { echo "== [$(date +%H:%M:%S)] $*"; }

step "source: $REPO $BRANCH @ ${PIN:0:12}"
[ -d "$SRC/.git" ] || git clone --depth 1 --single-branch --branch "$BRANCH" "$REPO" "$SRC"
if [ "$(git -C "$SRC" rev-parse HEAD)" != "$PIN" ]; then
  git -C "$SRC" fetch --depth 1 origin "$PIN"
  git -C "$SRC" checkout -q --detach "$PIN"
fi
# always pristine sources before the edits below (the tree is this script's own: a previous run's edits are undone)
git -C "$SRC" reset -q --hard "$PIN"

step "patches"
cd "$SRC"
python3 - <<'PY'
def edit(path, old, new, label):
    s=open(path).read()
    if new in s: print("  = %s already applied"%label); return
    assert s.count(old)==1, (label, s.count(old))
    open(path,'w').write(s.replace(old,new)); print("  + %s"%label)
# 1. target.c pastes the gallium alias into the loader entry point __driDriverGetExtensions_<alias>. A kernel display
# driver name with a hyphen (Allwinner's sunxi-drm) is no C identifier ("expected '=' ... before '-' token"). Mesa's
# loader maps '-' to '_' when it looks the symbol up, and Radxa's /opt/pvr build exports
# __driDriverGetExtensions_sunxi_drm, so only this define is underscorified; the file name (sunxi-drm_dri.so) and the
# string uses (pvrdri.c PVR_DRIVER_ALIAS, pvr_wsi.c PVR_DISPLAY_DRIVER: compared with the kernel's driver name) keep it.
edit("src/gallium/drivers/pvr_alias/meson.build",
     "compile_args: '-DGALLIUM_PVR_ALIAS=@0@'.format(gallium_pvr_alias)\n",
     "compile_args: '-DGALLIUM_PVR_ALIAS=@0@'.format(gallium_pvr_alias.underscorify())\n",
     "pvr_alias: loader entry point underscorified (__driDriverGetExtensions_sunxi_drm)")
# 2. The Qt fix. Fedora's Qt is a desktop-GL build: QEglConfigChooser asks eglChooseConfig for EGL_RENDERABLE_TYPE =
# EGL_OPENGL_BIT and never lowers it. The DDK has no desktop-GL module, so no config matched, Qt printed "Cannot find
# EGLConfig, returning null config" and every Qt Quick window stayed without a surface (the logout prompt first:
# docs/DDK-WAYLAND-2026-09-10.md, section 4). Qt switches itself to OpenGL ES when the config it is handed has no
# EGL_OPENGL_BIT (q_glFormatFromConfig), so on a display WITHOUT desktop GL a request for it is answered with the ES
# configs: the bit is dropped from the match criteria only. eglGetConfigAttrib still reports no GL bit, and a program
# that binds EGL_OPENGL_API outright still fails, at eglCreateContext instead of here. Proven from outside first with
# tools/gpu-debug/egl-es-config-shim.c (2026-11-15: kinfocenter, systemsettings, plasma-systemmonitor on the PowerVR,
# GLES 3.2, 0 HWR; docs/RADXA-SYNC-2026-11-15.md).
edit("src/egl/main/eglconfig.c",
     """   if (!_eglParseConfigAttribList(&criteria, disp, attrib_list))
      return _eglError(EGL_BAD_ATTRIBUTE, "eglChooseConfig");

   result = _eglFilterConfigArray(""",
     """   if (!_eglParseConfigAttribList(&criteria, disp, attrib_list))
      return _eglError(EGL_BAD_ATTRIBUTE, "eglChooseConfig");

   /* a7a: this display has no desktop GL; match a desktop-GL request against the ES configs */
   if (!(disp->ClientAPIs & EGL_OPENGL_BIT)) {
      if (criteria.RenderableType != EGL_DONT_CARE &&
          (criteria.RenderableType & EGL_OPENGL_BIT)) {
         criteria.RenderableType &= ~EGL_OPENGL_BIT;
         if (!(criteria.RenderableType &
               (EGL_OPENGL_ES_BIT | EGL_OPENGL_ES2_BIT | EGL_OPENGL_ES3_BIT_KHR)))
            criteria.RenderableType |= EGL_OPENGL_ES2_BIT;
      }
      if (criteria.Conformant != EGL_DONT_CARE &&
          (criteria.Conformant & EGL_OPENGL_BIT))
         criteria.Conformant &= ~EGL_OPENGL_BIT;
   }

   result = _eglFilterConfigArray(""",
     "egl: a desktop-GL config request on a GLES-only display matches the ES configs (the Qt fix)")
# 3. Clients under KWin 6.8 (the update fix). A Wayland client learns its GPU from the compositor's linux-dmabuf
# feedback: the "main device", of which Mesa opens the render node (loader_get_render_node; its only caller is
# default_dmabuf_feedback_main_device in platform_wayland.c). KWin 6.7 names the PowerVR (0xE280, card1/renderD128).
# KWin 6.8 (new GpuManager; under KWIN_RENDER_NODES=/dev/dri/none it composites on its fallback KMS render device)
# names the display controller (0xE200, card0, sunxi-drm), which has no render node. Mesa then wants wl_drm, KWin
# offers none, and every client fell to softpipe (2026-11-16 after `dnf upgrade`: plasmashell on swrast_dri.so,
# Xwayland "Main linux-dmabuf device has no render node", games without an EGL config). A device without a render
# node is never what a client renders with, so the lookup answers with the render node of the GPU (the only one on
# this board). Nothing changes while the compositor names a device that has a render node. Proven from outside first
# with tools/gpu-debug/wl-render-node-shim.c (eglinfo, es2gears and an SDL2 game on the PowerVR under KWin 6.8.3).
edit("src/loader/loader.c",
     r"""         log_(_LOADER_DEBUG, "MESA-LOADER: failed to allocate memory for render node\n");
   }

   drmFreeDevice(&dev_ptr);

   return render_node;""",
     r"""         log_(_LOADER_DEBUG, "MESA-LOADER: failed to allocate memory for render node\n");
   }

   drmFreeDevice(&dev_ptr);

   /* a7a: the compositor names a display-only device; render on the GPU's node */
   if (!render_node) {
      drmDevicePtr devices[8];
      int count = drmGetDevices2(0, devices, 8);

      for (int i = 0; i < count && !render_node; i++) {
         if (devices[i]->available_nodes & (1 << DRM_NODE_RENDER))
            render_node = strdup(devices[i]->nodes[DRM_NODE_RENDER]);
      }
      if (count > 0)
         drmFreeDevices(devices, count);
      if (render_node)
         log_(_LOADER_DEBUG, "MESA-LOADER: the compositor's device has no render node, using %s\n", render_node);
   }

   return render_node;""",
     "loader: a compositor device without a render node is answered with the GPU's render node (KWin 6.8)")
PY
grep -q 'a7a: this display has no desktop GL' src/egl/main/eglconfig.c || { echo "the Qt fix is not in src/egl/main/eglconfig.c"; false; }
grep -q 'a7a: the compositor names a display-only device' src/loader/loader.c || { echo "the KWin 6.8 fix is not in src/loader/loader.c"; false; }

step "meson (prefix $PREFIX)"
rm -rf build
meson setup build --prefix="$PREFIX" --libdir=lib --buildtype=release -Db_ndebug=true \
  -Dplatforms=x11,wayland -Dgallium-drivers=pvr,swrast -Dgallium-pvr-alias=sunxi-drm -Dvulkan-drivers=pvr \
  -Degl=enabled -Dgbm=enabled -Dglx=disabled -Dglvnd=false -Dopengl=true -Dgles1=enabled -Dgles2=enabled \
  -Dshared-glapi=enabled -Ddri3=enabled -Dexpat=enabled -Dzstd=enabled -Dxlib-lease=enabled \
  -Dllvm=disabled -Dgallium-va=disabled -Dgallium-vdpau=disabled -Dgallium-xa=disabled -Dgallium-nine=false \
  -Dgallium-opencl=disabled -Dgallium-rusticl=false -Dosmesa=false -Dmicrosoft-clc=disabled -Dintel-clc=disabled \
  -Dvideo-codecs= -Dvulkan-layers= -Dtools= -Dbuild-tests=false -Dperfetto=false \
  -Dvalgrind=disabled -Dlibunwind=disabled -Dlmsensors=disabled > "$W/meson-setup.log" 2>&1 \
  || { tail -30 "$W/meson-setup.log"; false; }
grep -E "^\s+(Platforms|EGL drivers|GBM|GLX|Gallium drivers|Vulkan drivers|OpenGL ES|shared-glapi|LLVM)" "$W/meson-setup.log" | head -20 || true

step "ninja"
ninja -C build > "$W/ninja.log" 2>&1 || { grep -E "error|FAILED" "$W/ninja.log" | head -20; false; }

STAMP=$(date +%Y%m%d%H%M); STAGE=$W/stage-$STAMP; TARBALL=$W/mesa-img-a7a-24.0.1-${PIN:0:12}-$STAMP.tar.gz
step "stage: $TARBALL"
rm -rf "$STAGE"; DESTDIR=$STAGE ninja -C build install > "$W/install-stage.log" 2>&1
L=$STAGE$PREFIX/lib
# the Xwayland helper: KWin 6.8 names its clients a device without a render node, and Xwayland asks libdrm about that
# device itself (glamor off, X11 programs on the CPU); edit 3 cannot reach it. tools/board/Xwayland (a7a-gpu-tools)
# preloads this library, which answers the lookup with the GPU's render node. Source: tools/gpu-debug/
# wl-render-node-shim.c, copied next to this script. Proven 2026-11-17 on the board: an X11 EGL program got the PowerVR.
SHIM_SRC=$W/wl-render-node-shim.c
[ -f "$SHIM_SRC" ] || { echo "no $SHIM_SRC: copy tools/gpu-debug/wl-render-node-shim.c next to this script"; false; }
mkdir -p "$L/a7a"
gcc -O2 -Wall -shared -fPIC -o "$L/a7a/wl-render-node-shim.so" "$SHIM_SRC" -ldl
nm -D --defined-only "$L/a7a/wl-render-node-shim.so" | grep -q " drmGetDeviceFromDevId$" || { echo "the Xwayland helper does not define drmGetDeviceFromDevId"; false; }
# the checks the rest of the work relies on; a build that misses one is not staged
need() { [ -e "$L/$1" ] || { echo "missing in the stage: $PREFIX/lib/$1"; false; }; }
for f in libEGL.so.1 libgbm.so.1 libglapi.so.0 libGLESv2.so.2 libGLESv1_CM.so.1 libpvr_mesa_wsi.so \
         dri/pvr_dri.so dri/sunxi-drm_dri.so dri/swrast_dri.so a7a/wl-render-node-shim.so; do need "$f"; done
[ ! -e "$L/libGL.so.1" ] || { echo "the stage carries libGL.so.1 (GLX): it must not (see -Dglx above)"; false; }
grep -qa EGL_EXT_platform_wayland "$L/libEGL.so.1" || { echo "libEGL.so.1 has no Wayland platform"; false; }
grep -qa "the compositor's device has no render node" "$L/libEGL.so.1" || { echo "libEGL.so.1 does not carry the KWin 6.8 fix (edit 3)"; false; }
for e in pvr sunxi_drm; do
  nm -D --defined-only "$L/dri/pvr_dri.so" | grep -q " __driDriverGetExtensions_$e$" || { echo "dri/pvr_dri.so does not export __driDriverGetExtensions_$e"; false; }
done
readelf -d "$L/libpvr_mesa_wsi.so" | grep -q 'libwayland-client' || { echo "libpvr_mesa_wsi.so is not linked with libwayland-client"; false; }
( cd "$STAGE" && find opt -type f -o -type l | sort | sed 's/^/  /' ) > "$W/stage-files.txt"
echo "  $(wc -l < "$W/stage-files.txt") files and links (list: $W/stage-files.txt)"
# the closed GLES libraries need a libglapi.so.0 (DT_NEEDED, no symbol imports): same exports as Radxa's?
if [ -e /opt/pvr/lib/libglapi.so.0 ]; then
  diff <(nm -D --defined-only /opt/pvr/lib/libglapi.so.0 | awk '{print $3}' | sort) \
       <(nm -D --defined-only "$L/libglapi.so.0" | awk '{print $3}' | sort) > "$W/glapi-exports.diff" \
    && echo "  libglapi.so.0 exports: identical to /opt/pvr's" || echo "  libglapi.so.0 exports differ from /opt/pvr's: $W/glapi-exports.diff"
fi
tar -C "$STAGE" -czf "$TARBALL" opt; rm -rf "$STAGE"
echo "  $(du -h "$TARBALL" | cut -f1) $TARBALL"
echo ok > "$W/last-build.status"
step "done: nothing installed. On the laptop: tools/build-mesa-img-rpm.sh (fetches the newest tarball), then dnf install the RPM here"
