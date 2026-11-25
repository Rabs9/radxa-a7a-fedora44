"""Tests for tools/board/a7a-irqbalance-policy, irqbalance's --policyscript that keeps the open stack's GPU
interrupt out of irqbalance's hands (docs/SPEED-2026-09-10.md, Item E 1(a)). The script reads /proc/interrupts and
the GPU device's driver link; A7A_PROC_INTERRUPTS / A7A_GPU_DEVICE point it at fakes."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "board" / "a7a-irqbalance-policy"
INTERRUPTS = """           CPU0       CPU1       CPU2       CPU3       CPU4       CPU5       CPU6       CPU7
 11:      12345          0          0          0          0          0          0          0     GICv3  30 Level     arch_timer
447:       9876          0          0          0          0          0          0          0     GICv3 113 Level     sunxi-mmc
484:       5555          0          0          0          0          0       4444          0     GICv3 146 Level     gpu
"""


class TestIrqPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.interrupts = root / "interrupts"
        self.interrupts.write_text(INTERRUPTS)
        self.gpu = root / "sys/bus/platform/devices/1800000.gpu"
        self.gpu.mkdir(parents=True)
        for d in ("powervr", "pvrsrvkm"):
            (root / "sys/bus/platform/drivers" / d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def bind(self, driver):
        link = self.gpu / "driver"
        if link.is_symlink():
            link.unlink()
        if driver:
            os.symlink(f"../../drivers/{driver}", link)

    def run_policy(self, *args):
        env = dict(os.environ, A7A_PROC_INTERRUPTS=str(self.interrupts), A7A_GPU_DEVICE=str(self.gpu))
        return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=20)

    def test_the_open_stacks_gpu_interrupt_is_banned(self):
        self.bind("powervr")
        r = self.run_policy("/sys/devices/platform/soc/1800000.gpu", "484")
        self.assertEqual((r.returncode, r.stdout), (0, "ban=true\n"))
        # irqbalance 1.9.5 passes the literal "(null)" as the device path for a platform IRQ (it only resolves PCI
        # devices): the script must not depend on $1 (reviewer, classify.c add_new_irq -> get_irq_user_policy)
        r = self.run_policy("(null)", "484")
        self.assertEqual((r.returncode, r.stdout), (0, "ban=true\n"))

    def test_every_other_interrupt_is_left_to_irqbalance(self):
        self.bind("powervr")
        for irq in ("11", "447", "999", ""):
            r = self.run_policy("/sys/devices/whatever", irq)
            self.assertEqual((r.returncode, r.stdout), (0, ""), irq)
        r = self.run_policy()
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_ddk_and_no_driver_are_left_alone(self):
        # the affinity was measured on the open stack only; the DDK's GPU interrupt stays irqbalance's
        for driver in ("pvrsrvkm", None):
            self.bind(driver)
            r = self.run_policy("/sys/devices/platform/soc/1800000.gpu", "484")
            self.assertEqual((r.returncode, r.stdout), (0, ""), driver)

    def test_a_missing_interrupts_file_is_not_an_error(self):
        self.bind("powervr")
        self.interrupts.unlink()
        r = self.run_policy("/sys/devices/platform/soc/1800000.gpu", "484")
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
