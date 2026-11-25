"""Tests for tools/build-mesa-img-rpm.sh and tools/rpm/mesa-img-a7a.spec: the Wayland-capable DDK Mesa (Imagination's
glue, TI powervr/24.0.1) packaged under /opt/pvr-wl (docs/DDK-WAYLAND-2026-09-10.md). The staging tarball is faked
here (the real one is built on the board by tools/mesa-img/build-mesa-img.sh); rpmbuild packages it for real."""

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

P = Path(__file__).resolve().parent.parent
HAVE_RPMBUILD = shutil.which("rpmbuild") is not None
STAGED = ["libEGL.so.1.0.0", "libgbm.so.1.0.0", "libglapi.so.0.0.0", "libGLESv2.so.2.0.0", "libGLESv1_CM.so.1.1.0",
          "libpvr_mesa_wsi.so", "dri/pvr_dri.so", "a7a/wl-render-node-shim.so"]
LINKS = {"libEGL.so.1": "libEGL.so.1.0.0", "libgbm.so.1": "libgbm.so.1.0.0", "libglapi.so.0": "libglapi.so.0.0.0",
         "libGLESv2.so.2": "libGLESv2.so.2.0.0", "libGLESv1_CM.so.1": "libGLESv1_CM.so.1.1.0"}
DDK_LIBS = ["libpvr_dri_support.so", "libGLESv2_PVR_MESA.so", "libGLESv1_CM_PVR_MESA.so", "libsrv_um.so",
            "libglslcompiler.so", "libusc.so", "libufwriter.so"]
RPM_NAME = "mesa-img-a7a-24.0.1-202609101700.aarch64.rpm"


def run(args, env=None):
    return subprocess.run([str(P / "tools" / "build-mesa-img-rpm.sh"), *args], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=300)


@unittest.skipUnless(HAVE_RPMBUILD, "rpmbuild not installed")
class TestMesaImgRpm(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, name="mesa-img-a7a-24.0.1-7c82c1eebc67-202609101700.tar.gz", extra=None, drop=None) -> Path:
        root = Path(self.tmp.name) / "stage"
        lib = root / "opt/pvr-wl/lib"
        (lib / "dri").mkdir(parents=True)
        (lib / "a7a").mkdir()          # the Xwayland helper (2026-11-17)
        for f in STAGED:
            if f != drop:
                (lib / f).write_bytes(b"\x7fELF fake " + f.encode())
        for link, target in LINKS.items():
            (lib / link).symlink_to(target)
        if drop != "dri/pvr_dri.so":
            (lib / "dri/sunxi-drm_dri.so").hardlink_to(lib / "dri/pvr_dri.so")
        (root / "opt/pvr-wl/share/drirc.d").mkdir(parents=True)
        (root / "opt/pvr-wl/share/drirc.d/00-mesa-defaults.conf").write_text("<driconf/>\n")
        if extra:
            (root / extra).parent.mkdir(parents=True, exist_ok=True)
            (root / extra).write_text("stray\n")
        tar = Path(self.tmp.name) / name
        with tarfile.open(tar, "w:gz") as t:
            t.add(root / "opt", arcname="opt")
            if extra and not extra.startswith("opt/"):
                t.add(root / extra.split("/")[0], arcname=extra.split("/")[0])
        return tar

    def query(self, rpm, *fmt):
        return subprocess.run(["rpm", "-qp", *fmt, str(rpm)], capture_output=True, text=True).stdout

    def built(self):
        r = run([str(self.stage())], {"A7A_OUTPUT": str(self.out)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rpm = self.out / "rpms" / RPM_NAME
        self.assertTrue(rpm.exists(), list((self.out / "rpms").glob("*")))
        return rpm

    def test_package_from_a_staging_tarball(self):
        files = self.query(self.built(), "-l").split()
        for f in ["/opt/pvr-wl", "/opt/pvr-wl/lib", "/opt/pvr-wl/lib/libEGL.so.1", "/opt/pvr-wl/lib/libEGL.so.1.0.0",
                  "/opt/pvr-wl/lib/dri/pvr_dri.so", "/opt/pvr-wl/lib/dri/sunxi-drm_dri.so",
                  "/opt/pvr-wl/lib/libpvr_mesa_wsi.so", "/opt/pvr-wl/share/drirc.d/00-mesa-defaults.conf",
                  "/opt/pvr-wl/lib/a7a/wl-render-node-shim.so", "/etc/ld.so.conf.d/00-a7a-pvr-wl.conf"]:
            self.assertIn(f, files)
        # nothing outside /opt/pvr-wl except the loader entry a7a-gpu-session writes on the DDK boot; no Vulkan ICD
        self.assertEqual([f for f in files if not f.startswith("/opt/pvr-wl")], ["/etc/ld.so.conf.d/00-a7a-pvr-wl.conf"])

    def test_ddk_links_expose_only_the_closed_libraries_the_glue_loads(self):
        links = {}
        for line in self.query(self.built(), "--dump").splitlines():
            path, *rest = line.split()
            if path.startswith("/opt/pvr-wl/lib/ddk/"):
                links[path.rsplit("/", 1)[1]] = rest[-1]   # the link target is the last field of --dump
        self.assertEqual(sorted(links), sorted(DDK_LIBS))
        for name, target in links.items():
            self.assertEqual(target, f"/opt/pvr/lib/{name}")
            self.assertNotRegex(name, r"\.so\.\d")   # ldconfig deletes dangling VERSIONED links (fresh image: no /opt/pvr)

    def test_ghost_entry_carries_a_mode_and_is_not_shipped_with_content(self):
        fmt = "[%{FILENAMES} %{FILEFLAGS:fflags} %{FILEMODES:perms}\n]"
        lines = [l for l in self.query(self.built(), "--qf", fmt).splitlines() if l.startswith("/etc/")]
        self.assertEqual(len(lines), 1, lines)
        path, flags, mode = lines[0].split()
        self.assertIn("g", flags)                 # %ghost: a7a-gpu-session creates it, an erase removes it
        self.assertEqual(mode, "-rw-r--r--")      # a mode, or rpm -V flags a present ghost as 0000

    def test_no_automatic_provides_and_the_fedora_requires(self):
        rpm = self.built()
        provides = self.query(rpm, "--provides")
        self.assertNotIn("libEGL", provides)
        self.assertNotIn("libgbm", provides)
        requires = self.query(rpm, "--requires").split()
        for pkg in ["libwayland-client", "libwayland-server", "libdrm", "libxcb", "expat", "libxshmfence"]:
            self.assertIn(pkg, requires)
        self.assertFalse([r for r in requires if "pvr" in r or ".so" in r], requires)
        scripts = self.query(rpm, "--scripts")
        self.assertIn("postinstall program: /sbin/ldconfig", scripts)
        self.assertIn("postuninstall program: /sbin/ldconfig", scripts)

    def test_description_names_the_pinned_source(self):
        info = self.query(self.built(), "-i")
        self.assertIn("7c82c1eebc67", info)
        self.assertIn("github.com/TexasInstruments/mesa/tree/powervr/24.0.1", info)

    def test_tarball_name_and_content_are_checked(self):
        def refused(tar, text):
            r = run([str(tar)], {"A7A_OUTPUT": str(self.out)})
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(text, r.stdout + r.stderr)
            shutil.rmtree(Path(self.tmp.name) / "stage")
        refused(self.stage(name="mesa-img.tar.gz"), "mesa-img-a7a-<mesa version>-<12-hex pin>")
        refused(self.stage(extra="usr/local/lib64/stray.so"), "outside opt/pvr-wl")
        refused(self.stage(drop="dri/pvr_dri.so"), "dri/pvr_dri.so")
        # a GLX build must never be packaged: its libGL.so.1 would shadow glvnd's GLX in the loader cache
        refused(self.stage(extra="opt/pvr-wl/lib/libGL.so.1"), "libGL")
        self.assertFalse((self.out / "rpms").exists() and list((self.out / "rpms").glob("mesa-img-a7a-*.rpm")))

    def test_relative_tarball_path_works(self):
        tar = self.stage()
        r = subprocess.run([str(P / "tools" / "build-mesa-img-rpm.sh"), tar.name], capture_output=True, text=True,
                           cwd=tar.parent, env={**os.environ, "A7A_OUTPUT": str(self.out)}, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(list((self.out / "rpms").glob("mesa-img-a7a-*.rpm"))), 1)


if __name__ == "__main__":
    unittest.main()
