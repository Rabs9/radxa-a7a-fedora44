"""Tests for the image stage: tools/assemble-image.sh, then the image registry and the release note."""

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import image


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


FAKE_ASSEMBLE = r'''
IMG="$A7A_OUTPUT/a7a-fedora-44-kde-20260906.img"
truncate -s 8M "$IMG"; printf 'firmware' | dd of="$IMG" conv=notrunc status=none
sha256sum "$IMG" > "$IMG.sha256"
echo "6.6.98-a7apvr.202609091821" > "$IMG.kernel"
ls "$A7A_OUTPUT/rpms" > "$IMG.rpms"
echo "== [00:00:00] done: $IMG (8M apparent, 4K on disk)"
'''
RPMS = ["a7a-extlinux-20260909-202609092000.noarch.rpm", "a7a-board-tools-20260909-202609092000.noarch.rpm",
        "a7a-gpu-tools-20260909-202609092000.noarch.rpm",
        "mesa-pvr-a7a-26.1.8-202609092030.aarch64.rpm", "mesa-img-a7a-24.0.1-202609101747.aarch64.rpm",
        "kernel-a7a-6.6.98_a7a.202609091931-202609092000.aarch64.rpm",
        "kernel-a7apvr-6.6.98_a7apvr.202609091821-202609092000.aarch64.rpm"]


class TestKernelSelection(unittest.TestCase):
    """newest(), previous() and ofrelease() as tools/assemble-image.sh defines them (the lines are taken from the
    script), run in bash against a scratch output/rpms; rpm is faked and answers kernel-uname-r from the file name."""

    FILES = ["kernel-a7a-6.6.98_a7a.202609072057-202609072100.aarch64.rpm",       # the first packaged kernel
             "kernel-a7a-6.6.98_a7a.202609091931-202609092000.aarch64.rpm",       # the release the board runs ...
             "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm",       # ... and its rebuild (the installed one)
             "kernel-a7a-6.6.98_a7a.202611151729-202611151731.aarch64.rpm"]       # today's

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "out" / "rpms").mkdir(parents=True)
        for f in self.FILES:
            (root / "out" / "rpms" / f).write_bytes(b"rpm")
        _stub(root / "bin" / "rpm", r'''f=$(basename "${@: -1}"); r=${f#kernel-a7a-}; r=${r%%-*}; echo "kernel-uname-r = ${r/_/-}"''' + "\n")
        script = (Path(__file__).resolve().parent.parent / "tools" / "assemble-image.sh").read_text()
        self.defs = "\n".join(l for l in script.splitlines() if l.startswith(("newest() {", "relof() {", "previous() {", "ofrelease() {")))
        self.assertEqual(self.defs.count("\n"), 3, "the four selection functions are one line each in assemble-image.sh")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def sh(self, call: str) -> str:
        import subprocess
        env = dict(os.environ, PATH=f"{self.root / 'bin'}:{os.environ['PATH']}", OUT=str(self.root / "out"))
        r = subprocess.run(["bash", "-c", self.defs + "\n" + call], capture_output=True, text=True, env=env, timeout=30)
        return os.path.basename(r.stdout.strip())

    def test_newest_is_the_newest_build(self):
        self.assertEqual(self.sh("newest 'kernel-a7a-*.aarch64.rpm'"), self.FILES[3])

    def test_previous_is_the_newest_build_of_another_release(self):
        # not the older build of the same release as "previous", not the oldest release, not today's own rebuild
        self.assertEqual(self.sh("previous 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.202611151729"), self.FILES[2])
        (self.root / "out" / "rpms" / "kernel-a7a-6.6.98_a7a.202611151729-202611160010.aarch64.rpm").write_bytes(b"rpm")
        self.assertEqual(self.sh("previous 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.202611151729"), self.FILES[2])

    def test_previous_drifts_with_the_next_kernel_build_and_a_named_release_does_not(self):
        # the reason releases can be named: after one more kernel build "previous" is today's kernel, proven or not
        (self.root / "out" / "rpms" / "kernel-a7a-6.6.98_a7a.202612010900-202612010905.aarch64.rpm").write_bytes(b"rpm")
        self.assertEqual(self.sh("previous 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.202612010900"), self.FILES[3])
        self.assertEqual(self.sh("ofrelease 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.202609091931"), self.FILES[2])

    def test_nothing_earlier_and_an_unknown_release_give_nothing(self):
        for f in self.FILES[:3]:
            (self.root / "out" / "rpms" / f).unlink()
        self.assertEqual(self.sh("previous 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.202611151729"), "")
        self.assertEqual(self.sh("ofrelease 'kernel-a7a-*.aarch64.rpm' 6.6.98-a7a.209901010000"), "")


class TestImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.sudo_prefix = []
        self.reg = StageRegistry(self.cfg.registry_path)
        self.cfg.output_dir.mkdir()
        self.cfg.fedora_raw.write_bytes(b"\0" * 64)
        self.cfg.firmware_region.write_bytes(b"\0" * 64)
        pay = self.cfg.payload_dir; (pay / "boot").mkdir(parents=True)
        (pay / "kernelrelease").write_text("6.6.98+\n")
        (self.cfg.output_dir / "rpms").mkdir()
        for r in RPMS + ["kernel-a7apvr-6.6.98_a7apvr.202609091531-202609091600.aarch64.rpm"]:   # an older pvr kernel too
            (self.cfg.output_dir / "rpms" / r).write_bytes(b"rpm")
        _stub(self.cfg.tools_dir / "assemble-image.sh", FAKE_ASSEMBLE)
        _stub(self.cfg.tools_dir / "priv.sh", 'exit 2\n')      # a bare call answers 2, as the real helper does
        self.cfg.work_dir.mkdir()
        self.cfg.manifest_path.write_text(json.dumps({"mirror": {"commit": "4afcb3e" + "0" * 33},
                                                       "trees": {"kernel-6.6": {"commit": "61f3fe2" + "0" * 33, "branch": "b"}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_assemble_registers_the_image_and_writes_a_release_note(self):
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["image"], "a7a-fedora-44-kde-20260906.img")
        img = self.reg.images()["a7a-fedora-44-kde-20260906.img"]
        self.assertEqual(img["kernel_release"], "6.6.98-a7apvr.202609091821")     # from the .kernel sidecar, not the payload
        self.assertEqual(len(img["rpms"]), 8)      # the fake assemble lists every file in output/rpms (seven packages + the older pvr kernel)
        self.assertEqual(img["size"], 8 * 1024 * 1024)
        self.assertEqual(img["sha256"], hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest())
        self.assertEqual(img["kernel_commit"][:7], "61f3fe2")
        note = Path(r["release_note"]).read_text()
        self.assertIn("6.6.98-a7apvr.202609091821", note)
        self.assertIn("mesa-pvr-a7a-26.1.8-202609092030.aarch64.rpm", note)
        self.assertIn("61f3fe2", note)
        self.assertIn("not yet tested", note)
        self.assertEqual(self.reg.get_stage("image")["status"], "passed")

    def test_default_stack_reaches_the_script(self):
        # which kernel the image boots by default: the open stack (as every image so far) or the vendor DDK
        _stub(self.cfg.tools_dir / "assemble-image.sh",
              'echo "default=${A7A_IMAGE_DEFAULT:-unset}" > "$A7A_OUTPUT/seen-default"\n' + FAKE_ASSEMBLE)
        r = image.run(self.cfg, self.reg, default_stack="ddk")
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual((self.cfg.output_dir / "seen-default").read_text().strip(), "default=ddk")
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual((self.cfg.output_dir / "seen-default").read_text().strip(), "default=open")
        with self.assertRaises(ValueError):
            image.run(self.cfg, self.reg, default_stack="fast")

    def test_release_note_names_the_default_stack_and_the_keys(self):
        # the script records what it built in a .build sidecar; the note must not call a ddk image "open stack by default"
        _stub(self.cfg.tools_dir / "assemble-image.sh",
              FAKE_ASSEMBLE + 'printf "default_stack=ddk\\nssh_keys=0\\n" > "$IMG.build"\n')
        r = image.run(self.cfg, self.reg, default_stack="ddk", keys=False)
        self.assertEqual(r["status"], "passed", r)
        img = self.reg.images()["a7a-fedora-44-kde-20260906.img"]
        self.assertEqual(img["default_stack"], "ddk")
        self.assertEqual(img["ssh_keys"], 0)
        note = Path(r["release_note"]).read_text()
        self.assertIn("vendor DDK by default", note)
        self.assertNotIn("open stack by default", note)
        self.assertIn("no ssh keys", note)
        # an image without the sidecar (every image before 2026-11-15) is an open-stack image with the owner's keys
        _stub(self.cfg.tools_dir / "assemble-image.sh", 'rm -f "$A7A_OUTPUT"/*.build\n' + FAKE_ASSEMBLE)
        r = image.run(self.cfg, self.reg)
        note = Path(r["release_note"]).read_text()
        self.assertIn("open stack by default", note)
        self.assertEqual(self.reg.images()["a7a-fedora-44-kde-20260906.img"]["default_stack"], "open")

    def test_keys_can_be_left_out_of_the_image(self):
        # the owner's ssh public keys go into the image unless it is built for other people's boards
        _stub(self.cfg.tools_dir / "assemble-image.sh",
              'echo "keys=${A7A_IMAGE_KEYS:-unset}" > "$A7A_OUTPUT/seen-keys"\n' + FAKE_ASSEMBLE)
        image.run(self.cfg, self.reg)
        self.assertEqual((self.cfg.output_dir / "seen-keys").read_text().strip(), "keys=1")
        image.run(self.cfg, self.reg, keys=False)
        self.assertEqual((self.cfg.output_dir / "seen-keys").read_text().strip(), "keys=0")

    def test_previous_kernels_can_ride_along_as_extra_boot_entries(self):
        # 2026-11-15: both kernels of the image were rebuilt that day and had never run on the board, and the owner has
        # one card. The previous release of each flavour (the ones the board runs) as entries 3 and 4 is the way back;
        # only on request, and the note says so (a release image has no use for them)
        _stub(self.cfg.tools_dir / "assemble-image.sh",
              'echo "previous=${A7A_IMAGE_PREVIOUS_KERNELS:-unset}" > "$A7A_OUTPUT/seen-previous"\n' + FAKE_ASSEMBLE +
              '[ "${A7A_IMAGE_PREVIOUS_KERNELS:-0}" = 1 ] && printf "default_stack=ddk\\nssh_keys=2\\nprevious_kernels=6.6.98-a7a.202609091931,6.6.98-a7apvr.202609091821\\n" > "$IMG.build" || rm -f "$IMG.build"\n')
        r = image.run(self.cfg, self.reg)
        self.assertEqual((self.cfg.output_dir / "seen-previous").read_text().strip(), "previous=0")
        self.assertNotIn("entries 3 and 4", Path(r["release_note"]).read_text())
        self.assertEqual(self.reg.images()["a7a-fedora-44-kde-20260906.img"]["previous_kernels"], [])
        r = image.run(self.cfg, self.reg, default_stack="ddk", previous_kernels=True)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual((self.cfg.output_dir / "seen-previous").read_text().strip(), "previous=1")
        self.assertEqual(self.reg.images()["a7a-fedora-44-kde-20260906.img"]["previous_kernels"],
                         ["6.6.98-a7a.202609091931", "6.6.98-a7apvr.202609091821"])
        note = Path(r["release_note"]).read_text()
        self.assertIn("entries 3 and 4", note)
        self.assertIn("6.6.98-a7a.202609091931", note)
        self.assertIn("not for publication", note)
        # "previous" drifts with every kernel build: the releases can be named (vendor first, then open)
        image.run(self.cfg, self.reg, previous_kernels="6.6.98-a7a.202609091931,6.6.98-a7apvr.202609091821")
        self.assertEqual((self.cfg.output_dir / "seen-previous").read_text().strip(),
                         "previous=6.6.98-a7a.202609091931,6.6.98-a7apvr.202609091821")

    def test_fedoras_updates_can_be_applied_at_build_time(self):
        # 2026-11-15: Fedora's raw image is the release as of April (kwin 6.6, Mesa 26.0); the board has only ever run
        # the GPU desktop on an updated system. On request the build runs `dnf upgrade` inside the image, and the
        # registry and the note say as of which day (an image is then no longer a function of the repo alone)
        _stub(self.cfg.tools_dir / "assemble-image.sh",
              'echo "update=${A7A_IMAGE_UPDATE:-unset}" > "$A7A_OUTPUT/seen-update"\n' + FAKE_ASSEMBLE +
              '[ "${A7A_IMAGE_UPDATE:-0}" = 1 ] && printf "default_stack=open\\nssh_keys=2\\nupdated=2026-11-16\\n" > "$IMG.build" || rm -f "$IMG.build"\n')
        r = image.run(self.cfg, self.reg)
        self.assertEqual((self.cfg.output_dir / "seen-update").read_text().strip(), "update=0")
        self.assertIsNone(self.reg.images()["a7a-fedora-44-kde-20260906.img"]["updated"])
        self.assertIn("as Fedora published it", Path(r["release_note"]).read_text())
        r = image.run(self.cfg, self.reg, update=True)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual((self.cfg.output_dir / "seen-update").read_text().strip(), "update=1")
        self.assertEqual(self.reg.images()["a7a-fedora-44-kde-20260906.img"]["updated"], "2026-11-16")
        self.assertIn("updates as of 2026-11-16", Path(r["release_note"]).read_text())

    def test_compress_makes_xz_with_its_own_sha256(self):
        r = image.run(self.cfg, self.reg, compress=True)
        self.assertEqual(r["status"], "passed", r)
        xz = Path(r["path"] + ".xz")
        self.assertTrue(xz.exists())
        self.assertTrue(Path(str(xz) + ".sha256").exists())
        self.assertEqual(self.reg.images()[r["image"]]["xz_sha256"], hashlib.sha256(xz.read_bytes()).hexdigest())

    def test_missing_prerequisites_name_the_stage_to_run(self):
        self.cfg.fedora_raw.unlink()
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("rootfs", r["reason"])
        self.cfg.fedora_raw.write_bytes(b"\0")
        for f in (self.cfg.output_dir / "rpms").glob("mesa-pvr-a7a-*"):
            f.unlink()
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("build-mesa-rpm.sh", r["reason"])
        self.assertNotIn("kernel-a7apvr", r["reason"])
        # the DDK's Mesa with Wayland (docs/DDK-WAYLAND-2026-09-10.md) is part of the set too
        for f in (self.cfg.output_dir / "rpms").glob("mesa-img-a7a-*"):
            f.unlink()
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("build-mesa-img-rpm.sh", r["reason"])

    def test_image_rpms_picks_the_newest_build_of_each_package(self):
        # by the Release stamp (fixed width), not the Version: a 6.6.100 kernel built later must win over 6.6.98
        (self.cfg.output_dir / "rpms" / "kernel-a7apvr-6.6.100_a7apvr.202610010101-202610010102.aarch64.rpm").write_bytes(b"rpm")
        (self.cfg.output_dir / "rpms" / "mesa-pvr-a7a-26.1.10-202610010500.aarch64.rpm").write_bytes(b"rpm")
        picked = image.image_rpms(self.cfg)
        self.assertEqual(picked["kernel-a7apvr"].name, "kernel-a7apvr-6.6.100_a7apvr.202610010101-202610010102.aarch64.rpm")
        self.assertEqual(picked["mesa-pvr-a7a"].name, "mesa-pvr-a7a-26.1.10-202610010500.aarch64.rpm")
        for f in list((self.cfg.output_dir / "rpms").glob("*2026100*")):
            f.unlink()
        picked = image.image_rpms(self.cfg)
        self.assertEqual(picked["kernel-a7apvr"].name, "kernel-a7apvr-6.6.98_a7apvr.202609091821-202609092000.aarch64.rpm")
        self.assertEqual(picked["kernel-a7a"].name, "kernel-a7a-6.6.98_a7a.202609091931-202609092000.aarch64.rpm")
        self.assertEqual(sorted(picked), ["a7a-board-tools", "a7a-extlinux", "a7a-gpu-tools", "kernel-a7a", "kernel-a7apvr",
                                          "mesa-img-a7a", "mesa-pvr-a7a"])
        self.assertTrue(all(picked.values()))

    def test_no_sudo_means_dry_run_with_the_reason(self):
        _stub(self.cfg.tools_dir / "priv.sh", 'echo "sudo: a password is required" >&2; exit 1\n')
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "dry_run")
        self.assertIn("sudo", r["reason"])
        self.assertFalse(list(self.cfg.output_dir.glob("a7a-fedora-*.img")))

    def test_script_failure(self):
        _stub(self.cfg.tools_dir / "assemble-image.sh", 'echo boom; exit 1\n')
        r = image.run(self.cfg, self.reg)
        self.assertEqual(r["status"], "failed")
        self.assertIn("assemble-image.sh", r["reason"])

    def test_dry_run(self):
        r = image.run(self.cfg, self.reg, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertIn("assemble-image.sh", r["commands"][0])

    def test_register_existing_image(self):
        img = self.cfg.output_dir / "a7a-fedora-44-kde-20260905.img"
        img.write_bytes(b"\0" * 1024)
        info = image.register_existing(self.cfg, self.reg, img)
        self.assertEqual(info["size"], 1024)
        self.assertEqual(self.reg.images()[img.name]["kernel_release"], "6.6.98+")
        self.assertEqual(len(self.reg.images()[img.name]["sha256"]), 64)

    def test_release_note_lists_tests_after_they_exist(self):
        r = image.run(self.cfg, self.reg)
        self.reg.record_test(r["image"], mode="kernel", verdict="PASS", log="/l", milestones=["login:"])
        note = image.write_release_note(self.cfg, self.reg, r["image"])
        self.assertIn("kernel: PASS", note.read_text())


class TestAssembleScriptKeepsTheBuildMachineOut(unittest.TestCase):
    """What the script must keep doing, read from its text (it needs root, loop devices and an hour to run for real).
    2026-11-16: grub2's package script ran os-prober inside the image, with the build machine's disks in reach."""

    def setUp(self):
        self.script = (Path(__file__).resolve().parent.parent / "tools" / "assemble-image.sh").read_text()
        self.code = "\n".join(l for l in self.script.splitlines() if not l.lstrip().startswith("#"))

    def test_fedoras_image_is_attached_read_only_and_let_go_after_the_copy(self):
        attach = [l for l in self.code.splitlines() if 'loop-attach "$FEDORA_RAW"' in l]
        self.assertEqual(len(attach), 1)
        self.assertRegex(attach[0], r'loop-attach "\$FEDORA_RAW" \$\(\(FSTART \* 512\)\)')       # with an offset = priv.sh's read-only form
        self.assertNotIn("${FLOOP}p", self.code)
        copy_done, update = self.code.index('$PRIV loop-detach "$FLOOP"; FLOOP=""'), self.code.index("dnf upgrade")
        self.assertLess(copy_done, update)                 # no package script runs while it is attached
        self.assertLess(copy_done, self.code.index("a7a-fedora packages"))

    def test_no_package_script_probes_for_other_systems(self):
        self.assertIn("GRUB_DISABLE_OS_PROBER=true", self.code)
        self.assertLess(self.code.index("GRUB_DISABLE_OS_PROBER=true"), self.code.index("dnf upgrade"))
        for gone in ("/boot/grub2/grub.cfg", "/var/lib/os-prober", "/var/lib/systemd/random-seed"):
            self.assertIn(gone, self.code)
        # image commands go through the helper's sandbox, never a chroot of the script's own
        self.assertNotRegex(self.code, r"(^|[;&|]\s*|sudo\s+)chroot\s")

    def test_the_superblock_does_not_name_the_build_machine(self):
        close = self.code[self.code.index("$PRIV umount root; trap - EXIT"):]
        self.assertIn('ssv last_mounted /', close)
        self.assertLess(close.index("ssv last_mounted /"), close.index('sha256sum "$IMG"'))       # the checksum is of the final bytes


if __name__ == "__main__":
    unittest.main()
