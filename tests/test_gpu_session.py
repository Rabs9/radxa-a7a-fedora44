"""Tests for tools/board/a7a-gpu-session, the script behind a7a-gpu-session.service (docs/PACKAGING-2026-09-09.md,
"Item B follow-ups"): at boot it reads which GPU driver is bound and puts the desktop session config in line with
it. The script takes A7A_GPU_SESSION_ROOT (a fake root for /sys, /etc, /usr/bin) and A7A_KERNEL_RELEASE, and calls
systemctl / setcap / getcap / pvr-desktop through PATH, so fakes that log their calls and keep a little state drive
every branch here without root and without a board."""

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
SCRIPT = P / "tools" / "board" / "a7a-gpu-session"

FAKES = {
    # getcap: the cap is "present" while state/cap exists
    "getcap": r"""#!/bin/bash
    echo "getcap $*" >> "$A7A_GPU_SESSION_ROOT/calls.log"
    [ -e "$A7A_GPU_SESSION_ROOT/state/cap" ] && echo "$1 cap_sys_nice=ep"
    exit 0
    """,
    "setcap": r"""#!/bin/bash
    echo "setcap $*" >> "$A7A_GPU_SESSION_ROOT/calls.log"
    case "$1" in
      -r) rm -f "$A7A_GPU_SESSION_ROOT/state/cap" ;;
      cap_sys_nice+ep) touch "$A7A_GPU_SESSION_ROOT/state/cap" ;;
      *) echo "fake setcap: unexpected $*" >&2; exit 1 ;;
    esac
    """,
    # systemctl: enabled/active state per unit as files under state/
    "systemctl": r"""#!/bin/bash
    S="$A7A_GPU_SESSION_ROOT/state"; echo "systemctl $*" >> "$A7A_GPU_SESSION_ROOT/calls.log"
    quiet=0; now=0; args=()
    for a in "$@"; do case "$a" in --quiet) quiet=1 ;; --now) now=1 ;; --no-reload) : ;; *) args+=("$a") ;; esac; done
    cmd=${args[0]}; units=("${args[@]:1}")
    case "$cmd" in
      enable) for u in "${units[@]}"; do touch "$S/enabled-$u"; [ $now = 1 ] && touch "$S/active-$u"; done ;;
      disable) for u in "${units[@]}"; do rm -f "$S/enabled-$u"; [ $now = 1 ] && rm -f "$S/active-$u"; done ;;
      is-enabled) rc=1; for u in "${units[@]}"; do if [ -e "$S/enabled-$u" ]; then [ $quiet = 1 ] || echo enabled; rc=0; else [ $quiet = 1 ] || echo disabled; fi; done; exit $rc ;;
      is-active) rc=1; for u in "${units[@]}"; do if [ -e "$S/active-$u" ]; then [ $quiet = 1 ] || echo active; rc=0; else [ $quiet = 1 ] || echo inactive; fi; done; exit $rc ;;
      *) echo "fake systemctl: unexpected $*" >&2; exit 1 ;;
    esac
    """,
    # pvr-desktop: mirrors the real script's effects on the KWin drop-in dir and the cap; 'on' refuses unless
    # state/ddk-ok exists (the DDK userspace and the fdinfo marker)
    "pvr-desktop": r"""#!/bin/bash
    R="$A7A_GPU_SESSION_ROOT"; D="$R/etc/systemd/user/plasma-kwin_wayland.service.d"
    echo "pvr-desktop $*" >> "$R/calls.log"
    case "$1" in
      on) [ -e "$R/state/ddk-ok" ] || { echo "REFUSED: the DDK is not installed under /opt/pvr"; exit 1; }
          mkdir -p "$D"; rm -f "$D/software.conf"; rm -f "$R/state/cap"; printf '[Service]\nEnvironment=LD_LIBRARY_PATH=/opt/pvr/lib\n' > "$D/pvr.conf" ;;
      off) mkdir -p "$D"; rm -f "$D/pvr.conf"; m=O2ES; grep -qa DrmQPainterBackend "$R/usr/lib64/libkwin.so.6" 2>/dev/null && m=Q
           printf '[Service]\nEnvironment=KWIN_COMPOSE=%s\n' "$m" > "$D/software.conf" ;;
      none) rm -f "$R/etc/ld.so.conf.d/00-pvr.conf" ;;
      *) echo "fake pvr-desktop: unexpected $*" >&2; exit 1 ;;
    esac
    """,
    # ldconfig: the cache "holds" /opt/pvr-wl while state/ldcache-pvrwl exists; a rebuild follows the conf file
    "ldconfig": r"""#!/bin/bash
    R="$A7A_GPU_SESSION_ROOT"; echo "ldconfig $*" >> "$R/calls.log"
    if [ "${1:-}" = -p ]; then
      echo "12 libs found in cache \`/etc/ld.so.cache'"
      [ -e "$R/state/ldcache-pvrwl" ] && echo "	libEGL.so.1 (libc6,AArch64) => /opt/pvr-wl/lib/libEGL.so.1"
      echo "	libEGL.so.1 (libc6,AArch64) => /lib64/libEGL.so.1"
      exit 0
    fi
    if [ -e "$R/etc/ld.so.conf.d/00-a7a-pvr-wl.conf" ]; then touch "$R/state/ldcache-pvrwl"; else rm -f "$R/state/ldcache-pvrwl"; fi
    """,
}


class TestGpuSession(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text(textwrap.dedent(body))
            f.chmod(0o755)
        for d in ["state", "etc/systemd/user/plasma-kwin_wayland.service.d", "etc/ld.so.conf.d", "etc/a7a", "usr/bin",
                  "sys/bus/platform/drivers/powervr", "sys/bus/platform/drivers/pvrsrvkm"]:
            (self.root / d).mkdir(parents=True)
        (self.root / "usr/bin/kwin_wayland").write_bytes(b"\x7fELF")
        self.kwin(qpainter=True)       # the KWin the board has run so far (6.6, 6.7)
        self.drop = self.root / "etc/systemd/user/plasma-kwin_wayland.service.d"
        self.state = self.root / "state"
        self.calls = self.root / "calls.log"

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------------------------------------------------
    def gpu(self, driver=None, present=True):
        """The platform device node, with `driver` bound (a symlink like sysfs), or without a driver, or absent."""
        node = self.root / "sys/bus/platform/devices/1800000.gpu"
        if present:
            node.mkdir(parents=True, exist_ok=True)
            if driver:
                os.symlink(f"../../drivers/{driver}", node / "driver")   # like sysfs: the last component names the driver

    def kwin(self, qpainter: bool):
        """libkwin with or without the QPainter backend for real displays (KWin 6.8 dropped it)."""
        lib = self.root / "usr/lib64/libkwin.so.6"
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_bytes(b"\x7fELF\0\0" + (b"N4KWin18DrmQPainterBackendE\0" if qpainter else b"N4KWin16DrmEglBackendE\0") + b"\xff\xfe\0")

    def greeter_env(self):
        f = self.root / "run/a7a/greeter-kwin.env"
        return f.read_text() if f.exists() else None

    def config_open(self):
        (self.state / "cap").touch()

    def config_ddk(self):
        (self.drop / "pvr.conf").write_text("[Service]\nEnvironment=LD_LIBRARY_PATH=/opt/pvr/lib\n")
        (self.state / "enabled-a7a-kwin-nocap.path").touch()
        (self.state / "active-a7a-kwin-nocap.path").touch()

    def run_tool(self, *args, release="6.6.98-a7apvr.202609091821", wait="0"):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", A7A_GPU_SESSION_ROOT=str(self.root),
                   A7A_KERNEL_RELEASE=release, A7A_GPU_SESSION_WAIT=wait)
        return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)

    def logged(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def cap(self):
        return (self.state / "cap").exists()

    def nocap_enabled(self):
        return (self.state / "enabled-a7a-kwin-nocap.path").exists()

    # -- apply -----------------------------------------------------------------------------------------------------
    def test_open_kernel_replaces_the_ddk_config(self):
        self.gpu("powervr")
        self.config_ddk()
        (self.drop / "software.conf").write_text("[Service]\nEnvironment=KWIN_COMPOSE=Q\n")
        (self.root / "etc/ld.so.conf.d/00-pvr.conf").write_text("/opt/pvr/lib\n")
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.drop / "pvr.conf").exists())
        self.assertFalse((self.drop / "software.conf").exists())
        self.assertFalse((self.root / "etc/ld.so.conf.d/00-pvr.conf").exists())
        self.assertTrue(self.cap())
        self.assertFalse(self.nocap_enabled())
        calls = self.logged()
        # the path unit goes first: setcap changes an xattr, which PathChanged= would report and strip the cap again
        self.assertLess(next(i for i, c in enumerate(calls) if c.startswith("systemctl disable --no-reload --now")),
                        next(i for i, c in enumerate(calls) if c.startswith("setcap cap_sys_nice+ep")), calls)
        self.assertIn("pvr-desktop none", calls)
        self.assertIn("open stack", r.stdout)
        self.assertIn("applied the open config", r.stdout)

    def test_ddk_kernel_replaces_the_open_config(self):
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.drop / "pvr.conf").exists())
        self.assertFalse((self.drop / "software.conf").exists())
        self.assertFalse(self.cap())
        self.assertTrue(self.nocap_enabled())
        self.assertTrue((self.state / "active-a7a-kwin-nocap.path").exists())
        self.assertIn("pvr-desktop on", self.logged())
        self.assertIn("applied the ddk config", r.stdout)

    def test_second_run_changes_nothing(self):
        self.gpu("powervr")
        self.config_open()
        self.assertEqual(self.run_tool("apply").returncode, 0)
        self.calls.unlink(missing_ok=True)
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing changed", r.stdout)
        # only read-only probes (getcap, is-enabled/is-active, the loader-cache listing) ran
        self.assertEqual([c for c in self.logged() if not c.startswith(("getcap", "systemctl is-", "ldconfig -p"))], [])

    # -- pvr-wl: the Wayland-capable DDK Mesa as the system stack on the DDK boot (docs/DDK-WAYLAND-2026-09-10.md) --
    def pvr_wl_installed(self, ddk_links=True, enabled=True):
        """mesa-img-a7a's files. The real lib/ddk entries are links into /opt/pvr that `-e` follows; plain files stand
        in for resolvable links here. ddk_links=False = a hand-unpacked tree, which must never be activated.
        enabled = the owner's opt-in marker /etc/a7a/pvr-wl.enabled (pvr-wl is off without it)."""
        if enabled:
            (self.root / "etc/a7a/pvr-wl.enabled").touch()
        files = ["opt/pvr-wl/lib/libEGL.so.1"]
        if ddk_links:
            files += ["opt/pvr-wl/lib/ddk/libpvr_dri_support.so", "opt/pvr-wl/lib/ddk/libsrv_um.so"]
        for f in files:
            (self.root / f).parent.mkdir(parents=True, exist_ok=True)
            (self.root / f).write_bytes(b"\x7fELF")

    def pvr_wl_entry(self, text="/opt/pvr-wl/lib\n", cached=True):
        self.wlconf().parent.mkdir(parents=True, exist_ok=True)
        self.wlconf().write_text(text)
        if cached:
            (self.state / "ldcache-pvrwl").touch()

    def wlconf(self):
        return self.root / "etc/ld.so.conf.d/00-a7a-pvr-wl.conf"

    def cached(self):
        return (self.state / "ldcache-pvrwl").exists()

    def rebuilds(self):
        return sum(1 for c in self.logged() if c.strip() == "ldconfig")

    def test_ddk_boot_makes_pvr_wl_the_system_stack(self):
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.pvr_wl_installed()
        old = os.umask(0o077)   # a hand `sudo` run with a strict umask: the entry must still be 0644 (rpm -V, ld.so)
        try:
            r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        finally:
            os.umask(old)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.wlconf().read_text().splitlines()[1:], ["/opt/pvr-wl/lib", "/opt/pvr-wl/lib/ddk"])
        self.assertEqual(self.wlconf().stat().st_mode & 0o777, 0o644)
        # no Vulkan ICD system-wide: the DDK's Vulkan cannot present (docs/DDK-WAYLAND-2026-09-10.md, step 3)
        self.assertFalse((self.root / "etc/vulkan").exists())
        self.assertTrue(self.cached())
        self.assertEqual(self.rebuilds(), 1)
        self.assertIn("pvr-wl-written", r.stdout)
        # the DDK config itself is unchanged: KWin keeps its drop-in, the cap is stripped
        self.assertTrue((self.drop / "pvr.conf").exists())
        self.assertFalse(self.cap())
        r = self.run_tool("status", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("config: ddk", r.stdout)
        self.assertIn("pvr-wl: active", r.stdout)
        # the next boot changes nothing and does not rebuild the cache
        self.calls.unlink()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertIn("nothing changed", r.stdout)
        self.assertEqual(self.rebuilds(), 0)

    def test_open_boot_removes_pvr_wl_and_rebuilds_the_cache(self):
        self.gpu("powervr")
        self.config_ddk()
        self.pvr_wl_installed()
        self.pvr_wl_entry()
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        self.assertEqual(self.rebuilds(), 1)
        self.assertIn("pvr-wl-removed", r.stdout)
        r = self.run_tool("status")
        self.assertIn("config: open", r.stdout)
        self.assertIn("pvr-wl: absent", r.stdout)
        self.assertIn("match: yes", r.stdout)

    def test_open_boot_rebuilds_a_stale_cache_without_the_conf(self):
        # a crash between removing the conf and ldconfig: the cache still points at /opt/pvr-wl
        self.gpu("powervr")
        self.config_open()
        (self.state / "ldcache-pvrwl").touch()
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("pvr-wl=partial", r.stdout)
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.cached())
        self.assertEqual(self.rebuilds(), 1)

    def test_ddk_boot_rebuilds_a_stale_cache_and_rewrites_a_wrong_conf(self):
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.pvr_wl_installed()
        self.run_tool("apply", release="6.6.98-a7a.202609091931")
        (self.state / "ldcache-pvrwl").unlink()   # conf written, cache lost (crash before ldconfig)
        self.calls.unlink()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(self.cached())
        self.assertEqual(self.rebuilds(), 1)
        self.wlconf().write_text("/opt/pvr/lib\n")   # hand-edited: brought back to the packaged pair of directories
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.wlconf().read_text().splitlines()[1:], ["/opt/pvr-wl/lib", "/opt/pvr-wl/lib/ddk"])

    def test_ddk_boot_without_mesa_img_or_opted_out_has_no_pvr_wl(self):
        # not installed: the DDK config as before 2026-09-10, nothing written, no cache rebuild
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertEqual(self.rebuilds(), 0)
        self.assertIn("pvr-wl: absent (mesa-img-a7a with resolvable lib/ddk links not installed)",
                      self.run_tool("status", release="6.6.98-a7a.202609091931").stdout)
        # installed but not opted in (the default): nothing written; Fedora's desktop-GL Qt gets no EGL config from the
        # DDK stack (docs/DDK-WAYLAND-2026-09-10.md, step 4)
        self.pvr_wl_installed(enabled=False)
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertEqual(self.rebuilds(), 0)
        s = self.run_tool("status", release="6.6.98-a7a.202609091931").stdout
        self.assertIn("pvr-wl: absent (off: opt-in with", s)
        self.assertIn("match: yes", s)
        # opted in: active; the marker removed again: taken away, the DDK config stays
        (self.root / "etc/a7a/pvr-wl.enabled").touch()
        self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertTrue(self.wlconf().exists())
        (self.root / "etc/a7a/pvr-wl.enabled").unlink()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        s = self.run_tool("status", release="6.6.98-a7a.202609091931").stdout
        self.assertIn("config: ddk", s)
        self.assertIn("pvr-wl: absent (off: opt-in", s)

    def test_ddk_software_fallback_gets_no_pvr_wl(self):
        # pvr-desktop on refuses (e.g. no fdinfo-fixed pvrsrvkm, where a crashing GPU client panics the kernel): then no
        # system-wide GPU clients either
        self.gpu("pvrsrvkm")
        self.config_open()
        self.pvr_wl_installed()   # no state/ddk-ok: the fake pvr-desktop on refuses
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((self.drop / "software.conf").exists())
        self.assertFalse(self.wlconf().exists())
        self.assertEqual(self.rebuilds(), 0)

    def test_device_without_a_driver_removes_pvr_wl_even_when_pvr_desktop_fails(self):
        self.gpu(driver=None)
        self.config_ddk()
        self.pvr_wl_installed()
        self.pvr_wl_entry()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931", wait="0")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        # again with a pvr-desktop that fails: the loader entry goes first, whatever happens after it
        self.pvr_wl_entry()
        (self.drop / "pvr.conf").write_text("[Service]\n")
        (self.bin / "pvr-desktop").write_text("#!/bin/bash\necho \"pvr-desktop $*\" >> \"$A7A_GPU_SESSION_ROOT/calls.log\"\nexit 1\n")
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931", wait="0")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("pvr-desktop off failed", r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())

    def test_hand_unpacked_tree_without_ddk_links_is_never_activated(self):
        # /opt/pvr-wl from a tarball has no lib/ddk: an entry for it would leave every client without the closed
        # libraries (software rendering); only the package layout with resolvable links counts as installed
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.pvr_wl_installed(ddk_links=False)
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertEqual(self.rebuilds(), 0)
        self.assertIn("not installed", self.run_tool("status", release="6.6.98-a7a.202609091931").stdout)

    def test_manual_marker_still_takes_pvr_wl_away_on_the_open_stack(self):
        # the marker keeps the SESSION config (an experiment), but a DDK loader entry on the open kernel would put the
        # DDK's Mesa in front of Fedora's for every process, KWin included
        self.gpu("powervr")
        self.config_ddk()
        self.pvr_wl_installed()
        self.pvr_wl_entry()
        (self.root / "etc/a7a/gpu-session.manual").touch()
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("manual", r.stdout)
        self.assertIn("pvr-wl-removed", r.stdout)       # the journal names what was done
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        self.assertTrue((self.drop / "pvr.conf").exists())   # the session config itself is left alone
        self.assertFalse(self.cap())

    def test_manual_marker_on_the_ddk_leaves_pvr_wl_alone(self):
        self.gpu("pvrsrvkm")
        self.config_ddk()
        self.pvr_wl_installed()
        self.pvr_wl_entry()
        (self.root / "etc/a7a/gpu-session.manual").touch()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(self.wlconf().exists())
        self.assertEqual(self.rebuilds(), 0)

    def test_stray_libgl_in_the_tree_refuses_the_activation(self):
        # a libGL.so.1 there is not mesa-img-a7a's (no GLX): a leftover of an early GLX build would enter the cache and
        # shadow glvnd's GLX for every app (reviewer pass 2)
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.pvr_wl_installed()
        (self.root / "opt/pvr-wl/lib/libGL.so.1").write_bytes(b"\x7fELF")
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertIn("stray libGL.so.1", self.run_tool("status", release="6.6.98-a7a.202609091931").stdout)

    def test_unexpected_driver_or_kernel_takes_pvr_wl_away(self):
        # the entry is right only on a proven DDK boot: an unexpected driver, or a kernel that is neither flavour
        (self.root / "sys/bus/platform/drivers/panfrost").mkdir()
        self.gpu("panfrost")
        self.config_open()
        self.pvr_wl_entry()
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("pvr-wl-removed", r.stdout)       # the journal names what was done
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        (self.root / "sys/bus/platform/devices/1800000.gpu/driver").unlink()
        (self.root / "sys/bus/platform/devices/1800000.gpu").rmdir()
        self.pvr_wl_entry()
        r = self.run_tool("apply", release="6.17.0-100.fc44.aarch64")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("nothing changed", r.stdout)
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())

    def test_ddk_boot_keeps_a_hand_edited_drop_in(self):
        self.gpu("pvrsrvkm")
        self.config_ddk()
        (self.drop / "pvr.conf").write_text("[Service]\nEnvironment=LD_LIBRARY_PATH=/opt/pvr/lib\nEnvironment=QT_LOGGING_RULES=kwin_wayland_drm.debug=true\n")
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("kwin_wayland_drm.debug=true", (self.drop / "pvr.conf").read_text())
        self.assertNotIn("pvr-desktop on", self.logged())
        self.assertIn("nothing changed", r.stdout)

    def test_ddk_kernel_without_ddk_userspace_gets_a_software_session(self):
        # pvr-desktop on refuses (no /opt/pvr, or no fdinfo marker): QPainter beats KWin on llvmpipe (black screen)
        self.gpu("pvrsrvkm")
        self.config_open()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.drop / "pvr.conf").exists())
        self.assertTrue((self.drop / "software.conf").exists())
        self.assertIn("pvr-desktop off", self.logged())
        self.assertIn("software session", r.stdout)

    def test_no_gpu_device_falls_back_to_the_release_string(self):
        # QEMU virt has no 1800000.gpu at all: the emulator boots the open-stack kernel and must end up with the open config
        self.gpu(present=False)
        self.config_ddk()
        r = self.run_tool("apply", release="6.6.98-a7apvr.202609091821")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.cap())
        self.assertFalse((self.drop / "pvr.conf").exists())
        self.assertIn("no 1800000.gpu device", r.stdout)
        self.assertIn("release string", r.stdout)

    def test_vendor_kernel_without_a_gpu_device_gets_a_software_session(self):
        # QEMU virt has no 1800000.gpu: the DDK has nothing to drive there, whatever kernel the image boots by default;
        # its session config would leave KWin without a renderer and the emulator without a desktop to check
        self.gpu(present=False)
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.pvr_wl_installed()
        self.pvr_wl_entry()
        r = self.run_tool("apply", release="6.6.98-a7a.202611151729")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.drop / "software.conf").exists())
        self.assertFalse((self.drop / "pvr.conf").exists())
        self.assertFalse(self.wlconf().exists())
        self.assertFalse(self.cached())
        self.assertIn("no 1800000.gpu device", r.stdout)
        self.assertIn("software session", r.stdout)
        s = self.run_tool("status", release="6.6.98-a7a.202611151729")
        self.assertEqual(s.returncode, 0, s.stdout)
        self.assertIn("config: software", s.stdout)
        self.assertIn("match: yes", s.stdout)

    def test_device_without_a_bound_driver_gets_a_software_session(self):
        # the GPU node is there but nothing bound within the wait: a QPainter session is the only config that shows a
        # desktop (the open config would put KWin on llvmpipe = EGL_BAD_ALLOC); cap and nocap units are left alone
        self.gpu(driver=None)
        self.config_open()
        (self.state / "ddk-ok").touch()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931", wait="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.drop / "pvr.conf").exists())
        self.assertTrue((self.drop / "software.conf").exists())
        self.assertTrue(self.cap())
        self.assertIn("no driver bound", r.stdout)
        self.assertIn("software session", r.stdout)
        self.assertEqual([c for c in self.logged() if c.startswith(("setcap", "systemctl enable", "systemctl disable"))], [])
        # status agrees, and a second apply changes nothing
        r = self.run_tool("status", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stack: nogpu", r.stdout)
        self.assertIn("config: software", r.stdout)
        self.calls.unlink()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931", wait="0")
        self.assertIn("nothing changed", r.stdout)
        self.assertNotIn("pvr-desktop off", self.logged())

    def test_driver_that_binds_late_is_waited_for(self):
        self.gpu(driver=None)
        self.config_ddk()
        node = self.root / "sys/bus/platform/devices/1800000.gpu"
        # bind powervr 0.5 s after the script started, from a background shell
        binder = subprocess.Popen(["bash", "-c", f"sleep 0.5; ln -s ../../drivers/powervr '{node}/driver'"])
        try:
            r = self.run_tool("apply", release="6.6.98-a7a.000", wait="5")   # the release string would say DDK: the driver must win
        finally:
            binder.wait()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("open stack", r.stdout)
        self.assertTrue(self.cap())

    def test_enable_and_disable_never_reload_the_manager_during_boot(self):
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.state / "ddk-ok").touch()
        self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.gpu(present=False)   # no-op: node stays; switch the driver by re-creating the link
        node = self.root / "sys/bus/platform/devices/1800000.gpu"
        (node / "driver").unlink()
        os.symlink("../../drivers/powervr", node / "driver")
        self.run_tool("apply")
        for c in self.logged():
            if c.startswith(("systemctl enable", "systemctl disable")):
                self.assertIn("--no-reload", c, c)

    def test_unknown_kernel_without_a_driver_changes_nothing(self):
        self.gpu(present=False)
        self.config_ddk()
        r = self.run_tool("apply", release="6.17.0-100.fc44.aarch64")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertTrue((self.drop / "pvr.conf").exists())
        self.assertFalse(self.cap())
        self.assertIn("nothing changed", r.stdout)

    def test_unexpected_driver_changes_nothing(self):
        (self.root / "sys/bus/platform/drivers/panfrost").mkdir()
        self.gpu("panfrost")
        self.config_open()
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("panfrost", r.stdout)
        self.assertTrue(self.cap())
        self.assertEqual([c for c in self.logged() if c.startswith(("setcap", "pvr-desktop", "systemctl enable", "systemctl disable"))], [])

    def test_manual_marker_disables_apply(self):
        self.gpu("pvrsrvkm")
        self.config_open()
        (self.root / "etc/a7a/gpu-session.manual").touch()
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("manual", r.stdout)
        self.assertTrue(self.cap())
        self.assertFalse((self.drop / "pvr.conf").exists())

    def test_usage(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 2)
        self.assertIn("apply|status", r.stdout + r.stderr)

    # -- KWin without QPainter (6.8) ---------------------------------------------------------------------------------
    # 2026-11-16, Fedora's updates in the emulator: KWin 6.8 has no QPainter backend for real displays. With
    # KWIN_COMPOSE=Q (the greeter's static drop-in, and the software session) it says "Could not fulfill the requested
    # compositing mode", exits, and the screen stays black while every unit is "active". A `dnf upgrade` on the board
    # would have ended there.
    def test_kwin_with_qpainter_leaves_the_greeter_to_its_static_drop_in(self):
        for driver, release, prep in (("powervr", "6.6.98-a7apvr.202609091821", self.config_open),
                                      ("pvrsrvkm", "6.6.98-a7a.202609091931", self.config_ddk)):
            self.gpu(driver)
            prep()
            r = self.run_tool("apply", release=release)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIsNone(self.greeter_env(), driver)
            self.assertIn("greeter-kwin: static", self.run_tool("status", release=release).stdout)
            os.unlink(self.root / "sys/bus/platform/devices/1800000.gpu/driver")

    def test_kwin_without_qpainter_gets_a_mode_it_has_for_the_greeter(self):
        self.kwin(qpainter=False)
        self.gpu("powervr")
        self.config_open()
        r = self.run_tool("apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.greeter_env(), "KWIN_COMPOSE=O2ES\n")
        self.assertIn("greeter-env-written", r.stdout)
        # the second boot finds it in place
        r = self.run_tool("apply")
        self.assertIn("nothing changed", r.stdout)
        s = self.run_tool("status")
        self.assertEqual(s.returncode, 0, s.stdout)
        self.assertIn("greeter-kwin: env (KWIN_COMPOSE=O2ES)", s.stdout)

    def test_on_the_ddk_the_greeters_compositor_gets_what_the_sessions_has(self):
        # llvmpipe cannot create EGL images on this display (KWin 6.7, 2026-09-07), and QPainter is gone: the greeter's
        # KWin runs on the DDK like the session's, with the variables of its drop-in (hand edits included)
        self.kwin(qpainter=False)
        self.gpu("pvrsrvkm")
        self.config_ddk()
        (self.drop / "pvr.conf").write_text("[Service]\nEnvironment=LD_LIBRARY_PATH=/opt/pvr/lib\nEnvironment=KWIN_COMPOSE=O2ES\n"
                                            "# a comment\nEnvironment=KWIN_RENDER_NODES=/dev/dri/none\n")
        r = self.run_tool("apply", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.greeter_env(), "LD_LIBRARY_PATH=/opt/pvr/lib\nKWIN_COMPOSE=O2ES\nKWIN_RENDER_NODES=/dev/dri/none\n")

    def test_software_session_without_qpainter_and_a_drop_in_from_before_the_kwin_update(self):
        # no GPU (the emulator on the vendor kernel): the software session. Its drop-in was written when KWin still
        # had QPainter; after the update it names a mode that is gone and must be rewritten, not just found "in place"
        self.gpu(present=False)
        (self.drop / "software.conf").write_text("[Service]\nEnvironment=KWIN_COMPOSE=Q\n")
        self.kwin(qpainter=False)
        r = self.run_tool("apply", release="6.6.98-a7a.202611151729")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("pvr-desktop off", self.logged())
        self.assertNotIn("KWIN_COMPOSE=Q", (self.drop / "software.conf").read_text())
        self.assertEqual(self.greeter_env(), "KWIN_COMPOSE=O2ES\n")
        s = self.run_tool("status", release="6.6.98-a7a.202611151729")
        self.assertEqual(s.returncode, 0, s.stdout)
        # and back (a downgrade): QPainter again, the env file goes, the drop-in says Q again
        self.kwin(qpainter=True)
        r = self.run_tool("apply", release="6.6.98-a7a.202611151729")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("KWIN_COMPOSE=Q", (self.drop / "software.conf").read_text())
        self.assertIsNone(self.greeter_env())
        self.assertIn("greeter-env-removed", r.stdout)

    def test_status_calls_a_missing_greeter_env_what_it_is(self):
        self.kwin(qpainter=False)
        self.gpu("powervr")
        self.config_open()
        s = self.run_tool("status")
        self.assertEqual(s.returncode, 1, s.stdout)
        self.assertIn("greeter-kwin: MISSING", s.stdout)
        self.assertIn("match: no", s.stdout)

    def test_greeter_subcommand_is_for_a_kwin_update_in_a_running_system(self):
        # the package trigger on kwin-libs: the user logs out after `dnf upgrade` and the greeter starts with the new
        # KWin before any reboot. Only the greeter's env and a software drop-in that went stale; nothing else moves
        self.kwin(qpainter=False)
        self.gpu("pvrsrvkm")
        self.config_ddk()
        r = self.run_tool("greeter", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.greeter_env(), "LD_LIBRARY_PATH=/opt/pvr/lib\n")
        self.assertEqual([c for c in self.logged() if c.startswith(("setcap", "pvr-desktop", "systemctl enable", "systemctl disable", "ldconfig"))], [])
        # the software session's drop-in, when that is what the system runs
        os.unlink(self.root / "sys/bus/platform/devices/1800000.gpu/driver")
        (self.drop / "pvr.conf").unlink()
        (self.drop / "software.conf").write_text("[Service]\nEnvironment=KWIN_COMPOSE=Q\n")
        r = self.run_tool("greeter", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("KWIN_COMPOSE=Q", (self.drop / "software.conf").read_text())
        self.assertEqual(self.greeter_env(), "KWIN_COMPOSE=O2ES\n")

    def test_the_greeter_gets_its_env_under_the_manual_marker_and_on_an_unknown_kernel_too(self):
        # neither is a reason for a black login screen: the env is about what this KWin can do, not about the session
        self.kwin(qpainter=False)
        self.gpu("powervr")
        self.config_open()
        (self.root / "etc/a7a/gpu-session.manual").touch()
        self.assertEqual(self.run_tool("apply").returncode, 0)
        self.assertEqual(self.greeter_env(), "KWIN_COMPOSE=O2ES\n")
        (self.root / "etc/a7a/gpu-session.manual").unlink()
        (self.root / "run/a7a/greeter-kwin.env").unlink()
        os.unlink(self.root / "sys/bus/platform/devices/1800000.gpu/driver")
        os.rmdir(self.root / "sys/bus/platform/devices/1800000.gpu")
        r = self.run_tool("apply", release="6.17.0-100.fc44.aarch64")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertEqual(self.greeter_env(), "KWIN_COMPOSE=O2ES\n")

    # -- status ----------------------------------------------------------------------------------------------------
    def test_status_reports_a_match_and_a_mismatch(self):
        self.gpu("powervr")
        self.config_open()
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("stack: open", r.stdout)
        self.assertIn("config: open", r.stdout)
        self.assertIn("driver: powervr", r.stdout)
        self.assertIn("match: yes", r.stdout)
        (self.drop / "pvr.conf").write_text("[Service]\n")
        r = self.run_tool("status")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("config: mixed", r.stdout)
        self.assertIn("match: no", r.stdout)
        self.assertEqual([c for c in self.logged() if c.startswith(("setcap", "pvr-desktop", "systemctl enable", "systemctl disable"))], [])

    def test_status_on_the_ddk_accepts_the_software_fallback(self):
        self.gpu("pvrsrvkm")
        (self.drop / "software.conf").write_text("[Service]\nEnvironment=KWIN_COMPOSE=Q\n")
        r = self.run_tool("status", release="6.6.98-a7a.202609091931")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("config: software", r.stdout)
        self.assertIn("match: yes", r.stdout)

    def test_status_unknown_stack_exits_2(self):
        self.gpu(present=False)
        r = self.run_tool("status", release="6.17.0-100.fc44.aarch64")
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("stack: unknown", r.stdout)
        self.assertIn("match: unknown", r.stdout)

    def test_status_mentions_the_manual_marker(self):
        self.gpu("powervr")
        self.config_open()
        (self.root / "etc/a7a/gpu-session.manual").touch()
        r = self.run_tool("status")
        self.assertIn("manual: yes", r.stdout)


if __name__ == "__main__":
    unittest.main()
