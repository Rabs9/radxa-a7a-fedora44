/* SPDX-License-Identifier: GPL-2.0-only OR MIT */
/*
 * Compatibility shims to build the upstream drm/imagination driver (Linux
 * master, pinned in tools/pvr-graft.sh; v6.16 with PVR_UPSTREAM=v6.16) against
 * the 6.6.98 Allwinner BSP kernel.
 *
 * Injected into every file in this directory via ccflags-y -include, so the
 * upstream sources stay byte-identical and the entire delta is visible here
 * (plus the few edits a header cannot make, listed in tools/pvr-graft.sh, and
 * the 6.6 core patches in tools/patches/pvr-core/).
 *
 * Each shim maps a newer kernel API onto its 6.6 equivalent. None of them
 * change behaviour - where an argument is dropped it is because 6.6 has that
 * value fixed at exactly what the driver passes.
 */

#ifndef PVR_COMPAT66_H
#define PVR_COMPAT66_H

#include <linux/compiler.h>
#include <linux/device.h>
#include <linux/dma-fence.h>
#include <linux/err.h>
#include <linux/llist.h>
#include <linux/of.h>
#include <linux/overflow.h>
#include <linux/pm_domain.h>
#include <linux/sizes.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/xarray.h>

#include <drm/drm_exec.h>
#include <drm/drm_gem_shmem_helper.h>
#include <drm/gpu_scheduler.h>

/*
 * strscpy() became a macro with an optional size in 6.9 (sizeof(dst) when
 * omitted); 6.6 has the three-argument function only.
 */
#define __pvr_strscpy_2(dst, src)		strscpy((dst), (src), sizeof(dst))
#define __pvr_strscpy_3(dst, src, size)		strscpy((dst), (src), (size))
#define __pvr_strscpy_n(_1, _2, _3, NAME, ...)	NAME
#define strscpy(...) \
	__pvr_strscpy_n(__VA_ARGS__, __pvr_strscpy_3, __pvr_strscpy_2)(__VA_ARGS__)

/* init_llist_node() / llist_on_list() arrived in 6.9 (used by drm_gpuvm.c). */
static inline void init_llist_node(struct llist_node *node)
{
	node->next = node;
}

static inline bool llist_on_list(const struct llist_node *node)
{
	return node->next != node;
}

/*
 * drm_exec_for_each_locked_object() lost its index argument in 6.10 (the
 * index became an internal __UNIQUE_ID variable). Same loop as 6.6's.
 */
#undef drm_exec_for_each_locked_object
#undef drm_exec_for_each_locked_object_reverse
#define __pvr_drm_exec_for_each_locked_object(exec, obj, __index)		\
	for (unsigned long __index = 0;						\
	     ((obj) = drm_exec_obj(exec, __index)); ++__index)
#define drm_exec_for_each_locked_object(exec, obj)				\
	__pvr_drm_exec_for_each_locked_object(exec, obj, __UNIQUE_ID(drm_exec))
#define __pvr_drm_exec_for_each_locked_object_reverse(exec, obj, __index)	\
	for (unsigned long __index = (exec)->num_objects - 1;			\
	     ((obj) = drm_exec_obj(exec, __index)); --__index)
#define drm_exec_for_each_locked_object_reverse(exec, obj)			\
	__pvr_drm_exec_for_each_locked_object_reverse(exec, obj, __UNIQUE_ID(drm_exec))

/* linux/sizes.h stops at SZ_32G in 6.6. */
#ifndef SZ_64G
#define SZ_64G				_AC(0x1000000000, ULL)
#endif
#ifndef SZ_128G
#define SZ_128G				_AC(0x2000000000, ULL)
#endif

/* mem_is_zero() arrived in 6.11; memchr_inv() is its implementation. */
static inline bool pvr_compat_mem_is_zero(const void *s, size_t n)
{
	return !memchr_inv(s, 0, n);
}
#define mem_is_zero(s, n) pvr_compat_mem_is_zero((s), (n))

/*
 * The shmem helpers grew _locked suffixes in 6.10 when the locking convention
 * was made explicit. The 6.6 functions are the same code under the old names.
 */
#define drm_gem_shmem_vmap_locked(obj, map)	drm_gem_shmem_vmap((obj), (map))
#define drm_gem_shmem_vunmap_locked(obj, map)	drm_gem_shmem_vunmap((obj), (map))
#define drm_gem_shmem_pin_locked(obj)		drm_gem_shmem_pin((obj))
#define drm_gem_shmem_unpin_locked(obj)		drm_gem_shmem_unpin((obj))

/*
 * drm_sched_job_has_dependency() was added in 6.10. 6.6 keeps the same
 * xarray of dependencies, so the lookup is identical.
 */
static inline bool
pvr_compat_sched_job_has_dependency(struct drm_sched_job *job,
				    struct dma_fence *fence)
{
	struct dma_fence *f;
	unsigned long index;

	xa_for_each(&job->dependencies, index, f) {
		if (f == fence)
			return true;
	}

	return false;
}
#define drm_sched_job_has_dependency(job, fence) \
	pvr_compat_sched_job_has_dependency((job), (fence))

/*
 * drm_exec_init() gained a preallocation hint in 6.8. 6.6 allocates a
 * PAGE_SIZE array up front regardless, so the hint has nowhere to go.
 */
#define drm_exec_init(exec, flags, nr)		drm_exec_init((exec), (flags))

/*
 * drm_sched_job_init() gained a `credits` argument in 6.10 (the driver passes
 * 1, which is exactly 6.6's fixed behaviour) and a `drm_client_id` in 6.17
 * (only recorded for the scheduler's tracepoints, which 6.6 does not have).
 */
#define drm_sched_job_init(job, entity, credits, owner, ...) \
	drm_sched_job_init((job), (entity), (owner))

/*
 * 6.14 replaced drm_sched_init()'s argument list with a struct. Declare the
 * struct and unpack it into the 6.6 call.
 *
 * submit_wq and num_rqs are dropped: 6.6 runs a kthread per scheduler rather
 * than a shared workqueue, and always allocates one run queue per priority.
 * The driver's entity uses DRM_SCHED_PRIORITY_KERNEL, which 6.6 has.
 */
struct drm_sched_init_args {
	const struct drm_sched_backend_ops *ops;
	struct workqueue_struct *submit_wq;
	struct workqueue_struct *timeout_wq;
	u32 num_rqs;
	u32 credit_limit;
	unsigned int hang_limit;
	long timeout;
	atomic_t *score;
	const char *name;
	struct device *dev;
};

#define drm_sched_init(sched, args)					\
	drm_sched_init((sched), (args)->ops, (args)->credit_limit,	\
		       (args)->hang_limit, (args)->timeout,		\
		       (args)->timeout_wq, (args)->score,		\
		       (args)->name, (args)->dev)

/*
 * drm_sched_start() takes an errno since 6.12 (the driver passes 0); 6.6's
 * second argument is `bool full_recovery`, and 0 there means NO recovery: no
 * completion callback is re-armed on the pending jobs after a timeout, so the
 * queue never completes a job again (the 2026-09-09 wedge,
 * docs/OPEN-STACK-WEDGE-2026-09-09.md). 6.6 needs true, which is what every
 * 6.12+ call with errno 0 means.
 */
#define drm_sched_start(sched, errno)		drm_sched_start((sched), true)

/*
 * DRM_GPU_SCHED_STAT_RESET and DRM_GPU_SCHED_STAT_NO_HANG (6.17) come from
 * tools/patches/pvr-core/0002-drm-sched-no-hang-status.patch: NO_HANG needs
 * scheduler-side behaviour (re-insert the job), which a header cannot add.
 */

/*
 * kmalloc_obj() and family (7.0, "slab: Introduce kmalloc_obj() and family";
 * the driver was converted treewide in 69050f8d6d07 and bf4afc53b77a). Copied
 * from master's include/linux/slab.h and include/linux/gfp.h, minus the
 * _flex variants the driver does not use (they would need __set_flex_counter).
 */
#ifndef default_gfp
#define __pvr_default_gfp(a, b, ...) b
#define default_gfp(...) __pvr_default_gfp(, ##__VA_ARGS__, GFP_KERNEL)
#endif
#ifndef kmalloc_obj
#define __alloc_objs(KMALLOC, GFP, TYPE, COUNT)				\
({									\
	const size_t __obj_size = size_mul(sizeof(TYPE), COUNT);	\
	(TYPE *)KMALLOC(__obj_size, GFP);				\
})
#define kmalloc_obj(VAR_OR_TYPE, ...) \
	__alloc_objs(kmalloc, default_gfp(__VA_ARGS__), typeof(VAR_OR_TYPE), 1)
#define kmalloc_objs(VAR_OR_TYPE, COUNT, ...) \
	__alloc_objs(kmalloc, default_gfp(__VA_ARGS__), typeof(VAR_OR_TYPE), COUNT)
#define kzalloc_obj(P, ...) \
	__alloc_objs(kzalloc, default_gfp(__VA_ARGS__), typeof(P), 1)
#define kzalloc_objs(P, COUNT, ...) \
	__alloc_objs(kzalloc, default_gfp(__VA_ARGS__), typeof(P), COUNT)
#define kvmalloc_obj(P, ...) \
	__alloc_objs(kvmalloc, default_gfp(__VA_ARGS__), typeof(P), 1)
#define kvmalloc_objs(P, COUNT, ...) \
	__alloc_objs(kvmalloc, default_gfp(__VA_ARGS__), typeof(P), COUNT)
#define kvzalloc_obj(P, ...) \
	__alloc_objs(kvzalloc, default_gfp(__VA_ARGS__), typeof(P), 1)
#define kvzalloc_objs(P, COUNT, ...) \
	__alloc_objs(kvzalloc, default_gfp(__VA_ARGS__), typeof(P), COUNT)
#endif

/*
 * dev_pm_domain_attach_list() / dev_pm_domain_detach_list() (6.10,
 * drivers/base/power/common.c) used by pvr_power_domains_init() for GPUs in
 * two or more power domains. Copied from 6.10 as static inlines: the same
 * loop over dev_pm_domain_attach_by_name()/_by_id() (both in 6.6) plus a
 * device link per domain. Never executed on the A733 (its gpu node has no
 * power-domains property), but the code must build.
 */
#ifndef PD_FLAG_NO_DEV_LINK
#define PD_FLAG_NO_DEV_LINK		BIT(0)
#define PD_FLAG_DEV_LINK_ON		BIT(1)

struct dev_pm_domain_attach_data {
	const char * const *pd_names;
	const u32 num_pd_names;
	const u32 pd_flags;
};

struct dev_pm_domain_list {
	struct device **pd_devs;
	struct device_link **pd_links;
	u32 num_pds;
};

static inline int
pvr_compat_dev_pm_domain_attach_list(struct device *dev,
				     const struct dev_pm_domain_attach_data *data,
				     struct dev_pm_domain_list **list)
{
	struct device_node *np = dev->of_node;
	struct dev_pm_domain_list *pds;
	struct device *pd_dev = NULL;
	int ret, i, num_pds = 0;
	bool by_id = true;
	u32 pd_flags = data ? data->pd_flags : 0;
	u32 link_flags = pd_flags & PD_FLAG_NO_DEV_LINK ? 0 :
			DL_FLAG_STATELESS | DL_FLAG_PM_RUNTIME;

	if (dev->pm_domain)
		return -EEXIST;

	/* For now this is limited to OF based platforms. */
	if (!np)
		return 0;

	if (data && data->pd_names) {
		num_pds = data->num_pd_names;
		by_id = false;
	} else {
		num_pds = of_count_phandle_with_args(np, "power-domains",
						     "#power-domain-cells");
	}

	if (num_pds <= 0)
		return 0;

	pds = devm_kzalloc(dev, sizeof(*pds), GFP_KERNEL);
	if (!pds)
		return -ENOMEM;

	pds->pd_devs = devm_kcalloc(dev, num_pds, sizeof(*pds->pd_devs),
				    GFP_KERNEL);
	if (!pds->pd_devs)
		return -ENOMEM;

	pds->pd_links = devm_kcalloc(dev, num_pds, sizeof(*pds->pd_links),
				     GFP_KERNEL);
	if (!pds->pd_links)
		return -ENOMEM;

	if (link_flags && pd_flags & PD_FLAG_DEV_LINK_ON)
		link_flags |= DL_FLAG_RPM_ACTIVE;

	for (i = 0; i < num_pds; i++) {
		if (by_id)
			pd_dev = dev_pm_domain_attach_by_id(dev, i);
		else
			pd_dev = dev_pm_domain_attach_by_name(dev,
							data->pd_names[i]);
		if (IS_ERR_OR_NULL(pd_dev)) {
			ret = pd_dev ? PTR_ERR(pd_dev) : -ENODEV;
			goto err_attach;
		}

		if (link_flags) {
			struct device_link *link;

			link = device_link_add(dev, pd_dev, link_flags);
			if (!link) {
				ret = -ENODEV;
				goto err_link;
			}

			pds->pd_links[i] = link;
		}

		pds->pd_devs[i] = pd_dev;
	}

	pds->num_pds = num_pds;
	*list = pds;
	return num_pds;

err_link:
	dev_pm_domain_detach(pd_dev, true);
err_attach:
	while (--i >= 0) {
		if (pds->pd_links[i])
			device_link_del(pds->pd_links[i]);
		dev_pm_domain_detach(pds->pd_devs[i], true);
	}
	return ret;
}

static inline void
pvr_compat_dev_pm_domain_detach_list(struct dev_pm_domain_list *list)
{
	int i;

	if (!list)
		return;

	for (i = 0; i < list->num_pds; i++) {
		if (list->pd_links[i])
			device_link_del(list->pd_links[i]);
		dev_pm_domain_detach(list->pd_devs[i], true);
	}
}

#define dev_pm_domain_attach_list(dev, data, list) \
	pvr_compat_dev_pm_domain_attach_list((dev), (data), (list))
#define dev_pm_domain_detach_list(list) \
	pvr_compat_dev_pm_domain_detach_list((list))
#endif /* PD_FLAG_NO_DEV_LINK */

/*
 * <linux/pwrseq/consumer.h> (6.11) does not exist in 6.6: the graft adds
 * compat-include/ to the include path with master's copy of that header, whose
 * !CONFIG_POWER_SEQUENCING branch is all stubs (only the TH1520 uses pwrseq).
 */

#endif /* PVR_COMPAT66_H */
