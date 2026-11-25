#!/bin/bash
# ext4-last-mounted.sh <image file> <byte offset of the ext4 in it>
# ext4 keeps the path it was mounted on last in its superblock (64 bytes at offset 136): for an image that was built
# here, a directory under the build machine's /home/<owner>. The board writes "/" there at its first mount; the file
# that people download must say "/" already, and nothing else.
# 2026-11-16: `debugfs -w -R "ssv last_mounted /"` wrote two bytes ("/" and a NUL) and left the other 62, and the
# check that followed read the field through debugfs, which stops at the NUL: the image went out to review with
# "/<NUL>ome/<owner>/Projects/..." in it. tune2fs -M clears the field before it writes, and the check here reads the
# raw bytes.
# Tests: tests/test_last_mounted.py (a real ext4 in a file, no root needed).
set -euo pipefail
IMG=${1:?image file}; OFF=${2:?byte offset of the filesystem}
[[ "$OFF" =~ ^[0-9]+$ ]] || { echo "$0: the offset must be a number of bytes"; exit 2; }
[ -f "$IMG" ] || { echo "$0: $IMG is not a file (this is for image files, never a device)"; exit 2; }
tune2fs -M / "$IMG?offset=$OFF" > /dev/null
field=$(dd if="$IMG" bs=1 skip=$((OFF + 1024 + 136)) count=64 status=none | tr -d '\0')
[ "$field" = / ] || { echo "the last-mounted field of $IMG holds ${#field} bytes that are not NUL, not the single '/'"; exit 1; }
echo "last mounted on: / (the other 63 bytes of the field are NUL)"
