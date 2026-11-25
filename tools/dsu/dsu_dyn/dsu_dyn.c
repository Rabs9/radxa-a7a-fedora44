// SPDX-License-Identifier: GPL-2.0
/* dsu_dyn: dynamic DSU/L3 frequency governor for the Radxa Cubie A7A (A733).
 *
 * Standalone port of the vendor sunxi-dsufreq policy (bsp/drivers/dsufreq/) to a
 * cpufreq TRANSITION NOTIFIER, because the vendor driver's hook (set_dsufreq_cb)
 * only exists when the kernel is built with AW_SUNXI_DSUFREQ — which the golden
 * image's kernel is not, and its cpufreq-dt is built in. The policy is identical:
 *
 *   dsu_target = min( 3/4 * max(little_freq, big_freq)  rounded to DCXO multiple,
 *                     fastest DSU OPP whose voltage <= the A55s' current OPP voltage )
 *   and if the big cores are fast (>= 1488 MHz) hold at least the fastest DSU OPP
 *   available at the table's minimum voltage.
 *
 * The DSU shares its rail (dcdc5) with the A55 cluster, so the voltage cap is the
 * whole safety story: this module NEVER touches the regulator — the DSU only runs
 * as fast as the voltage the A55s are already paying for. On this board's bin
 * (26m-vf0400): idle A55s (800 mV) still allow 910 MHz (> the 780 boot default),
 * and loaded A55s (>= 1000 mV) unlock the bin max 1274 MHz. The over-bin 1352 OC
 * stays the static dsu_oc module's job. Downward CPU transitions lower the DSU in
 * PRECHANGE (before cpufreq drops the voltage); upward ones raise it in POSTCHANGE
 * (after the voltage is up) — same ordering the vendor callbacks had. Measured
 * (2026-08-20): idle 312-910 by demand, all-core load 1274, big-only 1488-rule
 * floor 910, mem bandwidth 36.5 GB/s (vs 28.4 stock / 37.3-39.0 static-1352-OC),
 * 20-cycle load flap storm with zero errors.
 *
 * The DSU OPP table (with the per-chip speed-bin "vf" voltage variant, resolved via
 * the exported sunxi_get_vf_index / determine_dcxo_clk_source helpers) is attached
 * to the dsufreq platform device at load and detached at unload. insmod to enable,
 * rmmod to restore the boot rate. Runtime only, reboot-safe.
 *
 * Params: boost_hz (cap, default 1352 MHz), big_ext_hz (big-core threshold, 1488 MHz)
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/clk.h>
#include <linux/cpufreq.h>
#include <linux/pm_opp.h>
#include <linux/devfreq.h>
#include <linux/cpu.h>
#include <linux/mutex.h>
#include <linux/err.h>

#define MAX_OPPS 24
#define LITTLE_CPU 0
#define BIG_CPU 6

static unsigned long boost_hz = 1352000000UL;
module_param(boost_hz, ulong, 0444);
MODULE_PARM_DESC(boost_hz, "upper cap for the DSU clock in Hz");

static unsigned long big_ext_hz = 1488000000UL;
module_param(big_ext_hz, ulong, 0444);
MODULE_PARM_DESC(big_ext_hz, "big-core freq above which DSU holds max-freq-at-min-volt");

/* optional over-bin OC rung appended to the table (validated separately with the
 * static dsu_oc module — e.g. 1352 MHz @ 1000 mV held a 12-min soak on this board
 * even though its bin tops out at 1274). Off by default: bin-correct behavior. */
static unsigned long oc_hz;
module_param(oc_hz, ulong, 0444);
MODULE_PARM_DESC(oc_hz, "extra over-bin DSU rung in Hz (0 = off)");

static unsigned long oc_uv = 1000000UL;
module_param(oc_uv, ulong, 0444);
MODULE_PARM_DESC(oc_uv, "voltage the oc_hz rung requires (default 1000000)");

/* exported by the vendor SID / CCU drivers in this kernel */
extern u32 sunxi_get_vf_index(void);
extern u32 determine_dcxo_clk_source(void);

static struct clk *dsu_clk;
static struct device *dsufreq_dev;   /* owns the DSU OPP table */
static struct device *little_dev;    /* cpu0: live (bin-resolved) A55 OPPs */
static int prop_name_token;          /* vendor >=6.1 API returns int */
static bool table_added;

static unsigned long opp_hz[MAX_OPPS], opp_uv[MAX_OPPS];
static int opp_n;
static unsigned long max_f_by_min_v;
static unsigned long dcxo_hz = 24000000UL;

static DEFINE_MUTEX(gov_lock);
static unsigned long little_hz, big_hz;   /* current cluster rates (Hz) */
static unsigned long little_safe_hz;      /* voltage low-water mark: min(old,new)
					   * during a little transition, so a
					   * concurrent big-cluster event can't cap
					   * the DSU against a voltage the rail is
					   * in the middle of dropping from */
static unsigned long cur_dsu;             /* last granted DSU rate */
static unsigned long orig_rate;

static unsigned long little_volt(unsigned long lfreq_hz)
{
	struct dev_pm_opp *opp;
	unsigned long f = lfreq_hz, v;

	opp = devfreq_recommended_opp(little_dev, &f, DEVFREQ_FLAG_LEAST_UPPER_BOUND);
	if (IS_ERR(opp))
		return 0;
	v = dev_pm_opp_get_voltage(opp);
	dev_pm_opp_put(opp);
	return v;
}

static unsigned long compute_target(unsigned long lit, unsigned long big)
{
	unsigned long demand, cap = 0, volt, tgt;
	int i;

	demand = (max(lit, big) * 3) / 4;
	demand -= demand % dcxo_hz;

	/* cap by the voltage of the SAFE little rate, never the nominal one */
	volt = little_volt(min(lit, little_safe_hz));
	for (i = 0; i < opp_n; i++)
		if (volt && opp_uv[i] <= volt)
			cap = opp_hz[i];
	if (!cap)
		cap = opp_hz[0];

	tgt = min(demand, cap);
	if (big >= big_ext_hz && tgt < max_f_by_min_v)
		tgt = max_f_by_min_v;
	tgt = clamp(tgt, opp_hz[0], boost_hz);
	return tgt;
}

/* caller holds gov_lock */
static void apply(unsigned long tgt)
{
	if (tgt == cur_dsu)
		return;
	if (!clk_set_rate(dsu_clk, tgt))
		cur_dsu = clk_get_rate(dsu_clk);
	pr_debug("dsu_dyn: little=%lu big=%lu -> dsu=%lu\n", little_hz, big_hz, cur_dsu);
}

static int dsu_dyn_notify(struct notifier_block *nb, unsigned long action, void *data)
{
	struct cpufreq_freqs *freqs = data;
	int cpu = freqs->policy->cpu;
	unsigned long newf = (unsigned long)freqs->new * 1000UL;
	unsigned long tgt;

	if (cpu != LITTLE_CPU && cpu != BIG_CPU)
		return NOTIFY_OK;

	mutex_lock(&gov_lock);
	if (action == CPUFREQ_PRECHANGE) {
		if (cpu == LITTLE_CPU)
			little_safe_hz = min(little_hz, newf);
		/* drop the DSU before a voltage-lowering little transition */
		tgt = compute_target(cpu == LITTLE_CPU ? newf : little_hz,
				     cpu == BIG_CPU ? newf : big_hz);
		if (tgt < cur_dsu)
			apply(tgt);
	} else if (action == CPUFREQ_POSTCHANGE) {
		if (cpu == LITTLE_CPU) {
			little_hz = newf;
			little_safe_hz = newf;
		} else {
			big_hz = newf;
		}
		apply(compute_target(little_hz, big_hz));
	}
	mutex_unlock(&gov_lock);
	return NOTIFY_OK;
}

static struct notifier_block dsu_dyn_nb = { .notifier_call = dsu_dyn_notify };

static int build_dsu_table(void)
{
	char name[16];
	u32 vf = sunxi_get_vf_index();
	unsigned long f = 0, min_uv;
	struct dev_pm_opp *opp;
	int ret;

	if (determine_dcxo_clk_source() == 26000000) {
		dcxo_hz = 26000000UL;
		snprintf(name, sizeof(name), "26m-vf%04x", vf);
	} else {
		snprintf(name, sizeof(name), "vf%04x", vf);
	}
	pr_info("dsu_dyn: speed bin prop '%s'\n", name);

	prop_name_token = dev_pm_opp_set_prop_name(dsufreq_dev, name);
	if (prop_name_token < 0) {
		pr_err("dsu_dyn: set_prop_name failed %d\n", prop_name_token);
		return prop_name_token;
	}

	ret = dev_pm_opp_of_add_table(dsufreq_dev);
	if (ret) {
		pr_err("dsu_dyn: of_add_table failed %d\n", ret);
		return ret;
	}
	table_added = true;

	while (opp_n < MAX_OPPS &&
	       !IS_ERR(opp = dev_pm_opp_find_freq_ceil(dsufreq_dev, &f))) {
		unsigned long uv = dev_pm_opp_get_voltage(opp);

		dev_pm_opp_put(opp);
		/* the vendor DT marks OPPs that don't exist for this speed bin with a
		 * 0 uV entry — including them would let the volt-cap scan treat them
		 * as runnable at any rail voltage. Skip them. */
		if (uv) {
			opp_hz[opp_n] = f;
			opp_uv[opp_n] = uv;
			opp_n++;
		}
		f++;
	}
	if (!opp_n) {
		pr_err("dsu_dyn: empty DSU OPP table\n");
		return -ENOENT;
	}

	/* append the validated over-bin rung, if requested and sane: it must be
	 * faster than the bin max and need at least the bin max's voltage */
	if (oc_hz && opp_n < MAX_OPPS && oc_hz > opp_hz[opp_n - 1] &&
	    oc_uv >= opp_uv[opp_n - 1]) {
		opp_hz[opp_n] = oc_hz;
		opp_uv[opp_n] = oc_uv;
		opp_n++;
		pr_info("dsu_dyn: OC rung enabled: %lu MHz @ %lu mV\n",
			oc_hz / 1000000, oc_uv / 1000);
	} else if (oc_hz) {
		pr_warn("dsu_dyn: OC rung %lu Hz @ %lu uV rejected (must exceed bin max %lu Hz and its %lu uV)\n",
			oc_hz, oc_uv, opp_hz[opp_n - 1], opp_uv[opp_n - 1]);
	}

	min_uv = opp_uv[0];
	max_f_by_min_v = opp_hz[0];
	for (f = 1; f < opp_n; f++) {
		if (opp_uv[f] != min_uv)
			break;
		max_f_by_min_v = opp_hz[f];
	}
	return 0;
}

static int __init dsu_dyn_init(void)
{
	struct device_node *np;
	struct platform_device *pdev;
	struct cpufreq_policy *pol;
	unsigned long lmin_uv;
	int ret, i;

	np = of_find_compatible_node(NULL, NULL, "allwinner,dsufreq");
	if (!np) { pr_err("dsu_dyn: no allwinner,dsufreq node\n"); return -ENODEV; }
	pdev = of_find_device_by_node(np);
	of_node_put(np);
	if (!pdev) { pr_err("dsu_dyn: no dsufreq platform_device\n"); return -ENODEV; }
	dsufreq_dev = &pdev->dev;

	dsu_clk = clk_get(dsufreq_dev, NULL);
	if (IS_ERR(dsu_clk)) { pr_err("dsu_dyn: clk_get %ld\n", PTR_ERR(dsu_clk)); return PTR_ERR(dsu_clk); }

	little_dev = get_cpu_device(LITTLE_CPU);
	if (!little_dev) { ret = -ENODEV; goto put_clk; }

	ret = build_dsu_table();
	if (ret)
		goto teardown;

	/* safety invariant (vendor BUG_ON, graceful here): min DSU voltage must be
	 * coverable at the A55s' minimum OPP voltage */
	pol = cpufreq_cpu_get(LITTLE_CPU);
	if (!pol) { ret = -ENODEV; goto teardown; }
	lmin_uv = little_volt((unsigned long)pol->cpuinfo.min_freq * 1000UL);
	cpufreq_cpu_put(pol);
	if (!lmin_uv || opp_uv[0] > lmin_uv) {
		pr_err("dsu_dyn: DSU min %lu uV > A55 min %lu uV - unsafe, aborting\n",
		       opp_uv[0], lmin_uv);
		ret = -EINVAL;
		goto teardown;
	}

	orig_rate = clk_get_rate(dsu_clk);
	cur_dsu = orig_rate;
	little_hz = (unsigned long)cpufreq_quick_get(LITTLE_CPU) * 1000UL;
	little_safe_hz = little_hz;
	big_hz = (unsigned long)cpufreq_quick_get(BIG_CPU) * 1000UL;

	for (i = 0; i < opp_n; i++)
		pr_info("dsu_dyn: opp %2d: %4lu MHz @ %lu mV\n",
			i, opp_hz[i] / 1000000, opp_uv[i] / 1000);
	pr_info("dsu_dyn: max_f_by_min_v=%lu MHz dcxo=%lu orig=%lu MHz\n",
		max_f_by_min_v / 1000000, dcxo_hz / 1000000, orig_rate / 1000000);

	ret = cpufreq_register_notifier(&dsu_dyn_nb, CPUFREQ_TRANSITION_NOTIFIER);
	if (ret)
		goto teardown;

	mutex_lock(&gov_lock);
	apply(compute_target(little_hz, big_hz));
	mutex_unlock(&gov_lock);
	pr_info("dsu_dyn: active - little=%lu MHz big=%lu MHz dsu=%lu MHz\n",
		little_hz / 1000000, big_hz / 1000000, cur_dsu / 1000000);
	return 0;

teardown:
	if (table_added)
		dev_pm_opp_of_remove_table(dsufreq_dev);
	if (prop_name_token > 0)
		dev_pm_opp_put_prop_name(prop_name_token);
put_clk:
	clk_put(dsu_clk);
	return ret;
}

static void __exit dsu_dyn_exit(void)
{
	cpufreq_unregister_notifier(&dsu_dyn_nb, CPUFREQ_TRANSITION_NOTIFIER);
	mutex_lock(&gov_lock);
	clk_set_rate(dsu_clk, orig_rate);
	pr_info("dsu_dyn: restored DSU to %lu MHz\n", clk_get_rate(dsu_clk) / 1000000);
	mutex_unlock(&gov_lock);
	if (table_added)
		dev_pm_opp_of_remove_table(dsufreq_dev);
	if (prop_name_token > 0)
		dev_pm_opp_put_prop_name(prop_name_token);
	clk_put(dsu_clk);
}
module_init(dsu_dyn_init);
module_exit(dsu_dyn_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Dynamic DSU/L3 governor for Allwinner A733 (vendor volt-follower policy)");
