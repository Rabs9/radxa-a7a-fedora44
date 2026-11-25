# a7a-panel: the board's panel (tools/panel, Rust and iced): every clock live, the power profile, the clock controls of
# a7a-clock, the programs using the GPU, display and NPU, and the Assistant (a chat with the model a7a-llm serves).
# Cross-built on the laptop and packaged by tools/build-panel.sh, with --define "a7a_src <repo>" --define "a7a_bin
# <the binary>" --define "a7a_ver YYYYMMDD" --define "a7a_build YYYYMMDDHHMM". Packaging only: nothing is compiled here.
Name:           a7a-panel
Version:        %{a7a_ver}
Release:        %{a7a_build}
Summary:        Clock, power, hardware and assistant panel for the Radxa Cubie A7A
License:        GPL-2.0-only
URL:            https://github.com/Rabs9/radxa-a7a-fedora44
ExclusiveArch:  aarch64
AutoReqProv:    no
# the controls run a7a-clock, the Assistant runs a7a-llm (both in a7a-board-tools); the binary needs only glibc and
# libgcc, the display and GPU libraries are opened at run time
Requires:       a7a-board-tools
Requires:       glibc
Requires:       libgcc
# the Assistant's model server: Fedora's llama.cpp (a7a-llm uses it when there is no build in ~/llama.cpp); the model
# itself comes with `a7a-llm get`
Recommends:     llama-cpp
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none
%global __os_install_post %{nil}

%description
a7a-panel: the Overview (CPU clusters, GPU, memory, NPU, L3 clocks and temperatures, with a minute of history), the
Controls (power profile, governor, CPU caps, memory and NPU rates, the DSU module, the GPU clock's boot entry, the
presets, the WiFi band), the Hardware tab (programs by CPU use, and which of them hold the GPU, the display or the NPU)
and the Assistant (the local model, with read-only tools for this board and the web). Desktop entries: A7A Panel and
A7A Assistant.

%install
install -D -m 755 "%{a7a_bin}" %{buildroot}/usr/local/bin/a7a-panel
install -D -m 644 "%{a7a_src}/tools/board/a7a-panel.desktop" %{buildroot}/usr/share/applications/a7a-panel.desktop
install -D -m 644 "%{a7a_src}/tools/board/a7a-assistant.desktop" %{buildroot}/usr/share/applications/a7a-assistant.desktop

%files
/usr/local/bin/a7a-panel
/usr/share/applications/a7a-panel.desktop
/usr/share/applications/a7a-assistant.desktop
