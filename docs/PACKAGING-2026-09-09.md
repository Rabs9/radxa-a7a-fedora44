# Packaging the open GPU stack (Item B, 2026-09-09 night)

Goal: a fresh image gets everything from 2026-09-09 without hand steps, and a Fedora update or a reinstall can no
longer lose it. Until this session all of it existed only as files copied onto the board: the a7a Mesa PowerVR
Vulkan driver in /usr/local, the open-stack kernel entries, the board scripts, `/etc/modprobe.d/powervr-trace.conf`,
the mask on Fedora's ICD JSON. Design first (this section), results at the end.

## Facts the design rests on (checked on the board 2026-09-09 17:16 board time, a7a-gpu-check 0/0)

- The six board scripts, the modprobe file and `a7a-extlinux` on the board are byte-identical to the repo copies.
  The running kernel `6.6.98-a7apvr.202609091821` (open stack, soaked) and the DDK-flavour kernel
  `6.6.98-a7a.202609091931` (boot-tested) are the binaries to package: 1931 is still `output/payload`, 1821 is
  fetched back from the board (`tools/payload-from-board.sh`), so the RPMs carry the tested bits, not a rebuild.
- Fedora 44 has the sbin merge: `/usr/local/sbin` and `/usr/sbin` are symlinks to `bin`. Everything installs to
  `/usr/local/bin`; the sbin path keeps working.
- `rpm-build` is not on the board and does not need to be: the board only builds and stages the Mesa driver, the
  laptop packages every RPM with `rpmbuild --target aarch64` (packaging only, nothing compiled), as `kernel-a7a`
  already did.
- Only one RPM was ever installed on the board: `kernel-a7a-6.6.98_a7a.202609072057` (the 09-07 DDK kernel that the
  `g_1008_800` entry boots). It owns `/usr/local/sbin/a7a-extlinux`.

## Two traps in the 2026-09-07 kernel spec, fixed here

1. **A second version of the same kernel package was an upgrade.** dnf treats `kernel-a7a` like any package: the new
   version replaces the old one, the old kernel's files are deleted while its extlinux entry (the fallback) stays.
   Fix: `Provides: installonlypkg(kernel)` makes dnf keep versions side by side (`installonly_limit`, default 3,
   removes the oldest, never the running one), and `%postun` removes the erased kernel's extlinux entry
   (`a7a-extlinux remove <label>`, which refuses the default entry).
2. **`%post` pruned extlinux entries** as a side effect of an install. Removed: the `update` stage prunes explicitly,
   the packages never do.

## Packages (all in output/rpms/, spec files in tools/rpm/)

| package | contents | built by |
|---|---|---|
| `a7a-extlinux` (noarch) | `/usr/local/bin/a7a-extlinux` | `tools/build-tools-rpm.sh` |
| `kernel-a7a` / `kernel-a7apvr` | `/boot/vmlinuz-<rel>`, `/usr/lib/linux-image-<rel>/allwinner/*.dtb`, `/lib/modules/<rel>`, `/lib/firmware/aic8800D80/*` (+ `powervr/rogue_36.56.104.183_v1.fw` for the pvr flavour); `Requires: a7a-extlinux`; `%post`: depmod + `a7a-extlinux --check-files add <rel>` (`A7A_DEFAULT=1` in the environment of dnf makes it the default, the previous default becomes entry 2) | `tools/build-rpm.sh` (flavour from the release string) |
| `mesa-pvr-a7a` | `/usr/local/lib64/libvulkan_powervr_mesa.so`, `/usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json`, `/usr/local/share/drirc.d/00-mesa-defaults.conf` (meson installs the driconf defaults under its prefix; identical to Fedora's copy); `%post` masks `/usr/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json` as `.disabled-by-a7a`, `%postun` (erase) unmasks, `%triggerin -- mesa-vulkan-drivers` re-masks after every Fedora Mesa update | board: `tools/mesa/build-pvr-vulkan.sh` stages a tarball; laptop: `tools/build-mesa-rpm.sh` |
| `a7a-gpu-tools` | `/usr/local/bin/{prepare616,backtoddk,pvrwatch,fwtrace-tail,clockcheck616}.sh`, `/usr/local/bin/a7a-gpu-check`, `%config(noreplace) /etc/modprobe.d/powervr-trace.conf`; `Requires: a7a-extlinux` | `tools/build-tools-rpm.sh` |

`a7a-extlinux` has to be its own package: a file shipped by several kernel packages is an rpm file conflict the
first time its content changes, and it changes in this session (`remove`). The one-off transition on the owner's
board: `rpm -e --justdb kernel-a7a-6.6.98_a7a.202609072057` (database only, every file stays, `g_1008_800` keeps
booting; reversible with `rpm -i --justdb output/rpms/kernel-a7a-6.6.98_a7a.202609072057-*.rpm`), approved by the
owner.

Behaviour changes carried by the packages: `backtoddk.sh` boots the newest `fedora-6.6.98-a7a.<stamp>` entry (the
packaged DDK kernel; on the owner's board 1931) and only falls back to `g_1008_800` when no such entry exists, because
a fresh image has no `g_1008_800`. `build-pvr-vulkan.sh` no longer installs into /usr/local by default (the RPM is
the install path); `--install` keeps the direct install for the driver dev loop. The five scripts move from
`powervr-backport/` to `tools/board/` (symlinks left behind), `tools/board/` is the packaged source.

## Image and emulator

`tools/assemble-image.sh` no longer copies the payload and the scripts by hand. It writes the base
`extlinux.conf` (the DDK kernel entry + recovery), copies the newest RPM of each package into the image and installs
them in the chroot in two transactions: `a7a-extlinux`, `a7a-gpu-tools`, `mesa-pvr-a7a`, `kernel-a7a` (fallback), then
`kernel-a7apvr` with `A7A_DEFAULT=1` (default). Result: entry 1 = open stack, entry 2 = DDK kernel, entry 3 =
recovery on the open-stack kernel. The dnf guard (Fedora kernels excluded, ours allowed) stays. The image's default
kernel Image is extracted from its RPM to `output/kernels/Image-<rel>` and named in `<image>.kernel`, which
`tools/emulate.sh` uses instead of `output/payload` (still the fallback when the sidecar is missing). New emulator
checks: `rpm -q` of the five packages, the extlinux default, the ICD mask, `a7a-gpu-check` present.

On the DDK fallback entry the desktop still needs `backtoddk.sh`'s session config (`pvr-desktop on`, cap stripped):
the package makes the entry exist, not the session switch. Follow-up, not done here: a unit that picks the session
config from the bound GPU driver at boot.

## Reviewer pass (independent, before anything touched the board)

Eight findings, all fixed before the packages were rebuilt:

1. `%postun` dropped the boot entry whenever a version was erased, but two BUILDS of the same release can be
   installed side by side (install-only + a new Release stamp): erasing one would have flipped the default to the DDK
   kernel while the other still owned the files. Now the entry goes only when `/boot/vmlinuz-<rel>` is really gone
   (rpm removes files before `%postun`, and only when no other package owns them).
2. `backtoddk.sh` / `prepare616.sh` pinned `6.6.98` in the entry regex, fell back to labels a fresh image does not
   have, and rebooted even when the boot entry could not be switched (= the black-screen combination: DDK session
   config on the open-stack kernel). Now the target entry is chosen (newest build stamp of `fedora-*-a7a.<stamp>` /
   `fedora-*-a7apvr.<stamp>`) before anything changes, and a failed `a7a-extlinux --check-files default` aborts
   without the reboot.
3. The `rpm -e --justdb` transition was only a doc step: `a7a-extlinux` now `Conflicts:` with the old-scheme kernel
   packages (`kernel-a7a < 6.6.98_a7a.202609091931`, `= 6.6.98.plus`), which shipped the same inodes under other path
   strings (`/usr/local/sbin/...`, `/lib/firmware/...`).
4. `--check-files` judged every entry, so one stale hand-made entry would have failed every kernel `%post`. Now only
   the entry being added, the new default (fallback/default) and the recovery entry are judged.
5. "Newest package" was a byte sort of the file name; `6.6.100` and `26.1.10` would have lost to `6.6.98` / `26.1.8`.
   Now the fixed-width Release stamp (YYYYMMDDHHMM, the build) is the key in both `assemble-image.sh` and
   `src/stages/image.py`.
6. An erased default kernel with no other entry left the conf silently pointing at a deleted kernel; `%postun` now
   says so on stderr, and `fallback` refuses a fallback entry whose files are missing.
7. `pvr-desktop`, `pvr-run`, `a7a-clock`, `a7a-clock-gui`, `npu-run` stay hand-copied by `assemble-image.sh` (they
   predate this work; a Fedora update never touches `/usr/local`). Follow-up: an `a7a-board-tools` package.
8. `depmod` in `%post` rewrote files the package owned, so `rpm -V` would have flagged the kernel; the eleven
   depmod outputs are `%ghost` now, as in Fedora's kernel-core.
Minor: the emulate stage's "missing packages" message took the wrong word; the release note took DTB and extra-module
facts from `output/payload` for a packaged image (now from the kernel RPM); the drirc file above was undocumented.

A second, independent pass over the fix commit (with rpm probe packages on rpm 6.0.2 / dnf5 5.4.3) confirmed fixes
1, 5, 7 and the emulate message, and found four of them incomplete, fixed before the packages were rebuilt:
`%ghost` without `%attr(0644,root,root)` records mode 0000, so `rpm -V` would have flagged all 22 depmod files as
modified (the very thing fix 8 was for); `fallback --check-files` refused instead of walking to the next bootable
entry, so an erased default with a dead entry 2 would have left the board unbootable despite bootable entries 3-5
(now it skips entries whose files are missing); the board scripts validated the target entry's files only after the
session switch (now `a7a-extlinux --check-files --dry-run default` runs first, nothing changes on failure); the
`Conflicts` were version-only and would have blocked a legitimate re-packaging of the 09-07 payload (now
release-qualified to the two exact old builds). Also from that pass: the image build keeps the dnf logs until the
file checks pass (the depmod outputs are ghosts now, so `modules.dep` proves `%post` worked in the chroot), and the
release note warns when the kernel package an image was built from is gone. The second pass also proved the
`%postun` guard right for `dnf reinstall` (the old instance's `%postun` runs after the new files are laid down; the
kernel file exists, the entry stays), which would otherwise have dropped the boot entry on a plain reinstall.

## Plan (executed inline, reviewer pass before the board)

1. `a7a-extlinux remove <label>` (+ tests): drops one entry, refuses the default, exit 0 with a message when the
   label is absent (rpm scriptlets must not fail on it).
2. `tools/rpm/*.spec` + `tools/build-rpm.sh` (flavour-aware, spec file instead of the heredoc, installonly, no prune,
   `%postun` remove) + `tools/build-tools-rpm.sh` + `tools/build-mesa-rpm.sh` + `tools/payload-from-board.sh`;
   tests build the small packages for real with rpmbuild and check file lists, provides and scriptlets.
3. `tools/mesa/build-pvr-vulkan.sh`: stage + tarball by default, `--install` for the old behaviour.
4. `git mv` the five scripts to `tools/board/`, symlinks in `powervr-backport/`, `backtoddk.sh` newest-entry logic.
5. `tools/assemble-image.sh` (RPM install in the chroot, sidecar) + `tools/emulate.sh` (sidecar, checks) +
   `src/stages/image.py` release notes; the image stage's prerequisite becomes the RPM set.
6. Reviewer pass on the spec files and scripts; fix; commit.
7. Board: `a7a-gpu-check`, `rpm -e --justdb` of the old package, `dnf install --assumeno` preview, install the five,
   `rpm -V`, `a7a-gpu-check`. No reboot, no re-login (every file is identical to what is on disk).
8. Fresh image in the emulator (`python3 -m src.cli image` then `emulate`), results below, close Item B.

## Results (2026-09-09 night; board times are 3 h behind the laptop's)

**Packages built** (output/rpms; two reviewer passes before anything touched the board):

| package | version-release | from |
|---|---|---|
| `a7a-extlinux` | 20260909-202609092306 (noarch) | tools/a7a-extlinux |
| `a7a-gpu-tools` | 20260909-202609092306 (noarch) | tools/board/ |
| `mesa-pvr-a7a` | 26.1.8-202609091222 | the board's existing build tree, staged with `DESTDIR=... ninja install` (the .so sha256 `7504d44c...` = the file KWin was already running; the stage step needed `chown -R radxa` of the tree first, the earlier `sudo ninja install` had left a root-owned meson log) |
| `kernel-a7a` | 6.6.98_a7a.202609091931-202609092306 | output/payload (byte-identical to the board's boot-tested DDK-flavour kernel) |
| `kernel-a7apvr` | 6.6.98_a7apvr.202609091821-202609092306 | `tools/payload-from-board.sh 6.6.98-a7apvr.202609091821` (the soaked running kernel, sha256 verified against the board) |

**Board** (`radxa-cubie-a7a`, running `6.6.98-a7apvr.202609091821`, owner's session on the a7a driver, `a7a-gpu-check`
0 fail / 0 warn before and after): `rpm -e --justdb kernel-a7a-6.6.98_a7a.202609072057` (files kept, 13 extlinux
entries kept), `dnf install --assumeno` preview = 5 x Installing, nothing removed or upgraded, then the real
`dnf install -y --disablerepo='*' /home/radxa/rpms/*.rpm` (dnf history ID 20, board time 03:07:37): `%post` of both
kernels ran (`added fedora-...`, entries already existed so nothing moved), the default stayed
`fedora-6.6.98-a7apvr.202609091821`, entry 2 `fedora-6.6.98-a7a.202609091931`, all 13 entries kept. `rpm -V` of all
five packages: clean (the `%ghost %attr` fix at work: depmod's regenerated files verify). Fedora's ICD still masked,
`/usr/local/lib64/libvulkan_powervr_mesa.so` unchanged and still mapped by the session's KWin (4 mappings), no
re-login, no reboot. `dnf remove --assumeno kernel-a7apvr` refuses: "would result in removing of running kernel", so
`installonlypkg(kernel)` + the `/boot/vmlinuz-<rel>` ownership give dnf5's running-kernel protection. The switch
scripts' new pre-check (`a7a-extlinux --check-files --dry-run default <entry>`) accepts both
`fedora-6.6.98-a7a.202609091931` and `g_1008_800` on this board.

**Fresh image** `output/a7a-fedora-44-kde-20260909.img` (assembled twice, the second time with the packages after the
second review; 57 s from the sparse file to done): `dnf install` inside the aarch64 chroot took 21 s for the four
packages and 22 s for `kernel-a7apvr` with `A7A_DEFAULT=1`; the result inside the image is exactly the designed layout:
`* fedora-6.6.98-a7apvr.202609091821 [1]`, `fedora-6.6.98-a7a.202609091931 [2]`, `fedora-recovery [3]` on the
open-stack kernel, Fedora's PowerVR ICD masked, depmod's `modules.dep` present for both kernels (so `%post` worked
under qemu-user). Emulator (`python3 -m src.cli emulate`, kernel from the `.kernel` sidecar): **PASS** at the login
prompt after ~55 s, `uname 6.6.98-a7apvr.202609091821`, all five packages reported by `rpm -q`, extlinux default and
entry 2 as above, ICD masked, dnf kernel guard present, `a7a-gpu-check` present, 1 failed unit = the known gssproxy.
The image stage's registry entry and release note carry the package list and the kernel facts from the RPM.

**What a Fedora update or a reinstall can no longer lose**: the two kernels (install-only; `%postun` keeps the
extlinux conf consistent), the a7a Mesa driver and its ICD mask (`%triggerin` re-masks after every Mesa update), the
six board scripts and the modprobe file (config, kept on update), the extlinux editor. Still hand-copied by
`assemble-image.sh` (predates this work): `pvr-desktop`, `pvr-run`, `a7a-clock`, `a7a-clock-gui`, `npu-run`.

**Follow-ups**: (1) `a7a-board-tools` package for the five hand-copied scripts; (2) the `update` stage should ship
the kernel RPM instead of the tarball (it still hand-installs `a7a-extlinux`, now RPM-owned); (3) a boot-time unit that
picks the session config from the bound GPU driver, so the DDK fallback entry gets a usable desktop without
`backtoddk.sh`; (4) `build-pvr-vulkan.sh`'s staging path has only been exercised by hand from an existing build tree
(the script's own `DESTDIR` step is the same command); the next driver rebuild runs it for real; (5) `newest` = newest
build stamp, so re-packaging an old payload after a newer kernel exists makes the old one win the next image (by design,
documented).

## Removal paths and the re-mask trigger, exercised on the board (2026-09-10 morning, laptop time)

Owner's request: prove the `%postun` same-release guard, the real removal branch and the Mesa trigger on the board,
not only in the emulator. Board on `6.6.98-a7apvr.202609091821`, owner's session up, `a7a-gpu-check` 0/0 throughout.
Entry lists shown as `[1] [2] [3] ... total`.

**1a. Two builds of one release, remove the older.** `tools/build-rpm.sh` on the same 1931 payload gave
`kernel-a7a-6.6.98_a7a.202609091931-202609100733`; `dnf install --assumeno` preview: *Installing* (not Upgrading:
the install-only provide works), real install: `%post` "added fedora-6.6.98-a7a.202609091931" (idempotent, entries
unchanged: `* ...a7apvr.202609091821 [1]`, `...a7a.202609091931 [2]`, `...a7apvr.202609091531 [3]`, total 13). Both
builds installed. `dnf remove kernel-a7a-...-202609092306` (the older): exit 0, no scriptlet output (the guard saw
`/boot/vmlinuz-6.6.98-a7a.202609091931` still present), kernel image, DTB dir, modules (with `extra/pvrsrvkm.ko`,
`modules.dep`) all there, entry [2] and the default untouched, total 13, `rpm -V kernel-a7a` clean. **PASS.**

**1b. Real removal.** `tools/payload-from-board.sh 6.6.98-a7apvr.202609091531` (the v6.16 graft kernel, hand-installed,
not running) + `build-rpm.sh` → installed WITHOUT `A7A_DEFAULT`: `%post` "added fedora-...202609091531" (the entry
existed, stayed [3], default untouched), `rpm -V` clean, both kernel-a7apvr versions installed. `dnf remove` it:
`%postun` "removed fedora-6.6.98-a7apvr.202609091531"; `/boot/vmlinuz-...1531`, `/usr/lib/linux-image-...1531` gone,
`/usr/lib/modules/...1531/` left holding only the hand-install's dangling `build` symlink (never package-owned;
removed by hand, a fresh image never has it), the shared firmware kept, entries: `[1] ...1821 * [2] ...a7a.1931
[3] ...a7a.202609072057 [4] ...` total 12, no `1531` left in extlinux.conf, `uname -r` unchanged,
`dnf remove --assumeno kernel-a7apvr-...1821` refused ("would result in removing of running kernel"). **PASS**, with
one defect found: `rpm -V kernel-a7apvr` (1821) reported `.......T.` on `/usr/lib/firmware/powervr/rogue_36.56.104.183_v1.fw`.
Cause: `%install` copied the firmware with a plain `cp`, so every build recorded its own build time as the mtime of
a file that several packages share (both flavours, every version); the last install re-stamps it and the other
owners' `rpm -V` shows T. Fix (spec + test `test_shared_files_carry_the_same_mtime_in_every_package`): the shared
files (`/usr/lib/firmware/**`, `/etc/a7a/*`) get a pinned mtime, 2026-01-01 00:00 UTC, in every package.

**1c. Roll the board to the fixed builds.** `kernel-a7a-...-202609100744` and `kernel-a7apvr-...-202609100744`
installed side by side (entries unchanged), then the older builds removed. dnf gotcha: with two builds of the
RUNNING release installed, dnf5 protects BOTH (each owns `/boot/vmlinuz-$(uname -r)`) and refuses to remove even the
older build; `dnf remove --setopt=protect_running_kernel=false <older build>` is the way, and it is safe by 1a (the
newer build keeps every file). Result: one build per kernel, `rpm -V` clean for all five packages, the firmware at the
pinned mtime, 12 entries, default `fedora-6.6.98-a7apvr.202609091821`, running-kernel protection back with one build.

**2. Mesa re-mask trigger.** `sudo dnf reinstall mesa-vulkan-drivers` (26.1.8-1.fc44 from the updates repo, 122 MiB):
afterwards `/usr/share/vulkan/icd.d/` holds only `powervr_mesa_icd.aarch64.json.disabled-by-a7a` (the `%triggerin`
renamed Fedora's fresh JSON again), `vulkaninfo --summary` lists one PowerVR device (`PowerVR B-Series BXM-4-64 MC1`,
Mesa 26.1.8) plus Fedora's llvmpipe, `VK_LOADER_DEBUG=driver` shows the only PowerVR manifest found is
`/usr/local/share/vulkan/icd.d/powervr_mesa_icd.aarch64.json` → `/usr/local/lib64/libvulkan_powervr_mesa.so`, the
session's KWin (pid 2607) untouched with the driver still mapped, `a7a-gpu-check` 0/0, `rpm -V mesa-vulkan-drivers`
reports the JSON missing (intended). **PASS.**

**3. Tests.** `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -q` (the documented command, no venv):
**Ran 195 tests, OK** (36 of them in tests/test_rpm.py, test_extlinux.py, test_image.py). The image was assembled once
more from the final packages and booted in the emulator (PASS, same checks as above).

Left in output/rpms/attic: the superseded builds and the 1531 test package. Remaining kernel-a7apvr/kernel-a7a
packages in output/rpms are the `202609100744` builds installed on the board.

## Item B follow-ups (2026-09-10): boot-time session config, a7a-board-tools, the update stage via RPM

Design first (this section), results at the end. Owner's brief: (1) a boot-time unit that applies the session
config matching the kernel that booted, so the switch scripts shrink to "set the default entry and reboot"; (2) an
`a7a-board-tools` package for the five tools `assemble-image.sh` still hand-copies; (3) the `update` stage ships the
kernel RPM instead of the tarball. Reviewer pass on the unit and the spec files before anything touches the board.

### Facts (board 2026-09-10 05:50 board time, open stack, a7a-gpu-check 0/0)

- Both GPU drivers are loaded by udev from the DT compatible (`powervr`: `of:N*T*Cimg,img-rogue*`; `pvrsrvkm`:
  `of:N*T*Cimg,gpu*`), nothing in modules-load.d. This boot: udev coldplug finished at 7.1 s, the driver bound at
  9.5 s, `systemd-user-sessions` at 10.9 s, `plasmalogin.service` (= `display-manager.service`) started at 11.05 s.
  So a unit ordered before the display manager has to WAIT for the binding (~2.5 s after coldplug), not assume it.
- Session config today. Open stack: no `pvr.conf` and no `software.conf` in
  `/etc/systemd/user/plasma-kwin_wayland.service.d/`, `kwin_wayland` keeps `cap_sys_nice=ep`, `a7a-kwin-nocap.path`
  and `.service` disabled. DDK: `pvr.conf` from `pvr-desktop on` (which also removes `software.conf` and strips the
  cap), `a7a-kwin-nocap.path` enabled and started. The greeter drop-in
  `/etc/systemd/user/plasma-login-kwin_wayland.service.d/software.conf` (KWIN_COMPOSE=Q) is the same for both.
- The `a7a-kwin-nocap.{path,service}` units exist only as hand copies in `/etc/systemd/system/` on the owner's board
  and in `tools/board/systemd/`; no package ships them and `assemble-image.sh` does not write them, so a fresh image's
  `backtoddk.sh` would have failed at `systemctl enable a7a-kwin-nocap.path`. They ship with the session unit now.
- The five hand-copied tools and the desktop entry are byte-identical between the board and `tools/board/`; the
  greeter drop-in differs from the repo copy in its comment only.
- `a7a-clock-gui` imports PyQt6, which on the owner's board is a pip install under `/home/radxa/.local` (Fedora's
  `python3-pyqt6` was blocked by a kf6-kmime conflict on 2026-09-07); a fresh image has neither. Unchanged here: the
  package `Recommends: python3-pyqt6` (a weak dependency never fails a transaction) and the GUI says what is missing.
- `a7a-clock` needs `nmcli` (NetworkManager, in Fedora KDE) and `fdtput` (package `dtc`, NOT in the Fedora KDE image:
  the reviewer checked the image's rpmdb; the owner's board got it during the 09-07 clock work), so `dtc` is a weak
  dependency and `a7a-clock gpu` says what to install when it is missing; `pvr-run`/`npu-run` need only bash.

### 1. `a7a-gpu-session.service` (package a7a-gpu-tools)

`/usr/local/bin/a7a-gpu-session apply|status`, run by `a7a-gpu-session.service` (oneshot, `RemainAfterExit`,
`WantedBy=multi-user.target`, `Before=display-manager.service systemd-user-sessions.service`, so no greeter and no
user manager can read the drop-ins before they are settled). `apply`:

1. Which stack booted: the driver bound to `/sys/bus/platform/devices/1800000.gpu` (`powervr` = open, `pvrsrvkm` =
   DDK). The device node exists from early boot while the driver binds ~2.5 s after udev coldplug, so `apply` polls
   for the `driver` link (0.2 s steps, 30 s at most). When the node does not exist at all (QEMU) or no driver binds
   in time: when the node is absent (QEMU) the kernel release string decides (`-a7apvr.` = open, `-a7a.` = DDK);
   when the node is there but nothing bound within 30 s, the GPU is unusable and `pvr-desktop off` (a QPainter
   session) is the only config that shows a desktop, so that is what `apply` sets (stack `nogpu`); a kernel that is
   neither leaves everything untouched and the unit fails (visible in `systemctl --failed` and a7a-gpu-check).
2. Idempotent, check-then-fix: the four items of each config are checked first and only the ones that differ are
   changed, so a boot with the right config touches nothing (and keeps a hand-edited `pvr.conf`, e.g. from
   `pvr-desktop on --debug`).
   - DDK: `pvr-desktop on` when `pvr.conf` is missing (it writes the drop-in, removes `software.conf`, strips the
     cap); when `pvr-desktop on` refuses (no DDK userspace under /opt/pvr, no fdinfo marker) the unit falls back to
     `pvr-desktop off` = a usable QPainter session instead of the llvmpipe black screen, and says so. Then
     `setcap -r /usr/bin/kwin_wayland` if the cap is still there, then `systemctl enable --now a7a-kwin-nocap.path`
     (the service is started by the path unit on a kwin update).
   - Open stack: `systemctl disable --now a7a-kwin-nocap.path a7a-kwin-nocap.service` FIRST (setcap changes an xattr,
     which `PathChanged=` reports: restoring the cap with the path unit running would strip it again), remove
     `pvr.conf` and `software.conf` from the KWin drop-in directory, undo `pvr-desktop all` if present (its
     `/etc/ld.so.conf.d/00-pvr.conf` would put the DDK's EGL in front of Mesa for every process), then
     `setcap cap_sys_nice+ep /usr/bin/kwin_wayland` (worth 5.5x on the compositor).
   - Never touches extlinux, never reboots; `enable`/`disable` run with `--no-reload` (the drop-ins are read by the
     user managers at login, nothing needs PID 1 to reload). Nothing the unit calls may open a login session: it runs
     before `systemd-user-sessions.service`, and `user@.service` waits for that service (login barrier), so a
     `sudo -u <user>` inside the unit (pam_systemd asks logind for a session, logind waits for the user manager)
     would deadlock until the unit's timeout; `systemctl --user -M <user>@` is no better (a transient systemd-run
     service with `PAMName=login`, second reviewer pass). `pvr-desktop` reloaded the user managers with `sudo -u`;
     it now runs `systemctl reload user@<uid>.service` for the ACTIVE instances only (`user@.service` is
     `Type=notify-reload`, `ReloadSignal=RTMIN+25`: the manager re-executes and re-reads its unit files, what a
     systemd package update does; at boot no instance is active, radxa has no linger). Found by the reviewers before
     the first DDK boot.
3. Opt-out for experiments: the marker `/etc/a7a/gpu-session.manual` makes `apply` log and exit 0 without changes
   (a7a-gpu-check warns while it exists).

`status` prints the bound driver, the config found on disk (`ddk`, `open`, `mixed`) and whether it matches; exit 1
on a mismatch. a7a-gpu-check and the emulator use it. Tests: the script takes `A7A_GPU_SESSION_ROOT` (a fake root for
/sys, /etc, /usr/bin) and `A7A_KERNEL_RELEASE`, with fake `systemctl`/`setcap`/`getcap`/`pvr-desktop` on PATH that
log their calls, so tests/test_gpu_session.py drives every branch on the laptop.

Package changes: a7a-gpu-tools gains `a7a-gpu-session`, the unit in `/usr/lib/systemd/system/` with a preset
(`enable a7a-gpu-session.service`; `%systemd_post` presets it on a first install, `%systemd_preun` disables the
three units on erase; `%systemd_postun` expands to nothing on Fedora 44, the systemd file trigger reloads PID 1),
plus a `%triggerpostun -- a7a-gpu-tools < 20260910` that runs the preset once when the 20260909 package (no
unit) is upgraded, because `%systemd_post` skips upgrades (`$1 == 2`) and the unit would have stayed disabled on the
owner's board (reviewer). The trigger body calls `systemd-update-helper install-system-units` unconditionally: rpm
6.0.2 passes `$1 = 2` to a self-named `%triggerpostun` (the old header is still counted while it runs), so
`%systemd_post` inside the trigger was a no-op (second reviewer pass; proven with probe packages in a user
namespace: `post 2`, `triggerpostun 2 1`, new files on disk, old file gone). `a7a-kwin-nocap.{path,service}` go to `/usr/lib/systemd/system/` (NOT enabled by the
package: the session unit decides per boot), the greeter drop-in is plain `%config` (a differing hand copy becomes
`software.conf.rpmorig` and the packaged one takes over: nothing to remove by hand, `rpm -V` clean); the package
`Requires: a7a-board-tools` (for `pvr-desktop`). One-off on the owner's board before the install: remove the hand
copies of the two nocap units from /etc/systemd/system (they would shadow the packaged ones; a7a-gpu-check warns). `prepare616.sh` and `backtoddk.sh` shrink to: refuse unless `a7a-gpu-session.service` is enabled (`--force`
for experiments with the manual marker; checked BEFORE the entry changes, a warning after the switch would have
rebooted into the black screen), choose and validate the target entry, `a7a-extlinux --check-files default`, reboot
after 3 s (`--no-reboot` to only set the entry); the "do not log in again before the reboot" trap is gone because
the running kernel's config stays until the reboot.
`assemble-image.sh` stops writing the greeter drop-in (the package does); `emulate.sh` reports `a7a-gpu-session
status` and a7a-board-tools; the emulate stage fails an image whose status is not `open`.

### 2. `a7a-board-tools` (noarch)

`/usr/local/bin/{pvr-desktop,pvr-run,a7a-clock,a7a-clock-gui,npu-run}` and
`/usr/share/applications/a7a-clock.desktop` from `tools/board/`, built by `tools/build-tools-rpm.sh` as its third
package (Version = newest commit date touching the six files). `Requires: bash python3 sudo NetworkManager`,
`Recommends: dtc python3-pyqt6` (weak: neither is in the offline image build). Dependency direction: a7a-gpu-tools -> a7a-board-tools -> (nothing of ours);
a7a-board-tools does not depend on a7a-extlinux (`a7a-clock` edits extlinux.conf itself). `assemble-image.sh`
installs it in the first dnf transaction and drops the hand-copy step; `src/stages/image.py` PACKAGES/BUILDERS and
the `.rpms` sidecar list six packages; `emulate.sh` checks `rpm -q a7a-board-tools`.

### 3. `update` stage via the kernel RPM

`src/stages/update.py`: the package to ship is the newest build of the kernel RPM for the payload's release
(`output/rpms/kernel-<flavour>-<version>-<build>.aarch64.rpm`, the same name mapping as `tools/build-rpm.sh`), or
`--rpm <file>` for any kernel package (e.g. the DDK flavour when the payload is the open one). The newest
`a7a-extlinux` RPM travels with it and is installed only when the board has no a7a-extlinux at all (the kernel package
requires it; an older image has none; an installed one is never upgraded or downgraded inside a kernel transaction). On the board: `scp` into `/tmp/a7a-update/`, then one script:
`rpm -qp --provides` must show `kernel-uname-r = <release>`, `sudo env A7A_DEFAULT=1 dnf install -y
--disablerepo='*' <rpms>` (`--force` = `dnf reinstall` of an already installed build; an installed build without
`--force` gets `a7a-extlinux --check-files add <release> --default`, the package's own `%post` call, which moves it
to entry 1 and the previous default to entry 2, so "press 2" still means the kernel that ran before: the reviewer
caught that `default <label>` alone would have left the failed kernel at entry 2), then `a7a-extlinux list` must
show `fedora-<release>` as the default or the stage stops before the reboot. `--prune-keep` below 2 is refused. No more
tarball, no hand-installed `a7a-extlinux`, no `depmod`/marker/file copies (the package's `%post` does them), and no
automatic `prune`: dnf's `installonly_limit` retires the oldest kernel package (its `%postun` removes the entry), so
pruning is an explicit `--prune-keep N` now. Unchanged: the reachability/Fedora/already-running checks, the reboot,
the serial watch with entry 2 + `a7a-extlinux fallback` on a failed boot, the ssh poll without serial, the registry.

### Reviewer pass (independent, before anything touched the board)

Three blockers, all verified by the reviewer (image rpmdb, systemd 259 source, `rpm --eval`) and fixed before the
packages were built:

1. **The first DDK boot would have deadlocked the unit for 90 s.** `pvr-desktop on|off` reloaded every user's manager
   with `sudo -u <user> systemctl --user daemon-reload`; sudo goes through PAM, pam_systemd asks logind for a session
   and logind waits for `user@.service`, which waits for `systemd-user-sessions.service`, which waits for our unit
   (`Before=`). The unit would have been killed at `TimeoutStartSec`, the greeter delayed, the cap and the nocap
   units untouched, a7a-gpu-check FAIL. Fix: `pvr-desktop` reloads only running managers via
   `systemctl --user -M <user>@` (no PAM); rule recorded in the script and above.
2. **Upgrading a7a-gpu-tools on the owner's board would have left the unit disabled** (`%systemd_post` presets on a
   first install only; the board has the 20260909 package), and `backtoddk.sh` only warned after switching the
   entry, then rebooted = the black screen. Fix: `%triggerpostun -- a7a-gpu-tools < 20260910` presets the unit once
   on that upgrade; both switch scripts refuse (before touching the entry) unless the unit is enabled (`--force`).
3. **`Requires: dtc` would have failed the offline image build** (dtc is not in Fedora KDE). Fix: `Recommends: dtc`
   plus a guard with the install hint in `a7a-clock gpu`.

Important, also fixed: the update stage's already-installed path used `a7a-extlinux default <label>`, which does not
reorder, so a failed boot's "press 2" would have picked the failed kernel (now `add <rel> --default`, the `%post`
call; a bash-level test runs the install script against fakes plus the REAL a7a-extlinux and checks the order);
an a7a-extlinux RPM older than the board's would have been a downgrade inside the kernel transaction (now an
installed a7a-extlinux is never touched by the update stage); the emulator accepted a matching config even when the
unit had timed out (now `gpu_session_unit` must say `success`); `--prune-keep 1` would have removed the fallback
(refused below 2); the greeter drop-in as `%config(noreplace)` would have left the hand copy in place with an
`.rpmnew` (now plain `%config`). Minor: `--no-reload` on the unit's enable/disable, a `software` session instead of
a GPU config when the device is present but no driver binds, `.rpmorig`/`.rpmnew` leftovers reported by
a7a-gpu-check, doc facts corrected (dtc, `%systemd_postun`). Left as is: `--force` on the RUNNING kernel is a
`dnf reinstall` of the running kernel package, which dnf5's running-kernel protection may refuse (unverified; to be
previewed with `--assumeno` on the board before relying on it).

A second, independent pass over the fix commit (probe packages under `unshare -r`, systemd 259 traces) confirmed
the dtc, extlinux-reorder and `%config` fixes (the hand copy of the greeter drop-in becomes `.rpmorig`, the packaged
file lands, `rpm -V` clean) and found two of the fixes incomplete: the trigger's `%systemd_post` body never ran (rpm
passes `$1 = 2` to a self-named `%triggerpostun`; now an unconditional preset call, proven with the probe), and the
"PAM-free" `systemctl --user -M user@` reload was not PAM-free at all (it is `systemd-run ... -pPAMName=login`;
safe at boot only because no manager was running; now `systemctl reload user@<uid>.service` on the active
instances, which is what systemd-update-helper does for a systemd update). Also from that pass: the switch scripts'
refusal now says the fix (`systemctl enable a7a-gpu-session.service`) instead of "install a7a-gpu-tools >=
20260910", which after the upgrade would already have been true; a test for `--prune-keep 1`; the test on the built
package asserts the trigger body carries no `$1 -eq 1` guard.

### Plan (in this order; reviewer pass before the board)

1. `tools/board/a7a-gpu-session` + `tools/board/systemd/{a7a-gpu-session.service,a7a-gpu-tools.preset}` + tests.
2. `tools/rpm/a7a-gpu-tools.spec`, `tools/rpm/a7a-board-tools.spec`, `tools/build-tools-rpm.sh`, tests/test_rpm.py.
3. `prepare616.sh`/`backtoddk.sh` shrink; `a7a-gpu-check` reports the unit; `assemble-image.sh`, `emulate.sh`,
   `src/stages/image.py`, `src/stages/emulate.py` + tests.
4. `src/stages/update.py`, `src/cli.py`, `src/mcp_server.py`, tests/test_update.py, README.
5. Independent reviewer pass on the unit, the script, the spec files and the stage; fix; build the packages; tests.
6. Board (owner told before every reboot): install a7a-board-tools + a7a-gpu-tools (no reboot; `a7a-gpu-session
   status` must say open/matches), `backtoddk.sh` -> DDK desktop login, a7a-gpu-check; `prepare616.sh` -> open-stack
   login, a7a-gpu-check; the update stage `--dry-run` against the board, then for real if the owner wants.
7. Image rebuilt from the packages, emulator PASS, results below, follow-ups closed in docs/NEXT-SESSION-PROMPT.md.

### Results (2026-09-10; board times are 3 h behind the laptop's)

**Packages** (output/rpms; the previous builds in output/rpms/attic): `a7a-board-tools-20260910-202609101523`,
`a7a-gpu-tools-20260910-202609101523`, `a7a-extlinux-20260909-202609101523` (same content as the board's 202609092306
build, not installed on the board), the kernel and Mesa packages unchanged. 226 tests
(`QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -q`), 17 of them tests/test_gpu_session.py and 6
the bash-level install-script tests in tests/test_update.py.

**Board, no reboot** (running `6.6.98-a7apvr.202609091821`, owner's session up, a7a-gpu-check 0/0 before): the two
hand-copied nocap units parked in /home/radxa/rpms/attic/hand-copies (were disabled), `dnf install --assumeno`
preview = Upgrading a7a-gpu-tools + Installing a7a-board-tools, nothing else; the real install (repos off) printed
`Created symlink .../multi-user.target.wants/a7a-gpu-session.service` from the upgrade trigger; `rpm -V` clean for
all six packages; the greeter drop-in's hand copy became `software.conf.rpmorig` (comment-only difference, deleted);
`a7a-gpu-session status` = driver powervr, stack open, config open, match yes; `sudo a7a-gpu-session apply` and a
real `systemctl start a7a-gpu-session.service` on the running board: "open config already in place, nothing
changed"; a7a-gpu-check 0/0; the session's KWin untouched.

**Board, DDK direction** (`backtoddk.sh`: `default is now fedora-6.6.98-a7a.202609091931`, reboot after 3 s, serial
recorded in output/logs/serial-record-20260910-153239.log): the unit ran from 10.31 s to 11.05 s
(`kernel 6.6.98-a7a.202609091931: ddk stack (driver pvrsrvkm bound)` ... `applied the ddk config: pvr-desktop=on
nocap-enabled (was: open, now: ddk)`), plasmalogin started at 12.37 s, the owner logged in: "the gui works and is
way more responsive than before". a7a-gpu-check: **0 fail, 0 warn**, 0 HWR, KWin DDK drop-in present, cap stripped,
compositor renders through the DDK, `status` match yes. No deadlock (the `pvr-desktop` reload path fixed by the
reviewer would have cost 90 s here).

**Board, open-stack direction** (`prepare616.sh`: `default is now fedora-6.6.98-a7apvr.202609091821`, reboot): the
unit ran from 9.60 s to 10.28 s (`applied the open config: nocap-disabled pvr.conf-removed cap-restored (was: ddk,
now: open)`), plasmalogin at 11.79 s, ssh back after ~25 s, the owner logged in. a7a-gpu-check at "up 1 minute":
**0 fail, 0 warn**, 0 context resets, 0 job timeouts, kwin_wayland keeps cap_sys_nice, compositor renders through
the a7a driver (`/usr/local/lib64/libvulkan_powervr_mesa.so` mapped), nocap units disabled, no drop-in. (A first
check at "up 0 minute", with KWin 1 s old, showed 2 transient FAILs: empty maps and two D-state kworkers; both gone
seconds later. Not a defect: the check was run before the session had started.)

**Update stage against the board**: `python3 -m src.cli --dry-run update` reached the board and listed the four
commands with `kernel-a7a-6.6.98_a7a.202609091931-202609100744.aarch64.rpm` (the payload is the DDK flavour, so a
real run would be the already-installed path = `add --default` + reboot onto the DDK kernel) plus a7a-extlinux;
not run for real (a third reboot, the owner's call). `--force`'s `dnf reinstall` of the RUNNING kernel package,
previewed with `--assumeno`: dnf5 accepts it ("Reinstalling 1, Replacing 1"), no running-kernel refusal.

**Fresh image** `output/a7a-fedora-44-kde-20260910.img` (`python3 -m src.cli image`, 15:46-15:47 laptop time): the
first chroot transaction installed a7a-extlinux, a7a-board-tools, a7a-gpu-tools, mesa-pvr-a7a and kernel-a7a, the
second kernel-a7apvr with `A7A_DEFAULT=1`; `files: ok` including `/usr/lib/systemd/system/a7a-gpu-session.service`,
`/etc/systemd/system/multi-user.target.wants/a7a-gpu-session.service` (the preset ran in the chroot under
qemu-user), `/usr/local/bin/pvr-desktop`, `/usr/local/bin/a7a-clock`, the desktop entry and the greeter drop-in,
none of them hand-copied any more; entry 1 = open stack, entry 2 = DDK, recovery on the open-stack kernel, ICD
masked. `.rpms` sidecar = the six packages. **Emulator PASS** (`python3 -m src.cli emulate --mode kernel`, login
prompt after ~55 s): `a7a_packages` lists all six, `extlinux_default`/`entry2` as designed, ICD masked, dnf guard,
`gpu_session_unit = active enabled success`, `gpu_session = open yes` (no 1800000.gpu in QEMU: the release string
decided, and the unit restored the cap and left no drop-in), 1 failed unit = the known gssproxy.

**Closed**: the three follow-ups of Item B. **Open** (small): the update stage has not run for real against the board
(dry-run OK; a real run = one more reboot); `a7a-clock-gui` still needs PyQt6 from outside the image (weak
dependency); a7a-extlinux was rebuilt with the same content under a new Release (the board keeps 202609092306).
