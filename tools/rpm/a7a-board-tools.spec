# a7a-board-tools: the board tools of a7a-fedora that predate the GPU-stack work and were hand-copied into the image
# until 2026-09-10 (docs/PACKAGING-2026-09-09.md, "Item B follow-ups"). Fed by tools/build-tools-rpm.sh with
# --define "a7a_src <repo>/tools/board" --define "a7a_ver YYYYMMDD" --define "a7a_build YYYYMMDDHHMM".
# Sources: tools/board/ (the repo is the source of truth; never edit on the board).
Name:           a7a-board-tools
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        Board tools for the Radxa Cubie A7A: pvr-desktop, pvr-run, a7a-clock, npu-run, a7a-voice
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-a7a-fedora44
BuildArch:      noarch
AutoReqProv:    no
Requires:       bash
Requires:       python3
Requires:       sudo
# a7a-clock sets the WiFi band with nmcli (NetworkManager is in Fedora KDE) and edits the DTB of a boot entry with
# fdtput: package dtc, NOT in the Fedora KDE image (checked in the image's rpmdb, 2026-09-10), so a weak dependency
# (a hard one would fail the offline image build); a7a-clock says what to install when fdtput is missing
Requires:       NetworkManager
Recommends:     dtc
# a7a-clock codec sets the AC101's playback path with amixer (alsa-utils is in Fedora KDE; weak, as dtc)
Recommends:     alsa-utils
# KDE's power slider reaches the a7a-* tuned profiles through tuned-ppd (Fedora KDE's provider of the slider); the image
# maps the slider to them in /etc/tuned/ppd.conf (tools/master-image.sh). Weak: the profiles are inert without tuned.
Recommends:     tuned-ppd
# a7a-voice listens through pw-record, chimes with pw-play, reads answers aloud through pw-cat and reports with
# notify-send (all in Fedora KDE; weak, as alsa-utils); its speech engine, models and voice are downloaded by
# `a7a-voice setup` and `a7a-voice speak on`, not packaged
Recommends:     pipewire-utils
Recommends:     libnotify
# typing by voice ("type …", dictation, the found key commands) presses keys through dotool (Fedora's)
Recommends:     dotool
%undefine __brp_mangle_shebangs

%description
pvr-desktop on|off|all|none|status (the KWin session on the PowerVR DDK: writes the plasma-kwin_wayland drop-in,
strips cap_sys_nice; a7a-gpu-session applies it at boot on the DDK kernel), pvr-run (one program on the DDK),
a7a-clock (CPU/GPU/DSU/RAM/NPU clocks, voltages and profiles; the GPU clock goes into the default boot entry's DTB;
its window is a7a-panel's Controls tab), npu-run (VIPLite programs with the NPU SDK's libraries), and the tuned profiles
a7a-powersave, a7a-balanced and a7a-performance that KDE's power slider switches between (a7a-clock power), and
a7a-llm (the local language model: get, build, serve, stop, bench; the Assistant tab of a7a-panel drives it), and
a7a-gpu-stat (the GPU's load, memory and recoveries from the driver's debugfs, for a7a-clock json and the panel; its
sudo runs stay out of the journal: /etc/sudoers.d/a7a-gpu-stat), and a7a-ac101-dtb with a7a-codec.service
(a7a-clock codec ac101: the 3.5 mm jack of the boards built with the AC101 codec, which the packaged device tree does
not describe) and 51-a7a-ac101.conf for WirePlumber (that jack in 16-bit: the BSP's I2S driver sent 24-bit audio
48 dB too quiet), and a7a-x11.conf in tmpfiles.d (the X11 socket directories stay out of the /tmp clean-up: they are
created at systemd's build date here, and a log-in after the first clean-up had no Xwayland), and a7a-voice with its
user service (voice commands: a wake phrase, then a command typed in a7a-panel's Voice tab; off until turned on;
and the Assistant's answers read aloud, with speak answers on in a7a-panel's Assistant tab).

%install
mkdir -p %{buildroot}/usr/local/bin %{buildroot}/usr/lib/systemd/system %{buildroot}/usr/lib/tmpfiles.d
mkdir -p %{buildroot}/usr/share/wireplumber/wireplumber.conf.d %{buildroot}/usr/lib/systemd/user
for f in pvr-desktop pvr-run a7a-clock npu-run llm/a7a-llm a7a-gpu-stat a7a-ac101-dtb a7a-voice a7a-clock-keep; do
  install -m 755 "%{a7a_src}/$f" %{buildroot}/usr/local/bin/$(basename $f)
done
install -D -m 440 "%{a7a_src}/sudoers/a7a-gpu-stat" %{buildroot}/etc/sudoers.d/a7a-gpu-stat
# enabled by `a7a-clock codec ac101` only, never by a preset: a board built with the AC101B never runs it
install -m 644 "%{a7a_src}/systemd/a7a-codec.service" %{buildroot}/usr/lib/systemd/system/a7a-codec.service
# the time the next boot starts its clock from (a7a-clock-keep): enabled by the package itself, as links under
# /usr/lib (no scriptlets: a board updated to this package has them at its next boot; inert until chronyd is in sync)
for u in a7a-clock-keep.service a7a-clock-keep-tick.service a7a-clock-keep.timer; do
  install -m 644 "%{a7a_src}/systemd/$u" %{buildroot}/usr/lib/systemd/system/$u
done
mkdir -p %{buildroot}/usr/lib/systemd/system/multi-user.target.wants %{buildroot}/usr/lib/systemd/system/timers.target.wants
ln -s ../a7a-clock-keep.service %{buildroot}/usr/lib/systemd/system/multi-user.target.wants/a7a-clock-keep.service
ln -s ../a7a-clock-keep.timer %{buildroot}/usr/lib/systemd/system/timers.target.wants/a7a-clock-keep.timer
# x lines act only in the clean-up, so nothing to apply at install time
install -m 644 "%{a7a_src}/tmpfiles.d/a7a-x11.conf" %{buildroot}/usr/lib/tmpfiles.d/a7a-x11.conf
# matches only the AC101 card's playback node, so inert on a board without it; WirePlumber reads it at its next start
install -m 644 "%{a7a_src}/wireplumber/51-a7a-ac101.conf" %{buildroot}/usr/share/wireplumber/wireplumber.conf.d/51-a7a-ac101.conf
# a user service, enabled per user by `a7a-voice on` (the panel's Voice tab), never by a preset
install -m 644 "%{a7a_src}/systemd/a7a-voice.service" %{buildroot}/usr/lib/systemd/user/a7a-voice.service
# the seat's user may use the virtual keyboard typing by voice needs (as KDE Connect's rule allows already)
install -D -m 644 "%{a7a_src}/udev/70-a7a-voice-uinput.rules" %{buildroot}/usr/lib/udev/rules.d/70-a7a-voice-uinput.rules
# the wake spotter on the NPU (a7a-voice npu on): its Python (VIPLite through ctypes, the search in numpy) and data
# (the encoder as an NBG, the CPU part's weights; --define a7a_npu, files checked by tools/build-tools-rpm.sh)
install -d %{buildroot}/usr/lib/a7a-voice/npu
install -m 644 "%{a7a_src}/npu/npunet.py" "%{a7a_src}/npu/npu_kws.py" %{buildroot}/usr/lib/a7a-voice/npu/
install -m 644 "%{a7a_npu}/kws_encoder_fp16_a733.nb" "%{a7a_npu}/kws_cpu.npz" %{buildroot}/usr/lib/a7a-voice/npu/
for t in a7a-powersave a7a-balanced a7a-performance; do
  install -D -m 644 "%{a7a_src}/tuned/$t/tuned.conf" %{buildroot}/usr/lib/tuned/profiles/$t/tuned.conf
  install -m 755 "%{a7a_src}/tuned/script.sh" %{buildroot}/usr/lib/tuned/profiles/$t/script.sh
done

%files
/usr/local/bin/pvr-desktop
/usr/local/bin/pvr-run
/usr/local/bin/a7a-clock
/usr/local/bin/npu-run
/usr/local/bin/a7a-llm
/usr/local/bin/a7a-gpu-stat
/usr/local/bin/a7a-ac101-dtb
/usr/local/bin/a7a-voice
/usr/local/bin/a7a-clock-keep
/usr/lib/systemd/system/a7a-codec.service
/usr/lib/systemd/system/a7a-clock-keep.service
/usr/lib/systemd/system/a7a-clock-keep-tick.service
/usr/lib/systemd/system/a7a-clock-keep.timer
/usr/lib/systemd/system/multi-user.target.wants/a7a-clock-keep.service
/usr/lib/systemd/system/timers.target.wants/a7a-clock-keep.timer
/usr/lib/systemd/user/a7a-voice.service
/usr/lib/udev/rules.d/70-a7a-voice-uinput.rules
%dir /usr/lib/a7a-voice
/usr/lib/a7a-voice/npu
/usr/lib/tmpfiles.d/a7a-x11.conf
/usr/share/wireplumber/wireplumber.conf.d/51-a7a-ac101.conf
%attr(0440,root,root) %config(noreplace) /etc/sudoers.d/a7a-gpu-stat
%dir /usr/lib/tuned/profiles/a7a-powersave
%dir /usr/lib/tuned/profiles/a7a-balanced
%dir /usr/lib/tuned/profiles/a7a-performance
/usr/lib/tuned/profiles/a7a-powersave/tuned.conf
/usr/lib/tuned/profiles/a7a-powersave/script.sh
/usr/lib/tuned/profiles/a7a-balanced/tuned.conf
/usr/lib/tuned/profiles/a7a-balanced/script.sh
/usr/lib/tuned/profiles/a7a-performance/tuned.conf
/usr/lib/tuned/profiles/a7a-performance/script.sh
