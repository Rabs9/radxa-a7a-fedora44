#!/bin/bash
# public-export: the public copy of this repository (for GitHub) from the development repository (Gitea).
#   tools/public-export.sh [OUT]       OUT: a git repository of its own, default output/public/radxa-a7a-fedora44
# 1. the files of HEAD (git archive: committed files only, nothing of the working tree);
# 2. minus the paths in tools/public-exclude.list (session logs, drafts, scratch scripts, one machine's run log);
# 3. tools/public-check.py on what is left: this machine's names and addresses, e-mail and hardware addresses,
#    home paths, and the strings of the builder's private file (A7A_PRIVATE, default
#    ~/.config/a7a-public/private.txt; the export refuses to run without one). Anything found: it stops, OUT untouched;
# 4. OUT's files replaced by them and committed as ONE commit, author and committer the public identity
#    (A7A_PUBLIC_AUTHOR, "Name <address>", default Rabs9's GitHub no-reply address), its date in UTC.
# It never pushes: OUT's remote and the push are the owner's. History: each export is one commit on OUT's main
# branch, so the public history is a list of releases; the development history stays on Gitea.
set -euo pipefail
P=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$P/output/public/radxa-a7a-fedora44}
AUTHOR=${A7A_PUBLIC_AUTHOR:-"Rabs9 <241389662+Rabs9@users.noreply.github.com>"}
PRIVATE=${A7A_PRIVATE:-$HOME/.config/a7a-public/private.txt}
NAME=${AUTHOR% <*}; MAIL=${AUTHOR##*<}; MAIL=${MAIL%>}
[ -n "$NAME" ] && [ -n "$MAIL" ] && [ "$NAME <$MAIL>" = "$AUTHOR" ] || { echo "public-export: A7A_PUBLIC_AUTHOR must be \"Name <address>\""; exit 2; }
[ -f "$PRIVATE" ] || { echo "public-export: no private file $PRIVATE (the strings of yours to look for, one per line); refusing"; exit 2; }
# OUT's files are all replaced: only a directory that is missing, empty, or one this script made (its mark lives in
# OUT/.git, never committed) is taken; never this repository, a folder holding it, or any other (review 2026-11-24:
# a clone of this repository has the script in it too, and was taken; its card backups would have gone)
MARK=.git/a7a-public-export
RP=$(realpath -m "$P"); RO=$(realpath -m "$OUT")
case "$RP/" in "$RO"/*) echo "public-export: $OUT is this repository or holds it; refusing"; exit 2 ;; esac
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ] && [ ! -f "$OUT/$MARK" ]; then
  echo "public-export: $OUT is not empty and was not made by this script; refusing to replace its files"
  exit 2
fi
git -C "$P" diff --quiet HEAD -- 2>/dev/null || echo "public-export: uncommitted changes are left out (only HEAD is exported)"

STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
git -C "$P" archive HEAD | tar -x -C "$STAGE"
# the list as committed (the one in the stage), each line a glob relative to the root; one that matches nothing is
# said (a session log renamed would otherwise go out without a word)
python3 - "$STAGE" <<'PY' || exit 2
import shutil, sys
from pathlib import Path
stage = Path(sys.argv[1])
lst = stage / "tools/public-exclude.list"
if not lst.is_file():
    sys.exit("public-export: tools/public-exclude.list is not in HEAD")
for raw in lst.read_text().splitlines():
    g = raw.strip().rstrip("\r")
    if not g or g.startswith("#"):
        continue
    if g.startswith("/") or ".." in Path(g).parts:
        sys.exit(f"public-export: {g!r} in public-exclude.list is not a path inside the repository")
    hits = list(stage.glob(g))
    if not hits:
        print(f"public-export: note: {g!r} of public-exclude.list matches nothing in HEAD")
    for h in hits:
        if h.is_dir() and not h.is_symlink():
            shutil.rmtree(h)
        else:
            h.unlink()
PY

if ! python3 "$P/tools/public-check.py" "$STAGE" --private "$PRIVATE" --allow "$STAGE/tools/public-allow.txt" --repo "$P"; then
  echo "public-export: stopped; nothing written to $OUT"
  exit 1
fi

mkdir -p "$OUT"
[ -d "$OUT/.git" ] || { git -C "$OUT" init -q -b main && : > "$OUT/$MARK"; }
[ -f "$OUT/$MARK" ] || { echo "public-export: $OUT has a git repository this script did not make; refusing"; exit 2; }
git -C "$OUT" rm -rq --ignore-unmatch -- . >/dev/null
find "$OUT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf -- {} +
cp -a "$STAGE"/. "$OUT"/
# --force: a file of HEAD that .gitignore names (registry.json is tracked and ignored) goes out as it is, not dropped
git -C "$OUT" add -A --force
if git -C "$OUT" diff --cached --quiet; then
  echo "public-export: $OUT already holds this tree; no commit"
  exit 0
fi
TZ=UTC git -C "$OUT" -c user.name="$NAME" -c user.email="$MAIL" -c commit.gpgsign=false commit -q --author="$AUTHOR" \
  -m "a7a-fedora $(date -u +%Y-%m-%d)" \
  -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
echo "public-export: committed $(git -C "$OUT" rev-parse --short HEAD) in $OUT ($(git -C "$OUT" ls-files | wc -l) files); not pushed"
