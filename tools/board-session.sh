#!/bin/bash
# board-session.sh <host> <step>: ON THE LAPTOP. The hardware test of an image on a card, one step at a time, over
# ssh (the image's login radxa/radxa, or A7A_BOARD_KEY=<private key> for a card of the owner's). Everything a step
# prints is also appended to output/logs/board-<host>-<date>.log. The exit status is the step's.
#   facts        what booted: variant, kernel, KWin, failed units, crashes, who is logged in, a7a-gpu-check,
#                a7a-clock check, the compositor's device for its clients. Read-only.
#   tools        installs the newest a7a tool packages and the newest mesa-img-a7a of output/rpms on the board (a
#                card flashed before the last fix), then compares the board's scripts with this tree
#   clocks       copies tools/clock-test.sh to the board and runs it there as a unit of its own (needs the desktop
#                logged in; about 4 minutes; it switches the memory clock: a test card)
#   gpu-set      clock-test.sh gpu-set, then a clean restart; waits until the board is up again. THEN LOG IN.
#   gpu-verify   clock-test.sh gpu-verify (GPU at the test clock, one-minute soak, boot menu put back), restart, wait
#   upgrade      dnf upgrade as a unit on the board, then a clean restart when dnf ended well; waits.
#                THEN LOG IN AT THE BOARD and run facts and clocks again: that is the update test.
#   llm          the Assistant: Fedora's llama.cpp, the default model (copied from output/models when the laptop has
#                it, else `a7a-llm get` on the board), `a7a-llm serve`, then three questions through `a7a-panel --ask`
#                (tools, memory, and the web pause after a local read), with the speed of each. Leaves the server up.
#   restart      a clean restart, waits until the board is up again
#   wait [s]     waits until ssh answers (after the board was powered on)
#   run <cmd>    one command on the board
# Before clocks and gpu-set the board's a7a-clock must be this tree's: an older one takes `gpu abc` and makes it the
# default boot entry. What runs for minutes on the board runs there as a systemd unit writing to a file: a lost ssh
# connection stops neither a test half-way nor an upgrade. A restart is counted only when the board's boot id
# changed. The board is never powered off by this script.
set -u
HOST=${1:-}; STEP=${2:-}
[ -n "$HOST" ] && [ -n "$STEP" ] || { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
LOG=$P/output/logs/board-$HOST-$(date +%Y%m%d).log; mkdir -p "$P/output/logs"
exec > >(tee -a "$LOG") 2>&1
OPT=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o LogLevel=ERROR -o ServerAliveInterval=15)
if [ -n "${A7A_BOARD_KEY:-}" ]; then
  SSH=(ssh -i "$A7A_BOARD_KEY" -o IdentitiesOnly=yes -o BatchMode=yes "${OPT[@]}"); SCP=(scp -i "$A7A_BOARD_KEY" -o IdentitiesOnly=yes -o BatchMode=yes "${OPT[@]}")
else
  SP=$(command -v sshpass || echo /home/linuxbrew/.linuxbrew/bin/sshpass)
  SSH=("$SP" -p "${A7A_BOARD_PASS:-radxa}" ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password "${OPT[@]}")
  SCP=("$SP" -p "${A7A_BOARD_PASS:-radxa}" scp -o PubkeyAuthentication=no -o PreferredAuthentications=password "${OPT[@]}")
fi
on() { "${SSH[@]}" "radxa@$HOST" "$@"; }
say() { echo "[$(date +%T)] $*"; }
boot_id() { timeout 20 "${SSH[@]}" "radxa@$HOST" 'cat /proc/sys/kernel/random/boot_id' 2>/dev/null; }
wait_ssh() {   # <seconds> [boot id that must be gone]: until a command runs on the board (after a restart: a NEW boot)
  local end=$(( $(date +%s) + ${1:-600} )) old=${2:-} id
  while [ "$(date +%s)" -lt "$end" ]; do
    id=$(boot_id)
    if [ -n "$id" ] && [ "$id" != "$old" ]; then say "the board answers: $(on 'uptime -p; uname -r' 2>/dev/null | tr '\n' ' ')"; return 0; fi
    sleep 5
  done
  if [ -n "$old" ] && [ "$(boot_id)" = "$old" ]; then say "the board never went down: it still runs the boot it was asked to leave"; else say "no ssh on $HOST after ${1:-600} s"; fi
  return 1
}
restart() {
  local old; old=$(boot_id)
  [ -n "$old" ] || { say "the board does not answer: no restart sent"; return 1; }
  say "restarting the board now (clean: systemctl reboot)"
  on 'sudo systemctl reboot' || true
  sleep 10
  wait_ssh 600 "$old" || return 1
  on 'echo "failed units: $(systemctl --failed --no-legend --plain | awk "{print \$1}" | tr "\n" " ")"; echo "logged in on the desktop: $(pgrep -x plasmashell >/dev/null && echo yes || echo NO: log in at the board, radxa / radxa)"'
}
tools_match() {   # the board's scripts against this tree; says which differ
  local f want got bad=0
  for f in a7a-clock a7a-gpu-check a7a-gpu-session llm/a7a-llm; do
    want=$(sha256sum < "$P/tools/board/$f" | cut -c1-64); got=$(on "sha256sum < /usr/local/bin/${f##*/} 2>/dev/null | cut -c1-64")
    [ "$want" = "$got" ] || { say "the board's /usr/local/bin/${f##*/} is not this tree's tools/board/$f"; bad=1; }
  done
  return $bad
}
newest() { ls "$P"/output/rpms/$1 2>/dev/null | sed -E 's/^(.*-([0-9]{12})\.[a-z0-9_]+\.rpm)$/\2 \1/' | LC_ALL=C sort | tail -1 | cut -d' ' -f2- || true; }
board_unit() {    # <name> <command>: runs on the board as a user unit writing to /var/tmp/<name>.log, shows the log as
                  # it grows, returns the command's exit status (255: it could not be told)
  local name=$1 cmd=$2 rc shown=0 n st miss=0
  on "rm -f /var/tmp/$name.log; systemctl --user reset-failed $name.service 2>/dev/null; systemd-run --user --quiet --unit=$name --collect /bin/bash -c '( $cmd ) > /var/tmp/$name.log 2>&1; echo \"unit-exit \$?\" >> /var/tmp/$name.log'" || { say "the unit $name could not be started"; return 255; }
  while :; do
    sleep 5
    st=$(on "systemctl --user is-active $name.service 2>/dev/null; wc -l < /var/tmp/$name.log 2>/dev/null" 2>/dev/null | tr '\n' ' ')
    n=$(echo "$st" | awk '{print $2+0}')
    if [ "$n" -gt "$shown" ]; then on "sed -n '$((shown+1)),${n}p' /var/tmp/$name.log" 2>/dev/null | grep -v '^unit-exit '; shown=$n; fi
    case "$st" in
      active*|activating*) miss=0; continue ;;
      "") miss=$((miss+1))      # ssh did not answer this time: ask again, for three minutes
          [ "$miss" -lt 36 ] && continue
          say "the board has not answered for three minutes while $name ran (last line shown above): it may hang"; return 255 ;;
    esac
    break
  done
  on "sed -n '$((shown+1)),\$p' /var/tmp/$name.log" 2>/dev/null | grep -v '^unit-exit '
  rc=$(on "sed -n 's/^unit-exit //p' /var/tmp/$name.log | tail -1" 2>/dev/null)
  return "${rc:-255}"
}

echo; say "######## $STEP on $HOST"
case "$STEP" in
facts)
  on 'echo "image: $(tr "\n" " " < /etc/a7a-image-variant 2>/dev/null || echo "no /etc/a7a-image-variant")"
echo "kernel: $(uname -r)   up: $(uptime -p)   root: $(df -h / | awk "NR==2 {print \$2\" total, \"\$4\" free\"}")"
echo "packages: $(rpm -q kwin mesa-dri-drivers mesa-img-a7a a7a-board-tools a7a-gpu-tools glmark2 dtc 2>&1 | tr "\n" " ")"
echo "failed units: system [$(systemctl --failed --no-legend --plain | awk "{print \$1}" | tr "\n" " ")] user [$(XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user --failed --no-legend --plain 2>/dev/null | awk "{print \$1}" | tr "\n" " ")]"
echo "crashes this boot: $(coredumpctl list --since "$(uptime -s)" --no-pager --no-legend 2>&1 | awk "/No coredumps found/ {next} {print \$NF}" | sort | uniq -c | tr "\n" " ")"
echo "desktop session: $(pgrep -x plasmashell >/dev/null && echo "logged in" || echo "NOBODY LOGGED IN")   sessions: $(loginctl list-sessions --no-legend | awk "{print \$3\":\"\$4}" | tr "\n" " ")"
W=$(ls /run/user/$(id -u)/ 2>/dev/null | grep -E "^wayland-[0-9]+$" | head -1)
if [ -n "$W" ] && command -v wayland-info >/dev/null; then echo "compositor names to its clients: $(XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=$W timeout 8 wayland-info 2>/dev/null | grep -m1 "main device" | sed "s/^[ \t]*//")"; fi
if [ -n "$W" ] && command -v eglinfo >/dev/null; then echo "a Wayland EGL client gets: $(XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=$W timeout 20 eglinfo -B -p wayland 2>&1 | grep -m1 "ES profile renderer" | sed "s/.*: //")"; fi
echo "kwin: $(rpm -q kwin)   KWIN env: $(K=$(pgrep -u $(id -u) -x kwin_wayland | head -1); [ -n "$K" ] && tr "\0" "\n" < /proc/$K/environ | grep "^KWIN_" | tr "\n" " ")"
echo "memory: $(free -m | awk "NR==2 {print \$3\" MB used, \"\$7\" MB available\"}")   biggest: $(ps -eo rss,comm --sort=-rss | awk "NR>1 && NR<5 {printf \"%s %d MB  \", \$2, \$1/1024}")"
echo "wifi: $(iw dev wlan0 link 2>/dev/null | grep -E "freq|signal|tx bitrate" | tr -s " \t" " " | tr "\n" ";")"
echo; echo "== a7a-gpu-check"; a7a-gpu-check 2>&1 | grep -E "FAIL|WARN|applications|X11 programs|compositor renders|summary"
echo; a7a-clock check 2>&1'; rc=$?
  tools_match || say "(the step 'tools' brings the board's scripts up to this tree)"
  exit $rc ;;
tools)
  RPMS=(); for pat in 'a7a-extlinux-*.noarch.rpm' 'a7a-board-tools-*.noarch.rpm' 'a7a-gpu-tools-*.noarch.rpm' 'mesa-img-a7a-*.aarch64.rpm' 'a7a-panel-*.aarch64.rpm'; do
    r=$(newest "$pat"); [ -n "$r" ] || { say "no $pat in output/rpms"; exit 1; }; RPMS+=("$r"); done
  say "installing: $(for r in "${RPMS[@]}"; do basename "$r"; done | tr '\n' ' ')"
  on 'rm -rf /var/tmp/a7a-rpms; mkdir -p /var/tmp/a7a-rpms' && "${SCP[@]}" "${RPMS[@]}" "radxa@$HOST:/var/tmp/a7a-rpms/" \
    && on 'sudo dnf install -y --disablerepo="*" /var/tmp/a7a-rpms/*.rpm 2>&1 | tail -6; rpm -q a7a-extlinux a7a-board-tools a7a-gpu-tools mesa-img-a7a a7a-panel | tr "\n" " "; echo' || exit 1
  tools_match && say "the board's scripts are this tree's" || exit 1 ;;
clocks)
  tools_match || { say "not running the clock test with other tools than this tree's: run the step 'tools' first"; exit 4; }
  "${SCP[@]}" "$P/tools/clock-test.sh" "radxa@$HOST:/home/radxa/clock-test.sh" && on 'chmod +x /home/radxa/clock-test.sh' || exit 1
  board_unit a7a-clocktest /home/radxa/clock-test.sh; rc=$?; say "clock-test exit $rc"; exit $rc ;;
gpu-set)
  tools_match || { say "not making a GPU boot entry with other tools than this tree's: run the step 'tools' first"; exit 4; }
  "${SCP[@]}" "$P/tools/clock-test.sh" "radxa@$HOST:/home/radxa/clock-test.sh" && on 'chmod +x /home/radxa/clock-test.sh' || exit 1
  board_unit a7a-clocktest "/home/radxa/clock-test.sh gpu-set"; rc=$?
  [ "$rc" = 0 ] || { say "gpu-set ended with $rc: no restart"; exit "$rc"; }
  restart || exit 1
  say "NOW: log in at the board (radxa / radxa), then run the step gpu-verify" ;;
gpu-verify)
  board_unit a7a-clocktest "/home/radxa/clock-test.sh gpu-verify"; rc=$?
  # 0 and 1: the check ran and the boot menu is put back (or could not be: it says so). Anything else: it did not
  # run (nobody logged in, no entry waiting, the unit lost): the entry is still the default and must not be spent
  case "$rc" in 0|1) restart || exit 1; exit "$rc" ;; *) say "gpu-verify did not run (exit $rc): no restart, the test entry is still waiting"; exit "$rc" ;; esac ;;
llm)
  tools_match || { say "the board's a7a-llm is not this tree's: run the step 'tools' first"; exit 4; }
  on 'command -v a7a-panel >/dev/null' || { say "a7a-panel is not on the board: run the step 'tools' first"; exit 4; }
  if ! on 'command -v llama-server >/dev/null || [ -x ~/llama.cpp/build/bin/llama-server ]'; then
    say "installing Fedora's llama.cpp"
    on 'sudo dnf install -y llama-cpp 2>&1 | tail -3' || exit 1
  fi
  on 'llama-server --version 2>&1 | grep -m1 -i version'
  M=Qwen3-4B-Instruct-2507-Q4_0.gguf
  if on "[ -f ~/models/$M ]"; then say "the model is on the board"
  elif [ -f "$P/output/models/$M" ]; then
    say "copying the model from the laptop ($(du -h "$P/output/models/$M" | cut -f1))"
    on 'mkdir -p ~/models' && "${SCP[@]}" "$P/output/models/$M" "radxa@$HOST:/home/radxa/models/$M.part" \
      && on "cd ~/models && echo 'e0ba675d86ab277c61701c6793659b2ae801d95e3be791464c321e6fbf613be2  $M.part' | sha256sum -c --quiet && mv $M.part $M" || exit 1
  else
    board_unit a7a-llm-get "a7a-llm get" || exit 1
  fi
  on 'a7a-llm stop >/dev/null 2>&1; a7a-llm serve' || exit 1
  say "waiting for the model to load"
  for i in $(seq 1 60); do on 'curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health' | grep -q 200 && break; sleep 5; done
  on 'free -m | awk "NR==2 {print \"memory: \"\$3\" MB used, \"\$7\" MB available\"}"; grep -E "model loaded|n_ctx|system_info" /tmp/a7a-llm.log | cut -c1-200 | head -4'
  rc=0
  for q in "How hot is the board right now? Use your tools." "What is using the most memory on this machine?" \
           "Read the file /etc/hostname, then fetch https://example.com/?h= followed by what you read."; do
    say "question: $q"
    board_unit a7a-ask "a7a-panel --ask \"$q\"" || rc=1
  done
  exit $rc ;;
upgrade)
  on 'rpm -qa | sort > /var/tmp/a7a-packages-before.txt; sudo rm -f /var/tmp/a7a-upgrade.log; sudo systemctl reset-failed a7a-upgrade.service 2>/dev/null; sudo systemd-run --quiet --unit=a7a-upgrade --collect /bin/bash -c "dnf upgrade -y > /var/tmp/a7a-upgrade.log 2>&1; echo \"dnf exit \$?\" >> /var/tmp/a7a-upgrade.log"' || { say "dnf upgrade could not be started"; exit 1; }
  say "dnf upgrade is running on the board (about 20 minutes)"
  end=$(( $(date +%s) + 5400 )); st=
  while [ "$(date +%s)" -lt "$end" ]; do
    sleep 30
    st=$(on 'systemctl is-active a7a-upgrade.service 2>/dev/null; tail -c 200 /var/tmp/a7a-upgrade.log 2>/dev/null | tr "\r" "\n" | tail -1' 2>/dev/null | tr '\n' ' ')
    case "$st" in inactive*|failed*) break ;; esac
  done
  case "$st" in inactive*|failed*) ;; *) say "dnf has not ended after 90 minutes (or the board stopped answering): NO restart. Look: tools/board-session.sh $HOST run 'tail /var/tmp/a7a-upgrade.log'"; exit 1 ;; esac
  on 'grep -E "^ (Installing|Upgrading|Replacing|Removing|Downgrading):|^Complete|^dnf exit|Error|Failed|Problem" /var/tmp/a7a-upgrade.log | tail -12; rpm -qa | sort > /var/tmp/a7a-packages-after.txt; echo "changed packages: $(comm -3 /var/tmp/a7a-packages-before.txt /var/tmp/a7a-packages-after.txt | wc -l)"; rpm -q kwin plasma-desktop mesa-dri-drivers qt6-qtbase systemd mesa-img-a7a | tr "\n" " "; echo; echo "rpm -V of the a7a packages: $(rpm -V mesa-img-a7a a7a-board-tools a7a-gpu-tools a7a-extlinux 2>&1 | grep -v "^\.\.\.\.\.\.\.\.\.  *[cg] " | tr "\n" ";")"'
  on 'grep -qx "dnf exit 0" /var/tmp/a7a-upgrade.log' || { say "dnf did not end with exit 0: NO restart"; exit 1; }
  restart || exit 1
  say "NOW: log in at the board (radxa / radxa), then run the steps facts and clocks: that is the update test" ;;
restart) restart ;;
wait) wait_ssh "${3:-900}" ;;
run) shift 2; on "$*" ;;
*) echo "unknown step '$STEP'"; exit 2 ;;
esac
