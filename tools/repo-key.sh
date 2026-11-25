#!/bin/bash
# repo-key: make the key the a7a-fedora packages are signed with. ONCE: every board trusts it from the image on.
#   tools/repo-key.sh
# The secret key goes into its own GnuPG home, A7A_GNUPGHOME (default ~/.config/a7a-public/gnupg, mode 700), never
# into this repository; gpg asks for a passphrase (pinentry), which nobody but its owner types. The public key is
# written to tools/repo/RPM-GPG-KEY-a7a-fedora (commit it: the a7a-repo package carries it). Then the secret key
# and its revocation certificate are exported into A7A_KEY_BACKUP (default ~/a7a-fedora-key-backup, mode 700; gpg asks
# for the passphrase again), a folder that must not be inside any git checkout (review 2026-11-24: written into the
# current folder, the repository's root, one `git add -A` would have published it): copy it to a stick kept offline
# and delete it from this machine. Losing the key means no more updates for boards already out there until their
# owners import a new one by hand.
set -euo pipefail
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
export GNUPGHOME=${A7A_GNUPGHOME:-$HOME/.config/a7a-public/gnupg}
PUB=${A7A_REPO_KEY:-$P/tools/repo/RPM-GPG-KEY-a7a-fedora}
UID_=${A7A_KEY_UID:-"radxa-a7a-fedora44 packages <241389662+Rabs9@users.noreply.github.com>"}
BACKUP=${A7A_KEY_BACKUP:-$HOME/a7a-fedora-key-backup}
case "$BACKUP" in /*) ;; *) echo "repo-key: A7A_KEY_BACKUP must be an absolute path"; exit 2 ;; esac
# never into a git checkout (this repository or any other): the secret key would be one `git add` from a commit
d=$BACKUP; while [ ! -e "$d" ]; do d=$(dirname "$d"); done
if git -C "$d" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
  echo "repo-key: $BACKUP is inside a git checkout; the key backup goes outside every repository"; exit 2
fi
[ ! -e "$BACKUP" ] || [ -z "$(ls -A "$BACKUP" 2>/dev/null)" ] || { echo "repo-key: $BACKUP is not empty"; exit 2; }
[ ! -e "$PUB" ] || { echo "repo-key: $PUB exists: boards trust that key. A new one needs a key change (a new a7a-repo signed with the old key), not this script"; exit 2; }
mkdir -p "$GNUPGHOME"; chmod 700 "$GNUPGHOME"
if gpg --list-secret-keys --with-colons 2>/dev/null | grep -q '^sec'; then
  echo "repo-key: $GNUPGHOME already holds a secret key; export its public key instead:"
  echo "  GNUPGHOME=$GNUPGHOME gpg --armor --export > $PUB"
  exit 2
fi
# RSA 4096, signing only, no expiry: an expired key would stop every board's updates on the day it expires
gpg --quick-generate-key "$UID_" rsa4096 sign never
FPR=$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr/ {print $10; exit}')
[ -n "$FPR" ] || { echo "repo-key: gpg made no key"; exit 1; }
mkdir -p "$(dirname "$PUB")"
gpg --armor --export "$FPR" > "$PUB"
# the backup: the secret key (gpg asks for its passphrase once more) and the revocation certificate
mkdir -p "$BACKUP"; chmod 700 "$BACKUP"
( umask 077
  gpg --armor --export-secret-keys "$FPR" > "$BACKUP/a7a-fedora-signing-key.asc"
  cp "$GNUPGHOME/openpgp-revocs.d/$FPR.rev" "$BACKUP/a7a-fedora-signing-key.rev" )
grep -q 'PRIVATE KEY BLOCK' "$BACKUP/a7a-fedora-signing-key.asc" || { echo "repo-key: the secret key was not exported (the passphrase?); run: GNUPGHOME=$GNUPGHOME gpg --armor --export-secret-keys $FPR > $BACKUP/a7a-fedora-signing-key.asc"; exit 1; }
gpgconf --kill gpg-agent 2>/dev/null || true          # the passphrase does not stay cached
echo
echo "repo-key: key $FPR"
echo "  public key: $PUB (commit it; tools/build-tools-rpm.sh then builds a7a-repo)"
echo "  secret key: $GNUPGHOME"
echo "  backup:     $BACKUP (the secret key and its revocation certificate): copy this folder to a stick kept"
echo "              offline, check the copy, then delete it here: rm -r $BACKUP"
