"""Tests for the update stage: a newer kernel onto a running a7a-fedora board over ssh as its RPM (since 2026-09-10;
docs/PACKAGING-2026-09-09.md, "Item B follow-ups"), fallback kept, rollback."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.registry import StageRegistry
from src.stages import update


class FakeRunner:
    """Answers ssh/scp argv by matching substrings of the remote command; records every call."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def run(self, argv, timeout=30):
        self.calls.append(argv)
        joined = " ".join(argv)
        for i, (needle, answer) in enumerate(self.script):
            if needle in joined:
                if answer[0] == "once":
                    self.script.pop(i)
                    return answer[1], answer[2]
                return answer
        return 255, "ssh: connect to host: No route"


def _payload(cfg: Config, rel="6.6.99-a7apvr.202609101200"):
    pay = cfg.payload_dir
    (pay / "boot").mkdir(parents=True)
    (pay / "boot" / f"Image-{rel}").write_bytes(b"kernel RGDB")
    (pay / "kernelrelease").write_text(rel + "\n")


KERNEL_RPM = "kernel-a7apvr-6.6.99_a7apvr.202609101200-202609101230.aarch64.rpm"
OLDER_BUILD = "kernel-a7apvr-6.6.99_a7apvr.202609101200-202609101205.aarch64.rpm"
EXTLINUX_RPM = "a7a-extlinux-20260910-202609101230.noarch.rpm"


def _rpms(cfg: Config, names=(KERNEL_RPM, OLDER_BUILD, EXTLINUX_RPM, "a7a-extlinux-20260909-202609092306.noarch.rpm",
                              "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm")):
    d = cfg.output_dir / "rpms"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"rpm " + n.encode())


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(project_root=root, output_dir=root / "output", work_dir=root / "work",
                          tools_dir=root / "tools", registry_path=root / "registry.json")
        self.cfg.tools_dir.mkdir()
        self.cfg.board_host = "192.168.1.50"
        self.cfg.board_tailscale = "100.101.102.103"
        self.cfg.update_poll_seconds = 0.01
        self.cfg.update_poll_timeout = 1
        self.reg = StageRegistry(self.cfg.registry_path)
        _payload(self.cfg)
        _rpms(self.cfg)
        self.good = [
            ("scp ", (0, "")),
            ("uname -r", ("once", 0, "6.6.98-a7apvr.202609091821\nradxa-cubie-a7a\nPRETTY_NAME=\"Fedora Linux 44 (KDE Plasma Desktop Edition)\"\n")),
            ("mkdir -p /tmp/a7a-update", (0, "")),
            ("a7a-update-install", (0, "installed 6.6.99-a7apvr.202609101200\n* fedora-6.6.99-a7apvr.202609101200 [1]\n")),
            ("systemctl reboot", (255, "Connection to 192.168.1.50 closed by remote host.")),
            ("uname -r", (0, "6.6.99-a7apvr.202609101200\n")),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def test_refuses_when_the_board_already_runs_this_release(self):
        runner = FakeRunner([("uname -r", (0, "6.6.99-a7apvr.202609101200\nradxa\nPRETTY_NAME=\"Fedora Linux 44\"\n"))])
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "skipped")
        self.assertIn("already runs 6.6.99-a7apvr.202609101200", r["reason"])

    def test_unreachable_board_fails_early(self):
        r = update.run(self.cfg, self.reg, runner=FakeRunner([]))
        self.assertEqual(r["status"], "failed")
        self.assertIn("not reachable", r["reason"])

    def test_refuses_a_board_that_is_not_a7a_fedora(self):
        runner = FakeRunner([("uname -r", (0, "6.6.98+\nradxa\nPRETTY_NAME=\"Debian GNU/Linux 13 (trixie)\"\n"))])
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "refused")
        self.assertIn("Debian", r["reason"])

    def test_no_rpm_for_the_payload_is_a_clean_failure(self):
        for f in (self.cfg.output_dir / "rpms").glob("kernel-a7apvr-*"):
            f.unlink()
        runner = FakeRunner(self.good)
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "failed")
        self.assertIn("build-rpm.sh", r["reason"])
        self.assertIn("6.6.99-a7apvr.202609101200", r["reason"])
        self.assertEqual(runner.calls, [])            # decided before the board is touched

    def test_picks_the_newest_build_of_the_payload_release(self):
        self.assertEqual(update.kernel_rpm_for(self.cfg, "6.6.99-a7apvr.202609101200").name, KERNEL_RPM)
        self.assertEqual(update.kernel_rpm_for(self.cfg, "6.6.98-a7a.202609091931").name, "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm")
        self.assertIsNone(update.kernel_rpm_for(self.cfg, "6.6.98-a7a.202609072057"))
        self.assertEqual(update.release_of(Path(KERNEL_RPM)), "6.6.99-a7apvr.202609101200")
        self.assertEqual(update.release_of(Path("kernel-a7a-6.6.98.plus-202609062352.aarch64.rpm")), "6.6.98+")
        self.assertIsNone(update.release_of(Path("mesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm")))

    def test_transfer_install_reboot_confirm(self):
        # the first post-reboot poll fails (board still down), the second answers with the new release
        script = self.good[:5] + [("uname -r", ("once", 255, "refused")), ("uname -r", (0, "6.6.99-a7apvr.202609101200\n"))]
        runner = FakeRunner(script)
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["release"], "6.6.99-a7apvr.202609101200")
        self.assertEqual(r["previous_release"], "6.6.98-a7apvr.202609091821")
        self.assertEqual(r["rpms"], [KERNEL_RPM, EXTLINUX_RPM])      # the newest kernel build for the payload + the newest a7a-extlinux
        joined = [" ".join(c) for c in runner.calls]
        scp = next(c for c in joined if c.startswith("scp "))
        self.assertIn(f"output/rpms/{KERNEL_RPM}", scp)
        self.assertIn(f"output/rpms/{EXTLINUX_RPM}", scp)
        self.assertNotIn(OLDER_BUILD, scp)
        self.assertTrue(scp.endswith(":/tmp/a7a-update/"), scp)
        self.assertNotIn("tar.gz", scp)
        self.assertFalse((self.cfg.output_dir / "updates").exists())      # no tarball any more
        install = next(c for c in joined if "a7a-update-install" in c)
        self.assertIn("bash -s 6.6.99-a7apvr.202609101200 0 0", install)
        self.assertIn("kernel-uname-r = $REL", install)                    # the package must be the kernel it claims
        self.assertIn("A7A_DEFAULT=1 dnf install -y --disablerepo='*'", install)
        self.assertIn("dnf reinstall", install)                            # the --force path
        self.assertNotIn("depmod", install)                                # the package's %post does that
        self.assertNotIn("install -m 755 /tmp/a7a-extlinux", install)      # a7a-extlinux is package-owned
        self.assertNotIn("pvrsrvkm-fdinfo-fixed", install)                 # kernel-a7a ships the marker
        self.assertNotIn("prune", install.split("PRUNE_KEEP")[0])          # no automatic prune
        self.assertIn('$1 == "*" && $2 == l', install)                     # the new kernel must be the default before the reboot
        self.assertTrue(any("systemctl reboot" in c for c in joined))
        self.assertEqual(r["ssh_after"]["release"], "6.6.99-a7apvr.202609101200")
        self.assertEqual(self.reg.get_stage("update")["status"], "passed")
        self.assertEqual(self.reg.data["board"]["release"], "6.6.99-a7apvr.202609101200")
        self.assertEqual(self.reg.data["board"]["previous_release"], "6.6.98-a7apvr.202609091821")
        self.assertEqual(self.reg.data["board"]["rpms"], [KERNEL_RPM, EXTLINUX_RPM])

    def test_explicit_rpm_and_force_and_prune(self):
        rpm = self.cfg.output_dir / "rpms" / "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm"
        runner = FakeRunner([("scp ", (0, "")), ("mkdir -p /tmp/a7a-update", (0, "")),
                             ("uname -r", ("once", 0, "6.6.98-a7apvr.202609091821\nradxa\nPRETTY_NAME=\"Fedora Linux 44\"\n")),
                             ("a7a-update-install", (0, "installed\n")), ("systemctl reboot", (255, "closed")),
                             ("uname -r", (0, "6.6.98-a7a.202609091931\n"))])
        r = update.run(self.cfg, self.reg, runner=runner, rpm=str(rpm), force=True, prune_keep=4)
        self.assertEqual(r["status"], "passed", r)
        self.assertEqual(r["release"], "6.6.98-a7a.202609091931")      # from the package name, not the payload
        joined = [" ".join(c) for c in runner.calls]
        install = next(c for c in joined if "a7a-update-install" in c)
        self.assertIn("bash -s 6.6.98-a7a.202609091931 1 4", install)
        self.assertIn(rpm.name, next(c for c in joined if c.startswith("scp ")))

    def test_prune_keep_below_2_is_refused_before_the_board_is_touched(self):
        runner = FakeRunner(self.good)
        r = update.run(self.cfg, self.reg, runner=runner, prune_keep=1)
        self.assertEqual(r["status"], "failed")
        self.assertIn("at least 2", r["reason"])
        self.assertEqual(runner.calls, [])

    def test_explicit_rpm_must_be_a_kernel_package_that_exists(self):
        mesa = self.cfg.output_dir / "rpms" / "mesa-pvr-a7a-26.1.8-202609091222.aarch64.rpm"
        mesa.write_bytes(b"rpm")
        r = update.run(self.cfg, self.reg, runner=FakeRunner(self.good), rpm=str(mesa))
        self.assertEqual(r["status"], "failed")
        self.assertIn("not a kernel-a7a", r["reason"])
        r = update.run(self.cfg, self.reg, runner=FakeRunner(self.good), rpm="/nowhere/kernel-a7a-1-2.aarch64.rpm")
        self.assertEqual(r["status"], "failed")
        self.assertIn("no such file", r["reason"])

    def test_install_failure_stops_before_the_reboot(self):
        script = self.good[:3] + [("a7a-update-install", (1, "fedora-6.6.99-a7apvr.202609101200 is not the default extlinux entry: not rebooting\n"))]
        runner = FakeRunner(script)
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "failed")
        self.assertIn("install failed", r["reason"])
        self.assertIn("not the default", r["reason"])
        self.assertFalse(any("systemctl reboot" in " ".join(c) for c in runner.calls))

    def test_no_answer_after_reboot_without_serial_gives_the_manual_rollback(self):
        runner = FakeRunner(self.good[:5])
        r = update.run(self.cfg, self.reg, runner=runner)
        self.assertEqual(r["status"], "failed")
        self.assertIn("did not come back", r["reason"])
        self.assertIn("entry 2", r["next_step"])

    def test_serial_failure_rolls_back_through_the_fallback_entry(self):
        self.cfg.serial_port = "/dev/ttyFAKE"
        watches = [{"verdict": "FAIL", "reason": "kernel panic: boom", "milestones": ["uboot", "kernel", "panic"], "entry": "1", "events": [], "checklist": {}, "seconds": 3, "log": "/l1"},
                   {"verdict": "PASS", "reason": "first-boot checklist reported", "milestones": ["uboot", "menu", "kernel", "checklist"], "entry": "2", "events": [], "checklist": {}, "seconds": 30, "log": "/l2"}]
        asked = []

        def watcher(choose_entry=None):
            asked.append(choose_entry)
            return watches.pop(0)
        script = self.good[:5] + [("a7a-extlinux fallback", (0, "default is now fedora\n6.6.98-a7apvr.202609091821\n")), ("uname -r", (0, "6.6.98-a7apvr.202609091821\n"))]
        runner = FakeRunner(script)
        r = update.run(self.cfg, self.reg, runner=runner, watcher=watcher)
        self.assertEqual(r["status"], "rolled_back", r)
        self.assertEqual(asked, [None, "2"])
        self.assertTrue(any("a7a-extlinux fallback" in " ".join(c) for c in runner.calls))
        self.assertIn("6.6.98-a7apvr.202609091821", r["reason"])
        self.assertEqual(self.reg.data["board"]["release"], "6.6.98-a7apvr.202609091821")

    def test_serial_pass_confirms(self):
        self.cfg.serial_port = "/dev/ttyFAKE"
        watcher = lambda choose_entry=None: {"verdict": "PASS", "reason": "checklist", "milestones": ["checklist"], "entry": "1", "events": [], "checklist": {"modules": "40"}, "seconds": 40, "log": "/l"}
        runner = FakeRunner(self.good)
        r = update.run(self.cfg, self.reg, runner=runner, watcher=watcher)
        self.assertEqual(r["status"], "passed")
        self.assertEqual(r["serial"]["entry"], "1")

    def test_dry_run_lists_every_command_and_sends_nothing(self):
        runner = FakeRunner(self.good)
        r = update.run(self.cfg, self.reg, runner=runner, dry_run=True)
        self.assertEqual(r["status"], "dry_run")
        self.assertTrue(any("scp" in c and KERNEL_RPM in c for c in r["commands"]))
        self.assertTrue(any("systemctl reboot" in c for c in r["commands"]))
        self.assertEqual(r["rpms"], [KERNEL_RPM, EXTLINUX_RPM])
        self.assertEqual(len(runner.calls), 1)            # only the reachability check runs

    def test_no_board_configured(self):
        self.cfg.board_host = self.cfg.board_tailscale = None
        r = update.run(self.cfg, self.reg, runner=FakeRunner([]))
        self.assertEqual(r["status"], "failed")
        self.assertIn("board_host", r["reason"])


if __name__ == "__main__":
    unittest.main()


INSTALL_FAKES = {
    # rpm: NAME/VERSION/RELEASE/ARCH from the file name; the kernel packages provide kernel-uname-r = <release>;
    # `rpm -q` answers from $FAKE_STATE/installed (one NEVRA per line)
    "rpm": r'''#!/bin/bash
    S="$FAKE_STATE"; echo "rpm $*" >> "$S/calls.log"
    nevra_of() { local b; b=$(basename "$1" .rpm); echo "$b"; }
    fields() { local b n v r a rest; b=$(basename "$1" .rpm); a=${b##*.}; rest=${b%.*}; r=${rest##*-}; rest=${rest%-*}; v=${rest##*-}; n=${rest%-*}; echo "$n" "$v" "$r" "$a"; }
    case "$1 $2" in
      "-qp --qf")
        read -r n v r a <<<"$(fields "$4")"
        case "$3" in "%{NAME}") echo "$n" ;; "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}") echo "$n-$v-$r.$a" ;; *) echo "fake rpm: qf $3" >&2; exit 1 ;; esac ;;
      "-qp --provides")
        read -r n v r a <<<"$(fields "$3")"
        case "$n" in kernel-a7a|kernel-a7apvr) rel=${v//.plus/+}; rel=${rel//_/-}; echo "kernel-uname-r = $rel"; echo "installonlypkg(kernel)" ;; esac ;;
      "-q "*)
        q=$2; touch "$S/installed"
        if grep -qx "$q" "$S/installed"; then echo "$q"; exit 0; fi
        m=$(grep -E "^$q-[0-9]" "$S/installed" | head -1); [ -n "$m" ] && { echo "$m"; exit 0; }
        echo "package $q is not installed"; exit 1 ;;
      *) echo "fake rpm: unexpected $*" >&2; exit 1 ;;
    esac
    ''',
    # dnf: records the NEVRAs as installed and runs the kernel package's %post (a7a-extlinux add, --default with A7A_DEFAULT=1)
    "dnf": r'''#!/bin/bash
    S="$FAKE_STATE"; echo "dnf $* A7A_DEFAULT=${A7A_DEFAULT:-}" >> "$S/calls.log"
    cmd=$1; shift
    for a in "$@"; do
      case "$a" in *.rpm) ;; *) continue ;; esac
      b=$(basename "$a" .rpm); echo "$b" >> "$S/installed"
      case "$b" in kernel-a7a-*|kernel-a7apvr-*)
        rel=$(rpm -qp --provides "$a" | sed -n 's/^kernel-uname-r = //p')
        if [ "${A7A_DEFAULT:-0}" = 1 ]; then sudo /usr/local/bin/a7a-extlinux --check-files add "$rel" --default; else sudo /usr/local/bin/a7a-extlinux --check-files add "$rel"; fi ;;
      esac
    done
    ''',
    # sudo: `env K=V cmd` exports and runs; /usr/local/bin/a7a-extlinux is the repo tool on the fake conf and root
    "sudo": r'''#!/bin/bash
    [ "$1" = env ] && { shift; while [[ "${1:-}" == *=* ]]; do export "$1"; shift; done; }
    if [ "$1" = /usr/local/bin/a7a-extlinux ]; then shift; exec python3 "$FAKE_REPO/tools/a7a-extlinux" --conf "$FAKE_STATE/extlinux.conf" --root "$FAKE_STATE/root" "$@"; fi
    exec "$@"
    ''',
}

CONF = """timeout 50
prompt 0
default fedora-6.6.98-a7apvr.202609091821
menu title a7a-fedora

label fedora-6.6.98-a7apvr.202609091821
    menu label Fedora 44 KDE, Linux 6.6.98-a7apvr.202609091821
    linux /boot/vmlinuz-6.6.98-a7apvr.202609091821
    fdtdir /usr/lib/linux-image-6.6.98-a7apvr.202609091821/
    append root=/dev/mmcblk0p3 rootwait

label fedora-6.6.98-a7a.202609091931
    menu label Fedora 44 KDE, Linux 6.6.98-a7a.202609091931
    linux /boot/vmlinuz-6.6.98-a7a.202609091931
    fdtdir /usr/lib/linux-image-6.6.98-a7a.202609091931/
    append root=/dev/mmcblk0p3 rootwait

label fedora-recovery
    menu label Fedora 44 KDE, Linux 6.6.98-a7apvr.202609091821, single user, no desktop
    linux /boot/vmlinuz-6.6.98-a7apvr.202609091821
    fdtdir /usr/lib/linux-image-6.6.98-a7apvr.202609091821/
    append root=/dev/mmcblk0p3 rootwait systemd.unit=multi-user.target
"""


class TestInstallScript(unittest.TestCase):
    """INSTALL_SCRIPT run by bash with fake rpm/dnf/sudo and the REAL tools/a7a-extlinux on a copy of the owner's
    entry layout ([1] open stack = default, [2] DDK, [3] recovery): the entry order after each path is what the
    rollback ("press 2 at the menu") relies on, and the string-matching tests above cannot see it."""

    PVR_NEW = "kernel-a7apvr-6.6.99_a7apvr.202609101200-202609101230.aarch64.rpm"
    DDK_OLD = "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm"
    EXT = "a7a-extlinux-20260910-202609101230.noarch.rpm"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state"
        self.src = Path(self.tmp.name) / "a7a-update"          # stands in for /tmp/a7a-update
        self.bin = Path(self.tmp.name) / "bin"
        for d in (self.state, self.src, self.bin):
            d.mkdir()
        for name, body in INSTALL_FAKES.items():
            f = self.bin / name
            f.write_text("\n".join(l[4:] if l.startswith("    ") else l for l in body.splitlines()) + "\n")
            f.chmod(0o755)
        (self.state / "extlinux.conf").write_text(CONF)
        root = self.state / "root"
        for rel in ["6.6.98-a7apvr.202609091821", "6.6.98-a7a.202609091931", "6.6.99-a7apvr.202609101200"]:
            (root / "boot").mkdir(parents=True, exist_ok=True)
            (root / "boot" / f"vmlinuz-{rel}").write_bytes(b"k")
            (root / "usr/lib" / f"linux-image-{rel}" / "allwinner").mkdir(parents=True)
        (self.state / "installed").write_text("kernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64\n"
                                              "kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64\n"
                                              "a7a-extlinux-20260909-202609092306.noarch\n")

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, rel, *rpms, force=0, prune=0):
        for r in rpms:
            (self.src / r).write_bytes(b"rpm")
        script = update.INSTALL_SCRIPT.replace("SRC=/tmp/a7a-update", f"SRC={self.src}")
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_STATE=str(self.state),
                   FAKE_REPO=str(Path(__file__).resolve().parent.parent))
        return subprocess.run(["bash", "-s", rel, str(force), str(prune)], input=script, capture_output=True, text=True, env=env, timeout=120)

    def entries(self):
        out = subprocess.run(["python3", str(Path(__file__).resolve().parent.parent / "tools/a7a-extlinux"), "--conf", str(self.state / "extlinux.conf"), "list"],
                             capture_output=True, text=True, check=True).stdout
        return [(l.split()[0] == "*", l.split()[1]) if l.startswith("*") else (False, l.split()[0]) for l in out.strip().splitlines()]

    def calls(self):
        return (self.state / "calls.log").read_text().splitlines()

    def test_new_kernel_becomes_entry_1_and_the_running_one_entry_2(self):
        r = self.install("6.6.99-a7apvr.202609101200", self.PVR_NEW, self.EXT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e[1] for e in self.entries()], ["fedora-6.6.99-a7apvr.202609101200", "fedora-6.6.98-a7apvr.202609091821",
                                                          "fedora-6.6.98-a7a.202609091931", "fedora-recovery"])
        self.assertTrue(self.entries()[0][0])
        dnf = [c for c in self.calls() if c.startswith("dnf ")]
        self.assertEqual(len(dnf), 1)
        self.assertIn("install -y --disablerepo=*", dnf[0])
        self.assertIn(self.PVR_NEW, dnf[0])
        self.assertNotIn(self.EXT, dnf[0])                     # a7a-extlinux is installed: kept as it is
        self.assertIn("A7A_DEFAULT=1", dnf[0])
        self.assertIn("keeping the installed a7a-extlinux-20260909", r.stdout)
        self.assertIn("installed 6.6.99-a7apvr.202609101200", r.stdout)
        self.assertIn("vmlinuz-6.6.99-a7apvr.202609101200", (self.state / "extlinux.conf").read_text().split("label fedora-recovery")[1])

    def test_board_without_a7a_extlinux_gets_it(self):
        (self.state / "installed").write_text("kernel-a7apvr-6.6.98_a7apvr.202609091821-202609100744.aarch64\n")
        r = self.install("6.6.99-a7apvr.202609101200", self.PVR_NEW, self.EXT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dnf = [c for c in self.calls() if c.startswith("dnf ")][0]
        self.assertIn(self.EXT, dnf)
        self.assertIn(self.PVR_NEW, dnf)

    def test_installed_kernel_on_entry_2_is_moved_first_and_the_previous_default_to_entry_2(self):
        # the --rpm use case: the DDK flavour is installed and sits on entry 2; the rollback must still mean the kernel that ran
        r = self.install("6.6.98-a7a.202609091931", self.DDK_OLD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([e[1] for e in self.entries()][:2], ["fedora-6.6.98-a7a.202609091931", "fedora-6.6.98-a7apvr.202609091821"])
        self.assertTrue(self.entries()[0][0])
        self.assertEqual([c for c in self.calls() if c.startswith("dnf ")], [])
        self.assertIn("already installed", r.stdout)
        self.assertIn("vmlinuz-6.6.98-a7a.202609091931", (self.state / "extlinux.conf").read_text().split("label fedora-recovery")[1])

    def test_force_reinstalls_the_installed_kernel(self):
        r = self.install("6.6.98-a7a.202609091931", self.DDK_OLD, force=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        dnf = [c for c in self.calls() if c.startswith("dnf ")]
        self.assertEqual(len(dnf), 1)
        self.assertIn("reinstall -y --disablerepo=*", dnf[0])
        self.assertIn("A7A_DEFAULT=1", dnf[0])
        self.assertEqual([e[1] for e in self.entries()][:2], ["fedora-6.6.98-a7a.202609091931", "fedora-6.6.98-a7apvr.202609091821"])

    def test_wrong_kernel_package_stops_before_dnf(self):
        r = self.install("6.6.98-a7a.202609091931", self.PVR_NEW)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("does not provide kernel-uname-r = 6.6.98-a7a.202609091931", r.stdout)
        self.assertEqual([c for c in self.calls() if c.startswith("dnf ")], [])
        self.assertEqual(self.entries()[0], (True, "fedora-6.6.98-a7apvr.202609091821"))

    def test_prune_keep_runs_after_the_install(self):
        r = self.install("6.6.99-a7apvr.202609101200", self.PVR_NEW, prune=2)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = [e[1] for e in self.entries()]
        self.assertEqual(names[:2], ["fedora-6.6.99-a7apvr.202609101200", "fedora-6.6.98-a7apvr.202609091821"])
        self.assertNotIn("fedora-6.6.98-a7a.202609091931", names)
