// SPDX-License-Identifier: GPL-2.0
/* dsu_oc v2: runtime DSU/L3 clock raise for the Radxa Cubie A7A (Allwinner A733).
 * The dsufreq node has an OPP table up to 1352MHz but no active devfreq governor,
 * so the DSU/L3 sits at its boot rate (~780MHz) while the A76 cores run 3000MHz —
 * a real L3/interconnect bottleneck. Measured win @1352: +27% memory bandwidth
 * (29.6 -> 37.7 GB/s, sysbench 1M blocks x8 threads); sysbench cpu unchanged.
 *
 * The DSU shares its supply (dcdc5) with the A55 cluster. v1 just held a 1000mV
 * regulator floor; that made every A55 OPP below 1000mV impossible — cpufreq's
 * exact-point voltage requests (800mV etc.) no longer intersect the floor, so
 * __target_index fails -EINVAL and the A55s stick at their last OPP (observed
 * pinned at 2.8GHz at idle). v2 fixes this: before raising the clock it lifts
 * every A55 (cpu0 policy) OPP voltage below the floor up to the floor via
 * dev_pm_opp_adjust_voltage(), so the A55s keep their full 416MHz-2.8GHz range,
 * merely never dropping the shared rail below what the DSU needs. Originals are
 * restored on rmmod. v2 also refuses to raise the clock if the rail floor can't
 * be acquired (v1 only warned — raising the DSU on an 800mV rail is a crash risk).
 *
 * insmod to apply, rmmod to restore. No storage/firmware change, reboot-safe.
 * Params: freq=<Hz> uv=<microvolts floor>
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/clk.h>
#include <linux/regulator/consumer.h>
#include <linux/pm_opp.h>
#include <linux/cpu.h>
#include <linux/delay.h>
#include <linux/err.h>

static unsigned long freq = 1352000000UL;
module_param(freq, ulong, 0444);
MODULE_PARM_DESC(freq, "target DSU clock in Hz (default 1352000000)");

static unsigned int uv = 1000000;
module_param(uv, uint, 0444);
MODULE_PARM_DESC(uv, "voltage floor in microvolts (default 1000000)");

#define MAX_ADJ 32

static struct clk *dsu_clk;
static struct regulator *dsu_reg;
static struct device *cpu_dev;
static unsigned long orig_rate;
static unsigned long adj_freq[MAX_ADJ];
static unsigned long adj_uv[MAX_ADJ];
static int nadj;

/* Lift every available cpu0-policy OPP below the floor up to it. The A55 OPP
 * entries carry exact-point voltages (min=max=target), so restoring (v,v,v)
 * on unload reproduces the original table. */
static void a55_opps_raise(void)
{
	unsigned long f = 0, v;
	struct dev_pm_opp *opp;
	int ret;

	cpu_dev = get_cpu_device(0);
	if (!cpu_dev) { pr_warn("dsu_oc: no cpu0 device, skipping OPP adjust\n"); return; }

	while (!IS_ERR(opp = dev_pm_opp_find_freq_ceil(cpu_dev, &f))) {
		v = dev_pm_opp_get_voltage(opp);
		dev_pm_opp_put(opp);
		if (v && v < uv && nadj < MAX_ADJ) {
			ret = dev_pm_opp_adjust_voltage(cpu_dev, f, uv, uv, uv);
			if (!ret) {
				adj_freq[nadj] = f;
				adj_uv[nadj] = v;
				nadj++;
			} else {
				pr_warn("dsu_oc: adjust %lu Hz %lu->%u uV ret=%d\n", f, v, uv, ret);
			}
		}
		f++;
	}
	pr_info("dsu_oc: raised %d A55 OPPs to %u uV floor\n", nadj, uv);
}

static void a55_opps_restore(void)
{
	int i;

	if (!cpu_dev)
		return;
	for (i = nadj - 1; i >= 0; i--)
		dev_pm_opp_adjust_voltage(cpu_dev, adj_freq[i], adj_uv[i], adj_uv[i], adj_uv[i]);
	pr_info("dsu_oc: restored %d A55 OPP voltages\n", nadj);
	nadj = 0;
}

static int __init dsu_oc_init(void)
{
	struct device_node *np;
	struct platform_device *pdev;
	int ret, now_uv, tries;
	unsigned long now;

	np = of_find_compatible_node(NULL, NULL, "allwinner,dsufreq");
	if (!np) { pr_err("dsu_oc: no allwinner,dsufreq node\n"); return -ENODEV; }
	pdev = of_find_device_by_node(np);
	of_node_put(np);
	if (!pdev) { pr_err("dsu_oc: no dsufreq platform_device\n"); return -ENODEV; }

	dsu_clk = clk_get(&pdev->dev, NULL);
	if (IS_ERR(dsu_clk)) { pr_err("dsu_oc: clk_get failed %ld\n", PTR_ERR(dsu_clk)); return PTR_ERR(dsu_clk); }

	dsu_reg = regulator_get(&pdev->dev, "dsu");
	if (IS_ERR(dsu_reg)) { pr_err("dsu_oc: regulator_get(dsu) failed %ld\n", PTR_ERR(dsu_reg)); clk_put(dsu_clk); return PTR_ERR(dsu_reg); }

	orig_rate = clk_get_rate(dsu_clk);
	pr_info("dsu_oc: BEFORE rate=%lu Hz rail=%d uV\n", orig_rate, regulator_get_voltage(dsu_reg));

	/* 1) A55 OPPs first: after this, cpufreq's own requests keep the rail >= uv */
	a55_opps_raise();

	/* 2) our floor. If the A55s currently sit at a sub-floor voltage their held
	 * request [v,v] doesn't intersect ours and this fails; the adjusted OPP table
	 * makes the very next cpufreq transition lift the rail, so retry briefly. */
	for (tries = 0; tries < 20; tries++) {
		ret = regulator_set_voltage(dsu_reg, uv, 1540000);
		if (!ret)
			break;
		msleep(50);
	}
	if (ret) {
		pr_err("dsu_oc: could not hold %u uV floor (ret=%d) — NOT raising clock\n", uv, ret);
		a55_opps_restore();
		regulator_put(dsu_reg);
		clk_put(dsu_clk);
		return -EAGAIN;
	}

	/* 3) the clock itself */
	ret = clk_set_rate(dsu_clk, freq);
	now = clk_get_rate(dsu_clk);
	now_uv = regulator_get_voltage(dsu_reg);
	pr_info("dsu_oc: AFTER  set %lu->%lu Hz (ret=%d) actual=%lu Hz rail=%d uV\n",
		orig_rate, freq, ret, now, now_uv);
	return 0;
}

static void __exit dsu_oc_exit(void)
{
	if (!IS_ERR_OR_NULL(dsu_clk)) {
		clk_set_rate(dsu_clk, orig_rate);
		pr_info("dsu_oc: restored DSU rate to %lu Hz\n", clk_get_rate(dsu_clk));
		clk_put(dsu_clk);
	}
	a55_opps_restore();
	if (!IS_ERR_OR_NULL(dsu_reg))
		regulator_put(dsu_reg);
}
module_init(dsu_oc_init);
module_exit(dsu_oc_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Runtime DSU/L3 clock raise for Allwinner A733 (v2, A55-DVFS-safe)");
