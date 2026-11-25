#!/bin/bash
# The 39 "missing" symbols were mostly an artefact: those helper modules exist
# but are not loaded, so they are absent from kallsyms. What actually matters is
# whether the BSP kernel HAS drm_gpuvm (6.7) or only the older drm_gpuva_mgr.
set -u
K=$(uname -r)

echo "############ helper options in the 6.6.98 BSP config"
zcat /proc/config.gz 2>/dev/null | grep -iE 'GPUVM|GPUVA|DRM_SCHED|DRM_EXEC|GEM_SHMEM|DRM_TTM=' | sed 's/^/  /'

echo
echo "############ helper modules actually on disk"
for m in drm_gpuvm drm_gpuva_mgr drm_exec gpu-sched drm_shmem_helper drm_gem_shmem_helper drm_ttm_helper; do
    printf "  %-24s %s\n" "$m" "$(find /lib/modules/$K -name "${m}.ko*" 2>/dev/null | head -1 || echo '-')"
done

echo
echo "############ everything in drivers/gpu/drm top level"
ls -1 /lib/modules/$K/kernel/drivers/gpu/drm/ 2>/dev/null | sed 's/^/  /'

echo
echo "############ kernel source / headers available to build against?"
ls -d /lib/modules/$K/build /usr/src/linux* /usr/src/kernels/* 2>/dev/null | sed 's/^/  /'
[ -e /lib/modules/$K/build ] && echo "  build -> $(readlink -f /lib/modules/$K/build)"
[ -d /lib/modules/$K/build/include/drm ] && echo "  drm headers: $(ls /lib/modules/$K/build/include/drm 2>/dev/null | wc -l) files"
for h in drm_gpuvm.h drm_gpuva_mgr.h drm_exec.h gpu_scheduler.h drm_gem_shmem_helper.h; do
    printf "  %-26s %s\n" "$h" "$(find /lib/modules/$K/build/include -name "$h" 2>/dev/null | head -1 || echo MISSING)"
done
