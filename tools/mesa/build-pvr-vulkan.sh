#!/bin/bash
# build-pvr-vulkan.sh: ON THE BOARD. Build Mesa's PowerVR Vulkan driver (the standalone ICD libvulkan_powervr_mesa.so)
# from Fedora's mesa source RPM with a7a-fedora's fixes, for /usr/local, so the Vulkan loader uses it instead of
# Fedora's copy. Nothing else of Mesa is rebuilt (zink, EGL, GBM stay Fedora's). The result is a staging tarball
# /home/radxa/mesa-build/mesa-pvr-a7a-<mesa version>-<stamp>.tar.gz that tools/build-mesa-rpm.sh on the laptop
# packages as the mesa-pvr-a7a RPM (the install path since 2026-09-09: docs/PACKAGING-2026-09-09.md). --install
# additionally installs straight into /usr/local and masks Fedora's ICD (the driver dev loop; the next RPM install
# replaces those files).
# Fixes (docs/OPEN-STACK-WEDGE-2026-09-09.md, "compositor slowness"):
#   1. pvr_spm.c: the partial-render scratch buffer store reuses its buffer only for an IDENTICAL size; a compositor
#      alternates render sizes, so every BeginRendering freed and re-allocated (kernel-zeroed) a multi-MB buffer:
#      ~100 ms of CPU per frame for windows taller than ~500 rows. Reuse when the cached buffer is big enough.
#   2. pvr_physical_device.c: treat BVNC 36.56.104.183 (Allwinner A733) as conformant so no
#      PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 is needed anywhere (not CTS-certified; the DDK has the same GPU).
# usage: build-pvr-vulkan.sh [--install] [srcrpm]   (default: the newest mesa-*.src.rpm in the current directory)
set -euo pipefail
W=/home/radxa/mesa-build; cd "$W"
INSTALL=0; [ "${1:-}" = --install ] && { INSTALL=1; shift; }
SRPM=${1:-$(ls -t mesa-*.src.rpm | head -1)}
[ -f "$SRPM" ] || { echo "no mesa src.rpm in $W (dnf download --source mesa)"; exit 1; }
step() { echo "== [$(date +%H:%M:%S)] $*"; }
step "unpack $SRPM"
rpm2cpio "$SRPM" | cpio -id --quiet "./mesa-*.tar.xz"
T=$(ls -t mesa-*.tar.xz | head -1); D=${T%.tar.xz}
# always a clean tree: the edits below assume pristine sources (a leftover tree with a partial edit set is unsafe)
rm -rf "$D"; tar -xJf "$T"
cd "$D"
step "patches"
python3 - <<'PY'
import sys
def edit(path, old, new, label):
    s=open(path).read()
    if new in s: print("  = %s already applied"%label); return
    assert s.count(old)==1, (label, s.count(old))
    open(path,'w').write(s.replace(old,new)); print("  + %s"%label)
edit("src/imagination/vulkan/pvr_spm.c",
     "   if (store->head_ref && store->head_ref->size == size) {",
     "   /* a7a-fedora: reuse the cached buffer whenever it is large enough. Requiring an identical size made a\n"
     "    * compositor (KWin/zink), whose render areas alternate, free and re-allocate this multi-MB buffer on\n"
     "    * every BeginRendering: ~100 ms of CPU per frame (kernel zeroing) for windows taller than ~500 rows. */\n"
     "   if (store->head_ref && store->head_ref->size >= size) {",
     "pvr_spm.c: scratch buffer reuse by size >=")
edit("src/imagination/vulkan/pvr_physical_device.c",
     "   switch (bvnc) {\n   case PVR_BVNC_PACK(36, 53, 104, 796):\n      return true;",
     "   switch (bvnc) {\n   case PVR_BVNC_PACK(36, 53, 104, 796):\n"
     "   /* a7a-fedora: Allwinner A733 (Radxa Cubie A7A). Not CTS-certified; treated as conformant so that no\n"
     "    * PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 is needed in any environment. */\n"
     "   case PVR_BVNC_PACK(36, 56, 104, 183):\n      return true;",
     "pvr_physical_device.c: 36.56.104.183 conformant")

# ---- 3. render-state cache for dynamic rendering (the upstream FIXME "handle it with a proper hash table") ----
# Every vkCmdBeginRendering built a full render state (PM free list, HWRT dataset, PPP state, partial-render
# programs, scratch buffer) and tore it down when the command buffer was freed. zink (KWin, every GL app) begins a
# rendering per draw batch, so this ran on every frame and scaled with the render area. The state only depends on
# width/height/layers/alignments, sample count, output-register and tile-buffer counts, view mask and the scratch
# size, so cache it on the device by that key, refcounted by the sub-commands that use it.
edit("src/imagination/vulkan/pvr_framebuffer.h",
     "struct pvr_render_state {\n   struct list_head link;\n",
     "struct pvr_render_state {\n   struct list_head link;\n\n"
     "   /* a7a-fedora: dynamic-rendering render-state cache (pvr_rstate_cache_lookup). */\n"
     "   uint32_t cache_users;\n   uint32_t key_sample_count;\n   uint32_t key_output_regs_count;\n"
     "   uint32_t key_tile_buffers_count;\n   uint32_t key_view_mask;\n",
     "pvr_framebuffer.h: cache key fields")
edit("src/imagination/vulkan/pvr_device.h",
     "void pvr_rstate_entry_remove(struct pvr_device *device,\n                             const struct pvr_render_state *rstate);\n",
     "void pvr_rstate_entry_remove(struct pvr_device *device,\n                             const struct pvr_render_state *rstate);\n\n"
     "struct pvr_renderpass_hwsetup_render;\n"
     "struct pvr_render_state *\n"
     "pvr_rstate_cache_lookup(struct pvr_device *device,\n"
     "                        const struct pvr_render_state *key,\n"
     "                        const struct pvr_renderpass_hwsetup_render *hw_render);\n"
     "void pvr_rstate_cache_insert(struct pvr_device *device,\n"
     "                             struct pvr_render_state *rstate,\n"
     "                             const struct pvr_renderpass_hwsetup_render *hw_render);\n",
     "pvr_device.h: cache prototypes")
edit("src/imagination/vulkan/pvr_device.c",
     "void pvr_rstate_entry_remove(struct pvr_device *device,\n"
     "                             const struct pvr_render_state *rstate)\n{\n"
     "   simple_mtx_lock(&device->rs_mtx);\n   assert(rstate);\n\n"
     "   list_for_each_entry_safe (struct pvr_render_state,\n"
     "                             entry,\n"
     "                             &device->render_states,\n"
     "                             link) {\n"
     "      if (entry != rstate)\n         continue;\n\n"
     "      pvr_render_state_cleanup(device, &device->vk.alloc, rstate);\n"
     "      list_del(&entry->link);\n\n"
     "      vk_free(&device->vk.alloc, entry);\n   }\n\n"
     "   simple_mtx_unlock(&device->rs_mtx);\n}\n",
     "/* a7a-fedora: a sub-command dropping its reference; the render state stays cached (device destroy or\n"
     " * eviction frees it). */\n"
     "void pvr_rstate_entry_remove(struct pvr_device *device,\n"
     "                             const struct pvr_render_state *rstate)\n{\n"
     "   simple_mtx_lock(&device->rs_mtx);\n\n   assert(rstate);\n\n"
     "   list_for_each_entry (struct pvr_render_state, entry, &device->render_states, link) {\n"
     "      if (entry != rstate)\n         continue;\n\n"
     "      assert(entry->cache_users > 0);\n      entry->cache_users--;\n      break;\n   }\n\n"
     "   simple_mtx_unlock(&device->rs_mtx);\n}\n\n"
     "#define PVR_RSTATE_CACHE_MAX 64\n\n"
     "struct pvr_render_state *\n"
     "pvr_rstate_cache_lookup(struct pvr_device *device,\n"
     "                        const struct pvr_render_state *key,\n"
     "                        const struct pvr_renderpass_hwsetup_render *hw_render)\n{\n"
     "   struct pvr_render_state *found = NULL;\n\n"
     "   simple_mtx_lock(&device->rs_mtx);\n"
     "   list_for_each_entry (struct pvr_render_state, entry, &device->render_states, link) {\n"
     "      if (entry->width != key->width || entry->height != key->height ||\n"
     "          entry->layers != key->layers ||\n"
     "          entry->width_alignment != key->width_alignment ||\n"
     "          entry->height_alignment != key->height_alignment ||\n"
     "          entry->scratch_buffer_size != key->scratch_buffer_size ||\n"
     "          entry->render_count != 1 ||\n"
     "          entry->key_sample_count != hw_render->sample_count ||\n"
     "          entry->key_output_regs_count != hw_render->output_regs_count ||\n"
     "          entry->key_tile_buffers_count != hw_render->tile_buffers_count ||\n"
     "          entry->key_view_mask != hw_render->view_mask)\n"
     "         continue;\n\n"
     "      entry->cache_users++;\n"
     "      /* most recently used first */\n"
     "      list_del(&entry->link);\n      list_add(&entry->link, &device->render_states);\n"
     "      found = entry;\n      break;\n   }\n"
     "   simple_mtx_unlock(&device->rs_mtx);\n\n   return found;\n}\n\n"
     "void pvr_rstate_cache_insert(struct pvr_device *device,\n"
     "                             struct pvr_render_state *rstate,\n"
     "                             const struct pvr_renderpass_hwsetup_render *hw_render)\n{\n"
     "   uint32_t count = 0;\n\n"
     "   rstate->cache_users = 1;\n"
     "   rstate->key_sample_count = hw_render->sample_count;\n"
     "   rstate->key_output_regs_count = hw_render->output_regs_count;\n"
     "   rstate->key_tile_buffers_count = hw_render->tile_buffers_count;\n"
     "   rstate->key_view_mask = hw_render->view_mask;\n\n"
     "   simple_mtx_lock(&device->rs_mtx);\n"
     "   list_add(&rstate->link, &device->render_states);\n"
     "   list_for_each_entry_safe (struct pvr_render_state, entry, &device->render_states, link) {\n"
     "      count++;\n"
     "      if (count <= PVR_RSTATE_CACHE_MAX || entry->cache_users)\n         continue;\n"
     "      /* least recently used, unreferenced: every command buffer using it is gone */\n"
     "      pvr_render_state_cleanup(device, &device->vk.alloc, entry);\n"
     "      list_del(&entry->link);\n      vk_free(&device->vk.alloc, entry);\n   }\n"
     "   simple_mtx_unlock(&device->rs_mtx);\n}\n",
     "pvr_device.c: cache lookup/insert, remove = unref")
edit("src/imagination/vulkan/pvr_arch_cmd_buffer.c",
     "   /* FIXME: When adding support for caching rt datasets and render states,\n"
     "    * remove this and handle it with a proper hash table.\n    */\n"
     "   pvr_rstate_entry_add(device, rstate);\n",
     "   /* a7a-fedora: the render state is looked up in / inserted into the device cache once its key is\n"
     "    * complete, in vkCmdBeginRendering (pvr_rstate_cache_lookup). */\n",
     "pvr_arch_cmd_buffer.c: no early list add")
edit("src/imagination/vulkan/pvr_arch_cmd_buffer.c",
     "   result = pvr_arch_render_state_setup(device,\n"
     "                                        NULL,\n"
     "                                        state->render_pass_info.rstate,\n"
     "                                        1,\n"
     "                                        &dr_info->hw_render);\n"
     "   if (result != VK_SUCCESS) {\n"
     "      vk_command_buffer_set_error(&cmd_buffer->vk, result);\n"
     "      goto err_cleanup_tile_buffers;\n   }\n",
     "   {\n"
     "      /* a7a-fedora: reuse a cached render state with the same key instead of building one per render */\n"
     "      struct pvr_render_state *cached =\n"
     "         pvr_rstate_cache_lookup(device, state->render_pass_info.rstate, &dr_info->hw_render);\n\n"
     "      if (cached) {\n"
     "         vk_free(&device->vk.alloc, state->render_pass_info.rstate);\n"
     "         state->render_pass_info.rstate = cached;\n"
     "      } else {\n"
     "         result = pvr_arch_render_state_setup(device,\n"
     "                                              NULL,\n"
     "                                              state->render_pass_info.rstate,\n"
     "                                              1,\n"
     "                                              &dr_info->hw_render);\n"
     "         if (result != VK_SUCCESS) {\n"
     "            vk_command_buffer_set_error(&cmd_buffer->vk, result);\n"
     "            goto err_cleanup_tile_buffers;\n         }\n\n"
     "         pvr_rstate_cache_insert(device, state->render_pass_info.rstate, &dr_info->hw_render);\n"
     "      }\n   }\n",
     "pvr_arch_cmd_buffer.c: lookup or setup+insert")
edit("src/imagination/vulkan/pvr_arch_cmd_buffer.c",
     "err_cleanup_render_state:\n   pvr_render_state_cleanup(device, &device->vk.alloc, state->render_pass_info.rstate);\n",
     "err_cleanup_render_state:\n   /* a7a-fedora: the render state is cached; drop this render's reference only */\n"
     "   pvr_rstate_entry_remove(device, state->render_pass_info.rstate);\n",
     "pvr_arch_cmd_buffer.c: error path unrefs")

# ---- 4. load-op program cache: the per-render "load/clear the attachments" fragment program was compiled (pco) and
# uploaded on every BeginRendering; it only depends on the load/clear masks, formats and the MRT layout ----
edit("src/imagination/vulkan/pvr_mrt.h",
     "struct pvr_load_op {\n   bool is_hw_object;\n",
     "struct pvr_load_op {\n   bool is_hw_object;\n"
     "   /* a7a-fedora: the programs below belong to the device's load-op cache; do not free them here. */\n"
     "   bool progs_from_cache;\n",
     "pvr_mrt.h: progs_from_cache flag")
edit("src/imagination/vulkan/pvr_device.h",
     "   simple_mtx_t rs_mtx;\n   struct list_head render_states;\n};\n",
     "   simple_mtx_t rs_mtx;\n   struct list_head render_states;\n\n"
     "   /* a7a-fedora: cache of compiled load-op programs (pvr_arch_load_op_shader_generate) */\n"
     "   simple_mtx_t loadop_cache_mtx;\n   struct list_head loadop_cache;\n   uint32_t loadop_cache_count;\n};\n\n"
     "void pvr_loadop_cache_finish(struct pvr_device *device);\n",
     "pvr_device.h: load-op cache fields")
edit("src/imagination/vulkan/pvr_arch_device.c",
     "   simple_mtx_init(&device->rs_mtx, mtx_plain);\n   list_inithead(&device->render_states);\n",
     "   simple_mtx_init(&device->rs_mtx, mtx_plain);\n   list_inithead(&device->render_states);\n"
     "   simple_mtx_init(&device->loadop_cache_mtx, mtx_plain);\n   list_inithead(&device->loadop_cache);\n"
     "   device->loadop_cache_count = 0;\n",
     "pvr_arch_device.c: cache init")
edit("src/imagination/vulkan/pvr_arch_device.c",
     "   simple_mtx_unlock(&device->rs_mtx);\n   simple_mtx_destroy(&device->rs_mtx);\n",
     "   simple_mtx_unlock(&device->rs_mtx);\n   simple_mtx_destroy(&device->rs_mtx);\n\n"
     "   pvr_loadop_cache_finish(device);\n",
     "pvr_arch_device.c: cache finish")
for f in ("src/imagination/vulkan/pvr_arch_mrt.c", "src/imagination/vulkan/pvr_arch_pass.c"):
    edit(f,
         "static void pvr_load_op_fini(struct pvr_load_op *load_op)\n{\n"
         "   pvr_bo_suballoc_free(load_op->pds_tex_state_prog.pvr_bo);\n",
         "static void pvr_load_op_fini(struct pvr_load_op *load_op)\n{\n"
         "   if (load_op->progs_from_cache)\n      return; /* a7a-fedora: owned by the device's load-op cache */\n\n"
         "   pvr_bo_suballoc_free(load_op->pds_tex_state_prog.pvr_bo);\n",
         f.split('/')[-1]+": fini respects the cache")
edit("src/imagination/vulkan/pvr_arch_mrt.c",
     "#include \"vk_log.h\"\n",
     "#include <string.h>\n#include <unistd.h>\n#include \"vk_log.h\"\n",
     "pvr_arch_mrt.c: string.h")
edit("src/imagination/vulkan/pvr_arch_mrt.c",
     "VkResult\npvr_arch_load_op_shader_generate(struct pvr_device *device,\n"
     "                                 const VkAllocationCallbacks *allocator,\n"
     "                                 struct pvr_load_op *load_op)\n{\n",
     "static VkResult\npvr_load_op_shader_generate_uncached(struct pvr_device *device,\n"
     "                                     const VkAllocationCallbacks *allocator,\n"
     "                                     struct pvr_load_op *load_op)\n{\n",
     "pvr_arch_mrt.c: rename generate -> uncached")
edit("src/imagination/vulkan/pvr_arch_mrt.c",
     "err_free_pds_frag_prog:\n   pvr_bo_suballoc_free(load_op->pds_frag_prog.pvr_bo);\n\n"
     "err_free_usc_frag_prog_bo:\n   pvr_bo_suballoc_free(load_op->usc_frag_prog_bo);\n\n   return result;\n}\n",
     "err_free_pds_frag_prog:\n   pvr_bo_suballoc_free(load_op->pds_frag_prog.pvr_bo);\n\n"
     "err_free_usc_frag_prog_bo:\n   pvr_bo_suballoc_free(load_op->usc_frag_prog_bo);\n\n   return result;\n}\n\n"
     "/* a7a-fedora: cache of compiled load-op programs. A compositor begins a rendering per draw batch, and the\n"
     " * load-op program only depends on the clear/load masks, the destination formats and the MRT layout, so the\n"
     " * same handful of programs was compiled and uploaded thousands of times per minute. */\n"
     "#define PVR_LOADOP_CACHE_MAX 256\n\n"
     "struct pvr_loadop_cache_key {\n"
     "   uint16_t rt_clear_mask;\n   uint16_t rt_load_mask;\n   uint16_t rt_2d_view_3d_mask;\n   uint16_t unresolved_msaa_mask;\n"
     "   int32_t depth_clear_to_reg;\n   VkFormat dest_vk_format[PVR_LOAD_OP_CLEARS_LOADS_MAX_RTS];\n"
     "   uint32_t num_render_targets;\n   uint32_t num_output_regs;\n   uint32_t num_tile_buffers;\n};\n\n"
     "struct pvr_loadop_cache_entry {\n"
     "   struct list_head link;\n   struct pvr_loadop_cache_key key;\n   struct usc_mrt_resource *mrt_resources;\n\n"
     "   struct pvr_suballoc_bo *usc_frag_prog_bo;\n   uint32_t const_shareds_count;\n   uint32_t shareds_count;\n"
     "   uint32_t num_tile_buffers;\n   struct pvr_pds_upload pds_frag_prog;\n   struct pvr_pds_upload pds_tex_state_prog;\n"
     "   uint32_t temps_count;\n};\n\n"
     "static void pvr_loadop_cache_key_init(struct pvr_loadop_cache_key *key,\n"
     "                                      const struct pvr_load_op *load_op)\n{\n"
     "   const struct usc_mrt_setup *setup = load_op->clears_loads_state.mrt_setup;\n\n"
     "   memset(key, 0, sizeof(*key));\n"
     "   key->rt_clear_mask = load_op->clears_loads_state.rt_clear_mask;\n"
     "   key->rt_load_mask = load_op->clears_loads_state.rt_load_mask;\n"
     "   key->rt_2d_view_3d_mask = load_op->clears_loads_state.rt_2d_view_3d_mask;\n"
     "   key->unresolved_msaa_mask = load_op->clears_loads_state.unresolved_msaa_mask;\n"
     "   key->depth_clear_to_reg = load_op->clears_loads_state.depth_clear_to_reg;\n"
     "   memcpy(key->dest_vk_format, load_op->clears_loads_state.dest_vk_format, sizeof(key->dest_vk_format));\n"
     "   if (setup) {\n      key->num_render_targets = setup->num_render_targets;\n"
     "      key->num_output_regs = setup->num_output_regs;\n      key->num_tile_buffers = setup->num_tile_buffers;\n   }\n}\n\n"
     "static bool pvr_loadop_cache_entry_matches(const struct pvr_loadop_cache_entry *entry,\n"
     "                                           const struct pvr_loadop_cache_key *key,\n"
     "                                           const struct usc_mrt_setup *setup)\n{\n"
     "   if (memcmp(&entry->key, key, sizeof(*key)))\n      return false;\n"
     "   for (uint32_t i = 0; i < key->num_render_targets; i++) {\n"
     "      const struct usc_mrt_resource *a = &entry->mrt_resources[i];\n"
     "      const struct usc_mrt_resource *b = &setup->mrt_resources[i];\n\n"
     "      /* only what the load-op program depends on: where each render target lives (the mrt_desc request\n"
     "       * description is not part of the generated code) */\n"
     "      if (a->type != b->type || a->intermediate_size != b->intermediate_size ||\n"
     "          a->reg.output_reg != b->reg.output_reg || a->reg.offset != b->reg.offset)\n"
     "         return false;\n   }\n   return true;\n}\n\n"
     "void pvr_loadop_cache_finish(struct pvr_device *device)\n{\n"
     "   list_for_each_entry_safe (struct pvr_loadop_cache_entry, entry, &device->loadop_cache, link) {\n"
     "      pvr_bo_suballoc_free(entry->pds_tex_state_prog.pvr_bo);\n"
     "      pvr_bo_suballoc_free(entry->pds_frag_prog.pvr_bo);\n"
     "      pvr_bo_suballoc_free(entry->usc_frag_prog_bo);\n"
     "      vk_free(&device->vk.alloc, entry->mrt_resources);\n      list_del(&entry->link);\n"
     "      vk_free(&device->vk.alloc, entry);\n   }\n"
     "   simple_mtx_destroy(&device->loadop_cache_mtx);\n}\n\n"
     "VkResult\npvr_arch_load_op_shader_generate(struct pvr_device *device,\n"
     "                                 const VkAllocationCallbacks *allocator,\n"
     "                                 struct pvr_load_op *load_op)\n{\n"
     "   const struct usc_mrt_setup *setup = load_op->clears_loads_state.mrt_setup;\n"
     "   struct pvr_loadop_cache_key key;\n   struct pvr_loadop_cache_entry *entry;\n   VkResult result;\n\n"
     "   pvr_loadop_cache_key_init(&key, load_op);\n\n"
     "   simple_mtx_lock(&device->loadop_cache_mtx);\n"
     "   list_for_each_entry (struct pvr_loadop_cache_entry, e, &device->loadop_cache, link) {\n"
     "      if (!pvr_loadop_cache_entry_matches(e, &key, setup))\n         continue;\n\n"
     "      load_op->usc_frag_prog_bo = e->usc_frag_prog_bo;\n"
     "      load_op->const_shareds_count = e->const_shareds_count;\n"
     "      load_op->shareds_count = e->shareds_count;\n"
     "      load_op->num_tile_buffers = e->num_tile_buffers;\n"
     "      load_op->pds_frag_prog = e->pds_frag_prog;\n"
     "      load_op->pds_tex_state_prog = e->pds_tex_state_prog;\n"
     "      load_op->temps_count = e->temps_count;\n"
     "      load_op->progs_from_cache = true;\n"
     "      /* most recently used first */\n      list_del(&e->link);\n      list_add(&e->link, &device->loadop_cache);\n"
     "      simple_mtx_unlock(&device->loadop_cache_mtx);\n      return VK_SUCCESS;\n   }\n"
     "   simple_mtx_unlock(&device->loadop_cache_mtx);\n\n"
     "   result = pvr_load_op_shader_generate_uncached(device, allocator, load_op);\n"
     "   if (result != VK_SUCCESS)\n      return result;\n\n"
     "   load_op->progs_from_cache = false;\n\n"
     "   simple_mtx_lock(&device->loadop_cache_mtx);\n"
     "   if (device->loadop_cache_count >= PVR_LOADOP_CACHE_MAX) {\n"
     "      simple_mtx_unlock(&device->loadop_cache_mtx);\n      return VK_SUCCESS; /* the load op keeps owning its programs */\n   }\n\n"
     "   entry = vk_zalloc(&device->vk.alloc, sizeof(*entry), 8, VK_SYSTEM_ALLOCATION_SCOPE_DEVICE);\n"
     "   if (entry && key.num_render_targets) {\n"
     "      const size_t sz = key.num_render_targets * sizeof(*setup->mrt_resources);\n"
     "      entry->mrt_resources = vk_alloc(&device->vk.alloc, sz, 8, VK_SYSTEM_ALLOCATION_SCOPE_DEVICE);\n"
     "      if (entry->mrt_resources)\n         memcpy(entry->mrt_resources, setup->mrt_resources, sz);\n"
     "      else {\n         vk_free(&device->vk.alloc, entry);\n         entry = NULL;\n      }\n   }\n"
     "   if (entry) {\n"
     "      entry->key = key;\n"
     "      entry->usc_frag_prog_bo = load_op->usc_frag_prog_bo;\n"
     "      entry->const_shareds_count = load_op->const_shareds_count;\n"
     "      entry->shareds_count = load_op->shareds_count;\n"
     "      entry->num_tile_buffers = load_op->num_tile_buffers;\n"
     "      entry->pds_frag_prog = load_op->pds_frag_prog;\n"
     "      entry->pds_tex_state_prog = load_op->pds_tex_state_prog;\n"
     "      entry->temps_count = load_op->temps_count;\n"
     "      list_add(&entry->link, &device->loadop_cache);\n      device->loadop_cache_count++;\n"
     "      load_op->progs_from_cache = true; /* the cache owns them from now on */\n   }\n"
     "   simple_mtx_unlock(&device->loadop_cache_mtx);\n\n   return VK_SUCCESS;\n}\n",
     "pvr_arch_mrt.c: load-op program cache")
# ---- debug prints for both caches: PVR_A7A_CACHE_DEBUG=1 ----
edit("src/imagination/vulkan/pvr_arch_mrt.c",
     "   pvr_loadop_cache_key_init(&key, load_op);\n\n   simple_mtx_lock(&device->loadop_cache_mtx);\n",
     "   pvr_loadop_cache_key_init(&key, load_op);\n\n"
     "   static int a7a_dbg = -1;\n   if (a7a_dbg < 0)\n      a7a_dbg = getenv(\"PVR_A7A_CACHE_DEBUG\") != NULL || access(\"/tmp/pvr-a7a-cache-debug\", F_OK) == 0;\n"
     "   if (a7a_dbg) {\n      bool hit = false;\n"
     "      simple_mtx_lock(&device->loadop_cache_mtx);\n"
     "      list_for_each_entry (struct pvr_loadop_cache_entry, e, &device->loadop_cache, link)\n"
     "         if (pvr_loadop_cache_entry_matches(e, &key, setup)) { hit = true; break; }\n"
     "      simple_mtx_unlock(&device->loadop_cache_mtx);\n"
     "      fprintf(stderr, \"a7a loadop cache %s: clear %x load %x 2d3d %x msaa %x dclr %d fmt0 %u rts %u regs %u tb %u res0 type %u isz %u u0 %u u1 %u (entries %u)\\n\",\n"
     "              hit ? \"HIT \" : \"MISS\", key.rt_clear_mask, key.rt_load_mask, key.rt_2d_view_3d_mask,\n"
     "              key.unresolved_msaa_mask, key.depth_clear_to_reg, key.dest_vk_format[0], key.num_render_targets,\n"
     "              key.num_output_regs, key.num_tile_buffers,\n"
     "              setup && setup->num_render_targets ? setup->mrt_resources[0].type : 0,\n"
     "              setup && setup->num_render_targets ? setup->mrt_resources[0].intermediate_size : 0,\n"
     "              setup && setup->num_render_targets ? setup->mrt_resources[0].reg.output_reg : 0,\n"
     "              setup && setup->num_render_targets ? setup->mrt_resources[0].reg.offset : 0,\n"
     "              device->loadop_cache_count);\n   }\n\n"
     "   simple_mtx_lock(&device->loadop_cache_mtx);\n",
     "pvr_arch_mrt.c: cache debug print")
edit("src/imagination/vulkan/pvr_arch_cmd_buffer.c",
     "      struct pvr_render_state *cached =\n"
     "         pvr_rstate_cache_lookup(device, state->render_pass_info.rstate, &dr_info->hw_render);\n\n",
     "      struct pvr_render_state *cached =\n"
     "         pvr_rstate_cache_lookup(device, state->render_pass_info.rstate, &dr_info->hw_render);\n\n"
     "      static int a7a_dbg = -1;\n      if (a7a_dbg < 0)\n         a7a_dbg = getenv(\"PVR_A7A_CACHE_DEBUG\") != NULL || access(\"/tmp/pvr-a7a-cache-debug\", F_OK) == 0;\n"
     "      if (a7a_dbg)\n"
     "         fprintf(stderr, \"a7a rstate cache %s: %ux%u layers %u align %ux%u scratch %llu samples %u regs %u tb %u views %x\\n\",\n"
     "                 cached ? \"HIT \" : \"MISS\", state->render_pass_info.rstate->width, state->render_pass_info.rstate->height,\n"
     "                 state->render_pass_info.rstate->layers, state->render_pass_info.rstate->width_alignment,\n"
     "                 state->render_pass_info.rstate->height_alignment,\n"
     "                 (unsigned long long)state->render_pass_info.rstate->scratch_buffer_size,\n"
     "                 dr_info->hw_render.sample_count, dr_info->hw_render.output_regs_count,\n"
     "                 dr_info->hw_render.tile_buffers_count, dr_info->hw_render.view_mask);\n\n",
     "pvr_arch_cmd_buffer.c: rstate cache debug print")

# ---- 5. sub-allocator chunk free list: it kept ONE freed chunk per heap, a frame uses several, so the rest were
# destroyed and re-created (kernel-zeroed) every frame (the remaining kernel time in KWin's profile) ----
edit("src/imagination/vulkan/pvr_bo.h",
     "   /* Previous buffer that can be used when a new buffer object is needed */\n   struct pvr_bo *bo_cached;\n",
     "   /* a7a-fedora: freed chunks kept for reuse (was a single bo_cached) */\n"
     "#define PVR_SUBALLOC_CACHE_MAX 16\n   struct pvr_bo *bo_cached[PVR_SUBALLOC_CACHE_MAX];\n   uint32_t bo_cached_count;\n",
     "pvr_bo.h: chunk free list")
edit("src/imagination/vulkan/pvr_bo.c",
     "   pvr_bo_free(allocator->device, allocator->bo);\n   pvr_bo_free(allocator->device, allocator->bo_cached);\n\n   simple_mtx_destroy(&allocator->mtx);\n",
     "   pvr_bo_free(allocator->device, allocator->bo);\n"
     "   for (uint32_t i = 0; i < allocator->bo_cached_count; i++)\n      pvr_bo_free(allocator->device, allocator->bo_cached[i]);\n"
     "   allocator->bo_cached_count = 0;\n\n   simple_mtx_destroy(&allocator->mtx);\n",
     "pvr_bo.c: fini frees the list")
edit("src/imagination/vulkan/pvr_bo.c",
     "   if (allocator->bo_cached) {\n      struct pvr_winsys_bo *bo_cached = allocator->bo_cached->bo;\n\n"
     "      if (alloc_size <= bo_cached->size)\n         allocator->bo = allocator->bo_cached;\n      else\n"
     "         pvr_bo_free(allocator->device, allocator->bo_cached);\n\n      allocator->bo_cached = NULL;\n   }\n",
     "   /* a7a-fedora: take a cached chunk that is large enough (most recently freed first) */\n"
     "   for (uint32_t i = allocator->bo_cached_count; i > 0; i--) {\n"
     "      struct pvr_bo *cached = allocator->bo_cached[i - 1];\n\n"
     "      if (alloc_size <= cached->bo->size) {\n"
     "         allocator->bo = cached;\n"
     "         allocator->bo_cached[i - 1] = allocator->bo_cached[allocator->bo_cached_count - 1];\n"
     "         allocator->bo_cached_count--;\n         break;\n      }\n   }\n",
     "pvr_bo.c: alloc takes from the list")
edit("src/imagination/vulkan/pvr_bo.c",
     "   if (p_atomic_read(&suballoc_bo->bo->ref_count) == 1 &&\n       !suballoc_bo->allocator->bo_cached) {\n"
     "      suballoc_bo->allocator->bo_cached = suballoc_bo->bo;\n   } else {\n",
     "   if (p_atomic_read(&suballoc_bo->bo->ref_count) == 1 &&\n"
     "       suballoc_bo->allocator->bo_cached_count < PVR_SUBALLOC_CACHE_MAX) {\n"
     "      suballoc_bo->allocator->bo_cached[suballoc_bo->allocator->bo_cached_count++] = suballoc_bo->bo;\n   } else {\n",
     "pvr_bo.c: free pushes to the list")
# ---- 6. uniform buffers per stage 12 -> 13 (upstream Mesa cece4db940, 2026-07-31, "pvr: increase
# maxPerStageDescriptorUniformBuffers value"). Mesa's GL state tracker keeps one uniform buffer for the default
# uniform block (st_extensions.c:266) and exposes ARB_uniform_buffer_object only with >= 12 left in every stage
# (st_extensions.c:325), which is one of the two terms the OpenGL ES 3.0 / GL 3.1 gates fail on here
# (docs/GPU-GAP-2026-09-10.md). The value is only reported: the descriptor code has no per-stage UBO array.
# maxPerStageResources follows (the sum of the per-stage descriptor limits + 4 colour attachments, 48 -> 49 in
# 26.1.8; upstream main had 56 -> 57 because its storage-buffer limit is 16). ----
edit("src/imagination/vulkan/pvr_physical_device.c",
     "      .maxPerStageDescriptorUniformBuffers = 12,\n",
     "      /* a7a-fedora: upstream cece4db940: 12 for the GL state tracker + 1 for the default uniform block */\n"
     "      .maxPerStageDescriptorUniformBuffers = 13,\n",
     "pvr_physical_device.c: 13 uniform buffers per stage (upstream cece4db940)")
edit("src/imagination/vulkan/pvr_physical_device.c",
     "      .maxPerStageResources = 48,\n",
     "      .maxPerStageResources = 49, /* a7a-fedora: + the 13th uniform buffer (upstream cece4db940) */\n",
     "pvr_physical_device.c: maxPerStageResources 49")
# ---- Held back (2026-09-10): maxImageArrayLayers and maxImageDimension3D 256 -> 2048 (upstream Mesa af80126eed and
# e4b29dedd8). The vendor DDK proves 2048 for both and 26.1.8 already reports 2048 per format (pvr_formats.c:680-713),
# but the third reviewer pass found what raising the DEVICE limits would open, so they are not a capability yet:
#  - pco computes array-layer offsets in 32 bits (pco_nir_tex.c:667, 1189, 1364; the view offset pvr_arch_image.c:115
#    -> uint32_t in pvr_tex_state.h:96): an array over 4 GiB samples (and a load op loads) the wrong layer, which
#    2048 layers make reachable at ordinary layer sizes (1100 x 1024x1024 RGBA8);
#  - Mesa's GL sets GL_MAX_FRAMEBUFFER_LAYERS from the array-layer limit (st_extensions.c:580) and zink clears an
#    image's whole layer/depth count in one rendering (zink_clear.c:489), against maxFramebufferLayers 256.
# Neither unlocks a GL/GLES version before transform feedback (both are GL 4.1 terms). They come back with those
# fixes; tools/gpu-debug/image-limit-check.py is their device check. docs/GPU-GAP-2026-09-10.md.
import re
src=open("src/imagination/vulkan/pvr_arch_cmd_buffer.c").read()
first=re.search(r'^#include .*$', src, re.M).group(0)
edit("src/imagination/vulkan/pvr_arch_cmd_buffer.c", first+"\n", "#include <stdlib.h>\n#include <unistd.h>\n"+first+"\n", "pvr_arch_cmd_buffer.c: stdlib/unistd")
PY
VKDRV=$(grep -q "'imagination-experimental'" meson.options && echo imagination-experimental || echo imagination)
step "meson (vulkan driver '$VKDRV' only)"
[ -d build ] || meson setup build --prefix=/usr/local --libdir=lib64 --buildtype=release \
  -Dvulkan-drivers=$VKDRV -Dgallium-drivers= -Dplatforms=wayland,x11 -Dglx=disabled -Degl=disabled -Dgbm=disabled \
  -Dllvm=enabled -Dopengl=false -Dgles1=disabled -Dgles2=disabled -Dshared-glapi=disabled -Dvulkan-layers= \
  -Dtools= -Dbuild-tests=false -Dvideo-codecs= -Dlmsensors=disabled -Dvalgrind=disabled -Dlibunwind=disabled \
  -Dimagination-srv=false > meson-setup.log 2>&1 || { tail -30 meson-setup.log; exit 1; }
step "ninja"
ninja -C build > ninja.log 2>&1 || { grep -E "error|FAILED" ninja.log | head -20; exit 1; }
ls -l build/src/imagination/vulkan/libvulkan_powervr_mesa.so
# the a7a markers tools/board/a7a-gpu-check looks for (strings in the .so). grep the file itself: under
# `set -o pipefail`, `strings | grep -q` fails with 141 whenever grep stops reading early (SIGPIPE to strings), i.e.
# exactly when the marker IS there (2026-09-10, the first run of this staging path)
grep -qa "a7a loadop cache" build/src/imagination/vulkan/libvulkan_powervr_mesa.so || { echo "the built driver lacks the a7a markers (patch set incomplete?)"; exit 1; }
MESAVER=${D#mesa-}; STAMP=$(date +%Y%m%d%H%M); STAGE=$W/stage-$STAMP; TARBALL=$W/mesa-pvr-a7a-$MESAVER-$STAMP.tar.gz
step "stage for the RPM: $TARBALL"
rm -rf "$STAGE"; DESTDIR=$STAGE ninja -C build install > install-stage.log 2>&1
( cd "$STAGE" && find usr/local -type f -o -type l | sort | sed 's/^/  /' )
J=$(ls "$STAGE"/usr/local/share/vulkan/icd.d/powervr_*icd.*.json); grep -q '"library_path": *"/usr/local/lib64/libvulkan_powervr_mesa.so"' "$J" || { echo "ICD JSON does not point at /usr/local/lib64: $(cat "$J")"; exit 1; }
tar -C "$STAGE" -czf "$TARBALL" usr; rm -rf "$STAGE"
echo "  $(du -h "$TARBALL" | cut -f1) $TARBALL"
if [ "$INSTALL" = 1 ]; then
  step "--install: straight into /usr/local + mask Fedora's ICD (dev loop; the next mesa-pvr-a7a RPM replaces these files)"
  sudo ninja -C build install > install.log 2>&1
  F=/usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json
  [ -f "$F" ] && sudo mv "$F" "$F.disabled-by-a7a" && echo "  Fedora's ICD masked ($F.disabled-by-a7a; move back to revert)"
  # Fedora's driver and ours both report "Mesa 26.1.8": only the loader's own log says which file it loaded
  step "check: the Vulkan loader must pick /usr/local/lib64/libvulkan_powervr_mesa.so"
  VK_LOADER_DEBUG=driver vulkaninfo --summary > "$W/install-check.log" 2>&1 || true
  if grep -qa "/usr/local/lib64/libvulkan_powervr_mesa.so" "$W/install-check.log"; then
    echo "done: new apps use the a7a driver now; KWin picks it up at the next login"
  else
    echo "the loader did not pick /usr/local/lib64/libvulkan_powervr_mesa.so: see $W/install-check.log"; exit 1
  fi
else
  echo "done: nothing installed. On the laptop: tools/build-mesa-rpm.sh (fetches the newest tarball), then dnf install output/rpms/mesa-pvr-a7a-$MESAVER-$STAMP.aarch64.rpm here; KWin picks it up at the next login"
fi
