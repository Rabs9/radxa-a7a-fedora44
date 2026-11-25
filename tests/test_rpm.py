"""Tests for the packaging scripts (tools/build-rpm.sh, build-tools-rpm.sh, build-mesa-rpm.sh) and their spec files
in tools/rpm/. They run rpmbuild for real on tiny fake inputs (a few hundred bytes of payload) and inspect the
packages with rpm -qp, so a spec regression shows up here and not on the board. Skipped without rpmbuild."""

import os
import shutil
import subprocess
import tarfile
import hashlib
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
HAVE_RPM = shutil.which("rpmbuild") is not None and shutil.which("rpm") is not None


def rpm_q(rpm: Path, *flags: str) -> str:
    return subprocess.run(["rpm", "-qp", *flags, str(rpm)], capture_output=True, text=True, check=True).stdout


def run(script: str, env: dict, *args: str) -> subprocess.CompletedProcess:
    e = dict(os.environ, **env)
    return subprocess.run([str(P / "tools" / script), *args], capture_output=True, text=True, env=e, timeout=300)


def unpack(rpm: Path, into: Path) -> None:
    """rpm2cpio | cpio -idm, without a shell."""
    with subprocess.Popen(["rpm2cpio", str(rpm)], stdout=subprocess.PIPE) as r2c:
        subprocess.run(["cpio", "-idm", "--quiet"], stdin=r2c.stdout, cwd=into, check=True)
        r2c.stdout.close()
    if r2c.returncode:
        raise RuntimeError(f"rpm2cpio {rpm} exited {r2c.returncode}")


def fake_payload(root: Path, rel: str, pvr: bool, pvrsrvkm: bool = True) -> Path:
    pay = root / f"payload-{rel}"
    (pay / "boot" / "dtb" / "allwinner").mkdir(parents=True)
    (pay / "boot" / f"Image-{rel}").write_bytes(b"\x7fELF fake kernel " + rel.encode())
    (pay / "boot" / "dtb" / "allwinner" / "sun60i-a733-cubie-a7a.dtb").write_bytes(b"\xd0\x0d\xfe\xed fake dtb")
    mods = pay / "lib" / "modules" / rel
    (mods / "kernel" / "drivers").mkdir(parents=True)
    (mods / "kernel" / "drivers" / "fake.ko").write_bytes(b"ko")
    (mods / "extra").mkdir()
    (mods / "extra" / "aic8800_fdrv.ko").write_bytes(b"ko")
    if pvrsrvkm:
        (mods / "extra" / "pvrsrvkm.ko").write_bytes(b"ko")
    (mods / "modules.dep").write_text("")
    (mods / "modules.order").write_text("kernel/drivers/fake.ko\n")
    os.symlink("/build/a7a-fedora/work/kernel-6.6", mods / "build")
    fw = pay / "lib" / "firmware" / "aic8800D80"
    fw.mkdir(parents=True)
    (fw / "fmacfw_8800d80_u02.bin").write_bytes(b"fw")
    if pvr:
        (pay / "lib" / "firmware" / "powervr").mkdir()
        (pay / "lib" / "firmware" / "powervr" / "rogue_36.56.104.183_v1.fw").write_bytes(b"rogue")
    (pay / "kernelrelease").write_text(rel + "\n")
    return pay


@unittest.skipUnless(HAVE_RPM, "rpmbuild/rpm not installed")
class TestKernelRpm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, rel: str, pvr: bool, **kw) -> Path:
        pay = fake_payload(Path(self.tmp.name), rel, pvr, **kw)
        r = run("build-rpm.sh", {"A7A_OUTPUT": str(self.out), "A7A_PAYLOAD": str(pay), "A7A_RPM_BUILD": "202609092000"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rpms = list((self.out / "rpms").glob("*.rpm"))
        self.assertEqual(len(rpms), 1, rpms)
        return rpms[0]

    def test_open_flavour_is_kernel_a7apvr_with_firmware_and_install_only(self):
        rel = "6.6.98-a7apvr.202609091821"
        rpm = self.build(rel, pvr=True, pvrsrvkm=False)
        self.assertEqual(rpm.name, "kernel-a7apvr-6.6.98_a7apvr.202609091821-202609092000.aarch64.rpm")
        files = rpm_q(rpm, "-l").splitlines()
        self.assertIn(f"/boot/vmlinuz-{rel}", files)
        self.assertIn(f"/usr/lib/linux-image-{rel}/allwinner/sun60i-a733-cubie-a7a.dtb", files)
        self.assertIn(f"/usr/lib/modules/{rel}/kernel/drivers/fake.ko", files)
        self.assertIn("/usr/lib/firmware/aic8800D80/fmacfw_8800d80_u02.bin", files)
        self.assertIn("/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw", files)
        self.assertNotIn("/usr/lib/firmware/powervr", files)           # linux-firmware owns the directory
        self.assertNotIn(f"/usr/lib/modules/{rel}/build", files)       # the laptop's source tree: useless on the board
        self.assertNotIn("/etc/a7a/pvrsrvkm-fdinfo-fixed", files)     # DDK marker only in the vendor flavour
        self.assertFalse([f for f in files if "a7a-extlinux" in f], "a7a-extlinux is its own package now")
        provides = rpm_q(rpm, "--provides")
        self.assertIn("installonlypkg(kernel)", provides)
        self.assertIn(f"kernel-uname-r = {rel}", provides)
        self.assertIn(f"kernel-a7apvr-{rel}", provides)
        requires = rpm_q(rpm, "--requires")
        self.assertIn("a7a-extlinux", requires)
        self.assertIn("kmod", requires)
        scripts = rpm_q(rpm, "--scripts")
        self.assertIn(f"depmod {rel}", scripts)
        self.assertIn(f"a7a-extlinux --check-files add {rel} --default", scripts)
        self.assertIn(f"a7a-extlinux --check-files add {rel}\n", scripts)
        self.assertIn(f"L=fedora-{rel}", scripts)
        self.assertIn('a7a-extlinux remove "$L"', scripts)
        self.assertIn("a7a-extlinux --check-files fallback", scripts)
        self.assertIn(f"[ ! -e /boot/vmlinuz-{rel} ]", scripts)         # another build of the same release may still own it
        self.assertNotIn("prune", scripts)
        # depmod's output is %ghost (generated by %post), so rpm -V stays clean and an erase removes it
        flags = {a: (int(b), c) for a, b, c in (line.split() for line in rpm_q(rpm, "--qf", "[%{FILENAMES} %{FILEFLAGS} %{FILEMODES:perms}\n]").splitlines())}
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/modules.dep"][0] & 64, 64)
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/modules.alias.bin"][0] & 64, 64)
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/kernel/drivers/fake.ko"][0] & 64, 0)
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/modules.order"][0] & 64, 0)     # from the kernel build, shipped
        # a %ghost without %attr records mode 0000 and rpm -V then flags depmod's 0644 files as modified (review 2)
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/modules.dep"][1], "-rw-r--r--")
        self.assertEqual(flags[f"/usr/lib/modules/{rel}/modules.weakdep"][1], "-rw-r--r--")
        self.assertIn("/usr/local/bin/a7a-extlinux", scripts)
        self.assertNotIn("/usr/local/sbin/", scripts)

    def test_shared_files_carry_the_same_mtime_in_every_package(self):
        # the WiFi firmware is in both flavours and every version, the GPU firmware in every kernel-a7apvr, the DDK
        # marker in every kernel-a7a: with per-build mtimes the last install re-stamps them and rpm -V of the other
        # package reports .......T. (2026-09-10 on the board); the spec pins them to a constant
        import time
        a = self.build("6.6.98-a7apvr.202609091821", pvr=True, pvrsrvkm=False)
        old = Path(self.tmp.name) / "payload-6.6.98-a7apvr.202609091821"
        for f in old.rglob("*"):
            if f.is_file():
                os.utime(f, (1_000_000_000, 1_000_000_000))       # a payload with quite different timestamps
        (self.out / "rpms" / a.name).rename(self.out / "a.rpm")
        b = self.build("6.6.98-a7a.202609091931", pvr=False)
        shared_a = dict(line.split() for line in rpm_q(self.out / "a.rpm", "--qf", "[%{FILENAMES} %{FILEMTIMES}\n]").splitlines())
        shared_b = dict(line.split() for line in rpm_q(b, "--qf", "[%{FILENAMES} %{FILEMTIMES}\n]").splitlines())
        fw = "/usr/lib/firmware/aic8800D80/fmacfw_8800d80_u02.bin"
        self.assertEqual(shared_a[fw], "1767225600")
        self.assertEqual(shared_b[fw], "1767225600")
        self.assertEqual(shared_a["/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw"], "1767225600")
        self.assertEqual(shared_b["/etc/a7a/pvrsrvkm-fdinfo-fixed"], "1767225600")
        self.assertNotEqual(shared_b["/usr/lib/modules/6.6.98-a7a.202609091931/kernel/drivers/fake.ko"], "1767225600")   # per-release files keep their own

    def test_vendor_flavour_is_kernel_a7a_with_the_fdinfo_marker(self):
        rel = "6.6.98-a7a.202609091931"
        rpm = self.build(rel, pvr=False)
        self.assertTrue(rpm.name.startswith("kernel-a7a-6.6.98_a7a.202609091931-"), rpm.name)
        files = rpm_q(rpm, "-l").splitlines()
        self.assertIn(f"/usr/lib/modules/{rel}/extra/pvrsrvkm.ko", files)
        self.assertIn("/etc/a7a/pvrsrvkm-fdinfo-fixed", files)
        self.assertNotIn("/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw", files)
        self.assertIn("installonlypkg(kernel)", rpm_q(rpm, "--provides"))

    def test_open_flavour_takes_the_firmware_from_the_repo_when_the_payload_lacks_it(self):
        rel = "6.6.98-a7apvr.202609091821"
        rpm = self.build(rel, pvr=False, pvrsrvkm=False)
        self.assertIn("/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw", rpm_q(rpm, "-l"))

    def test_vendor_flavour_without_pvrsrvkm_is_refused(self):
        pay = fake_payload(Path(self.tmp.name), "6.6.98-a7a.202609091931", pvr=False, pvrsrvkm=False)
        r = run("build-rpm.sh", {"A7A_OUTPUT": str(self.out), "A7A_PAYLOAD": str(pay)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("pvrsrvkm", r.stdout + r.stderr)

    def test_relative_payload_path_works(self):
        # A7A_PAYLOAD=output/payload-<rel> as typed on the command line: rpmbuild's %install runs in another directory
        rel = "6.6.98-a7apvr.202609091821"
        fake_payload(Path(self.tmp.name), rel, pvr=True, pvrsrvkm=False)
        e = dict(os.environ, A7A_OUTPUT=str(self.out), A7A_PAYLOAD=f"payload-{rel}", A7A_RPM_BUILD="202609092000")
        r = subprocess.run([str(P / "tools" / "build-rpm.sh")], capture_output=True, text=True, env=e, cwd=self.tmp.name, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(list((self.out / "rpms").glob("kernel-a7apvr-*.rpm"))), 1)

    def test_missing_payload_is_a_clean_failure(self):
        r = run("build-rpm.sh", {"A7A_OUTPUT": str(self.out), "A7A_PAYLOAD": str(self.out / "nope")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no kernel payload", r.stdout + r.stderr)


@unittest.skipUnless(HAVE_RPM, "rpmbuild/rpm not installed")
class TestToolsRpms(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir()
        # the NPU spotter's data stands in as two small files with their own checksum list (the real ones are made in
        # output/npu-kws, outside the repo)
        npu = Path(self.tmp.name) / "npu-kws"
        npu.mkdir()
        (npu / "kws_encoder_fp16_a733.nb").write_bytes(b"NBG stand-in")
        (npu / "kws_cpu.npz").write_bytes(b"npz stand-in")
        sha = Path(self.tmp.name) / "npu-kws.sha256"
        sha.write_text("".join(f"{hashlib.sha256((npu / f).read_bytes()).hexdigest()}  {f}\n"
                               for f in ("kws_encoder_fp16_a733.nb", "kws_cpu.npz")))
        self.npu = npu
        # the signing key's public half stands in as text: the build checks only that it is an armored public key
        self.key = Path(self.tmp.name) / "RPM-GPG-KEY-stand-in"
        self.key.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nstand-in\n-----END PGP PUBLIC KEY BLOCK-----\n")
        self.env = {"A7A_OUTPUT": str(self.out), "A7A_RPM_BUILD": "202609092000", "A7A_NPU_DATA": str(npu),
                    "A7A_NPU_SHA": str(sha), "A7A_REPO_KEY": str(self.key)}
        r = run("build-tools-rpm.sh", self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.rpms = {p.name.split("-2")[0]: p for p in (self.out / "rpms").glob("*.rpm")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_packages_do_not_carry_the_build_machines_name(self):
        # rpm writes the host it was built on into every package, and from there into the package database of every
        # system that installs it: an image made from the owner's card had the build machine's name in it seven times (2026-11-16)
        import socket
        for name, rpm in self.rpms.items():
            host = rpm_q(rpm, "--qf", "%{BUILDHOST}")
            self.assertEqual(host, "a7a-fedora", name)
            self.assertNotEqual(host, socket.gethostname(), name)

    def test_a7a_repo_package(self):
        rpm = self.rpms["a7a-repo"]
        self.assertTrue(rpm.name.endswith(".noarch.rpm"), rpm.name)
        self.assertEqual(sorted(rpm_q(rpm, "-l").split()), ["/etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora", "/etc/yum.repos.d/a7a-fedora.repo"])
        # the owner's edits to the repo file survive an update; the key is the package's own
        self.assertEqual(rpm_q(rpm, "-c").split(), ["/etc/yum.repos.d/a7a-fedora.repo"])
        root = Path(self.tmp.name) / "r"
        root.mkdir()
        unpack(rpm, root)
        self.assertEqual((root / "etc/yum.repos.d/a7a-fedora.repo").read_bytes(), (P / "tools/repo/a7a-fedora.repo").read_bytes())
        self.assertEqual((root / "etc/pki/rpm-gpg/RPM-GPG-KEY-a7a-fedora").read_bytes(), self.key.read_bytes())
        self.assertEqual(rpm_q(rpm, "--scripts"), "")

    def test_without_the_key_a7a_repo_is_left_out_and_a_secret_key_is_refused(self):
        out = Path(self.tmp.name) / "o2"
        r = run("build-tools-rpm.sh", dict(self.env, A7A_OUTPUT=str(out), A7A_REPO_KEY=str(Path(self.tmp.name) / "none")))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a7a-repo left out", r.stdout)
        self.assertEqual(list((out / "rpms").glob("a7a-repo-*")), [])
        secret = Path(self.tmp.name) / "secret.asc"
        secret.write_text(self.key.read_text() + "-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nx\n-----END PGP PRIVATE KEY BLOCK-----\n")
        r = run("build-tools-rpm.sh", dict(self.env, A7A_OUTPUT=str(out), A7A_REPO_KEY=str(secret)))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("holds a secret key", r.stdout + r.stderr)

    def test_a7a_extlinux_package(self):
        rpm = self.rpms["a7a-extlinux"]
        self.assertTrue(rpm.name.endswith(".noarch.rpm"), rpm.name)
        self.assertEqual(rpm_q(rpm, "-l").split(), ["/usr/local/bin/a7a-extlinux"])
        self.assertIn("python3", rpm_q(rpm, "--requires"))
        self.assertEqual(rpm_q(rpm, "--scripts"), "")
        # refuses the 2026-09-06/07 kernel-a7a packages that shipped this file under another path string
        conflicts = rpm_q(rpm, "--conflicts")
        self.assertIn("kernel-a7a = 6.6.98_a7a.202609072057-202609072103", conflicts)
        self.assertIn("kernel-a7a = 6.6.98.plus-202609062352", conflicts)
        self.assertNotIn("<", conflicts)       # release-qualified: re-packaging the 09-07 payload with this spec stays installable

    def test_a7a_gpu_tools_package(self):
        rpm = self.rpms["a7a-gpu-tools"]
        files = rpm_q(rpm, "-l").split()
        for f in ["prepare616.sh", "backtoddk.sh", "pvrwatch.sh", "fwtrace-tail.sh", "clockcheck616.sh", "a7a-gpu-check",
                  "a7a-gpu-session", "a7a-gpu-irq-affinity", "a7a-irqbalance-policy", "a7a-gpu-gap", "Xwayland",
                  "plasma-keyboard"]:
            self.assertIn(f"/usr/local/bin/{f}", files)
        # the GPU gap report reads Mesa's version gates from its data file (docs/GPU-GAP-2026-09-10.md)
        self.assertIn("/usr/local/share/a7a-gpu-gap/a7a-gpu-gap-mesa-26.1.8-gates.json", files)
        # the session unit, the nocap units it enables per boot, the preset that enables the session unit, the greeter drop-in
        for f in ["/usr/lib/systemd/system/a7a-gpu-session.service", "/usr/lib/systemd/system/a7a-kwin-nocap.path",
                  "/usr/lib/systemd/system/a7a-kwin-nocap.service", "/usr/lib/systemd/system-preset/80-a7a-gpu-tools.preset",
                  "/etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf", "/etc/modprobe.d/powervr-trace.conf",
                  # the greeter UI stays on Fedora's Mesa on both stacks (docs/DDK-WAYLAND-2026-09-10.md, reviewer pass 2)
                  "/etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf",
                  # irqbalance keeps its hands off the GPU interrupt (Item E 1(a) survives the boot)
                  "/etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf",
                  # Item E: the GPU interrupt goes to the big cluster, the session compositor is pinned to it
                  "/usr/lib/udev/rules.d/90-a7a-gpu-irq.rules",
                  "/etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf"]:
            self.assertIn(f, files)
        self.assertEqual(sorted(rpm_q(rpm, "--configfiles").split()),
                         ["/etc/modprobe.d/powervr-trace.conf",
                          "/etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf",
                          "/etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf",
                          "/etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf",
                          "/etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf"])
        # the greeter drop-in is plain %config (a differing hand copy becomes .rpmorig, the packaged one takes over);
        # the modprobe file keeps noreplace (RPMFILE_CONFIG = 1, RPMFILE_NOREPLACE = 16)
        flags = {l.split()[0]: int(l.split()[1]) for l in rpm_q(rpm, "--qf", "[%{FILENAMES} %{FILEFLAGS}\n]").splitlines() if "/etc/" in l}
        self.assertEqual(flags["/etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf"] & 17, 1)
        self.assertEqual(flags["/etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf"] & 17, 1)
        self.assertEqual(flags["/etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf"] & 17, 1)
        self.assertEqual(flags["/etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf"] & 17, 1)
        self.assertEqual(flags["/etc/modprobe.d/powervr-trace.conf"] & 17, 17)
        requires = rpm_q(rpm, "--requires")
        self.assertIn("a7a-extlinux", requires)
        self.assertIn("a7a-board-tools", requires)       # a7a-gpu-session and backtoddk.sh call pvr-desktop
        self.assertIn("python3", requires)               # a7a-gpu-gap
        # 18 until 2026-09-10 + a7a-gpu-gap, its data directory and the gates file + the greeter UI drop-in and its directory
        # + the irqbalance policy script, its drop-in and the drop-in directory
        # + the Xwayland wrapper (2026-11-17) + the login screen's keyboard wrapper (2026-11-18)
        self.assertEqual(len(files), 28)
        scripts = rpm_q(rpm, "--scripts")
        # the session unit is enabled through the preset on the first install and disabled with the nocap units on erase;
        # the package never starts or enables the nocap units itself (the session unit does, per boot)
        self.assertIn("install-system-units a7a-gpu-session.service", scripts)
        self.assertIn("remove-system-units a7a-gpu-session.service a7a-kwin-nocap.path a7a-kwin-nocap.service", scripts)
        # the irqbalance drop-in takes effect on an upgrade too: daemon-reload BEFORE the restart (rpm runs %posttrans
        # before systemd's file trigger), then the GPU affinity again, only while powervr is bound (reviewer pass 3)
        # the commands only: the scriptlet's comment names the same things in another order
        post = "\n".join(l for l in scripts.split("posttrans scriptlet")[1].splitlines() if not l.lstrip().startswith("#"))
        self.assertLess(post.index("daemon-reload"), post.index("try-restart irqbalance.service"))
        self.assertLess(post.index("try-restart irqbalance.service"), post.index("/usr/local/bin/a7a-gpu-irq-affinity"))
        self.assertIn('= powervr ]', post)
        self.assertIn("/run/systemd/system", post)       # not in the image-build chroot
        self.assertNotIn("a7a-kwin-nocap", scripts.split("preuninstall")[0])
        # an upgrade from the 20260909 package (no unit) must enable the unit too: %post's macro only presets on a
        # first install ($1 == 1), so a trigger on the old version's removal repeats it with $1 == 1
        triggers = rpm_q(rpm, "--triggers")
        self.assertIn("a7a-gpu-tools < 20260910", triggers)
        self.assertIn("install-system-units a7a-gpu-session.service", triggers)
        self.assertIn("triggerpostun", triggers)
        # ... unconditionally: rpm 6.0.2 passes $1 = 2 to a self-named %triggerpostun during the upgrade (the old
        # header is still counted), so %systemd_post's guard would turn the trigger into a no-op (2nd reviewer pass)
        self.assertNotIn("$1 -eq 1", triggers.split("triggerpostun")[1])
        root = Path(self.tmp.name) / "p"
        root.mkdir()
        unpack(rpm, root)
        self.assertEqual((root / "usr/lib/systemd/system-preset/80-a7a-gpu-tools.preset").read_text().strip().splitlines()[-1],
                         "enable a7a-gpu-session.service")
        unit = (root / "usr/lib/systemd/system/a7a-gpu-session.service").read_text()
        self.assertIn("Before=display-manager.service systemd-user-sessions.service", unit)
        self.assertIn("ExecStart=/usr/local/bin/a7a-gpu-session apply", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_a7a_board_tools_package(self):
        rpm = self.rpms["a7a-board-tools"]
        self.assertTrue(rpm.name.endswith(".noarch.rpm"), rpm.name)
        files = rpm_q(rpm, "-l").split()
        tuned = [f"/usr/lib/tuned/profiles/a7a-{m}{f}" for m in ("powersave", "balanced", "performance")
                 for f in ("", "/tuned.conf", "/script.sh")]
        # the NPU wake spotter (a7a-voice npu on): its code and the data tools/npu/npu-kws.sha256 names
        npu = ["/usr/lib/a7a-voice", "/usr/lib/a7a-voice/npu"] + [f"/usr/lib/a7a-voice/npu/{f}" for f in
                                                                  ("npunet.py", "npu_kws.py", "kws_encoder_fp16_a733.nb", "kws_cpu.npz")]
        self.assertEqual(sorted(files), sorted(["/usr/local/bin/pvr-desktop", "/usr/local/bin/pvr-run", "/usr/local/bin/a7a-clock",
                                                "/usr/local/bin/npu-run", "/usr/local/bin/a7a-llm",
                                                "/usr/local/bin/a7a-gpu-stat", "/etc/sudoers.d/a7a-gpu-stat",
                                                "/usr/local/bin/a7a-ac101-dtb",
                                                "/usr/lib/systemd/system/a7a-codec.service",
                                                "/usr/lib/tmpfiles.d/a7a-x11.conf",
                                                "/usr/share/wireplumber/wireplumber.conf.d/51-a7a-ac101.conf",
                                                "/usr/local/bin/a7a-voice", "/usr/lib/systemd/user/a7a-voice.service",
                                                "/usr/local/bin/a7a-clock-keep", "/usr/lib/systemd/system/a7a-clock-keep.service",
                                                "/usr/lib/systemd/system/a7a-clock-keep-tick.service",
                                                "/usr/lib/systemd/system/a7a-clock-keep.timer",
                                                "/usr/lib/systemd/system/multi-user.target.wants/a7a-clock-keep.service",
                                                "/usr/lib/systemd/system/timers.target.wants/a7a-clock-keep.timer",
                                                "/usr/lib/udev/rules.d/70-a7a-voice-uinput.rules"] + tuned + npu))
        requires = rpm_q(rpm, "--requires")
        for dep in ["bash", "python3", "sudo", "NetworkManager"]:
            self.assertIn(dep, requires)
        self.assertNotIn("a7a-", requires)                # nothing of ours: a7a-gpu-tools depends on this package, not the reverse
        self.assertNotIn("dtc", requires)                 # not in the Fedora KDE image: a hard dependency would fail the offline image build
        recommends = rpm_q(rpm, "--recommends")
        self.assertNotIn("pyqt6", recommends.lower())     # the Qt clock window is gone: a7a-panel took its place
        self.assertIn("dtc", recommends)
        self.assertIn("alsa-utils", recommends)           # amixer: the AC101's playback path (a7a-clock codec)
        self.assertEqual(rpm_q(rpm, "--scripts"), "")
        root = Path(self.tmp.name) / "b"
        root.mkdir()
        unpack(rpm, root)
        for f in ["pvr-desktop", "pvr-run", "a7a-clock", "npu-run", "llm/a7a-llm", "a7a-gpu-stat", "a7a-ac101-dtb",
                  "a7a-voice", "a7a-clock-keep"]:
            installed = root / "usr/local/bin" / Path(f).name
            self.assertEqual(installed.read_bytes(), (P / "tools/board" / f).read_bytes(), f)
            self.assertTrue(os.access(installed, os.X_OK), f)
        # the clock keeper: switched on by the package itself (links under /usr/lib: no scriptlets), the units as in the tree
        for wants, unit in (("multi-user.target.wants", "a7a-clock-keep.service"), ("timers.target.wants", "a7a-clock-keep.timer")):
            link = root / "usr/lib/systemd/system" / wants / unit
            self.assertEqual(os.readlink(link), f"../{unit}")
        for unit in ("a7a-clock-keep.service", "a7a-clock-keep-tick.service", "a7a-clock-keep.timer"):
            self.assertEqual((root / "usr/lib/systemd/system" / unit).read_bytes(), (P / "tools/board/systemd" / unit).read_bytes())
        self.assertIn("ExecStop=/usr/local/bin/a7a-clock-keep", (root / "usr/lib/systemd/system/a7a-clock-keep.service").read_text())
        # the AC101 unit: started only where `a7a-clock codec ac101` was chosen (it enables the unit; no preset does)
        unit = (root / "usr/lib/systemd/system/a7a-codec.service").read_text()
        self.assertEqual(unit, (P / "tools/board/systemd/a7a-codec.service").read_text())
        self.assertIn("ConditionPathExists=/etc/a7a/codec", unit)
        self.assertIn("ExecStart=/usr/local/bin/a7a-clock codec reapply\n", unit)
        # and at shutdown the trees again: a kernel installed during this boot has the AC101 at its first boot
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn("ExecStop=/usr/local/bin/a7a-clock codec reapply-trees", unit)
        self.assertFalse((root / "usr/share/applications").exists())    # the panel package carries the menu entries
        # sudo ignores a drop-in that others may write, and one that does not parse locks sudo for everyone
        drop = root / "etc/sudoers.d/a7a-gpu-stat"
        self.assertEqual(drop.read_bytes(), (P / "tools/board/sudoers/a7a-gpu-stat").read_bytes())
        modes = rpm_q(rpm, "--qf", "[%{FILENAMES} %{FILEMODES:octal} %{FILEUSERNAME} %{FILEFLAGS:fflags}\n]")
        self.assertIn("/etc/sudoers.d/a7a-gpu-stat 100440 root cn", modes)
        # the X11 socket directories out of the /tmp clean-up (tests/test_x11_tmpfiles.py: what the lines do)
        self.assertEqual((root / "usr/lib/tmpfiles.d/a7a-x11.conf").read_bytes(), (P / "tools/board/tmpfiles.d/a7a-x11.conf").read_bytes())
        self.assertIn("/usr/lib/tmpfiles.d/a7a-x11.conf 100644 root ", modes)
        # the AC101 jack in 16-bit (tests/test_ac101_wireplumber.py: what the rule matches)
        rule = "usr/share/wireplumber/wireplumber.conf.d/51-a7a-ac101.conf"
        self.assertEqual((root / rule).read_bytes(), (P / "tools/board/wireplumber/51-a7a-ac101.conf").read_bytes())
        self.assertIn(f"/{rule} 100644 root ", modes)
        # voice commands: a user unit, off until `a7a-voice on` (tests/test_a7a_voice.py: what the listener does)
        voice = "usr/lib/systemd/user/a7a-voice.service"
        self.assertEqual((root / voice).read_bytes(), (P / "tools/board/systemd/a7a-voice.service").read_bytes())
        self.assertIn(f"/{voice} 100644 root ", modes)
        self.assertIn("pipewire-utils", recommends)
        self.assertIn("libnotify", recommends)
        if shutil.which("visudo"):
            self.assertEqual(subprocess.run(["visudo", "-cqf", str(drop)]).returncode, 0)
        self.assertIn("tuned-ppd", recommends)
        for m in ("powersave", "balanced", "performance"):
            d = root / f"usr/lib/tuned/profiles/a7a-{m}"
            self.assertEqual((d / "tuned.conf").read_bytes(), (P / f"tools/board/tuned/a7a-{m}/tuned.conf").read_bytes())
            self.assertEqual((d / "script.sh").read_bytes(), (P / "tools/board/tuned/script.sh").read_bytes())
            self.assertTrue(os.access(d / "script.sh", os.X_OK), m)
            self.assertIn("script=${i:PROFILE_DIR}/script.sh", (d / "tuned.conf").read_text())

    def test_the_npu_spotter_is_packaged_as_tested(self):
        root = Path(self.tmp.name) / "npu-x"
        root.mkdir()
        unpack(self.rpms["a7a-board-tools"], root)
        d = root / "usr/lib/a7a-voice/npu"
        for f in ("npunet.py", "npu_kws.py"):
            self.assertEqual((d / f).read_bytes(), (P / "tools/board/npu" / f).read_bytes(), f)
        for f in ("kws_encoder_fp16_a733.nb", "kws_cpu.npz"):
            self.assertEqual((d / f).read_bytes(), (self.npu / f).read_bytes(), f)
        # data that is not what the checksum list names: no package
        (self.npu / "kws_cpu.npz").write_bytes(b"changed")
        out2 = Path(self.tmp.name) / "out2"
        out2.mkdir()
        r = run("build-tools-rpm.sh", {"A7A_OUTPUT": str(out2), "A7A_NPU_DATA": str(self.npu),
                                        "A7A_NPU_SHA": str(Path(self.tmp.name) / "npu-kws.sha256")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not hold the NPU spotter files", r.stdout + r.stderr)

    def test_packaged_scripts_are_byte_identical_to_the_repo(self):
        # the repo is the source of truth; no shebang mangling, no stripping
        rpm = self.rpms["a7a-gpu-tools"]
        root = Path(self.tmp.name) / "x"
        root.mkdir()
        unpack(rpm, root)
        for f in ["prepare616.sh", "backtoddk.sh", "a7a-gpu-check", "a7a-gpu-session", "a7a-gpu-gap", "a7a-irqbalance-policy"]:
            self.assertEqual((root / "usr/local/bin" / f).read_bytes(), (P / "tools/board" / f).read_bytes(), f)
        self.assertTrue(os.access(root / "usr/local/bin/a7a-gpu-gap", os.X_OK))
        self.assertEqual((root / "usr/local/share/a7a-gpu-gap/a7a-gpu-gap-mesa-26.1.8-gates.json").read_bytes(),
                         (P / "tools/board/a7a-gpu-gap-mesa-26.1.8-gates.json").read_bytes())
        for u in ["a7a-gpu-session.service", "a7a-kwin-nocap.path", "a7a-kwin-nocap.service"]:
            self.assertEqual((root / "usr/lib/systemd/system" / u).read_bytes(), (P / "tools/board/systemd" / u).read_bytes(), u)
        self.assertEqual((root / "etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf").read_bytes(),
                         (P / "tools/board/systemd/plasma-login-kwin_wayland-software.conf").read_bytes())
        self.assertEqual((root / "etc/systemd/system/irqbalance.service.d/a7a-gpu-irq.conf").read_bytes(),
                         (P / "tools/board/systemd/irqbalance-a7a-gpu-irq.conf").read_bytes())
        self.assertEqual((root / "etc/systemd/user/plasma-login.service.d/a7a-fedora-mesa.conf").read_bytes(),
                         (P / "tools/board/systemd/plasma-login-fedora-mesa.conf").read_bytes())
        self.assertEqual((root / "etc/modprobe.d/powervr-trace.conf").read_bytes(), (P / "tools/board/modprobe.d/powervr-trace.conf").read_bytes())
        self.assertEqual((root / "etc/systemd/user/plasma-kwin_wayland.service.d/a7a-cpu-affinity.conf").read_bytes(),
                         (P / "tools/board/systemd/plasma-kwin_wayland-cpuaffinity.conf").read_bytes())
        self.assertEqual((root / "usr/lib/udev/rules.d/90-a7a-gpu-irq.rules").read_bytes(), (P / "tools/board/udev/90-a7a-gpu-irq.rules").read_bytes())
        rpm = self.rpms["a7a-extlinux"]
        unpack(rpm, root)
        self.assertEqual((root / "usr/local/bin/a7a-extlinux").read_bytes(), (P / "tools/a7a-extlinux").read_bytes())


@unittest.skipUnless(HAVE_RPM, "rpmbuild/rpm not installed")
class TestMesaRpm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, name="mesa-pvr-a7a-26.1.8-202609092030.tar.gz", library_path="/usr/local/lib64/libvulkan_powervr_mesa.so", extra=None) -> Path:
        root = Path(self.tmp.name) / "stage"
        (root / "usr/local/lib64").mkdir(parents=True)
        (root / "usr/local/share/vulkan/icd.d").mkdir(parents=True)
        (root / "usr/local/lib64/libvulkan_powervr_mesa.so").write_bytes(b"\x7fELF fake driver a7a loadop cache")
        (root / "usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json").write_text(
            '{\n    "ICD": {\n        "api_version": "1.4.354",\n        "library_arch": "64",\n        "library_path": "%s"\n    },\n    "file_format_version": "1.0.1"\n}\n' % library_path)
        if extra:
            (root / extra).parent.mkdir(parents=True, exist_ok=True)
            (root / extra).write_text("x")
        tar = Path(self.tmp.name) / name
        with tarfile.open(tar, "w:gz") as t:
            for p in sorted(root.rglob("*")):
                t.add(p, arcname=str(p.relative_to(root)), recursive=False)
        shutil.rmtree(root)
        return tar

    def test_package_from_a_staging_tarball(self):
        tar = self.stage()
        r = run("build-mesa-rpm.sh", {"A7A_OUTPUT": str(self.out)}, str(tar))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rpms = list((self.out / "rpms").glob("*.rpm"))
        self.assertEqual(len(rpms), 1)
        rpm = rpms[0]
        self.assertEqual(rpm.name, "mesa-pvr-a7a-26.1.8-202609092030.aarch64.rpm")
        files = rpm_q(rpm, "-l").split()
        self.assertEqual(sorted(files), ["/usr/local/lib64/libvulkan_powervr_mesa.so", "/usr/local/share/vulkan",
                                         "/usr/local/share/vulkan/icd.d", "/usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json"])
        self.assertIn("vulkan-loader", rpm_q(rpm, "--requires"))
        scripts = rpm_q(rpm, "--scripts")
        icd = "/usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json"
        self.assertIn(f"mv -f {icd} {icd}.disabled-by-a7a", scripts)           # %post masks Fedora's ICD
        self.assertIn(f"mv {icd}.disabled-by-a7a {icd}", scripts)              # %postun on erase unmasks it
        self.assertIn('[ "$1" = 0 ]', scripts)
        triggers = rpm_q(rpm, "--triggerscripts")
        self.assertIn("mesa-vulkan-drivers", triggers)
        self.assertIn("triggerin", triggers)
        self.assertIn(f"{icd}.disabled-by-a7a", triggers)

    def test_relative_tarball_path_works(self):
        tar = self.stage()
        r = subprocess.run([str(P / "tools" / "build-mesa-rpm.sh"), tar.name], capture_output=True, text=True,
                           env=dict(os.environ, A7A_OUTPUT=str(self.out)), cwd=self.tmp.name, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(list((self.out / "rpms").glob("mesa-pvr-a7a-*.rpm"))), 1)

    def test_tarball_name_and_content_are_checked(self):
        r = run("build-mesa-rpm.sh", {"A7A_OUTPUT": str(self.out)}, str(self.stage(name="mesa-driver.tar.gz")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("mesa-pvr-a7a-<mesa version>", r.stdout + r.stderr)
        r = run("build-mesa-rpm.sh", {"A7A_OUTPUT": str(self.out)}, str(self.stage(library_path="/usr/lib64/libvulkan_powervr_mesa.so")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not point at /usr/local/lib64", r.stdout + r.stderr)
        r = run("build-mesa-rpm.sh", {"A7A_OUTPUT": str(self.out)}, str(self.stage(extra="usr/local/bin/stray")))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("outside usr/local/lib64 and usr/local/share", r.stdout + r.stderr)
        self.assertEqual(list((self.out / "rpms").glob("*.rpm")), [])



@unittest.skipUnless(HAVE_RPM, "rpmbuild/rpm not installed")
class TestRewrap(unittest.TestCase):
    """tools/rewrap-rpm.sh (2026-11-24): the September kernel and Mesa packages the owner's card runs, packaged again
    without the build machine's name (BUILDHOST), their files untouched. The "old" packages here are built with the
    project's specs as before 2026-11-16, under another host name."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.out = self.d / "output"
        (self.out / "rpms").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def old(self, spec: Path, glob: str, **defines) -> Path:
        """A package as rpmbuild made them before _buildhost was set: the host "old-laptop"."""
        top = self.d / "top"
        shutil.rmtree(top, ignore_errors=True)
        args = ["rpmbuild", "--define", f"_topdir {top}", "--define", "_binary_payload w2T.xzdio",
                "--define", "_buildhost old-laptop", "--target", "aarch64-linux"]
        for k, v in defines.items():
            args += ["--define", f"{k} {v}"]
        r = subprocess.run(args + ["-bb", str(spec)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rpm = next((top / "RPMS").rglob(glob))
        dest = self.out / "rpms" / rpm.name
        shutil.copy(rpm, dest)
        return dest

    def old_kernel(self, rel: str, pvr: bool, spec: Path = P / "tools/rpm/kernel-a7a.spec") -> Path:
        pay = fake_payload(self.d, rel, pvr)
        flav = "a7apvr" if pvr else "a7a"
        return self.old(spec, f"kernel-{flav}-*.rpm", a7a_name=f"kernel-{flav}", a7a_flavour=flav, a7a_rel=rel,
                        a7a_ver=rel.replace("-", "_"), a7a_build="202609100744", a7a_payload=pay)

    def rewrap(self, *rpms: Path) -> subprocess.CompletedProcess:
        return run("rewrap-rpm.sh", {"A7A_OUTPUT": str(self.out)}, *map(str, rpms))

    def dump(self, rpm: Path) -> list[str]:
        """rpm --dump without the files' times"""
        return sorted(" ".join(l.split()[:2] + l.split()[3:]) for l in rpm_q(rpm, "--dump").splitlines())

    def check(self, old: Path, before: dict) -> None:
        new = self.out / "rpms" / old.name                           # the same name: Name, Version, Release kept
        self.assertEqual(rpm_q(new, "--qf", "%{BUILDHOST}"), "a7a-fedora")
        self.assertEqual(self.dump(new), before["dump"])
        for q in ("--requires", "--provides", "--scripts", "--triggers"):
            self.assertEqual(rpm_q(new, q), before[q], q)
        kept = self.out / "rpms-replaced" / old.name                  # the old one kept aside, not deleted
        self.assertEqual(rpm_q(kept, "--qf", "%{BUILDHOST}"), "old-laptop")

    def before(self, rpm: Path) -> dict:
        return {"dump": self.dump(rpm), **{q: rpm_q(rpm, q) for q in ("--requires", "--provides", "--scripts", "--triggers")}}

    def test_both_kernel_flavours_and_the_mesa_driver_are_wrapped_again_as_they_were(self):
        vendor = self.old_kernel("6.6.98-a7a.202609091931", pvr=False)
        open_ = self.old_kernel("6.6.98-a7apvr.202609091821", pvr=True)
        stage = self.d / "stage"
        (stage / "usr/local/lib64").mkdir(parents=True)
        (stage / "usr/local/share/vulkan/icd.d").mkdir(parents=True)
        (stage / "usr/local/lib64/libvulkan_powervr_mesa.so").write_bytes(b"\x7fELF fake driver")
        (stage / "usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json").write_text(
            '{"ICD": {"library_path": "/usr/local/lib64/libvulkan_powervr_mesa.so"}}\n')
        tar = self.d / "mesa-pvr-a7a-26.1.8-202609101604.tar.gz"
        with tarfile.open(tar, "w:gz") as t:
            for p in sorted(stage.rglob("*")):
                t.add(p, arcname=str(p.relative_to(stage)), recursive=False)
        mesa = self.old(P / "tools/rpm/mesa-pvr-a7a.spec", "mesa-pvr-a7a-*.rpm", a7a_mesa_ver="26.1.8",
                        a7a_build="202609101604", a7a_stage=tar)
        before = {p: self.before(p) for p in (vendor, open_, mesa)}
        r = self.rewrap(vendor, open_, mesa)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in (vendor, open_, mesa):
            self.check(p, before[p])
        self.assertEqual(r.stdout.count("re-wrapped"), 3, r.stdout)
        # again: nothing to do
        r = self.rewrap(*[self.out / "rpms" / p.name for p in (vendor, open_, mesa)])
        self.assertEqual((r.returncode, r.stdout.count("built as a7a-fedora already")), (0, 3), r.stdout)

    def test_a_package_that_would_come_out_different_is_left_as_it_was(self):
        # built with a spec whose %post differs from today's: the re-wrap would change what the package does
        spec = self.d / "kernel-a7a.spec"
        spec.write_text((P / "tools/rpm/kernel-a7a.spec").read_text().replace("depmod %{a7a_rel} || :", "depmod -a %{a7a_rel} || :", 1))
        old = self.old_kernel("6.6.98-a7a.202609091931", pvr=False, spec=spec)
        r = self.rewrap(old)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("scripts differ", r.stdout)
        self.assertIn("NOT the same package again", r.stdout)
        self.assertEqual(rpm_q(old, "--qf", "%{BUILDHOST}"), "old-laptop")     # still there, as it was
        self.assertFalse((self.out / "rpms-replaced").exists())

    def card(self, *rpms: Path) -> Path:
        """A package database with these packages installed, as the card has them (in a root of its own, as root
        in a user namespace)."""
        root = self.d / f"root{len(list(self.d.glob('root*')))}"
        root.mkdir()
        r = self.sh(f"rpm --root {root} --initdb && rpm --root {root} -i --nodeps --noscripts --ignorearch " + " ".join(map(str, rpms)))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return root

    def sh(self, cmd: str) -> subprocess.CompletedProcess:
        return subprocess.run(["unshare", "-r", "bash", "-c", cmd], capture_output=True, text=True, timeout=300)

    def put(self, root: Path, *rpms: Path) -> subprocess.CompletedProcess:
        return self.sh(f"A7A_RPM_ROOT={root} bash {P}/tools/master-rewrap.sh old-laptop " + " ".join(map(str, rpms)))

    @unittest.skipUnless(shutil.which("unshare"), "no unshare")
    def test_the_image_step_puts_the_re_wrapped_package_over_the_cards_copy(self):
        old = self.old_kernel("6.6.98-a7a.202609091931", pvr=False)
        as_built = self.d / old.name
        shutil.copy(old, as_built)
        root = self.card(as_built)
        self.assertEqual(self.rewrap(old).returncode, 0)
        new = self.out / "rpms" / old.name
        r = self.put(root, new)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the re-wrapped package is in (its files as they were)", r.stdout)
        self.assertIn("no package of the image names the build machine", r.stdout)
        self.assertEqual(self.sh(f"rpm --root {root} -q --qf '%{{BUILDHOST}}' kernel-a7a").stdout, "a7a-fedora")
        self.assertEqual(self.sh(f"rpm --root {root} -qa").stdout.split(), [old.name[:-len(".rpm")]])   # one, not two

    @unittest.skipUnless(shutil.which("unshare"), "no unshare")
    def test_the_image_step_refuses_a_card_whose_files_are_not_the_packages(self):
        old = self.old_kernel("6.6.98-a7a.202609091931", pvr=False)
        as_built = self.d / old.name
        shutil.copy(old, as_built)
        root = self.card(as_built)
        (root / "usr/lib/modules/6.6.98-a7a.202609091931/extra/aic8800_fdrv.ko").write_bytes(b"another build")
        self.assertEqual(self.rewrap(old).returncode, 0)
        r = self.put(root, self.out / "rpms" / old.name)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("the card's files are not as its package says", r.stdout)
        self.assertEqual(self.sh(f"rpm --root {root} -q --qf '%{{BUILDHOST}}' kernel-a7a").stdout, "old-laptop")
        # and a package naming the build machine that nothing re-wraps: the image is refused
        r = self.put(self.card(self.old_kernel("6.6.98-a7apvr.202609091821", pvr=True)))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("still name the build machine (old-laptop): kernel-a7apvr", r.stdout)

    def test_only_its_packages_and_only_from_output_rpms(self):
        old = self.old_kernel("6.6.98-a7a.202609091931", pvr=False)
        elsewhere = self.d / old.name
        shutil.copy(old, elsewhere)
        r = self.rewrap(elsewhere)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not in", r.stdout)
        self.assertTrue(elsewhere.exists())
        r = run("build-tools-rpm.sh", {"A7A_OUTPUT": str(self.out), "A7A_REPO_KEY": str(self.d / "no-key")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        board = next((self.out / "rpms").glob("a7a-board-tools-*.rpm"))
        r = self.rewrap(board)
        self.assertEqual(r.returncode, 0)                                     # built as a7a-fedora: nothing to do
        self.assertIn("built as a7a-fedora already", r.stdout)

if __name__ == "__main__":
    unittest.main()
