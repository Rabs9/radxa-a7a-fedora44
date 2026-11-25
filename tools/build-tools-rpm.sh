#!/bin/bash
# Package the board scripts of this repo as four noarch RPMs (spec files in tools/rpm/):
#   a7a-extlinux-<YYYYMMDD>-<build>.noarch.rpm    tools/a7a-extlinux -> /usr/local/bin/a7a-extlinux
#   a7a-board-tools-<YYYYMMDD>-<build>.noarch.rpm tools/board/{pvr-desktop,pvr-run,a7a-clock,npu-run},
#                                                 tools/board/llm/a7a-llm, a7a-gpu-stat, a7a-voice -> /usr/local/bin;
#                                                 sudoers/a7a-gpu-stat
#                                                 -> /etc/sudoers.d; tmpfiles.d/a7a-x11.conf -> /usr/lib/tmpfiles.d;
#                                                 wireplumber/51-a7a-ac101.conf -> /usr/share/wireplumber/wireplumber.conf.d;
#                                                 systemd/a7a-voice.service -> /usr/lib/systemd/user
#   a7a-gpu-tools-<YYYYMMDD>-<build>.noarch.rpm   tools/board/{prepare616,backtoddk,pvrwatch,fwtrace-tail,clockcheck616}.sh,
#                                                 a7a-gpu-check, a7a-gpu-session -> /usr/local/bin; tools/board/systemd/
#                                                 {a7a-gpu-session.service,a7a-kwin-nocap.path,a7a-kwin-nocap.service}
#                                                 -> /usr/lib/systemd/system + the preset; the greeter drop-in and
#                                                 modprobe.d/powervr-trace.conf as config (kept on update)
#   a7a-repo-<YYYYMMDD>-<build>.noarch.rpm        tools/repo/a7a-fedora.repo -> /etc/yum.repos.d, the signing key
#                                                 tools/repo/RPM-GPG-KEY-a7a-fedora -> /etc/pki/rpm-gpg (only once it exists)
# Version = the date of the newest commit touching the sources (today when they are uncommitted), Release = now.
#   A7A_RPM_BUILD=<n>   the Release field (default: now, YYYYMMDDHHMM)
set -euo pipefail
# the project directory: this script lives in tools/
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd); OUT=${A7A_OUTPUT:-$P/output}
. "$P/tools/rpm/lib.sh"
step() { echo "== [$(date +%H:%M:%S)] $*"; }
BUILD=${A7A_RPM_BUILD:-$(date +%Y%m%d%H%M)}
BOARD_SRC=(pvr-desktop pvr-run a7a-clock npu-run llm/a7a-llm a7a-gpu-stat sudoers/a7a-gpu-stat tuned/script.sh tuned/a7a-powersave/tuned.conf tuned/a7a-balanced/tuned.conf tuned/a7a-performance/tuned.conf a7a-ac101-dtb systemd/a7a-codec.service tmpfiles.d/a7a-x11.conf wireplumber/51-a7a-ac101.conf a7a-voice systemd/a7a-voice.service udev/70-a7a-voice-uinput.rules npu/npunet.py npu/npu_kws.py a7a-clock-keep systemd/a7a-clock-keep.service systemd/a7a-clock-keep-tick.service systemd/a7a-clock-keep.timer)
GPU_SRC=(prepare616.sh backtoddk.sh pvrwatch.sh fwtrace-tail.sh clockcheck616.sh a7a-gpu-check a7a-gpu-session Xwayland
         plasma-keyboard a7a-gpu-gap a7a-gpu-gap-mesa-26.1.8-gates.json
         modprobe.d/powervr-trace.conf systemd/a7a-gpu-session.service systemd/a7a-kwin-nocap.path
         systemd/a7a-kwin-nocap.service systemd/80-a7a-gpu-tools.preset systemd/plasma-login-kwin_wayland-software.conf
         systemd/plasma-login-fedora-mesa.conf a7a-irqbalance-policy systemd/irqbalance-a7a-gpu-irq.conf)
for f in a7a-extlinux "${BOARD_SRC[@]/#/board/}" "${GPU_SRC[@]/#/board/}"; do
  [ -f "$P/tools/$f" ] || { echo "missing source tools/$f"; exit 1; }
done
python3 -m py_compile "$P/tools/a7a-extlinux" "$P/tools/board/a7a-gpu-gap" "$P/tools/board/a7a-ac101-dtb" "$P/tools/board/a7a-voice" 2>/dev/null || { echo "tools/a7a-extlinux, tools/board/a7a-gpu-gap, a7a-ac101-dtb or a7a-voice does not compile"; exit 1; }
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$P/tools/board/a7a-gpu-gap-mesa-26.1.8-gates.json" || { echo "the a7a-gpu-gap gates file is not valid JSON"; exit 1; }
bash -n "$P"/tools/board/{prepare616,backtoddk,pvrwatch,fwtrace-tail,clockcheck616}.sh "$P"/tools/board/{a7a-gpu-check,a7a-gpu-session,pvr-desktop,pvr-run,a7a-clock,npu-run,llm/a7a-llm,a7a-gpu-stat,a7a-clock-keep,Xwayland,plasma-keyboard}
# a sudoers drop-in with a syntax error would lock sudo for everyone on the board
if command -v visudo > /dev/null; then visudo -cqf "$P/tools/board/sudoers/a7a-gpu-stat" || { echo "tools/board/sudoers/a7a-gpu-stat does not parse"; exit 1; }; fi
# WirePlumber skips a fragment it cannot parse, and the AC101 jack is then 48 dB quiet again
if command -v spa-json-dump > /dev/null; then spa-json-dump "$P/tools/board/wireplumber/51-a7a-ac101.conf" > /dev/null || { echo "tools/board/wireplumber/51-a7a-ac101.conf does not parse"; exit 1; }; fi

VER=$(a7a_src_date tools/a7a-extlinux)
step "a7a-extlinux $VER-$BUILD"
RPM1=$(a7a_rpmbuild "$P/tools/rpm/a7a-extlinux.spec" noarch "a7a-extlinux-*.rpm" \
  --define "a7a_src $P/tools" --define "a7a_ver $VER" --define "a7a_build $BUILD")
a7a_rpm_summary "$RPM1"

# the NPU wake spotter's data (a7a-voice npu on): made by tools/npu/freeze.py and tools/npu/convert-kws-encoder.sh
# into output/npu-kws, too big for the repo; the package takes exactly the files tested on the board
# (tools/npu/npu-kws.sha256)
NPU_DATA=${A7A_NPU_DATA:-$P/output/npu-kws}      # an input, not output: not under A7A_OUTPUT
NPU_SHA=${A7A_NPU_SHA:-$P/tools/npu/npu-kws.sha256}   # (the tests point both at stand-ins)
( cd "$NPU_DATA" && sha256sum --quiet -c "$NPU_SHA" ) \
  || { echo "output/npu-kws does not hold the NPU spotter files of tools/npu/npu-kws.sha256"; exit 1; }
python3 -m py_compile "$P/tools/board/npu/npunet.py" "$P/tools/board/npu/npu_kws.py" || { echo "tools/board/npu does not compile"; exit 1; }
VER=$(a7a_src_date "${BOARD_SRC[@]/#/tools/board/}")
step "a7a-board-tools $VER-$BUILD"
RPM2=$(a7a_rpmbuild "$P/tools/rpm/a7a-board-tools.spec" noarch "a7a-board-tools-*.rpm" \
  --define "a7a_src $P/tools/board" --define "a7a_npu $NPU_DATA" --define "a7a_ver $VER" --define "a7a_build $BUILD")
a7a_rpm_summary "$RPM2"

VER=$(a7a_src_date "${GPU_SRC[@]/#/tools/board/}")
step "a7a-gpu-tools $VER-$BUILD"
RPM3=$(a7a_rpmbuild "$P/tools/rpm/a7a-gpu-tools.spec" noarch "a7a-gpu-tools-*.rpm" \
  --define "a7a_src $P/tools/board" --define "a7a_ver $VER" --define "a7a_build $BUILD")
a7a_rpm_summary "$RPM3"
# a7a-repo: only once the signing key exists (tools/repo-key.sh makes it, once; A7A_REPO_KEY points the tests at a
# stand-in). Without it the other three are built and tools/master-image.sh refuses to make an image.
REPO_KEY=${A7A_REPO_KEY:-$P/tools/repo/RPM-GPG-KEY-a7a-fedora}; RPM4=""
if [ -f "$REPO_KEY" ]; then
  grep -q -- '-----BEGIN PGP PUBLIC KEY BLOCK-----' "$REPO_KEY" || { echo "$REPO_KEY is not an armored public key"; exit 1; }
  ! grep -q -- 'PRIVATE KEY BLOCK' "$REPO_KEY" || { echo "$REPO_KEY holds a secret key: never package it"; exit 1; }
  VER=$(a7a_src_date tools/repo)
  step "a7a-repo $VER-$BUILD"
  RPM4=$(a7a_rpmbuild "$P/tools/rpm/a7a-repo.spec" noarch "a7a-repo-*.rpm" \
    --define "a7a_src $P/tools/repo" --define "a7a_key $REPO_KEY" --define "a7a_ver $VER" --define "a7a_build $BUILD")
  a7a_rpm_summary "$RPM4"
else
  step "a7a-repo left out: no signing key yet ($REPO_KEY; tools/repo-key.sh makes it)"
fi
step "done: $RPM1 $RPM2 $RPM3${RPM4:+ $RPM4}"
