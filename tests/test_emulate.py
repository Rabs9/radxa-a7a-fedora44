"""Tests for the emulate stage: tools/emulate.sh's verdict and milestones into the image registry."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import emulate

P = Path(__file__).resolve().parent.parent


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


PASSING = r'''
echo "== emulate $1: $2 (timeout ${TIMEOUT}s), transcript $A7A_OUTPUT/emulate-x.log"
echo "  [00:00:01] milestone: Linux version"
echo "  [00:00:20] milestone: login:"
echo "  check: sshd = listening"
echo "  check: dnf = 4.20"
echo "  check: failed_units = 0"
echo "  check: uname = 6.6.98+"
echo "VERDICT: PASS - reached a login prompt ($1)"
'''
FAILING = r'''
echo "  [00:00:01] milestone: Linux version"
echo "VERDICT: FAIL - kernel panic; see /x.log"
exit 1
'''


class TestEmulate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.reg = StageRegistry(self.cfg.registry_path)
        self.cfg.output_dir.mkdir()
        self.img = self.cfg.output_dir / "a7a-fedora-44-kde-20260906.img"
        self.img.write_bytes(b"\0" * 16)
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pass_is_recorded_against_the_image(self):
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["milestones"], ["Linux version", "login:"])
        self.assertEqual(r["checks"], {"sshd": "listening", "dnf": "4.20", "failed_units": "0", "uname": "6.6.98+"})
        self.assertTrue(self.reg.image_tested(self.img.name))
        t = self.reg.images()[self.img.name]["tests"][0]
        self.assertEqual((t["mode"], t["verdict"]), ("kernel", "PASS"))
        self.assertEqual(t["checks"]["sshd"], "listening")
        self.assertEqual(self.reg.get_stage("emulate")["status"], "passed")

    def test_the_verdict_is_recorded_for_the_bytes_that_booted(self):
        import hashlib
        first = hashlib.sha256(self.img.read_bytes()).hexdigest()
        emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        img = self.reg.images()[self.img.name]
        self.assertEqual((img["sha256"], img["tests"][0]["sha256"]), (first, first))       # an image no stage built is described first
        self.assertEqual(self.reg.tested_sha256(self.img.name), first)
        # the file is replaced under the same name by something outside the image stage and fails: the PASS was the
        # other file's and goes with it
        self.img.write_bytes(b"\1" * 16)
        Path(str(self.img) + ".sha256").write_text(f"{first}  {self.img.name}\n")          # a stale sidecar is not believed
        _stub(self.cfg.tools_dir / "emulate.sh", FAILING)
        emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        img = self.reg.images()[self.img.name]
        second = hashlib.sha256(self.img.read_bytes()).hexdigest()
        self.assertEqual(img["sha256"], second)
        self.assertEqual([t["verdict"] for t in img["tests"]], ["FAIL"])
        self.assertEqual(img["previous_builds"][0]["sha256"], first)
        self.assertFalse(self.reg.image_tested(self.img.name))

    def test_a_logrotate_that_fails_fails_the_image(self):
        # 2026-11-16, the first boot on a board: logrotate.service failed (a file its rules expect was missing); no
        # emulator run had started it, its timer does not fire within one
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7a.1\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20261116-202611160821.noarch.rpm\n")
        base = ('echo "  check: uname = 6.6.98-a7a.1"; echo "  check: a7a_packages = a7a-extlinux-20261116 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7a.1"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; ')
        def verdict(value):
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', base + f'echo "  check: logrotate = {value}"'))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            return r["verdict"], r.get("reason") or ""
        self.assertEqual(verdict("inactive ")[0], "PASS")
        v, why = verdict("failed error: stat of /var/account/pacct failed: No such file or directory ")
        self.assertEqual(v, "FAIL")
        self.assertIn("logrotate", why)

    def test_every_boot_entrys_device_tree_must_be_in_the_image(self):
        # the emulator never loads one: an entry whose device tree is missing would stop U-Boot on the board only
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7a.1\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20261116-202611160821.noarch.rpm\n")
        base = ('echo "  check: uname = 6.6.98-a7a.1"; echo "  check: a7a_packages = a7a-extlinux-20261116 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7a.1"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; ')
        dtb = "/usr/lib/linux-image-6.6.98-a7a.1/allwinner/sun60i-a733-cubie-a7a.dtb"
        def verdict(value):
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', base + f'echo "  check: boot_dtb = {value}"'))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            return r["verdict"], r.get("reason") or ""
        self.assertEqual(verdict(dtb + " ")[0], "PASS")
        v, why = verdict(dtb + " MISSING:/usr/lib/linux-image-6.6.98-a7apvr.1/ ")
        self.assertEqual(v, "FAIL")
        self.assertIn("not in the image", why)
        self.assertEqual(verdict("")[0], "FAIL")              # a check that reports nothing is not a pass

    def test_unmasked_repart_turns_a_pass_into_a_fail(self):
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', 'echo "  check: uname = 6.6.98+"; echo "  check: repart_masked = enabled"'))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("systemd-repart", r["reason"])
        self.assertFalse(self.reg.image_tested(self.img.name))

    def test_packaged_image_needs_its_packages_and_the_open_stack_default(self):
        # an image built from the RPM set carries a .kernel sidecar; its checks must show the packages it was built with
        # (.rpms sidecar) and the open-stack kernel as the extlinux default (docs/PACKAGING-2026-09-09.md)
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609092306.noarch.rpm\na7a-gpu-tools-20260909-202609092306.noarch.rpm\n"
                                                 "mesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\nkernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\n"
                                                 "kernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-gpu-tools-20260909 mesa-pvr-a7a-26.1.8 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)
        self.assertTrue(self.reg.image_tested(self.img.name))
        for bad, word in [('echo "  check: a7a_packages = package mesa-pvr-a7a is not installed "', "mesa-pvr-a7a (check"),
                          ('echo "  check: extlinux_default = fedora-6.6.98-a7a.202609091931"', "default"),
                          ('echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json"', "ICD")]:
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + "; " + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn(word, r["reason"])
            self.assertNotIn(" is,", r["reason"])      # the package name, not the word after it, from "package X is not installed"
        # an image without the sidecar (built before the packages) is judged as before
        Path(str(self.img) + ".kernel").unlink()
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING)
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")

    def test_image_with_board_tools_needs_the_session_unit_to_match(self):
        # since 2026-09-10 the image carries a7a-board-tools and a7a-gpu-tools' a7a-gpu-session.service: the boot must
        # end with the session config matching the kernel (open in QEMU, from the release string), and the sixth
        # package installed
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609100900.noarch.rpm\na7a-board-tools-20260909-202609100900.noarch.rpm\n"
                                                 "a7a-gpu-tools-20260910-202609100900.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260909 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = open yes"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)
        for bad, word in [('echo "  check: gpu_session = mixed(pvr.conf=1 software.conf=0 cap=1 nocap=0 ld.so=0) no"', "a7a-gpu-session"),
                          ('echo "  check: gpu_session = "', "a7a-gpu-session"),
                          ('echo "  check: gpu_session_unit = enabled failed timeout"', "timeout"),
                          ('echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "', "a7a-board-tools")]:
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + "; " + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn(word, r["reason"])
        self.assertEqual(emulate.expected_packages(self.img), ("a7a-extlinux", "a7a-board-tools", "a7a-gpu-tools", "mesa-pvr-a7a", "kernel-a7a", "kernel-a7apvr"))

    def test_the_panel_and_the_repo_package_are_asked_for_and_counted_only_when_the_image_was_built_with_them(self):
        # 2026-11-24: the first image with a7a-panel and a7a-repo failed here: the guest's rpm -q did not ask for them
        script = (P / "tools" / "emulate.sh").read_text()
        line = next(l for l in script.splitlines() if "check: a7a_packages" in l)
        for name in ("a7a-panel", "a7a-repo", "a7a-board-tools", "kernel-a7apvr"):
            self.assertIn(f" {name}", line)
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        rpms = ("a7a-extlinux-20260909-202609100900.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        common = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; '
                  'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; ')
        have = ('echo "  check: a7a_packages = a7a-extlinux-20260909 mesa-pvr-a7a-26.1.8 kernel-a7a-6.6.98_a7a.202609091931 '
                'kernel-a7apvr-6.6.98_a7apvr.202609091821 package a7a-panel is not installed package a7a-repo is not installed "')
        # an image built without them: their "not installed" does not fail it
        Path(str(self.img) + ".rpms").write_text(rpms)
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', common + have))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)
        # an image built with them: they must be there
        Path(str(self.img) + ".rpms").write_text(rpms + "a7a-panel-20261124-202611241632.aarch64.rpm\na7a-repo-20261124-202611241632.noarch.rpm\n")
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("a7a-panel, a7a-repo", r["reason"])
        both = have.replace("package a7a-panel is not installed package a7a-repo is not installed", "a7a-panel-20261124 a7a-repo-20261124")
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', common + both))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)

    def test_image_with_mesa_img_needs_no_pvr_wl_entry_on_the_open_boot(self):
        # mesa-img-a7a's loader entry is written only when pvrsrvkm drives the GPU (docs/DDK-WAYLAND-2026-09-10.md):
        # the emulator boots the open-stack kernel, so a7a-gpu-session must report it absent
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609102100.noarch.rpm\na7a-board-tools-20260910-202609102100.noarch.rpm\n"
                                                 "a7a-gpu-tools-20260910-202609102100.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                                                 "mesa-img-a7a-24.0.1-202609101747.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 mesa-img-a7a-24.0.1 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = open yes"; '
                'echo "  check: pvr_wl = absent (mesa-img-a7a with resolvable lib/ddk links not installed)"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)
        for bad, word in [('echo "  check: pvr_wl = active"', "pvr_wl"),
                          ('echo "  check: pvr_wl = "', "pvr_wl"),
                          ('echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "', "mesa-img-a7a")]:
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + "; " + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn(word, r["reason"])


    def test_setup_wizard_on_the_seat_fails_a_packaged_image(self):
        # the image has its user (radxa) and promises the login screen. KDE's first-run wizard (plasma-setup.service,
        # enabled by Fedora's preset, runs until /etc/plasma-setup-done exists) holding the seat instead of the greeter
        # is a build defect; an image whose run predates the check (no sessions line) is not failed for it
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609102100.noarch.rpm\na7a-board-tools-20260910-202609102100.noarch.rpm\n"
                                                 "a7a-gpu-tools-20260910-202609102100.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                                                 "mesa-img-a7a-24.0.1-202609101747.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 mesa-img-a7a-24.0.1 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = open yes"; '
                'echo "  check: pvr_wl = absent (mesa-img-a7a with resolvable lib/ddk links not installed)"'
                '; echo "  check: sessions = plasmalogin:seat0:greeter radxa:-:manager radxa:-:user"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "PASS", r)
        bad = 'echo "  check: sessions = plasma-setup:-:manager-early plasma-setup:seat0:user radxa:-:manager radxa:-:user"'
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + "; " + bad))
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["verdict"], "FAIL", r)
        self.assertIn("plasma-setup", r["reason"])

    def test_an_empty_var_fails_a_packaged_image(self):
        # 2026-11-15: Fedora's raw image keeps /var in a btrfs subvolume of its own and the build copied only root and
        # home. Every image was built (and booted: tmpfiles rebuilds the skeleton) with 566 of the 570 directories its
        # packages own under /var missing; a card two months in use still lacked 45. logrotate never rotated a log, package
        # scriptlets had no /var/lib/rpm-state. A run that predates the check is not failed for it; a check that reports
        # nothing, or one whose root half did not run (sudo), is
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609102100.noarch.rpm\na7a-board-tools-20260910-202609102100.noarch.rpm\n"
                                                 "a7a-gpu-tools-20260910-202609102100.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                                                 "mesa-img-a7a-24.0.1-202609101747.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 mesa-img-a7a-24.0.1 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = open yes"; '
                'echo "  check: pvr_wl = absent (mesa-img-a7a with resolvable lib/ddk links not installed)"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")   # no var_dirs line
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + '; echo "  check: var_dirs = 0 missing of 570, 570 tested:"'))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + '; echo "  check: var_dirs = 0 missing of 100, 100 tested:"'))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")
        for bad in ('echo "  check: var_dirs = 566 missing of 570, 570 tested: /var/account /var/adm /var/cache/PackageKit /var/cache/abrt-di"',
                    'echo "  check: var_dirs = 1 missing of 570, 570 tested: /var/lib/rpm-state"',
                    'echo "  check: var_dirs = 0 missing of 99, 99 tested:"',      # fewer than any Fedora desktop owns
                    'echo "  check: var_dirs = 0 missing of 0, 1 tested:"',        # rpm listed nothing: not a pass
                    'echo "  check: var_dirs = 0 missing of 570,  tested:"',       # sudo failed: nothing was tested
                    'echo "  check: var_dirs = 0 missing of 570, 12 tested:"',     # the root loop saw a part of the list
                    'echo "  check: var_dirs = 0 missing of 570:"',                # the first form of the check, which could not tell
                    'echo "  check: var_dirs = "'):
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + "; " + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn("/var", r["reason"])
            self.assertIn("var_dirs", r["reason"])

    def test_earlier_kernels_must_be_the_entries_the_build_says(self):
        # an image built with --previous-kernels names them in its .build sidecar: the booted system's menu must have
        # them at 3 and 4 (the package names alone say nothing: the image has two kernel-a7a then). A run without the
        # check, or an image without them, is not judged
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7a.202611151729\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202611151958.noarch.rpm\na7a-board-tools-20260910-202611151958.noarch.rpm\n"
                                                 "a7a-gpu-tools-20261115-202611151958.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609101604.aarch64.rpm\n"
                                                 "mesa-img-a7a-24.0.1-202611151755.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202611151729-202611151731.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202611151717-202611151729.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7a.202611151729"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20261115 mesa-pvr-a7a-26.1.8 mesa-img-a7a-24.0.1 kernel-a7a-6.6.98_a7a.202611151729 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 kernel-a7apvr-6.6.98_a7apvr.202611151717 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7a.202611151729"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7apvr.202611151717"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = software yes"; '
                'echo "  check: pvr_wl = absent (enabled: /etc/a7a/pvr-wl.enabled)"')
        menu = '; echo "  check: extlinux_entries = fedora-6.6.98-a7a.202611151729 fedora-6.6.98-a7apvr.202611151717 fedora-6.6.98-a7a.202609091931 fedora-6.6.98-a7apvr.202609091821 fedora-recovery "'
        self.assertEqual(emulate.expected_packages(self.img).count("kernel-a7a"), 2)        # the nine-line sidecar parses
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + menu))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")      # no .build: not judged
        Path(str(self.img) + ".build").write_text("default_stack=ddk\nssh_keys=2\nprevious_kernels=6.6.98-a7a.202609091931,6.6.98-a7apvr.202609091821\n")
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")      # a run before the check
        for bad in ('; echo "  check: extlinux_entries = fedora-6.6.98-a7a.202611151729 fedora-6.6.98-a7apvr.202611151717 fedora-recovery "',
                    '; echo "  check: extlinux_entries = fedora-6.6.98-a7a.202611151729 fedora-6.6.98-a7apvr.202611151717 fedora-6.6.98-a7apvr.202609091821 fedora-6.6.98-a7a.202609091931 fedora-recovery "',
                    '; echo "  check: extlinux_entries = "'):
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn("entries 3 and 4", r["reason"])

    def test_a_login_screen_that_is_not_drawn_fails_a_packaged_image(self):
        # 2026-11-16, the first image with Fedora's updates: KWin 6.8 has no QPainter backend, the greeter's compositor
        # exited 40 s after it started, and the run PASSED: a login prompt on the serial console, a greeter session in
        # logind, plasmalogin "active", and a black screen. Three checks say what is really there: the compositor
        # process, the core dumps of the boot, and the picture itself (tools/vnc-shot.py)
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.202609091821\n")
        Path(str(self.img) + ".rpms").write_text("a7a-extlinux-20260909-202609102100.noarch.rpm\na7a-board-tools-20260910-202609102100.noarch.rpm\n"
                                                 "a7a-gpu-tools-20260910-202609102100.noarch.rpm\nmesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm\n"
                                                 "mesa-img-a7a-24.0.1-202609101747.aarch64.rpm\n"
                                                 "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm\nkernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64.rpm\n")
        good = ('echo "  check: uname = 6.6.98-a7apvr.202609091821"; '
                'echo "  check: a7a_packages = a7a-extlinux-20260909 a7a-board-tools-20260910 a7a-gpu-tools-20260910 mesa-pvr-a7a-26.1.8 mesa-img-a7a-24.0.1 kernel-a7a-6.6.98_a7a.202609091931 kernel-a7apvr-6.6.98_a7apvr.202609091821 "; '
                'echo "  check: extlinux_default = fedora-6.6.98-a7apvr.202609091821"; echo "  check: extlinux_entry2 = fedora-6.6.98-a7a.202609091931"; '
                'echo "  check: pvr_icd = powervr_mesa_icd.aarch64.json powervr_mesa_icd.aarch64.json.disabled-by-a7a"; '
                'echo "  check: gpu_session_unit = enabled active success"; echo "  check: gpu_session = open yes"; '
                'echo "  check: pvr_wl = absent (mesa-img-a7a with resolvable lib/ddk links not installed)"')
        fine = ('; echo "  check: greeter_compositor = running (1 kwin_wayland of plasmalogin)"; echo "  check: coredumps = 0"'
                '; echo "  check: screen = 1280x800 colours=18216 dark=0% top=#fcfcfc:1%"')
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + fine))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")
        # a core dump of something else is reported, not judged (drkonqi, a user's program)
        other = fine.replace("coredumps = 0", "coredumps = 1: plasma-ksplash-")
        _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + other))
        self.assertEqual(emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)["verdict"], "PASS")
        for bad, word in ((fine.replace("running (1 kwin_wayland of plasmalogin)", "not running"), "compositor"),
                          (fine.replace("greeter_compositor = running (1 kwin_wayland of plasmalogin)", "greeter_compositor = "), "compositor"),
                          (fine.replace("coredumps = 0", "coredumps = 2: kwin_wayland kwin_wayland"), "kwin_wayland"),
                          (fine.replace("colours=18216 dark=0% top=#fcfcfc:1%", "colours=2 dark=99% top=#000000:99%"), "screen"),
                          (fine.replace("1280x800 colours=18216 dark=0% top=#fcfcfc:1%", "no frame: [Errno 111] Connection refused"), "screen"),
                          (fine.replace("screen = 1280x800 colours=18216 dark=0% top=#fcfcfc:1%", "screen = "), "screen")):
            _stub(self.cfg.tools_dir / "emulate.sh", PASSING.replace('echo "  check: uname = 6.6.98+"', good + bad))
            r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
            self.assertEqual(r["verdict"], "FAIL", bad)
            self.assertIn(word, r["reason"])

    def test_fail_is_recorded_with_the_reason(self):
        _stub(self.cfg.tools_dir / "emulate.sh", FAILING)
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["verdict"], "FAIL")
        self.assertIn("kernel panic", r["reason"])
        self.assertFalse(self.reg.image_tested(self.img.name))

    def test_stock_pass_does_not_make_the_image_tested(self):
        r = emulate.run(self.cfg, self.reg, mode="stock", image=self.img)
        self.assertEqual(r["status"], "passed")
        self.assertFalse(self.reg.image_tested(self.img.name))

    def test_default_image_is_the_newest_in_output(self):
        older = self.cfg.output_dir / "a7a-fedora-44-kde-20260101.img"
        older.write_bytes(b"\0")
        os.utime(older, (1, 1))
        r = emulate.run(self.cfg, self.reg, mode="kernel")
        self.assertEqual(r["image"], self.img.name)

    def test_missing_image_fails(self):
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.cfg.output_dir / "nope.img")
        self.assertEqual(r["status"], "failed")
        self.assertIn("no image", r["reason"])

    def test_timeout_is_passed_to_the_script(self):
        self.cfg.emulate_timeout = 123
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img)
        self.assertIn("timeout 123s", Path(r["log"]).read_text())

    def test_dry_run(self):
        r = emulate.run(self.cfg, self.reg, mode="kernel", image=self.img, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertIn("emulate.sh kernel", r["commands"][0])
        self.assertEqual(self.reg.images().get(self.img.name), None)

    def test_bad_mode(self):
        r = emulate.run(self.cfg, self.reg, mode="weird", image=self.img)
        self.assertEqual(r["status"], "failed")

    def test_parse_output(self):
        p = emulate.parse_output("x\n  [1] milestone: A\n  check: k = v = w\nVERDICT: FAIL - why\n")
        self.assertEqual(p, {"milestones": ["A"], "checks": {"k": "v = w"}, "verdict": "FAIL", "reason": "why"})


class TestEmulateScript(unittest.TestCase):
    """the real tools/emulate.sh in a scratch project tree, against a fake qemu-system-aarch64 that prints its arguments"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.script = root / "tools" / "emulate.sh"
        self.script.parent.mkdir()
        self.script.write_text((Path(__file__).resolve().parent.parent / "tools" / "emulate.sh").read_text())
        self.script.chmod(0o755)
        out = root / "output"
        (out / "kernels").mkdir(parents=True)
        (out / "kernels" / "Image-6.6.98-a7apvr.1").write_bytes(b"\0")
        self.img = out / "a7a-fedora-44-kde-20260910.img"
        self.img.write_bytes(b"\0" * 16)
        Path(str(self.img) + ".kernel").write_text("6.6.98-a7apvr.1\n")
        self.qemu = _stub(root / "bin" / "qemu-system-aarch64", 'echo "QEMU $*"\n')

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, img=None, **env):
        import subprocess
        e = {k: v for k, v in os.environ.items() if not k.startswith("A7A_EMU_")}      # the caller's shell decides nothing here
        e.update(PATH=f"{self.qemu.parent}:{os.environ['PATH']}", **env)
        return subprocess.run(["bash", str(self.script), "interactive", str(img or self.img)], env=e, capture_output=True,
                              text=True, timeout=60)

    def _qemu_line(self, img=None, **env):
        r = self._run(img, **env)
        return next((ln for ln in r.stdout.splitlines() if ln.startswith("QEMU ")), r.stdout + r.stderr)

    def _netdev(self, img=None, **env):
        words = self._qemu_line(img, **env).split()
        return words[words.index("-netdev") + 1] if "-netdev" in words else " ".join(words)

    def test_the_vm_has_the_a733s_eight_cores(self):
        # 2026-09-10: with -smp 4 the image's CPUAffinity=6-7 drop-in for plasma-kwin_wayland.service (Item E) named no
        # existing CPU, KWin failed with 215/CPUAFFINITY and every client of the first-boot Plasma session dumped core
        self.assertIn(" -smp 8 ", self._qemu_line())

    def test_a_test_boot_never_writes_to_the_image(self):
        # the verdict, the checksum and the flash gate are about the file's bytes; and a boot that wrote to an image for
        # other people would put a machine-id, ssh host keys and a journal into it after it was checked for exactly those
        self.assertIn(" -snapshot ", self._qemu_line())
        self.assertIn(" -snapshot ", self._qemu_line(self._backup()))

    def test_the_cpu_count_can_be_overridden(self):
        self.assertIn(" -smp 2 ", self._qemu_line(A7A_EMU_SMP="2"))

    def _backup(self):
        b = self.img.parent / "backups" / "a7a-working-card-20261115-repaired.img"
        b.parent.mkdir(exist_ok=True)
        b.write_bytes(b"\0" * 16)
        Path(str(b) + ".kernel").write_text("6.6.98-a7apvr.1\n")
        return b

    def test_a_backup_of_a_used_system_boots_off_the_network_without_being_asked(self):
        # 2026-11-15: the backup of the working card is a copy of a real machine (its accounts, its identity on the
        # network); restrict=on keeps the copy from going online as that machine, the ssh forward still works.
        # Forgetting a variable once would be enough, so everything under output/backups is offline by itself
        net = self._netdev(self._backup())
        self.assertEqual(net, "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:2222-:22")
        self.assertIn("offline", self._run(self._backup()).stdout)          # and the run says which it is
        # through a link too: where the file really lies decides
        link = self.img.parent / "handy.img"
        link.symlink_to(self._backup())
        Path(str(link) + ".kernel").write_text("6.6.98-a7apvr.1\n")
        self.assertIn("restrict=on", self._netdev(link))
        # only an explicit 0 puts a backup online
        self.assertNotIn("restrict=on", self._netdev(self._backup(), A7A_EMU_OFFLINE="0"))

    def test_an_image_test_boot_keeps_its_network(self):
        self.assertEqual(self._netdev(), "user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22")
        self.assertIn("network open", self._run().stdout)
        self.assertEqual(self._netdev(A7A_EMU_OFFLINE="1"), "user,id=net0,restrict=on,hostfwd=tcp:127.0.0.1:2222-:22")

    def test_a_value_that_is_neither_0_nor_1_starts_nothing(self):
        # "true", "yes", "on" used to boot with the network open and no word about it
        for v in ("true", "yes", "on", "2", ""):
            r = self._run(A7A_EMU_OFFLINE=v)
            self.assertEqual(r.returncode, 2, v)
            self.assertNotIn("QEMU ", r.stdout, v)
            self.assertIn("A7A_EMU_OFFLINE", r.stdout + r.stderr, v)

    def test_the_journal_error_tally_is_not_cut_to_eight_lines(self):
        # head -8 hid the CPUAFFINITY line under 55 identical Qt messages
        tally = next(ln for ln in self.script.read_text().splitlines() if "journalctl -p err" in ln)
        self.assertNotIn("head -8", tally)
        self.assertIn("head -40", tally)


if __name__ == "__main__":
    unittest.main()
