#!/bin/bash
# Run INSIDE the system of a card that has been in use (read-only, as root, in tools/priv.sh's sandbox) by
# tools/master-image.sh: prints strings that belong to the owner or to this one machine, one per line as
# "<kind><TAB><string>". tools/master-canary.py then looks for them in the finished image: the filter rules are the
# reason nothing personal is in it, this is the check that the rules forgot nothing.
# The output holds secrets (the WiFi passphrase, key material): it goes into a file of mode 600 and is never printed.
# Two classes of kind:
#   <kind>        a string that is this owner's or this machine's and nobody else's (a key, an address, an id, a whole
#                 command line): found anywhere in the image, the build fails;
#   soft:<kind>   a string that is the owner's but may well occur elsewhere by chance (a file name, a word of a
#                 command line, a host he visited, an address of a private range): where it is found outside a
#                 package's own files is written into the report for a reader; it never fails the build.
# Strings shorter than 8 characters would match by chance and are left out; the host name is in the image by design
# (and is what KDE Connect calls the device unless the owner renamed it).
# Extended 2026-11-16 after the first review: every network address in the logs (a cut-off at 30 had dropped 13 of
# 43), both letter cases, what the logs say about the owner's network, all histories, what is behind the clipboard's
# and the browser's databases, the comments of ssh keys, the secrets of every kind of connection profile.
export LC_ALL=C
HOST=$(cat /etc/hostname 2>/dev/null)
out() { local v=${2//$'\t'/ }; v=${v//$'\r'/}; [ "${#v}" -ge 8 ] && [ "${#v}" -le 1000 ] && [ "$v" != "$HOST" ] && printf '%s\t%s\n' "$1" "$v"; }
each() { while IFS= read -r v; do out "$1" "$v"; done; }      # stdin: one string per line
MAC='([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}'
IP4P='(192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3})'
EMAIL='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+'
# hardware addresses, in both spellings; not the two that every text about networks contains
macs() { grep -aoE "$MAC" | tr A-F a-f | sort -u | grep -vxE '00:00:00:00:00:00|ff:ff:ff:ff:ff:ff' | while read -r m; do out network-address "$m"; out network-address "$(echo "$m" | tr a-f A-F)"; done; }

# -- the identity of the machine
out machine-id "$(cat /etc/machine-id 2>/dev/null)"
ls /var/log/journal 2>/dev/null | each machine-id
for f in /etc/ssh/ssh_host_*_key.pub; do [ -f "$f" ] && out ssh-host-public "$(awk '{print $2}' "$f")"; done
for f in /etc/ssh/ssh_host_*_key; do [ -f "$f" ] && out ssh-host-private "$(sed -n '3p' "$f")"; done
[ -f /etc/machine-info ] && sed -E 's/^[A-Z_]+=//; s/^"//; s/"$//' /etc/machine-info | each machine-info
# the password hashes the card has (the image gets the documented password; these must be gone, backups included)
awk -F: '$2 ~ /^\$/ {print $2}' /etc/shadow /etc/gshadow /etc/security/opasswd 2>/dev/null | each password-hash

# -- networks: connection profiles (every secret a profile can hold), NetworkManager's state, Bluetooth
for f in /etc/NetworkManager/system-connections/* /etc/sysconfig/network-scripts/ifcfg-* /etc/sysconfig/network-scripts/keys-* /etc/wpa_supplicant/*.conf /var/lib/iwd/*; do [ -f "$f" ] || continue
  for k in ssid psk uuid id identity anonymous-identity password private-key private-key-password preshared-key public-key wep-key0 wep-key1 wep-key2 wep-key3 leap-password pin username user apn number mac-address cloned-mac-address bssid endpoint; do
    sed -n -E "s/^[[:space:]]*$k[[:space:]]*=[[:space:]]*//p" "$f" | sed -E 's/^"//; s/"$//' | each "wifi-$k"
  done
  sed -n -E 's/^(WPA_PSK|KEY[0-9]?|IEEE_8021X_PASSWORD|ESSID|UUID|HWADDR|MACADDR|Passphrase|PreSharedKey)=//p' "$f" | sed -E 's/^"//; s/"$//' | each wifi-secret
  macs < "$f"
done
for f in /var/lib/NetworkManager/*; do [ -f "$f" ] || continue
  macs < "$f"
  grep -aoE '\b[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){7,}\b' "$f" | sort -u | each dhcp-id
  grep -aoE '\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b' "$f" | sort -u | each wifi-uuid
  grep -aoE "$IP4P" "$f" | sort -u | each soft:private-address
done
for f in /etc/wireguard/*.conf; do [ -f "$f" ] || continue
  sed -n -E 's/^[[:space:]]*(PrivateKey|PresharedKey|PublicKey|Endpoint|Address)[[:space:]]*=[[:space:]]*//p' "$f" | each vpn
done
for d in /var/lib/bluetooth/*/ /var/lib/bluetooth/*/*/ /var/lib/bluetooth/*/cache/*; do [ -e "$d" ] && basename "$d" | macs; done
for f in /var/lib/bluetooth/*/*/info /var/lib/bluetooth/*/settings /var/lib/bluetooth/*/cache/*; do [ -f "$f" ] || continue
  sed -n -E 's/^(Name|Alias)=//p' "$f" | sort -u | each bluetooth-name
  grep -aoE '^Key=[0-9A-Fa-f]{16,}' "$f" | sed 's/^Key=//' | each bluetooth-key
done

# -- what the logs say about the owner's network and hardware (the text logs; the journal is binary and compressed)
for f in /var/log/messages /var/log/secure /var/log/boot.log /var/log/cron /var/log/firewalld; do [ -f "$f" ] || continue
  macs < "$f"
  grep -aoE '\b(2[0-9a-f]{3}|fd[0-9a-f]{2}):[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f:]+' "$f" | sort -u | while read -r v; do out network-address-v6 "$v"; out network-prefix-v6 "$(echo "$v" | cut -d: -f1-4):"; done
  grep -aoE "(SSID|ssid)[ =:]+['\"][^'\"]{1,32}['\"]" "$f" | sed -E "s/^[^'\"]*['\"]//; s/['\"]$//" | sort -u | each wifi-ssid
  grep -aoE "$IP4P" "$f" | sort -u | each soft:private-address
  grep -aoE '\b[A-Za-z0-9][A-Za-z0-9-]{1,40}\.(local|lan|home|internal|ts\.net|home\.arpa)\b' "$f" | sort -u | each soft:lan-name
  grep -aoE "option (host_name|domain_name|domain_search|dhcp_client_identifier|dhcp6_client_id) +=> +'[^']+'" "$f" | sed -E "s/.*=> +'//; s/'$//" | sort -u | each soft:dhcp-option
  grep -aoE "SerialNumber: .+$" "$f" | sed 's/^SerialNumber: //' | sort -u | each soft:usb-serial
  grep -aiE 'chipid|sunxi.sid|efuse|serial *(number|no)?[ :=]|soc.?id' "$f" | grep -aoE '\b[0-9a-fA-F]{16,64}\b' | sort -u | head -200 | each soft:device-serial
  grep -aoE "rhost=[^ ]+|from [A-Za-z0-9][A-Za-z0-9.:-]+ port" "$f" | sed -E 's/^rhost=//; s/^from //; s/ port$//' | sort -u | each soft:ssh-peer
  # the keys that were used to log in, as sshd names them
  grep -aoE 'SHA256:[A-Za-z0-9+/]{43}' "$f" | sort -u | each ssh-key-fingerprint
done

# -- printers
for f in /etc/cups/printers.conf /etc/cups/printers.conf.O /etc/printcap; do [ -f "$f" ] || continue
  grep -aoE '^<(Default)?Printer [^>]+>' "$f" | sed -E 's/^<(Default)?Printer //; s/>$//' | each printer
  sed -n -E 's/^(DeviceURI|Info|Location|UUID) //p' "$f" | each printer
done

# -- the people: every home, and root's
for h in /home/*/ /root/; do
  # ssh: keys he lets in (and what he called them), his own keys, the machines he went to
  if [ -f "$h.ssh/authorized_keys" ]; then
    awk '{print $2}' "$h.ssh/authorized_keys" | each ssh-authorized-key
    awk '{for (i = 3; i <= NF; i++) print $i}' "$h.ssh/authorized_keys" | sort -u | each ssh-key-comment
  fi
  for f in "$h".ssh/id_*; do [ -f "$f" ] || continue
    case "$f" in *.pub) out ssh-user-key "$(awk '{print $2}' "$f")"; awk '{for (i = 3; i <= NF; i++) print $i}' "$f" | each ssh-key-comment ;; *) out ssh-user-key "$(sed -n '3p' "$f")" ;; esac
  done
  [ -f "$h.ssh/known_hosts" ] && { awk '{print $3}' "$h.ssh/known_hosts" | sort -u | each ssh-known-host-key; awk '{print $1}' "$h.ssh/known_hosts" | tr ',' '\n' | sort -u | each soft:ssh-peer; }
  # what holds a login in so many words: every line of some length
  for f in "$h".netrc "$h".git-credentials "$h".config/gh/hosts.yml "$h".ssh/config "$h".config/rclone/rclone.conf; do
    [ -f "$f" ] && sed -E 's/^[[:space:]]+//' "$f" | awk 'length($0) >= 12' | sort -u | each credentials
  done
  # histories: a whole command line of some length is his alone; its words and the short lines may be anybody's
  # (zsh and fish put a time stamp or "- cmd: " in front of the command)
  for f in "$h".bash_history "$h".zsh_history "$h".python_history "$h".local/share/fish/fish_history "$h".lesshst; do [ -f "$f" ] || continue
    sed -E 's/^: [0-9]+:[0-9]+;//; s/^- cmd: //' "$f" | awk 'length($0) >= 24' | sort -u | each shell-history
    sed -E 's/^: [0-9]+:[0-9]+;//; s/^- cmd: //' "$f" | awk 'length($0) >= 8 && length($0) < 24' | sort -u | each soft:history-line
    tr ' \t=' '\n\n\n' < "$f" | grep -aE '^.{12,}$' | sort -u | each soft:history-word
    grep -aoE "$IP4P" "$f" | sort -u | each soft:private-address
    grep -aoE '[A-Za-z0-9._-]+@[A-Za-z0-9._-]+' "$f" | sort -u | each soft:history-user-at-host
  done
  # who he is, as programs were told
  for f in "$h".gitconfig "$h".config/git/config; do [ -f "$f" ] && sed -n -E 's/^[[:space:]]*(name|email|user|url)[[:space:]]*=[[:space:]]*//p' "$f" | each identity; done
  for f in "$h".config/emailidentities "$h".config/emaildefaults "$h".config/kdeglobals; do [ -f "$f" ] && sed -n -E 's/^(Name|FullName|EmailAddress|Email Address|Organization|ReplyAddr)=//p' "$f" | sort -u | each identity; done
  find "$h".config "$h".local/share "$h".ssh -xdev -type f -size -3M 2>/dev/null | grep -v -E '/(baloo|Trash|containers|flatpak|akonadi/db_data|icons|fonts)/' | head -5000 | while IFS= read -r f; do grep -aIoE "$EMAIL" "$f" 2>/dev/null; done | sort -u | head -500 | each soft:mail-address
  # KDE Connect: this device and the ones it trusts
  for f in "$h".config/kdeconnect/config "$h".config/kdeconnect/trusted_devices "$h".config/kdeconnect/*/config; do [ -f "$f" ] || continue
    sed -n -E 's/^(name|deviceName|deviceId|id)=//p' "$f" | sort -u | each kdeconnect
  done
  for d in "$h".config/kdeconnect/*/; do [ -d "$d" ] && out kdeconnect "$(basename "$d")"; done
  [ -f "$h.config/kdeconnect/certificate.pem" ] && out kdeconnect "$(sed -n '3p' "$h.config/kdeconnect/certificate.pem")"
  # his screens, as KWin keeps them (not the connector's name, which is the same on every board)
  for f in "$h".config/kwinoutputconfig.json /var/lib/plasmalogin/.config/kwinoutputconfig.json; do [ -f "$f" ] || continue
    grep -oE '"(edidIdentifier|edidHash|uuid)": *"[^"]+"' "$f" | sed -E 's/^"[^"]+": *"//; s/"$//' | sort -u | each screen
  done
  # the clipboard: klipper keeps a database (the text of its rows, not its schema) and files beside it
  find "$h".local/share/klipper -xdev -type f 2>/dev/null | while IFS= read -r f; do
    strings -n 16 "$f" 2>/dev/null | grep -avE '^(CREATE |INSERT |SQLite format|sqlite_|indexsqlite_|table[a-z_]+)' | head -400
  done | sort -u | each clipboard
  # files he opened, places and folders the desktop remembers, the names of his own files
  # (a file of his own is under his home; file:///tmp or a web address is in packages' own texts as well)
  if [ -f "$h.local/share/recently-used.xbel" ]; then
    grep -oE 'href="[^"]+"' "$h.local/share/recently-used.xbel" | sed 's/^href="//; s/"$//' | grep -E '^file:///(home|root|run/media|mnt|media)/' | each recent-file
    grep -oE 'href="[^"]+"' "$h.local/share/recently-used.xbel" | sed 's/^href="//; s/"$//' | grep -vE '^file:///(home|root|run/media|mnt|media)/' | each soft:desktop-place
  fi
  # Plasma 6 keeps the lists of recent files in ~/.local/state
  for f in "$h".local/state/*staterc; do [ -f "$f" ] || continue
    grep -aoE '(file|smb|sftp|fish|ftp|nfs|mtp|kdeconnect|https?|webdav|davs?)://[^" <>,;]+' "$f" | sort -u | each soft:desktop-place
  done
  for f in "$h".config/plasma-org.kde.plasma.desktop-appletsrc "$h".config/dolphinrc "$h".config/okularpartrc "$h".config/gwenviewrc "$h".config/konsolesshconfig "$h".local/share/user-places.xbel; do [ -f "$f" ] || continue
    grep -aoE '(file|smb|sftp|fish|ftp|nfs|mtp|kdeconnect|https?|webdav|davs?)://[^" <>,;]+' "$f" | sort -u | each soft:desktop-place
  done
  for d in Documents Downloads Desktop Pictures Music Videos Public Templates .local/share/Trash/files; do
    [ -d "$h$d" ] && find "$h$d" -xdev -mindepth 1 2>/dev/null | head -3000 | while IFS= read -r p; do out soft:file-name "$(basename "$p")"; done
  done
  find "$h" -xdev -mindepth 1 -maxdepth 3 ! -path '*/.*' ! -path '*/ai-sdk/*' ! -path '*/dsu_*' 2>/dev/null | head -4000 | while IFS= read -r p; do out soft:file-name "$(basename "$p")"; done
  # the browser: profile directories are <random>.<name> ("Crash Reports", "Pending Pings" and the like are Firefox's
  # own); where he went is in its databases
  for d in "$h".config/mozilla/firefox/*.*/ "$h".mozilla/firefox/*.*/; do [ -d "$d" ] || continue
    out browser-profile "$(basename "$d")"
    [ -f "$d/prefs.js" ] && grep -aE 'sync\.username|fxaccounts|device\.name|services\.sync\.client\.name' "$d/prefs.js" | grep -oE ', *"[^"]{8,}"\)' | sed -E 's/^, *"//; s/"\)$//' | sort -u | each identity
    [ -f "$d/logins.json" ] && grep -aoE '"(hostname|formSubmitURL|httpRealm)":"[^"]+"' "$d/logins.json" | sed -E 's/^"[^"]+":"//; s/"$//' | sort -u | each soft:browser-host
    if [ -f "$d/places.sqlite" ] && command -v python3 >/dev/null; then
      python3 - "$d" <<'PY' 2>/dev/null
import os, shutil, sqlite3, sys, tempfile
p = sys.argv[1]; t = tempfile.mkdtemp(dir="/tmp")
for n in ("places.sqlite", "places.sqlite-wal", "formhistory.sqlite", "formhistory.sqlite-wal", "cookies.sqlite", "cookies.sqlite-wal"):
    if os.path.exists(os.path.join(p, n)):
        shutil.copy(os.path.join(p, n), os.path.join(t, n))
def q(db, sql, kind):
    try:
        for (v,) in sqlite3.connect(os.path.join(t, db)).execute(sql):
            if v and 8 <= len(str(v)) <= 200:
                print(f"{kind}\t{str(v).replace(chr(9), ' ').replace(chr(10), ' ')}")
    except Exception:
        pass
q("places.sqlite", "select distinct host from moz_origins", "soft:browser-host")
q("places.sqlite", "select distinct title from moz_places where title is not null limit 3000", "soft:browser-title")
q("places.sqlite", "select distinct title from moz_bookmarks where title is not null", "soft:browser-title")
q("formhistory.sqlite", "select distinct value from moz_formhistory", "soft:browser-form-value")
q("cookies.sqlite", "select distinct host from moz_cookies", "soft:browser-host")
PY
    fi
  done
done

# -- state that names his hardware and his sticks
for f in /var/lib/udisks2/mounted-fs /var/lib/AccountsService/users/*; do [ -f "$f" ] && strings -n 8 "$f" | head -200 | each soft:state; done
ls /var/spool/abrt 2>/dev/null | head -200 | each soft:state
true
