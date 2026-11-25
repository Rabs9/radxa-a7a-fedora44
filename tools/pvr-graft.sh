#!/bin/bash
# pvr-graft.sh: put the upstream drm/imagination driver into work/kernel-6.6 so build-kernel.sh can build the OPEN
# GPU stack kernel flavour (A7A_PVR=1). Idempotent, and pin-aware: a different PVR_UPSTREAM un-grafts the previous
# pin (reverse its core patches, remove its files) before grafting the new one.
#
#   PVR_UPSTREAM=<sha>   upstream master commit to graft (default: PVR_MASTER_PIN below, 7.3-rc2 + the 2026-09-09
#                        merges; docs/UPSTREAM-REGRAFT-2026-09-09.md lists the driver fixes it brings)
#   PVR_UPSTREAM=v6.16   the first graft (kernel 6.6.98-a7apvr.202609091531, docs/OPEN-STACK-WEDGE-2026-09-09.md),
#                        kept reproducible: same steps as before, patches from tools/patches/pvr-open/v6.16/
#
# Steps:
#   1. sparse clone of the pin into work/linux-master (or v6.16 into work/linux616): drivers/gpu/drm/imagination,
#      include/drm, include/uapi/drm; drm_gpuvm.c fetched at the same pin into work/pvr-extra
#   2. un-graft if work/kernel-6.6/.pvr-grafted names another pin or another patch set (the marker holds the pin and a
#      fingerprint of the core patches, driver patches, compat header and compat-include, so editing any of them
#      re-grafts on the next run; PVR_REGRAFT=1 forces it). Core files are restored with `git checkout` in the
#      kernel checkout (patch -R from the snapshot in .pvr-core-applied/ when it is not a git tree), grafted files
#      removed. A run that failed half-way leaves the snapshot without the marker and is un-grafted the same way.
#   3. graft: imagination/, drm_gpuvm.c + .h, uapi pvr_drm.h, byte-identical; then the 6.6 CORE patches:
#      master: tools/patches/pvr-core/ (GEM gpuva mutex + iterators for master's drm_gpuvm.c, the scheduler's
#              DRM_GPU_SCHED_STAT_NO_HANG re-insert path from 6.17, DRM_GPUVM/imagination in Kconfig+Makefile with
#              drm_gpuva_mgr.o out of drm.ko since drm_gpuvm.c replaces it)
#      v6.16:  the Kconfig/Makefile/drm_gem.h hunks of powervr-backport/0001-powervr-backport-adaptations.patch
#   4. the compat header (powervr-backport/pvr_compat66.h) -include'd for the directory, compat-include/ on the
#      include path (master's <linux/pwrseq/consumer.h>, all stubs without CONFIG_POWER_SEQUENCING), plus the edits a
#      header cannot make: platform_driver.remove wrapper (int in 6.6), drm_gpuvm.c's drm_exec_init() calls (v6.16;
#      master builds drm_gpuvm.o with the compat header, core patch 0003),
#      struct drm_driver.date (removed in 6.14, NULL here -> WARN in drm_version, "(null)" in dmesg)
#      v6.16 only: reset reasons 6..11 for the backported pvr_dump.c, the pvr_power.c -ENOENT edit, upstream
#      d994acc526c7 (context reset decode); all of these are in master
#   5. a7a driver patches: tools/patches/pvr-open/*.patch (master) or tools/patches/pvr-open/v6.16/*.patch; a patch
#      that neither applies nor is already applied is an error
# The DT edit (compatible img,img-rogue + core/sys/mem clock names + clock pin) lives in build-kernel.sh, which
# rewrites the board DTS on every run.
set -euo pipefail
P=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
W=$P/work; K=$W/kernel-6.6; B=$P/powervr-backport; I=$K/drivers/gpu/drm/imagination; X=$W/pvr-extra
RAW=https://raw.githubusercontent.com/torvalds/linux
PVR_MASTER_PIN=50d05c7c76c96b90462f24debacca971d2e86713
PVR_UPSTREAM=${PVR_UPSTREAM:-$PVR_MASTER_PIN}
MARK=$K/.pvr-grafted; SNAP=$K/.pvr-core-applied   # marker: "<pin> <patch-set fingerprint>"; snapshot of the applied core patches
step() { echo "== [$(date +%H:%M:%S)] pvr-graft: $*"; }
die() { echo "pvr-graft: $*" >&2; exit 1; }
[ -d "$K/drivers/gpu/drm" ] || die "no kernel tree at $K (run the sources stage first)"
mkdir -p "$X"

# ---- 1. upstream sources at the pin ------------------------------------------------------------------------------
if [ "$PVR_UPSTREAM" = v6.16 ]; then
  MODE=v616; V=$W/linux616; PATCHDIR=$P/tools/patches/pvr-open/v6.16
  if [ ! -d "$V/.git" ]; then
    step "sparse clone of v6.16 (imagination, include/drm, include/uapi/drm)"
    git clone -q --depth 1 --branch v6.16 --filter=blob:none --sparse https://github.com/torvalds/linux.git "$V"
    git -C "$V" sparse-checkout set drivers/gpu/drm/imagination include/drm include/uapi/drm >/dev/null
  else step "v6.16 sources present in $V"; fi
  PIN=$(git -C "$V" rev-parse HEAD)
  GPUVM=$X/drm_gpuvm.c.v616
  [ -f "$GPUVM" ] || curl -sfL --max-time 120 -o "$GPUVM" "$RAW/v6.16/drivers/gpu/drm/drm_gpuvm.c"
  [ -f "$X/d994acc5.patch" ] || curl -sfL --max-time 120 -o "$X/d994acc5.patch" "https://github.com/torvalds/linux/commit/d994acc526c7.patch"
  grep -q "pvr_dump_context_reset_notification" "$X/d994acc5.patch" || die "d994acc5.patch download is wrong"
  # the Kconfig / Makefile / drm_gem.h hunks of the other session's patch (its driver hunks are superseded by the header)
  python3 - "$B/0001-powervr-backport-adaptations.patch" "$X/0001-core-only.patch" <<'EOF'
import sys,re
src,dst=sys.argv[1],sys.argv[2]
blocks=re.split(r'(?m)^(?=--- a/)', open(src).read())
keep=[b for b in blocks if b.startswith('--- a/drivers/gpu/drm/Kconfig') or b.startswith('--- a/drivers/gpu/drm/Makefile') or b.startswith('--- a/include/drm/drm_gem.h')]
assert len(keep)==3, len(keep)
open(dst,'w').write(''.join(keep))
EOF
  CORE=("$X/0001-core-only.patch")
else
  MODE=master; V=$W/linux-master; PATCHDIR=$P/tools/patches/pvr-open; CORE=("$P"/tools/patches/pvr-core/*.patch)
  if [ ! -d "$V/.git" ]; then
    step "sparse clone of upstream master (imagination, scheduler, include/drm, include/uapi/drm)"
    git clone -q --depth 1 --filter=blob:none --sparse https://github.com/torvalds/linux.git "$V"
    git -C "$V" sparse-checkout set drivers/gpu/drm/imagination drivers/gpu/drm/scheduler include/drm include/uapi/drm >/dev/null
  fi
  if [ "$(git -C "$V" rev-parse HEAD)" != "$PVR_UPSTREAM" ]; then
    step "fetching pin $PVR_UPSTREAM"
    git -C "$V" fetch -q --depth 1 origin "$PVR_UPSTREAM" && git -C "$V" checkout -q "$PVR_UPSTREAM"
  fi
  PIN=$(git -C "$V" rev-parse HEAD)
  [ "$PIN" = "$PVR_UPSTREAM" ] || die "could not check out $PVR_UPSTREAM in $V (HEAD is $PIN)"
  step "upstream master pinned at $(git -C "$V" log -1 --format='%h %cd %s' --date=short | cut -c1-90)"
  GPUVM=$X/drm_gpuvm.c.$PIN
  [ -f "$GPUVM" ] || curl -sfL --max-time 120 -o "$GPUVM" "$RAW/$PIN/drivers/gpu/drm/drm_gpuvm.c"
  grep -q "drm_gpuvm_bo_put_deferred" "$GPUVM" || die "drm_gpuvm.c fetch at $PIN is wrong"
fi
[ -f "$V/drivers/gpu/drm/imagination/pvr_drv.c" ] || die "sparse checkout is missing the driver"

# ---- 2. fingerprint; un-graft another pin / patch set / a half-done run -------------------------------------------
FPRINT=$( { cat "${CORE[@]}" "$PATCHDIR"/*.patch "$B/pvr_compat66.h"; find "$B/compat-include" -type f -exec cat {} +; } | sha256sum | cut -c1-16)
WANT="$PIN $FPRINT"
ungraft() {
  local files pf
  # files the applied core patches touched: the snapshot; older markers listed their patches in .pvr-grafted-core
  # (2026-09-09 afternoon) or used the v6.16 extracted hunks only
  files=$( { [ -d "$SNAP" ] && cat "$SNAP"/*.patch; [ -f "$K/.pvr-grafted-core" ] && xargs cat < "$K/.pvr-grafted-core"; cat "$X/0001-core-only.patch"; } 2>/dev/null | grep -h '^--- a/' | sed 's#^--- a/##' | sort -u)
  if git -C "$K" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    [ -z "$files" ] || git -C "$K" checkout -q -- $files
    echo "  - restored from git: $(echo $files | tr '\n' ' ')"
  elif [ -d "$SNAP" ]; then
    for pf in $(ls -r "$SNAP"/*.patch); do patch -R -p1 -s -d "$K" < "$pf" || die "reversing $(basename "$pf") failed: not a git tree, fix the core files by hand"; echo "  - reversed $(basename "$pf")"; done
  else die "cannot un-graft: neither a git checkout nor a core-patch snapshot in $K"; fi
  rm -rf "$I" "$K/drivers/gpu/drm/drm_gpuvm.c" "$K/include/drm/drm_gpuvm.h" "$K/include/uapi/drm/pvr_drm.h" "$MARK" "$SNAP" "$K/.pvr-grafted-core"
}
if [ -f "$MARK" ] && { [ "$(cat "$MARK")" != "$WANT" ] || [ "${PVR_REGRAFT:-0}" = 1 ]; }; then
  step "un-graft $(cut -c1-12 "$MARK") (pin/patch set changed${PVR_REGRAFT:+ or PVR_REGRAFT}): restore core files, remove grafted files"
  ungraft
elif [ ! -f "$MARK" ] && [ -d "$SNAP" ]; then
  step "un-graft a half-done previous run"; ungraft
fi

# ---- 3. graft the driver + gpuvm + uapi, then the 6.6 core patches ---------------------------------------------
if [ ! -f "$MARK" ]; then
  step "graft $MODE driver ($(ls "$V/drivers/gpu/drm/imagination" | wc -l) files) + drm_gpuvm into the 6.6 tree"
  rm -rf "$I"; cp -a "$V/drivers/gpu/drm/imagination" "$I"
  cp "$GPUVM" "$K/drivers/gpu/drm/drm_gpuvm.c"
  cp "$V/include/drm/drm_gpuvm.h" "$K/include/drm/drm_gpuvm.h"
  cp "$V/include/uapi/drm/pvr_drm.h" "$K/include/uapi/drm/pvr_drm.h"
  rm -rf "$SNAP"; mkdir -p "$SNAP"
  for pf in "${CORE[@]}"; do
    cp "$pf" "$SNAP/"                       # recorded BEFORE applying: a failure below is still un-graftable
    out=$(patch -p1 -N -s -d "$K" < "$pf" 2>&1) || die "core patch $(basename "$pf") did not apply:
$out"
    echo "  + core: $(basename "$pf")"
  done
else step "graft in place ($(cut -c1-12 "$MARK"), patch set $FPRINT)"; fi

# ---- 4. compat header, compat include dir, the edits a header cannot make -------------------------------------
step "compat header + compat-include + the edits a header cannot make"
cp "$B/pvr_compat66.h" "$I/pvr_compat66.h"
rm -rf "$I/compat-include"; cp -a "$B/compat-include" "$I/compat-include"
python3 - "$K" "$MODE" <<'EOF'
import sys
K,MODE=sys.argv[1],sys.argv[2]; I=K+"/drivers/gpu/drm/imagination"
def edit(path, old, new, label, count=1):
    s=open(path).read()
    if new in s: print("  = %-40s already applied"%label); return
    n=s.count(old)
    if n!=count: print("  ! %-40s expected %d, found %d"%(label,count,n)); sys.exit(1)
    open(path,'w').write(s.replace(old,new)); print("  + %-40s %d site(s)"%(label,n))
mk=I+"/Makefile"; s=open(mk).read()
if "pvr_compat66.h" not in s:
    open(mk,'w').write("subdir-ccflags-y += -include $(srctree)/$(src)/pvr_compat66.h -I$(srctree)/$(src)/compat-include\n"+s); print("  + Makefile -includes pvr_compat66.h, -I compat-include")
else: print("  = Makefile already -includes the header")
edit(I+"/pvr_drv.c",
     "static struct platform_driver pvr_driver = {\n\t.probe = pvr_probe,\n\t.remove = pvr_remove,",
     "/* backport: 6.6's platform_driver.remove returns int (void from 6.11); wrap, keep pvr_remove() upstream. */\n"
     "static int pvr_remove_compat66(struct platform_device *plat_dev)\n{\n\tpvr_remove(plat_dev);\n\treturn 0;\n}\n\n"
     "static struct platform_driver pvr_driver = {\n\t.probe = pvr_probe,\n\t.remove = pvr_remove_compat66,",
     "pvr_drv.c .remove wrapper")
if MODE == "v616":  # master: drm_gpuvm.o is built with the compat header (core patch 0003), whose drm_exec_init macro drops the hint
    edit(K+"/drivers/gpu/drm/drm_gpuvm.c", "drm_exec_init(exec, vm_exec->flags, 0);", "drm_exec_init(exec, vm_exec->flags);", "drm_gpuvm.c drm_exec_init (v6.16)", count=2)
edit(I+"/pvr_drv.c", "\t.desc = PVR_DRIVER_DESC,\n", "\t.desc = PVR_DRIVER_DESC,\n\t.date = \"20250727\", /* backport: 6.6 still has the field (removed in 6.14); NULL warns in drm_version() */\n",
     "pvr_drv.c .date")
if MODE == "v616":
    edit(I+"/pvr_compat66.h", "#endif /* PVR_COMPAT66_H */",
         "/* upstream pvr_dump.c (d994acc5) names reset reasons 6..11 that v6.16's enum rogue_context_reset_reason lacks */\n"
         "#define ROGUE_CONTEXT_RESET_REASON_WGP_CHECKSUM 6\n#define ROGUE_CONTEXT_RESET_REASON_TRP_CHECKSUM 7\n"
         "#define ROGUE_CONTEXT_RESET_REASON_GPU_ECC_OK 8\n#define ROGUE_CONTEXT_RESET_REASON_GPU_ECC_HWR 9\n"
         "#define ROGUE_CONTEXT_RESET_REASON_FW_ECC_OK 10\n#define ROGUE_CONTEXT_RESET_REASON_FW_ECC_ERR 11\n\n#endif /* PVR_COMPAT66_H */",
         "compat: reset reasons 6..11 (v6.16)")
    edit(I+"/pvr_power.c",
         "\tif (domain_count < 0)\n\t\treturn domain_count;\n\n\tif (domain_count <= 1)\n\t\treturn 0;\n",
         "\t/* backport: a node with NO power-domains property gives -ENOENT here and aborted the probe on the A733,\n"
         "\t * whose GPU sits in no described domain; the next line shows 0 or 1 domains need no work. */\n"
         "\tif (domain_count == -ENOENT)\n\t\tdomain_count = 0;\n\tif (domain_count < 0)\n\t\treturn domain_count;\n\n\tif (domain_count <= 1)\n\t\treturn 0;\n",
         "pvr_power.c: no power-domains = 0 domains (v6.16)")
EOF

if [ "$MODE" = v616 ]; then
  step "upstream d994acc526c7: decode context reset notifications (pvr_dump.c/h) (v6.16 only; in master)"
  if [ ! -f "$I/pvr_dump.c" ]; then
    python3 - "$X/d994acc5.patch" "$I" <<'EOF'
import sys,re
patch,I=sys.argv[1],sys.argv[2]
blocks=re.split(r'(?m)^(?=diff --git )', open(patch).read())
for b in blocks:
    m=re.match(r'diff --git a/(\S+) b/', b)
    if not m: continue
    name=m.group(1).split('/')[-1]
    if name in ('pvr_dump.c','pvr_dump.h'):
        body=[l[1:] for l in b.splitlines() if l.startswith('+') and not l.startswith('+++')]
        open(I+'/'+name,'w').write('\n'.join(body)+'\n'); print("  + wrote %s (%d lines)"%(name,len(body)))
EOF
  fi
  python3 - "$I" <<'EOF'
import sys
I=sys.argv[1]
def edit(path, old, new, label):
    s=open(path).read()
    if new in s: print("  = %-40s already applied"%label); return
    assert s.count(old)==1, (label, s.count(old))
    open(path,'w').write(s.replace(old,new)); print("  + %s"%label)
edit(I+"/Makefile", "\tpvr_drv.o \\\n", "\tpvr_drv.o \\\n\tpvr_dump.o \\\n", "Makefile pvr_dump.o")
edit(I+"/pvr_ccb.c", '#include "pvr_drv.h"\n', '#include "pvr_drv.h"\n#include "pvr_dump.h"\n', "pvr_ccb.c include pvr_dump.h")
edit(I+"/pvr_ccb.c",
     '\tdefault:\n\t\tdrm_info(from_pvr_device(pvr_dev), "Received unknown FWCCB command %x\\n",\n\t\t\t cmd->cmd_type);\n',
     '\tcase ROGUE_FWIF_FWCCB_CMD_UPDATE_STATS:\n\t\t/* upstream d994acc5: no stats infrastructure yet; not "unknown" */\n\t\tbreak;\n'
     '\tcase ROGUE_FWIF_FWCCB_CMD_CONTEXT_RESET_NOTIFICATION:\n\t\tpvr_dump_context_reset_notification(pvr_dev,\n\t\t\t\t\t\t    &cmd->cmd_data.cmd_context_reset_notification);\n\t\tbreak;\n\n'
     '\tdefault:\n\t\tdrm_info(from_pvr_device(pvr_dev), "Received unknown FWCCB command (type=%d)\\n",\n\t\t\t cmd->cmd_type & ~ROGUE_CMD_MAGIC_DWORD_MASK);\n',
     "pvr_ccb.c reset notification + stats cases")
EOF
fi

# ---- 5. a7a driver patches -----------------------------------------------------------------------------------------
step "a7a patches for the open driver ($PATCHDIR)"
for pf in "$PATCHDIR"/*.patch; do
  [ -f "$pf" ] || continue
  if out=$(patch -p1 -N -s --dry-run -d "$K" < "$pf" 2>&1); then patch -p1 -N -s -d "$K" < "$pf" && echo "  + applied $(basename "$pf")"
  elif patch -R -p1 -s --dry-run -d "$K" < "$pf" >/dev/null 2>&1; then echo "  = $(basename "$pf") already applied"
  else die "$(basename "$pf") neither applies nor is applied on pin $PIN (edited patch? PVR_REGRAFT=1 re-grafts):
$out"; fi
done
grep -q "cmd_context_reset_notification" "$I/pvr_rogue_fwif.h" || die "union member name differs in pvr_rogue_fwif.h; check"
if [ "$MODE" = master ]; then
  grep -q "PVR_PACKED_BVNC(36, 56, 104, 183)" "$I/pvr_device.c" || die "A733 GPU ID patch missing: the driver would refuse the GPU (-ENODEV)"
  grep -q "domain_count == -ENOENT" "$I/pvr_power.c" || die "power-domain -ENOENT fix missing: the probe would fail"
fi
echo "$WANT" > "$MARK"
step "done ($MODE @ $(cut -c1-12 "$MARK"), patch set $FPRINT): $(ls "$I" | grep -c '\.[ch]$') sources in imagination/, drm_gpuvm.c $(wc -l < "$K/drivers/gpu/drm/drm_gpuvm.c") lines"
