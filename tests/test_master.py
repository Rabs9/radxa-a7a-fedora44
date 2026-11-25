"""Tests for the image made from a card that has been in use (tools/master-image.sh): what must not travel.
tools/master-filter.rules is run here with the real rsync over a model of the working card, as an ordinary user:
the rule file is the whole privacy decision, and a path it excludes is never read into the new filesystem."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
RULES = P / "tools" / "master-filter.rules"

# what the owner's card holds that is his, or this one machine's: none of it may arrive
GONE = [
    "home/radxa/.ssh/authorized_keys", "home/radxa/.bash_history", "home/radxa/.config/mozilla/firefox/x.default/logins.json",
    "home/radxa/.local/share/kwalletd/kdewallet.kwl", "home/radxa/.local/share/klipper/history2.lst",
    "home/radxa/.config/kdeconnect/privateKey.pem", "home/radxa/gluetest/ff/shot.png", "home/radxa/Documents/letter.odt",
    "root/.bash_history", "root/.ssh/id_ed25519", "root/.cache/x",
    "etc/machine-id", "var/lib/dbus/machine-id", "etc/ssh/ssh_host_ed25519_key", "etc/ssh/ssh_host_ed25519_key.pub",
    "etc/ssh/ssh_host_rsa_key", "etc/NetworkManager/system-connections/TheNet.nmconnection", "etc/resolv.conf",
    "etc/cups/printers.conf", "etc/cups/printers.conf.O", "etc/cups/subscriptions.conf", "etc/cups/ssl/host.key",
    "etc/cups/ppd/Brother.ppd", "etc/iscsi/initiatorname.iscsi", "etc/ipsec.d/key4.db", "etc/ipsec.d/cert9.db",
    "etc/passwd-", "etc/shadow-", "etc/systemd/system.control/user@1000.service.d/50-CPUWeight.conf",
    "etc/pki/tls/private/localhost.key", "etc/wireguard/wg0.conf", "etc/lvm/devices/system.devices",
    "var/lib/systemd/random-seed", "var/lib/systemd/credential.secret", "var/lib/a7a/rootfs-grown",
    "boot/extlinux/extlinux.conf.prev", "boot/extlinux/extlinux.conf.bak-20260907-rpm",
    "var/log/messages", "var/log/secure", "var/log/a7a-firstboot.log", "var/log/audit/audit.log",
    "var/log/journal/5e1f0c0ffee0a7a0f00d0123456789ab/system.journal",
    "var/cache/libdnf5/fedora-0123abcd/packages/kwin.rpm", "var/cache/man/index.db", "var/cache/cups/job.cache",
    "var/cache/PackageKit/44/metadata/updates/repodata/repomd.xml",
    "var/tmp/a7a-rpms/x.rpm", "var/spool/cups/c00001", "var/spool/anacron/cron.daily", "var/spool/cron/radxa",
    "var/spool/abrt/ccpp-2026-11-15-20:44:26.1-680351/coredump", "var/db/sudo/lectured/1000",
    "var/lib/plasmalogin/.config/kwinrc", "var/lib/plasmalogin/.cache/thumb.png",
    "var/lib/NetworkManager/secret_key", "var/lib/NetworkManager/internal-1234.lease", "var/lib/NetworkManager/seen-bssids",
    "var/lib/bluetooth/AA:BB:CC:DD:EE:FF/11:22:33:44:55:66/info", "var/lib/AccountsService/users/radxa",
    "var/lib/upower/history-charge-MX_Master-1234.dat", "var/lib/smartmontools/smartd.Samsung-S7U6.ata.state",
    "var/lib/systemd/coredump/core.kwin_wayland.985.zst", "var/lib/systemd/backlight/platform-backlight",
    "var/lib/systemd/timesync/clock", "var/lib/systemd/linger/radxa", "var/lib/samba/private/secrets.tdb",
    "var/lib/sss/db/cache_implicit_files.ldb", "var/lib/fwupd/pending.db", "var/lib/colord/mapping.db",
    "var/lib/lastlog/lastlog2.db", "var/lib/rsyslog/imjournal.state", "var/lib/udisks2/mounted-fs",
    "var/lib/private/systemd/journal-upload/state", "var/lib/chrony/drift", "var/lib/geoclue/x", "var/lib/abrt/abrt-db",
    "imjournal.state", "tmp/x", "run/user/1000/bus", "mnt/usb/photo.jpg", "media/sam/stick/a", "srv/www/index.html",
    "proc/1/cmdline", "sys/class/x", "dev/sda",
    # found by the review of 2026-11-16 in the first image: dnf5's history of every command (not under /var/lib/dnf),
    # GRUB files with the card's filesystem UUID, a trust anchor stamped in the owner's time zone
    "usr/lib/sysimage/libdnf5/transaction_history.sqlite", "usr/lib/sysimage/libdnf5/transaction_history.sqlite-wal",
    "usr/lib/sysimage/libdnf5/transaction_history.sqlite-shm", "usr/lib/sysimage/libdnf5/offline/transaction.json",
    "boot/grub2/grub.cfg", "boot/grub2/grubenv", "usr/lib/efi/grub2/1:2.12-64.fc44/EFI/fedora/grub.cfg",
    "var/lib/unbound/root.key", "var/lib/plymouth/boot-duration",
    # absent on that card by luck (it had no /var/lib/plocate): the index of every file name, the home included
    "var/lib/plocate/plocate.db", "var/lib/mlocate/mlocate.db", "var/lib/logrotate/logrotate.status",
    # leftovers of the bring-up, and caches of a remote's catalogue
    "etc/a7a/disabled/_etc_profile.d_91-powervr-mesa.sh", "usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw.ddk-bak",
    "var/lib/flatpak/appstream/fedora/aarch64/active/appstream.xml.gz", "var/lib/flatpak/oci/fedora.index.gz",
]
# directories whose own NAMES are the data: gone too
GONE_DIRS = ["var/log/journal/5e1f0c0ffee0a7a0f00d0123456789ab", "var/lib/bluetooth/AA:BB:CC:DD:EE:FF", "var/lib/plasmalogin/.config",
             "var/cache/libdnf5/fedora-0123abcd", "var/spool/abrt/ccpp-2026-11-15-20:44:26.1-680351", "home/radxa/.ssh",
             "home/radxa/gluetest", "root/.ssh", "mnt/usb", "etc/a7a/disabled", "usr/lib/sysimage/libdnf5/offline",
             "var/lib/flatpak/appstream/fedora"]
# the system itself: all of it arrives
KEPT = [
    "usr/bin/kwin_wayland", "usr/lib64/libkwin.so.6", "usr/lib/sysimage/rpm/rpmdb.sqlite", "usr/local/bin/a7a-extlinux",
    "opt/pvr/lib/libEGL.so.1", "opt/pvr-wl/lib/libEGL.so.1", "boot/vmlinuz-6.6.98-a7a.202609091931", "boot/extlinux/extlinux.conf",
    "etc/hostname", "etc/passwd", "etc/shadow", "etc/group", "etc/fstab", "etc/sudoers.d/radxa", "etc/a7a/pvr-wl.enabled",
    "etc/ssh/sshd_config", "etc/ssh/ssh_config.d/50-redhat.conf", "etc/ssh/moduli", "etc/NetworkManager/NetworkManager.conf",
    "etc/cups/cupsd.conf", "etc/pki/tls/certs/ca-bundle.crt", "etc/ipsec.d/policies/block", "etc/dnf/dnf.conf",
    "etc/systemd/system/multi-user.target.wants/sshd.service", "etc/.updated", "var/.updated",
    "root/.bashrc", "root/.bash_profile",
    "var/lib/alternatives/ld", "var/lib/selinux/targeted/active/commit_num", "var/lib/flatpak/repo/config",
    "var/lib/systemd/catalog/database", "var/lib/alsa/asound.state",
    "usr/lib/sysimage/libdnf5/packages.toml", "usr/lib/sysimage/libdnf5/nevras.toml", "var/lib/plocate/CACHEDIR.TAG",
    "boot/grub2/fonts/unicode.pf2", "usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw", "etc/a7a/pvrsrvkm-fdinfo-fixed",
]
# directories that must be there although what was in them is not (services fail without them: the empty /var of
# 2026-11-15), and mount points
KEPT_DIRS = ["home/radxa", "root", "tmp", "proc", "sys", "dev", "run", "mnt", "media", "srv", "var/tmp",
             "var/log", "var/log/audit", "var/log/journal", "var/cache/cups", "var/cache/man", "var/cache/libdnf5", "var/spool/cups",
             "var/spool/anacron", "var/spool/abrt", "var/lib/NetworkManager", "var/lib/bluetooth", "var/lib/plasmalogin",
             "var/lib/samba/private", "var/lib/sss/db", "var/lib/systemd/coredump", "var/lib/AccountsService/users",
             "var/lib/private/systemd/journal-upload", "etc/NetworkManager/system-connections", "etc/cups/ppd", "etc/cups/ssl",
             "var/lib/rpm-state", "var/lib/gssproxy/rcache", "var/lib/plocate", "var/lib/logrotate", "var/lib/unbound",
             "var/lib/flatpak/appstream", "var/lib/flatpak/oci", "boot/grub2"]


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class TestMasterFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "card"
        self.dst = Path(self.tmp.name) / "image"
        self.dst.mkdir()
        for rel in GONE + KEPT:
            f = self.src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"content of {rel}\n")
        for rel in KEPT_DIRS + GONE_DIRS:
            (self.src / rel).mkdir(parents=True, exist_ok=True)
        (self.src / "var/lib/rpm").symlink_to("../../usr/lib/sysimage/rpm")
        (self.src / "lost+found").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def copy(self, rules=RULES):
        # the options of tools/priv.sh copy-system, without the ones only root can honour (-A -X --numeric-ids)
        return subprocess.run(["rsync", "-aHx", f"--filter=merge {rules}", f"{self.src}/", f"{self.dst}/"],
                              capture_output=True, text=True, timeout=120)

    def test_nothing_of_the_owner_or_of_this_one_machine_arrives(self):
        r = self.copy()
        self.assertEqual(r.returncode, 0, r.stderr)
        arrived = [rel for rel in GONE if (self.dst / rel).exists() or (self.dst / rel).is_symlink()]
        self.assertEqual(arrived, [])
        self.assertEqual([d for d in GONE_DIRS if (self.dst / d).exists()], [])
        self.assertFalse((self.dst / "lost+found").exists())

    def test_the_system_arrives_whole(self):
        self.assertEqual(self.copy().returncode, 0)
        self.assertEqual([rel for rel in KEPT if not (self.dst / rel).is_file()], [])
        self.assertEqual([d for d in KEPT_DIRS if not (self.dst / d).is_dir()], [])
        self.assertEqual(Path(self.dst / "var/lib/rpm").readlink().as_posix(), "../../usr/lib/sysimage/rpm")
        self.assertEqual((self.dst / "etc/shadow").read_text(), "content of etc/shadow\n")

    def test_the_directories_that_stay_are_empty(self):
        self.assertEqual(self.copy().returncode, 0)
        for d in ("home/radxa", "tmp", "run", "mnt", "var/tmp", "var/lib/bluetooth", "var/lib/plasmalogin", "var/lib/NetworkManager",
                  "etc/NetworkManager/system-connections", "var/log/journal", "var/cache/libdnf5", "var/spool/abrt"):
            self.assertEqual(sorted(x.name for x in (self.dst / d).iterdir()), [], d)
        # no file at all under the trees that keep only their directories
        for d in ("var/log", "var/cache", "var/spool", "home", "var/lib/samba", "var/lib/sss", "var/lib/private"):
            self.assertEqual([str(f.relative_to(self.dst)) for f in (self.dst / d).rglob("*") if not f.is_dir()], [], d)

    def test_kernels_named_in_an_appended_rule_stay_behind_with_their_modules(self):
        # tools/master-image.sh appends one line per path of a kernel no package owns (the card carries seven from the
        # experiments of September; their boot entries go too)
        for rel in ("boot/vmlinuz-6.6.98+", "usr/lib/modules/6.6.98+/modules.dep", "usr/lib/linux-image-6.6.98+/allwinner/a.dtb",
                    "usr/lib/modules/6.6.98-a7a.202609091931/modules.dep"):
            f = self.src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
        rules = Path(self.tmp.name) / "rules"
        rules.write_text(RULES.read_text() + "- /boot/vmlinuz-6.6.98+\n- /usr/lib/modules/6.6.98+\n- /usr/lib/linux-image-6.6.98+\n")
        self.assertEqual(self.copy(rules).returncode, 0)
        self.assertFalse((self.dst / "boot/vmlinuz-6.6.98+").exists())
        self.assertFalse((self.dst / "usr/lib/modules/6.6.98+").exists())
        self.assertFalse((self.dst / "usr/lib/linux-image-6.6.98+").exists())
        self.assertTrue((self.dst / "usr/lib/modules/6.6.98-a7a.202609091931/modules.dep").exists())
        self.assertTrue((self.dst / "boot/vmlinuz-6.6.98-a7a.202609091931").exists())

    def test_every_rule_line_is_a_rule(self):
        # a typo in the file must not turn into "copy everything": only comments, blank lines, "- /..." and "+ /...",
        # and the two rules about the names of extended attributes
        for n, line in enumerate(RULES.read_text().splitlines(), 1):
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, r"^([-+] /\S+|-x (user|system)\.\*)$", f"line {n}")

    def test_notes_on_files_do_not_travel(self):
        # user.* extended attributes: where a file was downloaded from, tags, a cache's request headers (the flatpak
        # catalogue carried 1,029 of those). File capabilities and labels are security.*, and those are copied
        kept = self.src / "usr/bin/kwin_wayland"
        tagged = subprocess.run(["setfattr", "-n", "user.xdg.origin.url", "-v", "https://example.org/owner/file", str(kept)], capture_output=True)
        if tagged.returncode != 0:
            self.skipTest("no user extended attributes here (setfattr or the filesystem)")
        def attrs(dst):
            return subprocess.run(["getfattr", "-d", "--absolute-names", str(dst / "usr/bin/kwin_wayland")], capture_output=True, text=True).stdout
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        subprocess.run(["rsync", "-aHxX", f"{self.src}/", f"{plain}/"], check=True, capture_output=True)
        self.assertIn("user.xdg.origin.url", attrs(plain))               # the attribute does travel by default
        r = subprocess.run(["rsync", "-aHxX", f"--filter=merge {RULES}", f"{self.src}/", f"{self.dst}/"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.dst / "usr/bin/kwin_wayland").is_file())
        self.assertNotIn("user.", attrs(self.dst))


class TestCanary(unittest.TestCase):
    """tools/master-canary.py: strings of the owner's looked for in the finished image, reported without their text."""

    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.img = Path(self.tmp.name) / "image.img"
        self.can = Path(self.tmp.name) / "canaries.txt"
        self.can.write_bytes(b"wifi-psk\tcorrect horse battery staple\nmachine-id\t5e1f0c0ffee0a7a0f00d0123456789ab\n"
                             b"shell-history\tsudo dnf install -y supertuxkart\ntoo-short\tabc\nno tab on this line\n")
        self.noise = os.urandom(300000)

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *extra):
        import sys
        return subprocess.run([sys.executable, str(P / "tools" / "master-canary.py"), str(self.img), "--canaries", str(self.can), *extra],
                              capture_output=True, text=True, timeout=120)

    def test_a_clean_image_passes(self):
        self.img.write_bytes(self.noise)
        r = self.scan("--also", "my-laptop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("3 strings of the owner's alone", r.stdout)   # the short one and the line without a tab do not count
        self.assertIn("nothing of the owner's alone is in the image", r.stdout)

    def test_a_secret_anywhere_in_the_file_is_found_and_never_printed(self):
        # in the middle of the file, as it would sit in the free space of a filesystem
        self.img.write_bytes(self.noise + b"\0\0correct horse battery staple\0" + self.noise + b"5e1f0c0ffee0a7a0f00d0123456789ab" + self.noise[:1000])
        r = self.scan()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("FOUND wifi-psk: canary line 1: 1 time(s), first at byte 300002", r.stdout)
        self.assertIn("FOUND machine-id: canary line 2", r.stdout)
        self.assertNotIn("horse", r.stdout + r.stderr)
        self.assertNotIn("fcab130f", r.stdout + r.stderr)

    def test_a_named_string_is_shown(self):
        self.img.write_bytes(self.noise + b"built on my-laptop by" + self.noise)
        r = self.scan("--also", "my-laptop", "--also", "192.168.50.183")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FOUND 'my-laptop': 1 time(s)", r.stdout)
        self.assertNotIn("192.168.50.183'", r.stdout)

    def test_a_known_trace_is_shown_and_does_not_fail(self):
        self.img.write_bytes(self.noise + b"built on my-laptop by" + self.noise)
        r = self.scan("--note", "my-laptop")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("known 'my-laptop': 1 time(s)", r.stdout)

    def test_a_string_that_may_occur_by_chance_is_listed_and_does_not_fail(self):
        self.can.write_bytes(b"soft:file-name\tREADME-for-me.md\nwifi-psk\tcorrect horse battery staple\n")
        self.img.write_bytes(self.noise + b"see README-for-me.md" + self.noise)
        report = Path(self.tmp.name) / "report.txt"
        r = self.scan("--report", str(report))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("1 string(s) that may occur by chance", r.stdout)
        self.assertNotIn("seen soft:file-name", r.stdout)            # the list is for the reader of the report
        self.assertIn("seen soft:file-name: canary line 1", report.read_text())
        self.assertNotIn("README-for-me", report.read_text() + r.stdout)
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

    def test_a_secret_inside_a_longer_string_that_does_not_count_is_still_found(self):
        # grep -o reports one match per place, the longest: the passphrase inside a command line that is itself a
        # "soft" string, or inside a --note string, went unreported when everything was searched in one pass
        self.can.write_bytes(b"wifi-psk\tSECRETPSK99\nsoft:history-line\tnmcli con mod psk SECRETPSK99\n")
        self.img.write_bytes(self.noise + b"nmcli con mod psk SECRETPSK99\n" + self.noise)
        r = self.scan("--note", "mod psk SECRETPSK99")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("FOUND wifi-psk", r.stdout)

    def test_a_short_known_trace_is_shown(self):
        self.img.write_bytes(self.noise + b"(builder@a7a-fedora) #16 SMP" + self.noise)
        r = self.scan("--note", "builder@")
        self.assertEqual(r.returncode, 0)
        self.assertIn("known 'builder@': 1 time(s)", r.stdout)

    def test_a_search_that_cannot_run_is_not_a_pass(self):
        r = self.scan()                                          # no image file
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


@unittest.skipUnless(shutil.which("mkfs.ext4") and shutil.which("debugfs"), "e2fsprogs needed")
class TestCanaryPlaces(unittest.TestCase):
    """With --fs-offset a hit is mapped to the file it is in (a real ext4, made from a directory without root): that
    is what tells a chance hit inside a package's own file from the owner's string in a file of the machine's."""
    OFFSET = 2 * 1024 * 1024

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        tree = t / "tree"
        for rel, text in (("usr/share/doc/pkg/README", "see the changelog-of-the-package for details\n" * 50),
                          ("usr/lib/sysimage/history.db", "dnf install changelog-of-the-package\n" * 50),
                          ("etc/secret.conf", "psk=correct horse battery staple\n"),
                          ("usr/share/doc/pkg/NOTES", "built in /home/builder/project\n")):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)
        fs = t / "fs.img"
        with open(fs, "wb") as f:
            f.truncate(32 * 1024 * 1024)
        subprocess.run(["mkfs.ext4", "-q", "-F", "-b", "4096", "-d", str(tree), str(fs)], check=True, capture_output=True)
        self.img = t / "image.img"                   # the filesystem behind two megabytes of "boot firmware"
        self.img.write_bytes(b"\0" * 1000 + b"firmware says correct horse battery staple" + b"\0" * (self.OFFSET - 1042) + fs.read_bytes())
        self.can = t / "canaries.txt"
        self.can.write_bytes(b"wifi-psk\tcorrect horse battery staple\nsoft:history-word\tchangelog-of-the-package\n")
        self.owned = t / "owned.txt"
        self.owned.write_text("/usr/share/doc/pkg/README\n/usr/share/doc/pkg/NOTES\n/usr/lib/sysimage/history.db\n")
        self.ghost = t / "ghost.txt"
        self.ghost.write_text("/usr/lib/sysimage/history.db\n")
        self.report = t / "report.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *extra):
        import sys
        return subprocess.run([sys.executable, str(P / "tools" / "master-canary.py"), str(self.img), "--canaries", str(self.can),
                               "--fs-offset", str(self.OFFSET), "--report", str(self.report), *extra], capture_output=True, text=True, timeout=120)

    def test_hits_are_named_by_file_and_outside_the_filesystem(self):
        r = self.scan("--note", "/home/builder")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        text = self.report.read_text()
        self.assertIn("FOUND wifi-psk: canary line 1: ", text)
        self.assertIn("1 in /etc/secret.conf", text)
        self.assertIn("1 in [before the root filesystem", text)
        self.assertIn("known '/home/builder': 1 in /usr/share/doc/pkg/NOTES", text)
        self.assertNotIn("horse", text + r.stdout)

    def test_a_chance_hit_inside_a_packages_own_file_is_left_out_and_one_in_the_machines_file_is_listed(self):
        r = self.scan("--pristine", str(self.owned), "--not-pristine", str(self.ghost))
        text = self.report.read_text()
        self.assertIn("seen soft:history-word: canary line 2: 50 in /usr/lib/sysimage/history.db", text)
        self.assertNotIn("README", text)
        # without the list of what is the machine's own, the history file would pass for a package's file
        self.scan("--pristine", str(self.owned))
        self.assertNotIn("seen soft:", self.report.read_text())

    def test_the_owners_string_in_a_packages_verified_file_is_the_packages_text(self):
        # 00:11:22:33:44:55 is in a manual page; a card whose log has the same sample address must not fail the build
        # for it, and must fail when the string is ALSO somewhere that is not a package's file
        self.can.write_bytes(b"network-address\tchangelog-of-the-package\n")
        pristine = Path(self.tmp.name) / "pristine.txt"
        pristine.write_text("/usr/share/doc/pkg/README\n")
        r = self.scan("--pristine", str(pristine))                      # README and history.db both hold it
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("50 in /usr/lib/sysimage/history.db", self.report.read_text())
        self.assertNotIn("README", self.report.read_text())            # every hit is mapped, and only the ones that count are named
        pristine.write_text("/usr/share/doc/pkg/README\n/usr/lib/sysimage/history.db\n")
        r = self.scan("--pristine", str(pristine))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("package text network-address: canary line 1: 100 time(s)", r.stdout)
        self.assertIn("nothing of the owner's alone", r.stdout)

    def test_the_places_stay_in_the_private_report(self):
        # where a string was found can be the string (a directory named by an address): build logs are world-readable
        r = self.scan()
        self.assertEqual(r.returncode, 1)
        self.assertIn("FOUND wifi-psk: canary line 1: 2 time(s)", r.stdout)
        self.assertNotIn("/etc/secret.conf", r.stdout)
        self.assertIn("/etc/secret.conf", self.report.read_text())
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)

    def test_something_that_is_not_a_filesystem_is_not_searched_as_one(self):
        r = subprocess.run(["python3", str(P / "tools" / "master-canary.py"), str(self.img), "--canaries", str(self.can), "--fs-offset", "4096"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


class TestAudit(unittest.TestCase):
    """tools/master-audit.py with the real tools/master-allow.rules: what no package accounts for must be on the list."""
    ALLOW = P / "tools" / "master-allow.rules"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        # a model image: 1,200 package files (flags, verify flags, path: 4294967295 = everything is verified), and the
        # machine's own files of the kinds the working card has
        V = "4294967295"
        self.owned = [f"0 {V} /usr/share/pkg/file{i}" for i in range(1200)] + [
            f"0 {V} /usr/share/pkg", f"0 {V} /usr", f"0 {V} /usr/share", f"0 {V} /etc", f"0 {V} /etc/dnf/dnf.conf", "0 4294967260 /etc/hosts",
            f"0 {V} /boot", f"0 {V} /var", f"0 {V} /var/lib", f"64 {V} /etc/machine-id", f"64 {V} /etc/hostname",
            f"64 {V} /usr/lib/sysimage/rpm/rpmdb.sqlite", f"64 {V} /var/lib/plocate/plocate.db",
            f"64 {V} /usr/lib/sysimage/libdnf5/transaction_history.sqlite", f"64 {V} /etc/not-there", f"0 {V} /usr/lib/qt6/plugins/x.so",
            f"64 {V} /usr/bin/java", "0 4294967260 /etc/passwd"]
        self.present = [f"f 1 /usr/share/pkg/file{i}" for i in range(1200)] + [
            "d 2 /usr/share/pkg", "d 5 /usr", "d 9 /usr/share", "d 80 /etc", "f 1 /etc/dnf/dnf.conf", "f 1 /etc/hosts", "f 1 /etc/passwd", "d 4 /boot",
            "f 1 /etc/machine-id", "f 1 /etc/hostname", "f 1 /usr/lib/sysimage/rpm/rpmdb.sqlite", "l 1 /usr/bin/java",
            "d 2 /usr/lib/qt6", "d 2 /usr/lib/qt6/plugins", "f 1 /usr/lib/qt6/plugins/x.so",          # unowned directories above a package's file
            "d 2 /boot/extlinux", "f 1 /boot/extlinux/extlinux.conf", "l 1 /etc/systemd/system/multi-user.target.wants/sshd.service",
            "l 1 /etc/systemd/system/getty.target.wants/getty@tty1.service", "d 2 /etc/systemd/system/getty.target.wants",
            "d 2 /etc/systemd/system/multi-user.target.wants", "f 1 /etc/sudoers.d/radxa", "d 3 /home/radxa", "f 1 /home/radxa/.bashrc",
            "d 2 /var/lib/tpm2-tss/system/keystore", "d 2 /proc", "d 2 /tmp", "d 20 /"]
        self.verify = ["S.5....T.  c /etc/dnf/dnf.conf", "missing     /var/mail", ".M.......    /tmp", "........P    /usr/bin/kwin_wayland"]

    def tearDown(self):
        self.tmp.cleanup()

    def audit(self, more_owned=(), more_present=(), more_verify=(), allow=None, done=True, extra=()):
        import sys
        end = ["#done"] if done else []
        (self.t / "owned").write_text("\n".join(self.owned + list(more_owned) + end) + "\n")
        (self.t / "present").write_text("\n".join(self.present + list(more_present) + end) + "\n")
        (self.t / "verify").write_text("\n".join(self.verify + list(more_verify) + end) + "\n")
        lists = self.t / "lists"
        lists.mkdir(exist_ok=True)
        return subprocess.run([sys.executable, str(P / "tools" / "master-audit.py"), "--allow", str(allow or self.ALLOW), "--owned", str(self.t / "owned"),
                               "--present", str(self.t / "present"), "--verify", str(self.t / "verify"), "--lists", str(lists), "--complete", *extra],
                              capture_output=True, text=True, timeout=60)

    def loose(self, r):
        return [l[4:] for l in r.stdout.splitlines() if l.startswith("    ")]

    def test_the_model_of_the_working_card_passes(self):
        r = self.audit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("every path that no package accounts for is on the list", r.stdout)
        lists = self.t / "lists"
        self.assertEqual((lists / "ghost-files.txt").read_text().split(), ["/etc/hostname", "/etc/machine-id", "/usr/bin/java", "/usr/lib/sysimage/rpm/rpmdb.sqlite"])
        self.assertEqual((lists / "changed-files.txt").read_text().split(), ["/etc/dnf/dnf.conf"])
        self.assertEqual((lists / "unverified-files.txt").read_text().split(), ["/etc/hosts", "/etc/passwd"])
        self.assertIn("/boot/extlinux/extlinux.conf", (lists / "unowned-files.txt").read_text())
        self.assertIn("/etc/systemd/system/multi-user.target.wants/sshd.service", (lists / "unowned-files.txt").read_text())    # links are listed
        # a directory nobody owns is listed when it holds no package's file; one that merely lacks an owner is not
        dirs = (lists / "unowned-dirs.txt").read_text().split()
        self.assertIn("/home/radxa", dirs)
        self.assertNotIn("/usr/lib/qt6/plugins", dirs)
        self.assertNotIn("/proc", dirs)
        # pristine: what a string found in it certainly belongs to the package: regular, checksummed, unchanged
        pristine = (lists / "pristine-files.txt").read_text().split()
        self.assertIn("/usr/share/pkg/file7", pristine)
        for not_pristine in ("/etc/dnf/dnf.conf", "/etc/hosts", "/etc/passwd", "/etc/machine-id", "/usr/share/pkg", "/boot/extlinux/extlinux.conf"):
            self.assertNotIn(not_pristine, pristine)

    def test_the_modules_an_image_adds_to_a_kernel_are_let_in_and_nothing_beside_them(self):
        # exFAT, ISO 9660 and uinput in extra/a7a (tools/master-image.sh); the kernel package owns extra/ (aic8800)
        rel = "6.6.98-a7a.202609091931"
        owned = [f"0 4294967295 /usr/lib/modules/{rel}/extra", f"0 4294967295 /usr/lib/modules/{rel}/extra/aic8800_fdrv.ko"]
        present = [f"d 2 /usr/lib/modules/{rel}/extra", f"f 1 /usr/lib/modules/{rel}/extra/aic8800_fdrv.ko", f"d 2 /usr/lib/modules/{rel}/extra/a7a"]
        present += [f"f 1 /usr/lib/modules/{rel}/extra/a7a/{m}.ko" for m in ("exfat", "isofs", "uinput")]
        r = self.audit(more_owned=owned, more_present=present)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for loose in (f"f 1 /usr/lib/modules/{rel}/extra/a7a/other.ko", f"f 1 /usr/lib/modules/{rel}/extra/exfat.ko"):
            r = self.audit(more_owned=owned, more_present=present + [loose])
            self.assertEqual(r.returncode, 1, loose + ": " + r.stdout)
            self.assertEqual(self.loose(r), [loose.split(" ", 2)[2]])

    def test_what_a_used_card_can_hold_and_no_rule_names_stops_the_build(self):
        # the reviewers' lists of 2026-11-16: each of these was copied by the filter rules of that day
        loose = ["f 1 /etc/wpa_supplicant/wpa_supplicant.conf", "f 1 /var/lib/iwd/TheNet.psk", "f 1 /var/lib/tailscale/tailscaled.state",
                 "f 1 /etc/systemd/system/mnt-nas.mount", "f 1 /etc/systemd/system/backup.service", "f 1 /etc/systemd/network/wg0.netdev",
                 "f 1 /etc/cron.d/backup", "f 1 /etc/udev/rules.d/99-my-stick.rules", "f 1 /etc/profile.d/secrets.sh",
                 "f 1 /etc/yum.repos.d/private.repo", "f 1 /etc/letsencrypt/live/example.org/privkey.pem", "f 1 /etc/shadow.old",
                 "d 3 /etc/.git", "f 1 /etc/.git/config", "f 1 /data/photo.jpg", "d 2 /data", "f 1 /swap.img", "f 1 /nohup.out",
                 "f 1 /opt/tool/license.key", "f 1 /usr/local/etc/app.conf", "f 1 /usr/local/src/proj/.git/config",
                 "f 1 /usr/lib/firmware/brcm/brcmfmac43455-sdio.txt", "f 1 /boot/initramfs-6.19.10.img", "f 1 /boot/uEnv.txt",
                 "f 1 /boot/loader/entries/5e1f0c0ffee0a7a0f00d0123456789ab-6.19.10.conf", "f 1 /var/lib/flatpak/app/org.x.App/current/active/files/bin/x",
                 "f 1 /var/lib/flatpak/repo/objects/ab/cdef.file", "f 1 /var/lib/docker/volumes/v/_data/db", "f 1 /var/www/html/index.html",
                 "d 2 /var/cache/gdm/sam", "d 2 /var/log/remote/nas.home", "d 2 /var/spool/cups-pdf/SAM", "f 1 /var/lib/fprint/sam/print",
                 "f 1 /etc/systemd/user/my-sync.service", "f 1 /home/radxa/Documents/letter.odt", "f 1 /home/other/.bashrc", "f 1 /root/notes.txt",
                 # the second review: names that are data behind an "@", a unit FILE where systemctl makes links, a file
                 # where the list expects an empty directory, a socket, a pipe, a device node
                 "l 1 /etc/systemd/system/multi-user.target.wants/wg-quick@office-acme.service",
                 "l 1 /etc/systemd/system/multi-user.target.wants/openvpn-client@acme-corp.service",
                 "f 1 /etc/systemd/system/multi-user.target.wants/secrets.service", "f 1 /etc/alternatives/notes.txt",
                 "f 1 /var/lib/tpm2-tss/system/keystore/key", "f 1 /var/lib/flatpak/repo/objects", "f 1 /var/lib/dnf/repos/x",
                 "s 1 /var/lib/owner.sock", "p 1 /var/lib/owner.fifo", "c 1 /var/lib/owner-device"]
        r = self.audit(more_present=loose)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn(f"NOT ACCOUNTED FOR ({len(loose)})", r.stdout)
        self.assertEqual(sorted(self.loose(r)), sorted(l.split(" ", 2)[2] for l in loose))

    def test_a_ghost_an_unverified_or_a_changed_file_nobody_listed_stops_the_build(self):
        # %ghost: a package names it, so it is not "unowned", and rpm -V does not look at its content: dnf5's history and
        # plocate's index (both got past the first audit, which listed unowned files only). Unverified: rpm does not
        # checksum it (the second review: 1,334 such files, /etc/passwd and cups' configuration among them)
        r = self.audit(more_owned=["64 0 /usr/bin/owners-link", "0 4294967260 /etc/cups/printers.conf", "64 1 /etc/ghost-dir"],
                       more_present=["f 1 /var/lib/plocate/plocate.db", "f 1 /usr/lib/sysimage/libdnf5/transaction_history.sqlite",
                                     "f 1 /etc/cups/printers.conf", "d 2 /etc/ghost-dir", "f 1 /etc/ghost-dir/owners-file"],
                       more_verify=["S.5....T.  c /usr/share/pkg/file3", "....L....    /usr/share/pkg/file7", "..?......    /usr/share/pkg/file9",
                                    "S.5....T.  c /usr/share/pkg/with space"])
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(sorted(self.loose(r)), ["/etc/cups/printers.conf", "/etc/ghost-dir/owners-file", "/usr/lib/sysimage/libdnf5/transaction_history.sqlite",
                                                 "/usr/share/pkg/file3", "/usr/share/pkg/file7", "/usr/share/pkg/file9", "/usr/share/pkg/with space",
                                                 "/var/lib/plocate/plocate.db"])

    def test_a_ghost_link_is_judged_and_a_section_excuses_only_its_own_kind(self):
        # links that packages declare as ghosts (the alternatives) are 577 of the working card's ghost paths
        r = self.audit(more_owned=["64 0 /etc/owners-ghost-link"], more_present=["l 1 /etc/owners-ghost-link"])
        self.assertEqual(self.loose(r), ["/etc/owners-ghost-link"])
        # /usr/bin/* is let in for ghosts (alternatives); that does not let an unowned file in /usr/bin in
        r = self.audit(more_present=["f 1 /usr/bin/owners-tool"])
        self.assertEqual(self.loose(r), ["/usr/bin/owners-tool"])
        # and what [unowned] lets in is not thereby fine as a ghost or as a changed package file
        r = self.audit(more_owned=["64 0 /etc/sudoers.d/radxa"])
        self.assertEqual(self.loose(r), ["/etc/sudoers.d/radxa"])
        r = self.audit(more_owned=["0 4294967295 /etc/plasma-setup-done"], more_present=["f 1 /etc/plasma-setup-done"], more_verify=["S.5....T.    /etc/plasma-setup-done"])
        self.assertEqual(self.loose(r), ["/etc/plasma-setup-done"])

    def test_a_path_that_one_package_ships_and_another_declares_is_not_the_machines_own(self):
        r = self.audit(more_owned=["64 0 /usr/share/pkg/file5"])          # a ghost for one, payload of the other
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("/usr/share/pkg/file5", (self.t / "lists" / "ghost-files.txt").read_text())

    def test_a_foreign_owner_or_group_stops_the_build_whatever_the_list_says(self):
        for line in (".....U...    /boot", "......G..    /usr/share/pkg/file1", ".M...UG..  c /etc/dnf/dnf.conf"):
            r = self.audit(more_verify=[line])
            self.assertEqual(r.returncode, 1, line + r.stdout)
            self.assertIn("OWNER OR GROUP", r.stdout)
        r = self.audit(more_verify=[".M...UG..    /tmp", ".M.......    /boot"])     # the sandbox's own mount; a mode alone
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_listings_that_cannot_be_right_are_not_a_pass(self):
        self.assertEqual(self.audit(done=False).returncode, 2)                  # a command that did not finish
        self.assertEqual(self.audit(extra=["--min-paths", "100000"]).returncode, 2)      # a full image has a quarter of a million
        self.owned, self.present = self.owned[:50], self.present[:50]
        self.assertEqual(self.audit().returncode, 2)

    def test_patterns(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("master_audit", P / "tools" / "master-audit.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertTrue(m.to_regex("/etc/systemd/system/*.wants/*").fullmatch("/etc/systemd/system/multi-user.target.wants/sshd.service"))
        self.assertFalse(m.to_regex("/etc/systemd/system/*.wants/*").fullmatch("/etc/systemd/system/a.wants/b/c"))
        self.assertFalse(m.to_regex("/etc/alternatives/*").fullmatch("/etc/alternatives"))
        self.assertTrue(m.to_regex("/opt/pvr/**").fullmatch("/opt/pvr"))
        self.assertTrue(m.to_regex("/opt/pvr/**").fullmatch("/opt/pvr/lib/dri/x.so"))
        self.assertFalse(m.to_regex("/opt/pvr/**").fullmatch("/opt/pvr-other/x"))
        self.assertFalse(m.to_regex("/usr/share/mime/**").fullmatch("/usr/share/mimeX"))
        self.assertTrue(m.to_regex("/usr/lib/modules/*/modules.*").fullmatch("/usr/lib/modules/6.6.98-a7a.1/modules.dep.bin"))
        self.assertTrue(m.to_regex("/a/?").fullmatch("/a/b"))
        self.assertFalse(m.to_regex("/a?b").fullmatch("/a/b"))
        # types, and the "@" that only a pattern spelling it matches
        pats = [("d:/x", "d", m.to_regex("/x")), ("l:/w/*", "l", m.to_regex("/w/*")), ("l:/w/getty@tty1", "l", m.to_regex("/w/getty@tty1"))]
        self.assertEqual(m.audit([("d", "/x"), ("f", "/x"), ("l", "/w/a"), ("f", "/w/a"), ("l", "/w/vpn@office"), ("l", "/w/getty@tty1")], pats)[0],
                         ["/x", "/w/a", "/w/vpn@office"])
        bad = self.t / "bad.rules"
        bad.write_text("[unowned]\n/x\n[nonsense]\n/y\n")
        self.assertEqual(self.audit(allow=bad).returncode, 2)
        bad.write_text("/before/any/section\n")
        self.assertEqual(self.audit(allow=bad).returncode, 2)

    def test_the_list_lets_nothing_in_wholesale(self):
        # a pattern like /etc/** would make the audit a formality, in any section (a ghost is whatever the machine made
        # of a path a package only names)
        for line in self.ALLOW.read_text().splitlines():
            pattern = line.split(":", 1)[1] if line[:1] in "dlf" and ":" in line[:4] else line
            if not pattern.startswith("/"):
                continue
            self.assertNotRegex(pattern, r"^/(etc|var|usr|opt|boot|home|root|srv|var/lib|var/cache|usr/local|usr/lib|usr/share|home/radxa)(/\*{1,2})+(/\*\.[a-z]+)?$", line)
            self.assertNotRegex(pattern, r"^/(etc|home|root|var|opt)/\*\*", line)
            if pattern.endswith("/**"):
                self.assertGreaterEqual(pattern.count("/"), 3, line)


@unittest.skipUnless(shutil.which("bwrap"), "bubblewrap needed")
class TestCanaryCollection(unittest.TestCase):
    """tools/master-canaries.sh over a model of a used card (its /etc, /home, /root and /var bound over this
    machine's in an unprivileged sandbox): each kind of thing is collected, or a search for it finds nothing by
    construction."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        m = Path(cls.tmp.name)
        def put(rel, text):
            f = m / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        put("etc/hostname", "radxa-cubie-a7a\n")
        put("etc/machine-id", "5e1f0c0ffee0a7a0f00d0123456789ab\n")
        put("etc/ssh/ssh_host_ed25519_key.pub", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHostPublicKeyMaterial0123456789abcdef root@radxa\n")
        put("etc/ssh/ssh_host_ed25519_key", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\nHostPrivateKeyLineThree0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH\n")
        put("etc/NetworkManager/system-connections/TheNet.nmconnection",
            "[connection]\nid=TheNetAtHome\nuuid=0a1b2c3d-1111-4222-8333-444455556666\n[wifi]\nssid=TheNetAtHome\n[wifi-security]\npsk=correct horse battery staple\n"
            "[802-1x]\nidentity=owner@university.example\npassword=eduroam-secret-9\n[wireguard]\nprivate-key=WGprivateKEYmaterial0123456789abcdefghijk=\n")
        put("var/lib/NetworkManager/seen-bssids", "[TheNetAtHome]\nbssids=AA:BB:CC:00:11:22,\n")
        # 40 addresses in the log, lower and upper case, and the two that every text about networks contains
        macs = [f"de:ad:be:ef:{i:02x}:01" for i in range(40)]
        put("var/log/messages", "".join(f"Nov 15 kernel: wlan0: associated with {m_}\n" for m_ in macs)
            + "Nov 15 NetworkManager: address 2001:db8:1234:5678:aaaa:bbbb:cccc:dddd\nNov 15 wpa: SSID='NeighbourNet5G'\n"
            + "Nov 15 kernel: broadcast ff:ff:ff:ff:ff:ff from 00:00:00:00:00:00\nNov 15 dhcp: lease of 192.168.50.113 from nas-box.local\n"
            + "Nov 15 kernel: BSSID 0C:11:22:33:44:55\n")
        put("var/log/journal/5e1f0c0ffee0a7a0f00d0123456789ab/system.journal", "x")
        put("var/lib/bluetooth/11:22:33:44:55:66/77:88:99:AA:BB:CC/info", "[General]\nName=Owner's Headphones\n[LinkKey]\nKey=0123456789ABCDEF0123456789ABCDEF\n")
        put("home/radxa/.ssh/authorized_keys", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAuthorizedKeyMaterial0123456789abcd owner@laptop-name\n")
        put("home/radxa/.ssh/known_hosts", "nas-box.local,192.168.50.50 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKnownHostKeyMaterial0123456789abcdef\n")
        put("home/radxa/.bash_history", "ls\nsudo dnf install -y supertuxkart glmark2\nssh owner@nas-box.local\ncd ~/rpms\n")
        put("home/radxa/.zsh_history", ": 1794785624:0;scp secret-plans-2026.pdf owner@192.168.50.50:/srv/\n")
        put("home/radxa/.netrc", "machine git.example.org login owner password s3cr3t-token-value\n")
        put("home/radxa/.local/state/dolphinstaterc", "[Recent]\nUrls=file:///home/radxa/Documents/tax-return-2025.pdf\n")
        put("etc/shadow", "root:!::0:99999:7:::\nradxa:$y$j9T$abcdefghijklmnop$0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg:20000:0:99999:7:::\n")
        put("etc/wireguard/wg0.conf", "[Interface]\nPrivateKey = WGconfPRIVATEkey0123456789abcdefghijklmnop=\n")
        put("var/log/secure", "Nov 15 sshd[1]: Accepted publickey for radxa from 192.168.50.183 port 5 ssh2: ED25519 SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ\n")
        put("home/radxa/.gitconfig", "[user]\n\tname = Owner Full Name\n\temail = owner@mail.example\n")
        put("home/radxa/.config/kdeconnect/config", "[General]\nname=radxa-cubie-a7a\n")
        put("home/radxa/.config/kdeconnect/_0a1b2c3d_trusted_phone_id_/config", "[General]\nname=Owner's Phone\n")
        put("home/radxa/.config/kwinoutputconfig.json", '{"connectorName": "HDMI-A-1", "edidIdentifier": "GSM 30471 123456 11 2021 0", "edidHash": "0123456789abcdef0123456789abcdef"}\n')
        put("home/radxa/.local/share/klipper/history3.sqlite", "SQLite format 3\nCREATE TABLE main (uuid TEXT)\nthe text he copied to the clipboard last night\n")
        put("home/radxa/.local/share/klipper/data/1/note.txt", "a second clipboard entry, kept beside the database\n")
        put("home/radxa/.local/share/recently-used.xbel", '<bookmark href="file:///home/radxa/Documents/tax-return-2025.pdf">\n')
        put("home/radxa/Documents/tax-return-2025.pdf", "x")
        put("home/radxa/.config/mozilla/firefox/ab12cd34.default-release/prefs.js", 'user_pref("services.sync.username", "owner@mail.example");\n')
        put("root/.bash_history", "nmcli device wifi connect TheNetAtHome password hunter2hunter2\n")
        cls.model = m
        r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--ro-bind", str(m / "etc"), "/etc", "--ro-bind", str(m / "home"), "/home",
                            "--ro-bind", str(m / "root"), "/root", "--ro-bind", str(m / "var"), "/var", "--dev", "/dev", "--proc", "/proc",
                            "--tmpfs", "/tmp", "bash", "-s"], input=(P / "tools" / "master-canaries.sh").read_text(),
                           capture_output=True, text=True, timeout=120)     # on stdin, as the build feeds it: the model hides /home
        cls.ran = r
        cls.rows = [tuple(l.split("\t", 1)) for l in r.stdout.splitlines() if "\t" in l]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        if self.ran.returncode != 0 and "bwrap:" in self.ran.stderr:
            self.skipTest(f"no unprivileged sandbox here: {self.ran.stderr.strip()[:200]}")
        self.assertEqual(self.ran.returncode, 0, self.ran.stderr[:500])

    def values(self, kind):
        return {v for k, v in self.rows if k == kind}

    def test_the_identity_of_the_machine(self):
        self.assertEqual(self.values("machine-id"), {"5e1f0c0ffee0a7a0f00d0123456789ab"})
        self.assertEqual(self.values("ssh-host-public"), {"AAAAC3NzaC1lZDI1NTE5AAAAIHostPublicKeyMaterial0123456789abcdef"})
        self.assertEqual(self.values("ssh-host-private"), {"HostPrivateKeyLineThree0123456789abcdefghijklmnopqrstuvwxyzABCDEFGH"})
        self.assertEqual(self.values("password-hash"), {"$y$j9T$abcdefghijklmnop$0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg"})

    def test_every_secret_of_a_connection_profile(self):
        self.assertIn("correct horse battery staple", self.values("wifi-psk"))
        self.assertIn("TheNetAtHome", self.values("wifi-ssid"))
        self.assertIn("0a1b2c3d-1111-4222-8333-444455556666", self.values("wifi-uuid"))
        self.assertIn("eduroam-secret-9", self.values("wifi-password"))
        self.assertIn("owner@university.example", self.values("wifi-identity"))
        self.assertIn("WGprivateKEYmaterial0123456789abcdefghijk=", self.values("wifi-private-key"))

    def test_every_address_in_the_logs_in_both_spellings_and_none_dropped(self):
        got = self.values("network-address")
        for i in range(40):                                      # a cut-off at 30 dropped 13 of 43 on the real card
            self.assertIn(f"de:ad:be:ef:{i:02x}:01", got)
            self.assertIn(f"DE:AD:BE:EF:{i:02X}:01", got)
        for m in ("0c:11:22:33:44:55", "0C:11:22:33:44:55", "aa:bb:cc:00:11:22", "11:22:33:44:55:66", "77:88:99:aa:bb:cc"):
            self.assertIn(m, got)
        self.assertNotIn("00:00:00:00:00:00", got)
        self.assertNotIn("ff:ff:ff:ff:ff:ff", got)
        self.assertIn("2001:db8:1234:5678:aaaa:bbbb:cccc:dddd", self.values("network-address-v6"))
        self.assertIn("2001:db8:1234:5678:", self.values("network-prefix-v6"))
        self.assertIn("NeighbourNet5G", self.values("wifi-ssid"))

    def test_the_person(self):
        self.assertIn("AAAAC3NzaC1lZDI1NTE5AAAAIAuthorizedKeyMaterial0123456789abcd", self.values("ssh-authorized-key"))
        self.assertIn("owner@laptop-name", self.values("ssh-key-comment"))
        self.assertIn("AAAAC3NzaC1lZDI1NTE5AAAAIKnownHostKeyMaterial0123456789abcdef", self.values("ssh-known-host-key"))
        self.assertEqual(self.values("identity"), {"Owner Full Name", "owner@mail.example"})
        self.assertIn("Owner's Phone", self.values("kdeconnect"))
        self.assertIn("_0a1b2c3d_trusted_phone_id_", self.values("kdeconnect"))
        self.assertIn("Owner's Headphones", self.values("bluetooth-name"))
        self.assertIn("0123456789ABCDEF0123456789ABCDEF", self.values("bluetooth-key"))
        self.assertEqual(self.values("screen"), {"GSM 30471 123456 11 2021 0", "0123456789abcdef0123456789abcdef"})
        self.assertIn("ab12cd34.default-release", self.values("browser-profile"))
        self.assertIn("machine git.example.org login owner password s3cr3t-token-value", self.values("credentials"))
        self.assertIn("WGconfPRIVATEkey0123456789abcdefghijklmnop=", self.values("vpn"))
        self.assertIn("SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ", self.values("ssh-key-fingerprint"))
        self.assertIn("192.168.50.183", self.values("soft:ssh-peer"))

    def test_histories_of_every_shell_and_of_root(self):
        h = self.values("shell-history")
        self.assertIn("sudo dnf install -y supertuxkart glmark2", h)
        self.assertIn("scp secret-plans-2026.pdf owner@192.168.50.50:/srv/", h)           # without zsh's time stamp in front
        self.assertIn("nmcli device wifi connect TheNetAtHome password hunter2hunter2", h)
        self.assertNotIn("ls", {v for _, v in self.rows})          # too short to mean anything
        self.assertIn("secret-plans-2026.pdf", self.values("soft:history-word"))
        self.assertIn("ssh owner@nas-box.local", self.values("soft:history-line"))

    def test_the_clipboard_is_its_text_not_the_schema_of_its_database(self):
        c = self.values("clipboard")
        self.assertIn("the text he copied to the clipboard last night", c)
        self.assertIn("a second clipboard entry, kept beside the database", c)          # a file below a subdirectory
        self.assertFalse([v for v in c if v.startswith(("CREATE ", "SQLite format"))])

    def test_what_may_occur_by_chance_is_marked_so(self):
        self.assertIn("192.168.50.113", self.values("soft:private-address"))
        self.assertIn("nas-box.local", self.values("soft:lan-name"))
        self.assertIn("tax-return-2025.pdf", self.values("soft:file-name"))
        self.assertIn("file:///home/radxa/Documents/tax-return-2025.pdf", self.values("recent-file"))
        # the name of the machine is in the image by design, and is what KDE Connect calls it
        self.assertNotIn("radxa-cubie-a7a", {v for _, v in self.rows})
        # the connector's name is the same on every board
        self.assertNotIn("HDMI-A-1", {v for _, v in self.rows})


class TestSharedImageFiles(unittest.TestCase):
    def test_the_assembled_image_and_the_card_image_get_the_same_first_boot_files(self):
        # tools/assemble-image.sh writes them from here-documents, tools/master-image.sh from tools/image-files/: one text
        script = (P / "tools" / "assemble-image.sh").read_text()
        for marker, name in (("etc/systemd/system/a7a-grow-rootfs.service <<EOF", "a7a-grow-rootfs.service"),
                             ("etc/systemd/system-preset/00-a7a.preset <<EOF", "00-a7a.preset")):
            start = script.index("\n", script.index(marker)) + 1
            self.assertEqual(script[start:script.index("\nEOF\n", start)] + "\n", (P / "tools" / "image-files" / name).read_text(), name)

    def test_the_card_image_script_never_names_a_real_disk(self):
        # everything it does goes through loop devices of files under output/
        text = (P / "tools" / "master-image.sh").read_text()
        # (the one line that names a device is a pattern for the text of the image's own /etc/fstab)
        text = "\n".join(l for l in text.splitlines() if "/etc/fstab" not in l)
        self.assertNotRegex(text, r"/dev/(sd[a-z]|mmcblk|nvme)")
        for op in ("flash", "fsck-rootfs", "grow-rootfs", "restore-firmware", "boot-type", "read-card"):
            self.assertNotIn(f"$PRIV {op}", text)


if __name__ == "__main__":
    unittest.main()
