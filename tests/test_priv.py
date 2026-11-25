"""Tests for tools/priv.sh mount/umount: a mount asked for with "ro" must not be able to write to the device.
2026-11-15: `priv.sh mount /dev/sdb3 cardcheck ro,noload` on the working card, pulled from the running board, let the
kernel run ext4's orphan cleanup on it without the journal replay ("153 orphan inodes deleted"): "ro" is about the
mount, and ext4 writes to a writable device all the same. The real script runs in a scratch tree; id, blockdev, mount,
umount and findmnt are faked on PATH. The fakes keep what the kernel would: each device's read-only flag
(state/ro-<dev>) and what is mounted where (state/mp-<name> holds the device)."""
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
DEV = "/dev/loop987p3"

FAKES = {
    "id": 'echo 0\n',
    "blockdev": r'''S="$FAKE_STATE"; echo "blockdev $*" >> "$S/calls.log"; f="$S/ro-$(basename "$2")"
case "$1" in
  --getro) [ -e "$S/getro-fails" ] && exit 1; [ -e "$S/getro-garbage" ] && { echo RO; exit 0; }; cat "$f" 2>/dev/null || echo 0 ;;
  --setro) [ -e "$S/setro-fails" ] && exit 1; ls "$S"/../output/mnt/.ro-* > "$S/markers-at-setro" 2>/dev/null
           [ -e "$S/setro-noop" ] || echo 1 > "$f" ;;
  --setrw) echo 0 > "$f" ;;
esac
''',
    # mount: remembers how the device stood when the filesystem came up (after its own run time), and what is mounted
    "mount": r'''S="$FAKE_STATE"; echo "mount $*" >> "$S/calls.log"
for a in "$@"; do case "$a" in /dev/*) dev=$(basename "$a");; esac; done; mp=$(basename "${@: -1}")
[ -e "$S/mount-slow" ] && sleep 1
cat "$S/ro-$dev" 2>/dev/null > "$S/ro-at-mount" || echo 0 > "$S/ro-at-mount"
[ -e "$S/mount-fails" ] && exit 32
echo "$dev" > "$S/mp-$mp"; [ -e "$S/flag-lost" ] && echo 0 > "$S/ro-$dev"; exit 0
''',
    "umount": r'''S="$FAKE_STATE"; echo "umount $*" >> "$S/calls.log"; mp=$(basename "$1")
[ -e "$S/mp-$mp" ] || exit 32          # not mounted, as the real one answers
rm -f "$S/mp-$mp"
''',
    # findmnt -n -S <dev>: 0 when something has it mounted, 1 when not, 2 when it cannot tell
    "findmnt": r'''S="$FAKE_STATE"; echo "findmnt $*" >> "$S/calls.log"
[ -e "$S/findmnt-fails" ] && exit 2
[ "$1 $2" = "-n -S" ] || exit 2
for f in "$S"/mp-*; do [ -f "$f" ] && [ "$(cat "$f")" = "$(basename "$3")" ] && exit 0; done; exit 1
''',
}


class TestPrivMount(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.script = root / "tools" / "priv.sh"
        self.script.parent.mkdir()
        self.script.write_text((P / "tools" / "priv.sh").read_text())
        self.script.chmod(0o755)
        self.out = root / "output"
        self.out.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def priv(self, *args):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=self.env, timeout=60)

    def calls(self, prefix=""):
        f = self.state / "calls.log"
        return [c for c in (f.read_text().splitlines() if f.exists() else []) if c.startswith(prefix)]

    def ro(self, dev=DEV):
        f = self.state / f"ro-{os.path.basename(dev)}"
        return f.read_text().strip() if f.exists() else "0"

    def markers(self):
        return sorted(p.name for p in (self.out / "mnt").glob(".ro-*"))

    def flag(self, name, on=True):
        (self.state / name).touch() if on else (self.state / name).unlink()

    def test_ro_mount_makes_the_device_read_only_before_the_kernel_sees_it(self):
        r = self.priv("mount", DEV, "look", "ro,noload")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.state / "ro-at-mount").read_text().strip(), "1")
        self.assertEqual(self.calls("mount"), [f"mount -o ro,noload {DEV} {self.out}/mnt/look"])
        self.assertEqual(r.stdout.strip(), f"{self.out}/mnt/look")

    def test_ro_is_found_in_every_spelling_mount_takes(self):
        # mount(8) also takes ro=fs, ro=vfs and ro=recursive, and libmount makes each of them a read-only mount
        for n, opts in enumerate(("ro", "noload,ro", "ro,subvol=root", "defaults,ro", "rw,ro", "ro=fs,noload", "noload,ro=recursive", "ro=vfs")):
            dev = f"/dev/loop{900 + n}p3"
            self.assertEqual(self.priv("mount", dev, f"look{n}", opts).returncode, 0, opts)
            self.assertEqual((self.state / "ro-at-mount").read_text().strip(), "1", opts)
            self.assertEqual(self.ro(dev), "1", opts)

    def test_an_option_that_only_contains_the_letters_ro_is_not_a_read_only_mount(self):
        self.assertEqual(self.priv("mount", DEV, "work", "errors=remount-ro").returncode, 0)
        self.assertEqual(self.calls("blockdev --setro"), [])
        self.assertEqual((self.state / "ro-at-mount").read_text().strip(), "0")

    def test_a_writable_mount_is_left_alone(self):
        self.assertEqual(self.priv("mount", DEV, "work").returncode, 0)
        self.assertEqual(self.calls("blockdev --setro"), [])
        self.assertEqual(self.calls("mount"), [f"mount {DEV} {self.out}/mnt/work"])

    def test_umount_makes_the_device_writable_again(self):
        self.priv("mount", DEV, "look", "ro")
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.markers(), [".ro-loop987p3"])
        r = self.priv("umount", "look")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.ro(), "0")
        self.assertEqual(self.markers(), [])
        self.assertIn(f"findmnt -n -S {DEV}", self.calls("findmnt"))        # asked about THIS device, by source
        # and only once: a later umount of something else does not touch the device again
        self.priv("mount", "/dev/loop5", "other")
        self.priv("umount", "other")
        self.assertEqual(len(self.calls("blockdev --setrw")), 1)

    def test_device_stays_read_only_while_it_is_still_mounted_under_another_name(self):
        # assemble-image.sh mounts Fedora's btrfs three times (subvol=root, home, var), all "ro"
        self.priv("mount", DEV, "froot", "ro,subvol=root")
        self.priv("mount", DEV, "fhome", "ro,subvol=home")
        self.priv("mount", DEV, "fvar", "ro,subvol=var")
        self.priv("umount", "fvar")
        self.priv("umount", "fhome")
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.markers(), [".ro-loop987p3"])
        self.priv("umount", "froot")
        self.assertEqual(self.ro(), "0")

    def test_failed_ro_mount_wrote_nothing_and_leaves_the_device_as_it_was(self):
        # an ext4 that needs its journal replayed cannot be mounted "ro" from a read-only device: that is the point
        self.flag("mount-fails")
        r = self.priv("mount", DEV, "look", "ro")
        self.assertEqual(r.returncode, 32)                                # mount's own answer is passed on
        self.assertEqual((self.state / "ro-at-mount").read_text().strip(), "1")
        self.assertEqual(self.ro(), "0")
        self.assertIn("wrote nothing", r.stderr)
        self.assertIn("ro,noload", r.stderr)
        self.assertFalse((self.out / "mnt" / "look").exists())             # no empty mount point left behind
        self.assertEqual(self.markers(), [])
        self.assertEqual(len(self.calls("blockdev --setrw")), 1)

    def test_the_hint_does_not_recommend_what_was_just_tried(self):
        self.flag("mount-fails")
        r = self.priv("mount", DEV, "look", "ro,noload")
        self.assertIn("wrote nothing", r.stderr)
        self.assertNotIn("mounts only with ro,noload", r.stderr)
        self.assertIn("journalctl -k", r.stderr)

    def test_a_second_mount_that_fails_does_not_unprotect_the_first(self):
        # assemble-image.sh lets the home (and so far any later) subvolume mount fail: froot stays mounted meanwhile
        self.assertEqual(self.priv("mount", DEV, "froot", "ro,subvol=root").returncode, 0)
        self.flag("mount-fails")
        self.assertNotEqual(self.priv("mount", DEV, "fhome", "ro,subvol=home").returncode, 0)
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.markers(), [".ro-loop987p3"])
        self.assertEqual(self.calls("blockdev --setrw"), [])

    def test_a_device_that_was_read_only_already_stays_read_only(self):
        # a card with its write-protect tab set, a backup attached through a read-only loop (loop-attach <image> <offset>)
        (self.state / f"ro-{os.path.basename(DEV)}").write_text("1\n")
        self.priv("mount", DEV, "look", "ro")
        self.priv("umount", "look")
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.calls("blockdev --setrw"), [])

    def test_no_guard_no_mount(self):
        # the device cannot be made read-only, its state cannot be read or is not 0/1, or the flag does not take:
        # refuse, never fall back to a plain mount, and leave nothing behind
        for flag in ("setro-fails", "getro-fails", "getro-garbage", "setro-noop"):
            self.flag(flag)
            r = self.priv("mount", DEV, "look", "ro,noload")
            self.assertEqual(r.returncode, 2, flag + r.stdout + r.stderr)
            self.assertEqual(self.calls("mount"), [], flag)
            self.assertEqual(self.markers(), [], flag)
            self.assertFalse((self.out / "mnt" / "look").exists(), flag)
            self.flag(flag, False)

    def test_the_note_for_umount_is_written_before_the_flag_is_set(self):
        # the other order leaves a read-only device nobody hands back when the helper dies in between
        self.priv("mount", DEV, "look", "ro")
        self.assertIn(".ro-loop987p3", (self.state / "markers-at-setro").read_text())

    def test_a_writable_filesystem_is_not_given_a_read_only_view(self):
        # the device is mounted read-write somewhere already: ext4 would answer EBUSY with the flag set under a live
        # filesystem, btrfs would hand out a "read-only" view of a filesystem that keeps changing
        self.priv("mount", DEV, "work")
        r = self.priv("mount", DEV, "look", "ro")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("mounted already", r.stderr)
        self.assertEqual(self.calls("blockdev --setro"), [])
        self.assertEqual(len(self.calls("mount")), 1)

    def test_no_answer_about_other_mounts_no_mount(self):
        self.flag("findmnt-fails")
        r = self.priv("mount", DEV, "look", "ro")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("cannot tell", r.stderr)
        self.assertEqual(self.calls("blockdev --setro") + self.calls("mount"), [])

    def test_remount_is_refused(self):
        self.priv("mount", DEV, "look", "ro")
        r = self.priv("mount", DEV, "look", "remount,rw")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.calls("mount")), 1)

    def test_a_failed_umount_still_hands_back_what_nothing_has_mounted(self):
        # the mount went away without the helper (a plain umount, a session that was killed): `priv.sh umount` then
        # fails, and used to stop before the hand-back, leaving the partition read-only for e2fsck and resize2fs
        self.priv("mount", DEV, "look", "ro")
        (self.state / "mp-look").unlink()
        r = self.priv("umount", "look")
        self.assertEqual(r.returncode, 32)
        self.assertEqual(self.ro(), "0")
        self.assertEqual(self.markers(), [])

    def test_a_card_partition_is_handed_back_too(self):
        # the incident device was /dev/sdb3; a note left for it by a run that died is honoured by the next umount
        (self.out / "mnt").mkdir()
        (self.out / "mnt" / ".ro-sdb3").write_text("/dev/sdb3\n")
        (self.out / "mnt" / ".ro-mmcblk0p3").write_text("/dev/mmcblk0p3\n")
        (self.out / "mnt" / ".ro-nvme0n1p3").write_text("/dev/nvme0n1p3\n")      # never ours: dropped, not touched
        self.priv("umount", "nothing")
        self.assertEqual(sorted(self.calls("blockdev --setrw")), ["blockdev --setrw /dev/mmcblk0p3", "blockdev --setrw /dev/sdb3"])
        self.assertEqual(self.markers(), [])

    def test_when_findmnt_cannot_tell_the_guard_stays(self):
        self.priv("mount", DEV, "froot", "ro")
        self.priv("mount", DEV, "fhome", "ro")
        self.flag("findmnt-fails")
        self.priv("umount", "fhome")
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.markers(), [".ro-loop987p3"])
        self.flag("findmnt-fails", False)
        self.priv("umount", "froot")
        self.assertEqual(self.ro(), "0")

    def test_an_umount_in_another_session_cannot_strip_the_guard_of_a_mount_in_flight(self):
        # reviewer, 2026-11-15: between --setro and the filesystem coming up, nothing has the device mounted yet, so a
        # `priv.sh umount <anything>` elsewhere (an image build's cleanup) handed it back: writable under ext4 again
        self.priv("mount", "/dev/loop5", "other")
        self.flag("mount-slow")
        slow = subprocess.Popen(["bash", str(self.script), "mount", DEV, "look", "ro,noload"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.4)
        self.assertEqual(self.priv("umount", "other").returncode, 0)
        out, err = slow.communicate(timeout=30)
        self.assertEqual(slow.returncode, 0, out + err)
        self.assertEqual((self.state / "ro-at-mount").read_text().strip(), "1")
        self.assertEqual(self.ro(), "1")
        self.assertEqual(self.markers(), [".ro-loop987p3"])

    def test_a_flag_that_is_gone_once_the_filesystem_is_up_is_not_passed_off_as_a_read_only_mount(self):
        # a partition table re-read between the flag and the mount resets a partition's flag: say so, loudly
        self.flag("flag-lost")
        r = self.priv("mount", DEV, "look", "ro,noload")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("WRITABLE", r.stderr)
        self.assertNotIn("wrote nothing", r.stderr)
        self.assertEqual(self.calls("umount"), [f"umount {self.out}/mnt/look"])
        self.assertEqual(self.markers(), [])


class TestPrivChrootRun(unittest.TestCase):
    """chroot-run in a scratch tree. bwrap is faked: it logs its options and runs the command with the image's
    directory in $ROOT instead of entering it; mountpoint says yes for the image; chown only logs.
    2026-11-16: until that night the command ran in a bare chroot with the build machine's /dev, /sys and /proc bound
    in. A `dnf upgrade` in the image ran grub2's os-prober that way against every disk of the LAPTOP: it mounted the
    owner's USB stick read-write twice, modified the Fedora source image and read his only SD card."""

    FAKES = {
        "id": 'echo 0\n',
        "mountpoint": '[ ! -e "$FAKE_STATE/not-mounted" ]\n',
        "mount": 'echo "mount $*" >> "$FAKE_STATE/calls.log"\n',
        "umount": 'echo "umount $*" >> "$FAKE_STATE/calls.log"\n',
        "chown": 'echo "chown $*" >> "$FAKE_STATE/calls.log"\n',
        "chroot": 'echo "chroot $*" >> "$FAKE_STATE/calls.log"; exit 99\n',
        "bwrap": r"""echo "bwrap $*" >> "$FAKE_STATE/calls.log"
while [ $# -gt 0 ]; do
  case "$1" in
    --bind) [ "$3" = / ] && ROOT=$2; shift 3 ;;
    --setenv) shift 3 ;;
    --dev|--proc|--tmpfs|--cap-add|--cap-drop|--chdir|--hostname) shift 2 ;;
    --*) shift ;;
    *) break ;;
  esac
done
export ROOT
# like the real one, the sandbox dies with bwrap: every process in it, not only the first
setsid "$@" & c=$!
trap 'kill -KILL -- -$c 2>/dev/null; exit 143' TERM INT HUP
wait $c
""",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.script = root / "tools" / "priv.sh"
        self.script.parent.mkdir()
        self.script.write_text((P / "tools" / "priv.sh").read_text())
        self.out = root / "output"
        self.img = self.out / "mnt" / "root"
        for d in ("etc", "proc", "sys", "dev", "run", "tmp", "var/cache"):
            (self.img / d).mkdir(parents=True)
        self.state = root / "state"
        self.state.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in self.FAKES.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def priv(self, *args):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=self.env, timeout=60)

    def calls(self, prefix=""):
        f = self.state / "calls.log"
        return [c for c in (f.read_text().splitlines() if f.exists() else []) if c.startswith(prefix)]

    def bwrap_words(self):
        return self.calls("bwrap")[-1].split()

    def test_the_command_gets_a_dev_and_sys_of_its_own_and_nothing_of_the_build_machine(self):
        r = self.priv("chroot-run", "root", "echo hello from $ROOT")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"hello from {self.img}", r.stdout)
        w = self.bwrap_words()
        line = " ".join(w)
        self.assertIn(f"--bind {self.img} /", line)
        for own in ("--dev /dev", "--proc /proc", "--tmpfs /sys", "--tmpfs /run", "--tmpfs /tmp"):
            self.assertIn(own, line)
        for flag in ("--unshare-pid", "--die-with-parent"):
            self.assertIn(flag, w)
        # it can neither mount what it finds nor make itself a device node
        for cap in ("CAP_SYS_ADMIN", "CAP_MKNOD", "CAP_SYS_RAWIO", "CAP_SYS_MODULE"):
            self.assertIn(f"--cap-drop {cap}", line)
        # the build machine's /dev, /sys and /proc are not handed in, in any spelling, and nothing is mounted outside
        # the sandbox (what is mounted there cannot be left behind by a failure or a signal)
        for word in w:
            self.assertNotIn(word, ("--dev-bind", "--dev-bind-try"))
        binds = [(w[i + 1], w[i + 2]) for i, x in enumerate(w) if x in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try")]
        self.assertEqual(binds, [(str(self.img), "/")])
        self.assertEqual(self.calls("mount") + self.calls("umount") + self.calls("chroot"), [])

    def test_a_command_that_fails_gives_its_exit_code_and_leaves_nothing(self):
        r = self.priv("chroot-run", "root", "exit 7")
        self.assertEqual(r.returncode, 7)
        self.assertFalse((self.img / "etc/resolv.conf").exists())
        self.assertEqual(self.calls("mount") + self.calls("umount"), [])

    def test_the_build_machines_resolv_conf_is_there_for_the_command_and_gone_after_it(self):
        # Fedora's image has no /etc/resolv.conf: tmpfiles links it to systemd-resolved's at the first boot. The copy
        # the helper made for dnf stayed in every image until 2026-11-15, the build machine's "search home" included
        r = self.priv("chroot-run", "root", 'test -s "$ROOT/etc/resolv.conf" && echo present')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("present", r.stdout)
        self.assertFalse((self.img / "etc/resolv.conf").exists())
        self.assertEqual(self.priv("chroot-run", "root", "exit 3").returncode, 3)
        self.assertFalse((self.img / "etc/resolv.conf").exists())

    def test_a_signal_ends_the_command_and_still_takes_the_copy_away(self):
        # the update inside the image runs for half an hour: that is when someone presses Ctrl-C
        for sig in ("TERM", "INT", "HUP"):
            p = subprocess.Popen(["bash", str(self.script), "chroot-run", "root", "echo started; sleep 20; echo finished"],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(p.stdout.readline().strip(), "started")
            self.assertTrue((self.img / "etc/resolv.conf").exists())
            t0 = time.time()
            subprocess.run(["kill", f"-{sig}", str(p.pid)], check=True)
            out, err = p.communicate(timeout=15)
            self.assertLess(time.time() - t0, 10, sig)
            self.assertNotEqual(p.returncode, 0, sig)
            self.assertNotIn("finished", out, sig)
            self.assertFalse((self.img / "etc/resolv.conf").exists(), sig)

    def test_a_resolv_conf_the_system_has_is_left_alone(self):
        # a card that has been in use (NetworkManager's file), or a link into /run: used to be overwritten
        (self.img / "etc/resolv.conf").write_text("nameserver 192.168.50.183\n")
        self.priv("chroot-run", "root", "true")
        self.assertEqual((self.img / "etc/resolv.conf").read_text(), "nameserver 192.168.50.183\n")
        (self.img / "etc/resolv.conf").unlink()
        (self.img / "etc/resolv.conf").symlink_to("/etc/passwd")            # a link that resolves on the build machine
        self.priv("chroot-run", "root", "true")
        self.assertTrue((self.img / "etc/resolv.conf").is_symlink())
        self.assertEqual(os.readlink(self.img / "etc/resolv.conf"), "/etc/passwd")

    def test_dnfs_cache_can_live_outside_the_image(self):
        # 2026-11-16: `dnf upgrade` in the image downloaded 2 GiB into the image's own /var/cache and then lacked 600 MB
        # for the transaction (a 12 GB image). With --cache a directory under output/ is the image's cache for the
        # command: nothing of it lands in the image, and the next build finds the packages there
        cache = self.out / "dnf-cache"
        cache.mkdir()
        r = self.priv("chroot-run", "root", "--cache", str(cache), "exit 5")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn(f"--bind {cache} /var/cache/libdnf5", " ".join(self.bwrap_words()))
        self.assertTrue((self.img / "var/cache/libdnf5").is_dir())
        # back to the owner of output/, and only after the sandbox is gone
        log = self.calls()
        chown = [i for i, c in enumerate(log) if c.startswith("chown -R ") and c.endswith(str(cache))]
        self.assertEqual(len(chown), 1, log)
        self.assertGreater(chown[0], max(i for i, c in enumerate(log) if c.startswith("bwrap")))

    def test_what_is_refused(self):
        cache = self.out / "dnf-cache"
        cache.mkdir()
        inside = self.img / "var/cache"
        for args, word in ((("chroot-run", "..", "true"), "mount name"),
                           (("chroot-run", "root/../..", "true"), "mount name"),
                           (("chroot-run", "nothere", "true"), "missing"),
                           (("chroot-run", "root"), "no command"),
                           (("chroot-run", "root", "--cache"), "--cache"),
                           (("chroot-run", "root", "--cache", str(cache)), "no command"),
                           (("chroot-run", "root", "--cache", "/var/cache/libdnf5", "true"), "not under"),
                           (("chroot-run", "root", "--cache", str(self.out / "nothing-here"), "true"), "cache"),
                           (("chroot-run", "root", "--cache", str(inside), "true"), "mount"),          # chown -R over the image
                           (("chroot-run", "root", "--cache", str(self.out / "mnt"), "true"), "mount")):
            r = self.priv(*args)
            self.assertEqual(r.returncode, 2, f"{args}: {r.stdout}{r.stderr}")
            self.assertIn(word, r.stderr, args)
        self.assertEqual(self.calls("bwrap") + self.calls("chown"), [])
        # a directory that is no mounted filesystem (the leftover of a failed mount) is not an image
        (self.state / "not-mounted").touch()
        r = self.priv("chroot-run", "root", "true")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls("bwrap"), [])


class TestPrivCopySystem(unittest.TestCase):
    """copy-system and trim, the two operations of the image made from a card that has been in use
    (tools/master-image.sh). rsync, fstrim, mountpoint and findmnt are faked."""

    FAKES = {
        "id": 'echo 0\n',
        "mountpoint": '[ ! -e "$FAKE_STATE/not-mounted" ]\n',
        "findmnt": 'if [ -e "$FAKE_STATE/source-rw" ]; then echo "rw,relatime"; else echo "ro,relatime"; fi\n',
        "rsync": 'echo "rsync $*" >> "$FAKE_STATE/calls.log"\n',
        "fstrim": 'echo "fstrim $*" >> "$FAKE_STATE/calls.log"\n',
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.script = root / "tools" / "priv.sh"
        self.script.parent.mkdir()
        self.script.write_text((P / "tools" / "priv.sh").read_text())
        self.rules = root / "tools" / "master-filter.rules"
        self.rules.write_text("- /home/*/**\n")
        self.out = root / "output"
        self.src = self.out / "mnt" / "msrc"
        self.dst = self.out / "mnt" / "mroot"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        (self.dst / "lost+found").mkdir()          # a fresh ext4
        self.state = root / "state"
        self.state.mkdir()
        self.bin = root / "bin"
        self.bin.mkdir()
        for name, body in self.FAKES.items():
            f = self.bin / name
            f.write_text("#!/bin/bash\n" + body)
            f.chmod(f.stat().st_mode | stat.S_IEXEC)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def priv(self, *args):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=self.env, timeout=60)

    def test_a_write_into_the_image_cannot_be_led_out_of_it_by_the_images_own_links(self):
        # copy-dir and write-file run out here as root. A card whose /home is a link to /var/home (as on image-based
        # Fedora), or whose /var/tmp points to /tmp, would have sent "home/radxa/dsu" to this machine's directory
        outside = Path(self.tmp.name) / "this-machine"
        outside.mkdir()
        (self.dst / "home").symlink_to(outside)                       # an absolute link, as the card has it
        (self.dst / "usr/local").mkdir(parents=True)
        (self.dst / "usr/local/bin").mkdir()
        (self.dst / "usr/local/sbin").symlink_to("bin")               # a link that stays inside is fine
        payload = self.out / "ddk" / "dsu"
        payload.mkdir(parents=True)
        (payload / "x.ko").write_text("module")
        r = self.priv("copy-dir", str(payload), "mroot", "home/radxa/dsu")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("leads out of the image", r.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        r = subprocess.run(["bash", str(self.script), "write-file", "mroot", "home/radxa/.bashrc"], input="x", capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        r = subprocess.run(["bash", str(self.script), "write-file", "mroot", "usr/local/sbin/a7a-grow-rootfs"], input="#!/bin/sh\n", capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.dst / "usr/local/bin/a7a-grow-rootfs").read_text(), "#!/bin/sh\n")

    def calls(self, prefix=""):
        f = self.state / "calls.log"
        return [c for c in (f.read_text().splitlines() if f.exists() else []) if c.startswith(prefix)]

    def test_copies_the_system_through_the_rules_and_keeps_everything_else_as_it_is(self):
        r = self.priv("copy-system", "msrc", "mroot", str(self.rules))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        (call,) = self.calls("rsync")
        w = call.split()
        self.assertIn(f"--filter=merge {self.rules}", call)
        self.assertEqual(w[-2:], [f"{self.src}/", f"{self.dst}/"])
        for opt in ("--numeric-ids",):
            self.assertIn(opt, w)
        flags = next(x for x in w[1:] if x.startswith("-") and not x.startswith("--"))
        for letter in "aAXHx":                      # archive, ACLs, xattrs, hard links, and never across a mount
            self.assertIn(letter, flags)
        self.assertNotIn("--delete", call)

    def test_what_is_refused(self):
        outside = Path(self.tmp.name) / "rules-elsewhere"
        outside.write_text("- /nothing\n")
        for args, word in ((("copy-system", "../x", "mroot", str(self.rules)), "mount name"),
                           (("copy-system", "msrc", "mroot", str(outside)), "rules"),
                           (("copy-system", "msrc", "mroot", str(self.rules) + ".missing"), "rules"),
                           (("copy-system", "msrc", "mroot"), "rules"),
                           (("trim", "../x"), "mount name")):
            r = self.priv(*args)
            self.assertEqual(r.returncode, 2, f"{args}: {r.stdout}{r.stderr}")
            self.assertIn(word, r.stderr, args)
        # the source of a card's system is a read-only mount: the copy must not be able to change the backup
        (self.state / "source-rw").touch()
        r = self.priv("copy-system", "msrc", "mroot", str(self.rules))
        self.assertEqual(r.returncode, 2)
        self.assertIn("read-only", r.stderr)
        (self.state / "source-rw").unlink()
        # into a new, empty filesystem only
        (self.dst / "etc").mkdir()
        r = self.priv("copy-system", "msrc", "mroot", str(self.rules))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not empty", r.stderr)
        (self.dst / "etc").rmdir()
        (self.state / "not-mounted").touch()
        for args in (("copy-system", "msrc", "mroot", str(self.rules)), ("trim", "mroot")):
            self.assertEqual(self.priv(*args).returncode, 2, args)
        self.assertEqual(self.calls("rsync") + self.calls("fstrim"), [])

    def test_trim_gives_the_free_space_back_to_the_image_file(self):
        r = self.priv("trim", "mroot")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls("fstrim"), [f"fstrim -v {self.dst}"])


if __name__ == "__main__":
    unittest.main()
