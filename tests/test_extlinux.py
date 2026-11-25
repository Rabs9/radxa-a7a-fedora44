"""Tests for tools/a7a-extlinux: the extlinux.conf editor used on the board (add, fallback, list, prune)."""

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "a7a-extlinux"
_loader = importlib.machinery.SourceFileLoader("a7a_extlinux", str(SCRIPT))
ext = importlib.util.module_from_spec(importlib.util.spec_from_file_location("a7a_extlinux", str(SCRIPT), loader=_loader))
sys.modules["a7a_extlinux"] = ext          # dataclasses look the module up by name while the class is built
_loader.exec_module(ext)

CONF = """timeout 50
prompt 0
default fedora
menu title a7a-fedora

label fedora
    menu label Fedora 44 KDE, Linux 6.6.98+ (overclocked DTB)
    linux /boot/vmlinuz-6.6.98+
    fdtdir /usr/lib/linux-image-6.6.98+/
    append root=/dev/mmcblk0p3 rootwait rootfstype=ext4 console=ttyAS0,115200 loglevel=4 cma=128M selinux=0 systemd.unified_cgroup_hierarchy=1

label fedora-recovery
    menu label Fedora 44 KDE, Linux 6.6.98+, single user, no desktop
    linux /boot/vmlinuz-6.6.98+
    fdtdir /usr/lib/linux-image-6.6.98+/
    append root=/dev/mmcblk0p3 rootwait rootfstype=ext4 console=ttyAS0,115200 loglevel=7 cma=128M selinux=0 systemd.unit=multi-user.target
"""


class TestExtlinux(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / "extlinux.conf"
        self.conf.write_text(CONF)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_roundtrip(self):
        c = ext.parse(CONF)
        self.assertEqual(c.default, "fedora")
        self.assertEqual([e.label for e in c.entries], ["fedora", "fedora-recovery"])
        self.assertEqual(c.entries[0].fields["linux"], "/boot/vmlinuz-6.6.98+")
        self.assertEqual(ext.render(c), CONF)

    def test_add_new_kernel_as_default_keeps_previous_as_fallback(self):
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        labels = [e.label for e in c.entries]
        self.assertEqual(labels[:3], ["fedora-6.6.99+", "fedora", "fedora-recovery"])
        self.assertEqual(c.default, "fedora-6.6.99+")
        new = c.entries[0]
        self.assertEqual(new.fields["linux"], "/boot/vmlinuz-6.6.99+")
        self.assertEqual(new.fields["fdtdir"], "/usr/lib/linux-image-6.6.99+/")
        self.assertEqual(new.fields["append"], c.entries[1].fields["append"])
        self.assertIn("6.6.99+", new.fields["menu label"])
        # the recovery entry follows the new kernel too
        self.assertEqual(c.entries[2].fields["linux"], "/boot/vmlinuz-6.6.99+")

    def test_add_is_idempotent(self):
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertEqual([e.label for e in c.entries].count("fedora-6.6.99+"), 1)

    def test_fallback_switches_default_to_the_previous_kernel(self):
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        prev = ext.fallback(c)
        self.assertEqual(prev, "fedora")
        self.assertEqual(c.default, "fedora")
        self.assertEqual(c.entries[0].label, "fedora")               # the fallback becomes first (menu entry 1)
        self.assertEqual(c.entries[1].label, "fedora-6.6.99+")

    def test_prune_keeps_the_newest_kernels_and_the_recovery(self):
        c = ext.parse(CONF)
        for rel in ["6.6.99+", "6.6.100+", "6.6.101+"]:
            ext.add_kernel(c, rel, make_default=True)
        removed = ext.prune(c, keep=2)
        labels = [e.label for e in c.entries]
        self.assertEqual(labels, ["fedora-6.6.101+", "fedora-6.6.100+", "fedora-recovery"])
        self.assertEqual(sorted(removed), ["fedora", "fedora-6.6.99+"])
        self.assertEqual(c.default, "fedora-6.6.101+")

    def test_cli_add_and_list_and_fallback(self):
        rc, out = ext.main(["--conf", str(self.conf), "add", "6.6.99+", "--default"])
        self.assertEqual(rc, 0)
        self.assertIn("default fedora-6.6.99+", self.conf.read_text())
        rc, out = ext.main(["--conf", str(self.conf), "list"])
        self.assertEqual(rc, 0)
        self.assertIn("* fedora-6.6.99+", out)
        self.assertIn("  fedora ", out)
        rc, out = ext.main(["--conf", str(self.conf), "fallback"])
        self.assertEqual(rc, 0)
        self.assertIn("default fedora\n", self.conf.read_text())

    def test_cli_add_without_default_appends_second(self):
        rc, out = ext.main(["--conf", str(self.conf), "add", "6.6.99+"])
        c = ext.parse(self.conf.read_text())
        self.assertEqual(c.default, "fedora")
        self.assertEqual([e.label for e in c.entries][1], "fedora-6.6.99+")

    def test_the_image_builds_install_order_gives_the_menu_it_promises(self):
        # tools/assemble-image.sh relies on this: an entry added without --default lands at position 2. With earlier
        # kernels aboard it installs the earlier open one, then the earlier vendor one, then the image's open kernel.
        # A vendor-default image then reads: default, the other new kernel, earlier vendor, earlier open, recovery;
        # for an open-default image the last install is --default and the vendor kernel moves to 2
        template = CONF.replace("label fedora\n", "label fedora-NEWDDK\n").replace("default fedora\n", "default fedora-NEWDDK\n")
        for open_is_default in (False, True):
            c = ext.parse(template)
            ext.add_kernel(c, "OLDOPEN")
            ext.add_kernel(c, "OLDDDK")
            ext.add_kernel(c, "NEWOPEN", make_default=open_is_default)
            first_two = ["fedora-NEWOPEN", "fedora-NEWDDK"] if open_is_default else ["fedora-NEWDDK", "fedora-NEWOPEN"]
            self.assertEqual([e.label for e in c.entries], first_two + ["fedora-OLDDDK", "fedora-OLDOPEN", "fedora-recovery"])
            self.assertEqual(c.default, first_two[0])
            # recovery boots the default kernel: with earlier kernels aboard it is still not a proven one
            self.assertIn(first_two[0].replace("fedora-", "vmlinuz-") if open_is_default else "vmlinuz-6.6.98+", c.entries[-1].fields["linux"])

    def test_a_test_entry_boots_this_cards_kernel_with_another_root(self):
        # 2026-11-16: the owner has one SD card, and it holds the working system. The kernels have USB storage built in
        # and no initramfs is involved, so an image on a USB stick can be tried with the card's own kernel: one extra
        # entry with another root=, never the default, behind the kernels and in front of recovery
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        before = [e.label for e in c.entries]
        t = ext.add_test_entry(c, "usb", "6.6.98+", "PARTUUID=0A1B2C3D-0000-4000-8000-00000000AB03")
        self.assertEqual([e.label for e in c.entries], before[:-1] + ["test-usb", "fedora-recovery"])
        self.assertEqual(c.default, "fedora-6.6.99+")
        self.assertEqual(t.fields["linux"], "/boot/vmlinuz-6.6.98+")
        self.assertEqual(t.fields["fdtdir"], "/usr/lib/linux-image-6.6.98+/")
        self.assertIn("root=PARTUUID=0A1B2C3D-0000-4000-8000-00000000AB03 rootwait", t.fields["append"])
        self.assertNotIn("mmcblk0p3", t.fields["append"])
        self.assertIn("console=ttyAS0,115200", t.fields["append"])           # everything else as the card boots it
        self.assertIn("TEST", t.fields["menu label"])
        # the recovery entry and the other kernels are as they were
        self.assertIn("vmlinuz-6.6.99+", c.entries[-1].fields["linux"])
        self.assertIn("root=/dev/mmcblk0p3", c.entry("fedora").fields["append"])
        # again with another root: replaced, not doubled
        ext.add_test_entry(c, "usb", "6.6.98+", "/dev/sda3")
        self.assertEqual([e.label for e in c.entries].count("test-usb"), 1)
        self.assertIn("root=/dev/sda3 ", c.entry("test-usb").fields["append"])

    def test_a_test_entry_is_no_kernel_to_fall_back_to_or_to_prune(self):
        c = ext.parse(CONF)
        ext.add_test_entry(c, "usb", "6.6.98+", "/dev/sda3")
        self.assertIsNone(ext.fallback(c))                    # the only other entry is the test one: no rollback target
        ext.add_kernel(c, "6.6.99+", make_default=True)
        ext.add_kernel(c, "6.7.0+", make_default=True)
        self.assertEqual(ext.prune(c, keep=1), ["fedora-6.6.99+", "fedora"])
        self.assertEqual([e.label for e in c.entries], ["fedora-6.7.0+", "test-usb", "fedora-recovery"])
        self.assertEqual(ext.remove_entry(c, "test-usb"), "test-usb")

    def test_cli_add_test_checks_what_the_kernel_can_find_without_an_initramfs(self):
        rc, out = ext.main(["--conf", str(self.conf), "add-test", "usb", "6.6.98+", "PARTUUID=0a1b2c3d-0000-4000-8000-00000000ab03"])
        self.assertEqual(rc, 0, out)
        self.assertIn("label test-usb", self.conf.read_text())
        self.assertIn("default fedora\n", self.conf.read_text())
        for bad_root in ("UUID=0a1b2c3d-0000-4000-8000-00000000ab04", "LABEL=rootfs", "sda3", "PARTUUID=nonsense", "/dev/sda3 init=/bin/sh"):
            rc, out = ext.main(["--conf", str(self.conf), "add-test", "usb2", "6.6.98+", bad_root])
            self.assertEqual(rc, 1, bad_root)
        self.assertEqual(ext.main(["--conf", str(self.conf), "add-test", "Bad Name", "6.6.98+", "/dev/sda3"])[0], 1)
        self.assertNotIn("usb2", self.conf.read_text())
        # with --check-files the kernel must be on THIS disk: U-Boot loads it from here
        root = Path(self.tmp.name) / "root"
        (root / "boot").mkdir(parents=True)
        rc, out = ext.main(["--conf", str(self.conf), "--root", str(root), "--check-files", "add-test", "usb3", "6.6.98+", "/dev/sda3"])
        self.assertEqual(rc, 1)
        self.assertIn("missing", out)

    def test_a_standard_system_stays_standard_through_a_kernel_update(self):
        # the standard image boots the device trees with the vendor's clocks, kept beside the package's (overclocked)
        # ones in /usr/lib/linux-image-<release>-standard/. The entry of the next kernel is modelled on the default one
        c = ext.parse(CONF.replace("fdtdir /usr/lib/linux-image-6.6.98+/", "fdtdir /usr/lib/linux-image-6.6.98+-standard/"))
        self.assertIn("-standard/", c.entry("fedora").fields["fdtdir"])
        new = ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertEqual(new.fields["fdtdir"], "/usr/lib/linux-image-6.6.99+-standard/")
        self.assertEqual(c.entry("fedora-recovery").fields["fdtdir"], "/usr/lib/linux-image-6.6.99+-standard/")
        t = ext.add_test_entry(c, "usb", "6.6.98+", "/dev/sda3")
        self.assertEqual(t.fields["fdtdir"], "/usr/lib/linux-image-6.6.98+-standard/")
        # and the overclocked image stays what it is
        c = ext.parse(CONF)
        self.assertEqual(ext.add_kernel(c, "6.6.99+", make_default=True).fields["fdtdir"], "/usr/lib/linux-image-6.6.99+/")

    def test_a_kernel_without_its_standard_device_tree_is_refused_not_booted_overclocked(self):
        # what the kernel packages run: --check-files add <release> --default
        root = Path(self.tmp.name) / "root"
        for d in ("boot", "usr/lib/linux-image-6.6.99+", "usr/lib/linux-image-6.6.98+-standard"):
            (root / d).mkdir(parents=True)
        (root / "boot/vmlinuz-6.6.99+").write_text("k")
        self.conf.write_text(self.conf.read_text().replace("fdtdir /usr/lib/linux-image-6.6.98+/", "fdtdir /usr/lib/linux-image-6.6.98+-standard/"))
        before = self.conf.read_text()
        self.assertIn("-standard/", before)
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "add", "6.6.99+", "--default"])
        self.assertEqual(rc, 1, out)
        self.assertIn("linux-image-6.6.99+-standard", out)
        self.assertEqual(self.conf.read_text(), before)
        (root / "usr/lib/linux-image-6.6.99+-standard").mkdir()
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "add", "6.6.99+", "--default"])
        self.assertEqual(rc, 0, out)

    def test_nothing_that_is_typed_can_start_another_line_of_the_boot_menu(self):
        # the values go into extlinux.conf as they are: "$" in a pattern lets a trailing newline through, and the release
        # was not looked at at all (reviewer pass 3, 2026-11-16)
        before = self.conf.read_text()
        for argv in (["add-test", "usb\n", "6.6.98+", "/dev/sda3"],
                     ["add-test", "usb", "6.6.98+", "/dev/sda3\n"],
                     ["add-test", "usb", "6.6.98+\n    append root=/dev/sda3 init=/bin/sh", "/dev/sda3"],
                     ["add-test", "usb", "6.6.98+ x", "/dev/sda3"],
                     ["add-test", "usb", "../../etc", "/dev/sda3"],
                     ["add", "6.6.99+\nlabel x"],
                     ["add", "6.6.99+ init=/bin/sh", "--default"],
                     ["add", "", "--default"]):
            rc, out = ext.main(["--conf", str(self.conf)] + argv)
            self.assertEqual(rc, 1, argv)
            self.assertEqual(self.conf.read_text(), before, argv)

    def test_a_test_entry_is_never_the_default_and_never_the_model_for_a_kernel(self):
        ext.main(["--conf", str(self.conf), "add-test", "usb", "6.6.98+", "/dev/sda3"])
        rc, out = ext.main(["--conf", str(self.conf), "default", "test-usb"])
        self.assertEqual(rc, 1)
        self.assertIn("test entry", out)
        self.assertIn("default fedora\n", self.conf.read_text())
        # a menu edited by hand to boot the test entry: the next kernel's entry still boots THIS card's system
        c = ext.parse(self.conf.read_text())
        c.default = "test-usb"
        new = ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertIn("root=/dev/mmcblk0p3", new.fields["append"])
        self.assertNotIn("sda3", new.fields["append"])

    def test_a_test_entry_named_like_recovery_is_not_a_recovery_entry(self):
        # recovery entries follow the default kernel (add --default, fallback, remove rewrite their linux/fdtdir): a test
        # entry whose name ends that way must keep the kernel it was given, and the real recovery entry stays the last
        c = ext.parse(CONF)
        ext.add_test_entry(c, "usb-recovery", "6.6.98+", "/dev/sda3")
        self.assertEqual([e.label for e in c.entries][-2:], ["test-usb-recovery", "fedora-recovery"])
        ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertEqual(c.entry("test-usb-recovery").fields["linux"], "/boot/vmlinuz-6.6.98+")
        self.assertIn("vmlinuz-6.6.99+", c.entry("fedora-recovery").fields["linux"])
        self.assertNotIn(c.entry("test-usb-recovery"), ext.kernel_entries(c))

    def test_cli_refuses_missing_kernel_when_asked_to_check(self):
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", self.tmp.name, "add", "6.6.99+", "--default"])
        self.assertEqual(rc, 1)
        self.assertIn("vmlinuz-6.6.99+", out)

    def test_check_files_judges_only_the_entry_being_touched(self):
        # a stale hand-made entry (kernel deleted long ago) must not block a kernel package's %post, which adds
        # ANOTHER entry whose files exist; but a fallback onto the stale entry is refused
        root = Path(self.tmp.name)
        for rel in ["6.6.99+"]:
            (root / "boot").mkdir(exist_ok=True)
            (root / "boot" / f"vmlinuz-{rel}").write_bytes(b"k")
            (root / "usr/lib" / f"linux-image-{rel}").mkdir(parents=True)
        self.conf.write_text(CONF + "\nlabel g_1008_800\n    menu label old\n    linux /boot/vmlinuz-gone\n    fdtdir /usr/lib/linux-image-gone/\n    append root=/dev/mmcblk0p3\n")
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "add", "6.6.99+", "--default"])
        self.assertEqual(rc, 0, out)
        self.assertIn("default fedora-6.6.99+", self.conf.read_text())
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "default", "g_1008_800"])
        self.assertEqual(rc, 1)
        self.assertIn("vmlinuz-gone", out)
        self.assertIn("default fedora-6.6.99+", self.conf.read_text())
        # fallback with --check-files: "fedora" (6.6.98+) and g_1008_800 are both missing under root -> refused, untouched
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "fallback"])
        self.assertEqual(rc, 1)
        self.assertIn("no previous kernel entry", out)
        self.assertIn("default fedora-6.6.99+", self.conf.read_text())
        # ... and it walks past a dead entry to the next BOOTABLE one (the kernel packages' %postun after an erase)
        (root / "boot" / "vmlinuz-6.6.98+").write_bytes(b"k")
        (root / "usr/lib" / "linux-image-6.6.98+").mkdir(parents=True)
        ext.main(["--conf", str(self.conf), "add", "6.6.97+", "--default"])       # a new default whose files do not exist
        c = ext.parse(self.conf.read_text())
        self.assertEqual([e.label for e in c.entries][:2], ["fedora-6.6.97+", "fedora-6.6.99+"])
        c.entries.insert(1, ext.Entry("dead", {"linux": "/boot/vmlinuz-dead", "fdtdir": "/usr/lib/linux-image-dead/", "append": "x"}))
        self.conf.write_text(ext.render(c))
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "fallback"])
        self.assertEqual(rc, 0, out)
        self.assertIn("default fedora-6.6.99+", self.conf.read_text())       # "dead" (entry 2) skipped
        self.assertEqual(ext.parse(self.conf.read_text()).entry("fedora-recovery").fields["linux"], "/boot/vmlinuz-6.6.99+")
        # remove needs no file check at all
        rc, out = ext.main(["--conf", str(self.conf), "--check-files", "--root", str(root), "remove", "g_1008_800"])
        self.assertEqual(rc, 0, out)

    def test_set_default(self):
        rc, out = ext.main(["--conf", str(self.conf), "default", "fedora-recovery"])
        self.assertEqual(rc, 0)
        self.assertIn("default fedora-recovery", self.conf.read_text())
        rc, out = ext.main(["--conf", str(self.conf), "default", "nope"])
        self.assertEqual(rc, 1)

    def test_remove_drops_one_entry_and_keeps_the_rest(self):
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertEqual(ext.remove_entry(c, "fedora"), "fedora")
        self.assertEqual([e.label for e in c.entries], ["fedora-6.6.99+", "fedora-recovery"])
        self.assertEqual(c.default, "fedora-6.6.99+")

    def test_remove_repoints_a_recovery_entry_that_booted_the_removed_kernel(self):
        # the kernel packages' %postun: after `fallback` made 6.6.98+ the default again, removing the erased kernel's
        # entry must not leave fedora-recovery on the erased kernel
        c = ext.parse(CONF)
        ext.add_kernel(c, "6.6.99+", make_default=True)
        self.assertEqual(c.entry("fedora-recovery").fields["linux"], "/boot/vmlinuz-6.6.99+")
        ext.fallback(c)
        self.assertEqual(c.entry("fedora-recovery").fields["linux"], "/boot/vmlinuz-6.6.98+")      # fallback re-points it too
        c.entry("fedora-recovery").fields["linux"] = "/boot/vmlinuz-6.6.99+"                        # as an older a7a-extlinux left it
        ext.remove_entry(c, "fedora-6.6.99+")
        rec = c.entry("fedora-recovery")
        self.assertEqual(rec.fields["linux"], "/boot/vmlinuz-6.6.98+")
        self.assertEqual(rec.fields["fdtdir"], "/usr/lib/linux-image-6.6.98+/")
        self.assertIn("Linux 6.6.98+,", rec.fields["menu label"])

    def test_remove_refuses_the_default_entry(self):
        c = ext.parse(CONF)
        with self.assertRaises(ValueError):
            ext.remove_entry(c, "fedora")
        self.assertEqual([e.label for e in c.entries], ["fedora", "fedora-recovery"])

    def test_cli_remove(self):
        # the kernel packages call this from %postun: an absent label is not an error (exit 0), the default is refused
        rc, out = ext.main(["--conf", str(self.conf), "add", "6.6.99+", "--default"])
        rc, out = ext.main(["--conf", str(self.conf), "remove", "fedora"])
        self.assertEqual(rc, 0)
        self.assertIn("removed fedora", out)
        self.assertNotIn("label fedora\n", self.conf.read_text())
        rc, out = ext.main(["--conf", str(self.conf), "remove", "fedora"])
        self.assertEqual(rc, 0)
        self.assertIn("no entry fedora", out)
        rc, out = ext.main(["--conf", str(self.conf), "remove", "fedora-6.6.99+"])
        self.assertEqual(rc, 1)
        self.assertIn("default", out)
        self.assertIn("label fedora-6.6.99+", self.conf.read_text())


if __name__ == "__main__":
    unittest.main()
