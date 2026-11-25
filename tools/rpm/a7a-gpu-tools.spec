# a7a-gpu-tools: the GPU-stack board scripts of a7a-fedora (docs/GPU-ROADMAP-2026-09-09.md, docs/PACKAGING-2026-09-09.md).
# Fed by tools/build-tools-rpm.sh with --define "a7a_src <repo>/tools/board" --define "a7a_ver YYYYMMDD"
# --define "a7a_build YYYYMMDDHHMM". Sources: tools/board/ (the repo is the source of truth; never edit on the board).
Name:           a7a-gpu-tools
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        GPU stack switch, watch and check scripts for the Radxa Cubie A7A (open driver and vendor DDK)
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-a7a-fedora44
BuildArch:      noarch
AutoReqProv:    no
Requires:       bash
Requires:       a7a-extlinux
# a7a-gpu-session and backtoddk.sh call pvr-desktop
Requires:       a7a-board-tools
Requires:       sudo
Requires:       systemd
# a7a-gpu-gap (stdlib only)
Requires:       python3
Recommends:     vulkan-tools
Recommends:     glmark2
# a7a-gpu-gap snapshot runs eglinfo and glxinfo
Recommends:     egl-utils
Recommends:     glx-utils
%undefine __brp_mangle_shebangs

%description
a7a-gpu-session (+ a7a-gpu-session.service, before the display manager: reads which driver is bound to the GPU,
powervr or pvrsrvkm, and applies the matching KWin session config so both kernels get a working desktop without
a switch script), prepare616.sh / backtoddk.sh (make the newest kernel-a7apvr / kernel-a7a entry the default and
reboot), pvrwatch.sh / fwtrace-tail.sh (health samples and the firmware trace of the open driver under
/home/radxa/pvr-logs), clockcheck616.sh (GPU clock/voltage under the open driver), a7a-gpu-check [--bench]
(inventory and pass/warn checklist of whichever stack is running), a7a-gpu-gap (capability snapshots of the booted
stack and the gap report between the vendor DDK and the open stack, with Mesa's version gates in
/usr/local/share/a7a-gpu-gap), a7a-kwin-nocap.{path,service} (keep cap_sys_nice
off kwin_wayland on the DDK; enabled per boot by the session unit, never by this package), the greeter drop-in that
keeps the login screen's KWin on the QPainter backend (KWin 6.7 + llvmpipe cannot create EGL images here), the
greeter UI's drop-in that keeps it on Fedora's Mesa (on the DDK boot mesa-img-a7a's loader entry would be first), and
/etc/modprobe.d/powervr-trace.conf (arms the open driver's firmware trace at init; kept on update).

%install
mkdir -p %{buildroot}/usr/local/bin %{buildroot}/etc/modprobe.d %{buildroot}/usr/lib/systemd/system \
         %{buildroot}/usr/lib/systemd/system-preset %{buildroot}/etc/systemd/user/plasma-login-kwin_wayland.service.d \
         %{buildroot}/usr/lib/udev/rules.d %{buildroot}/etc/systemd/user/plasma-kwin_wayland.service.d \
         %{buildroot}/usr/local/share/a7a-gpu-gap %{buildroot}/etc/systemd/user/plasma-login.service.d \
         %{buildroot}/etc/systemd/system/irqbalance.service.d
for f in prepare616.sh backtoddk.sh pvrwatch.sh fwtrace-tail.sh clockcheck616.sh a7a-gpu-check a7a-gpu-session a7a-gpu-irq-affinity a7a-irqbalance-policy a7a-gpu-gap; do
  install -m 755 "%{a7a_src}/$f" %{buildroot}/usr/local/bin/$f
done
# KWin finds Xwayland on its PATH, /usr/local/bin before /usr/bin: the wrapper preloads mesa-img-a7a's render-node helper
# (X11 programs on the GPU under KWin 6.8; tools/board/Xwayland says why)
install -m 755 "%{a7a_src}/Xwayland" %{buildroot}/usr/local/bin/Xwayland
# the same lookup for the login screen's on-screen keyboard: Fedora's Mesa for it, as for the greeter's UI
# (tools/board/plasma-keyboard says why)
install -m 755 "%{a7a_src}/plasma-keyboard" %{buildroot}/usr/local/bin/plasma-keyboard
install -m 644 "%{a7a_src}/a7a-gpu-gap-mesa-26.1.8-gates.json" %{buildroot}/usr/local/share/a7a-gpu-gap/a7a-gpu-gap-mesa-26.1.8-gates.json
install -m 644 "%{a7a_src}/modprobe.d/powervr-trace.conf" %{buildroot}/etc/modprobe.d/powervr-trace.conf
for u in a7a-gpu-session.service a7a-kwin-nocap.path a7a-kwin-nocap.service; do
  install -m 644 "%{a7a_src}/systemd/$u" %{buildroot}/usr/lib/systemd/system/$u
done
install -m 644 "%{a7a_src}/systemd/80-a7a-gpu-tools.preset" %{buildroot}/usr/lib/systemd/system-preset/80-a7a-gpu-tools.preset
install -m 644 "%{a7a_src}/systemd/plasma-login-kwin_wayland-software.conf" %{buildroot}/etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf
install -m 644 "%{a7a_src}/systemd/plasma-login-fedora-mesa.conf" %{buildroot}/etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf
install -m 644 "%{a7a_src}/systemd/irqbalance-a7a-gpu-irq.conf" %{buildroot}/etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf
install -m 644 "%{a7a_src}/udev/90-a7a-gpu-irq.rules" %{buildroot}/usr/lib/udev/rules.d/90-a7a-gpu-irq.rules
install -m 644 "%{a7a_src}/systemd/plasma-kwin_wayland-cpuaffinity.conf" %{buildroot}/etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf

%post
# first install: preset (80-a7a-gpu-tools.preset enables the session unit for the next boot, nothing is started now)
%systemd_post a7a-gpu-session.service

# An upgrade from an a7a-gpu-tools without the unit (Version < 20260910): %%post sees $1 = 2 and the macro above
# skips the preset, so the unit would stay disabled and backtoddk.sh's DDK boot would land on the open-stack
# session config (black screen). This trigger runs once on that upgrade, after the new files are on disk and the
# old version is gone. Its body is the preset call itself, unconditional: inside a self-named %%triggerpostun rpm
# 6.0.2 still counts the old header and passes $1 = 2, so %%systemd_post's `$1 -eq 1` guard would make the trigger
# a no-op (second reviewer pass, 2026-09-10, proven with probe packages). The version condition is the guard.
%triggerpostun -- a7a-gpu-tools < 20260910
[ -x /usr/lib/systemd/systemd-update-helper ] && /usr/lib/systemd/systemd-update-helper install-system-units a7a-gpu-session.service || :

# KWin 6.8 dropped the QPainter backend the greeter and the software session run on: with the settings made for 6.7
# the compositor exits and the screen stays black (2026-11-16). a7a-gpu-session adapts both at every boot; this
# trigger does it when kwin-libs (libkwin.so.6) is installed or updated in a RUNNING system, so that a logout after
# `dnf upgrade` does not end at a black login screen before the first reboot. Also runs when this package is
# installed onto an existing kwin (the image build: what it writes into /run there is tidied away). Unconditional,
# and it must never fail the transaction.
%triggerin -- kwin-libs
[ -x /usr/local/bin/a7a-gpu-session ] && /usr/local/bin/a7a-gpu-session greeter >/dev/null 2>&1 || :

%preun
# erase: disable (and stop) the three units; the nocap units may be enabled by the session unit on a DDK boot
%systemd_preun a7a-gpu-session.service a7a-kwin-nocap.path a7a-kwin-nocap.service

%posttrans
# the irqbalance drop-in (a7a-gpu-irq.conf) on an upgrade too, not only at the next boot. rpm runs %%posttrans BEFORE
# systemd's file trigger, so reload here first, or try-restart brings irqbalance back on the old ExecStart without
# the policy (reviewer pass 3, rpm lib/transaction.cc). Then the GPU affinity again, after the new instance bans the
# IRQ, and only while powervr is bound: a7a-gpu-irq-affinity alone would also pin the DDK's interrupt (never
# measured). Skipped in the image-build chroot (no running systemd); every step may fail without failing the upgrade.
if [ -d /run/systemd/system ]; then
  systemctl daemon-reload >/dev/null 2>&1 || :
  systemctl try-restart irqbalance.service >/dev/null 2>&1 || :
  d=$(readlink /sys/bus/platform/devices/1800000.gpu/driver 2>/dev/null)
  [ "${d##*/}" = powervr ] && /usr/local/bin/a7a-gpu-irq-affinity >/dev/null 2>&1 || :
fi

%files
/usr/local/bin/prepare616.sh
/usr/local/bin/backtoddk.sh
/usr/local/bin/pvrwatch.sh
/usr/local/bin/fwtrace-tail.sh
/usr/local/bin/clockcheck616.sh
/usr/local/bin/a7a-gpu-check
/usr/local/bin/a7a-gpu-session
/usr/local/bin/a7a-gpu-irq-affinity
/usr/local/bin/a7a-irqbalance-policy
/usr/local/bin/a7a-gpu-gap
/usr/local/bin/Xwayland
/usr/local/bin/plasma-keyboard
%dir /usr/local/share/a7a-gpu-gap
/usr/local/share/a7a-gpu-gap/a7a-gpu-gap-mesa-26.1.8-gates.json
/usr/lib/udev/rules.d/90-a7a-gpu-irq.rules
/usr/lib/systemd/system/a7a-gpu-session.service
/usr/lib/systemd/system/a7a-kwin-nocap.path
/usr/lib/systemd/system/a7a-kwin-nocap.service
/usr/lib/systemd/system-preset/80-a7a-gpu-tools.preset
%dir /etc/systemd/user/plasma-kwin_wayland.service.d
# Item E 1(b): CPUAffinity=6-7 for the session compositor. Plain %%config like software.conf below: it is policy,
# identical for both stacks, and a7a-gpu-session/pvr-desktop only ever touch pvr.conf and software.conf here.
%config /etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf
%dir /etc/systemd/user/plasma-login-kwin_wayland.service.d
# plain %%config (not noreplace): the drop-in is policy, identical for both stacks; a hand copy that differs (the
# owner's board, comment only) becomes software.conf.rpmorig and the packaged one takes over, so nothing has to be
# removed by hand before the install and `rpm -V` stays clean
%config /etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf
%dir /etc/systemd/user/plasma-login.service.d
# the greeter UI on Fedora's Mesa on both stacks (LD_LIBRARY_PATH=/usr/lib64): on the DDK boot mesa-img-a7a's loader
# entry would hand it a DDK Mesa that cannot serve its QPainter compositor (softpipe); plain %%config (policy)
%config /etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf
%dir /etc/systemd/system/irqbalance.service.d
# irqbalance leaves the open stack's GPU interrupt on the big cluster (a7a-irqbalance-policy bans that one IRQ);
# without it the Item E 1(a) affinity is undone within seconds of irqbalance's start. Plain %%config (policy)
%config /etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf
%config(noreplace) /etc/modprobe.d/powervr-trace.conf
