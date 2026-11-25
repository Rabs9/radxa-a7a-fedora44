# Building a7a-fedora (for developers)

Fedora 44 KDE Plasma on the Radxa Cubie A7A (Allwinner A733): a builder that cross-compiles the
`Rabs9/radxa-cubie-a7a-kernel` tree (the Radxa BSP 6.6 kernel, the patches, the device tree, the
PowerVR and AIC8800 modules) plus this repo's own fixes (`tools/patches/`), packages the result as RPMs
(`kernel-a7a` for the vendor GPU stack, `kernel-a7apvr` for the open drm/imagination stack, `mesa-pvr-a7a` for the
a7a Mesa PowerVR driver, `a7a-gpu-tools` and `a7a-extlinux` for the board tools: docs/PACKAGING-2026-09-09.md),
assembles an SD image from the official Fedora aarch64 image with those packages installed inside it, boots it in QEMU before
anything is written, flashes it with a backup and a read-back, watches the first boot on the serial console
with a way back, and updates a running board. Every stage is a CLI command and an MCP tool, so an LLM can
drive the whole chain. GPL-2.0 (LICENSE), except `tools/panel/vendor/iced_tiny_skia`: iced's code, MIT (its own
LICENSE beside it). Development happens on a private Gitea remote (`gitea`); the
public copy is github.com/Rabs9/radxa-a7a-fedora44, its update packages github.com/Rabs9/radxa-a7a-fedora44-packages.

Status 2026-09-07: the board runs the Plasma Wayland desktop composited on the PowerVR GPU. What it took is
in `docs/GPU-TQ-FAULT-2026-09-07.md` (the 1200 MHz GPU overclock) and `docs/DE-IOMMU-STORM-2026-09-07.md`
(the PowerVR dma-buf exporter corrupting the display engine's IOMMU domain), and in `HANDOFF-WAYLAND.md`.

## What exists and what it did (2026-09-06)

| stage | tool | ran for real |
|---|---|---|
| sources | `src/stages/sources.py` (git) | mirror 4afcb3e + 4 Radxa trees pinned in `work/manifest.json`; all 8 patches applied |
| kernel | `tools/build-kernel.sh`, `tools/build-extras.sh`, `tools/build-rpm.sh` | 6.6.98+, 1,354 modules + pvrsrvkm/aic8800, RGDB embedded, DTB tx-delay 9 / 3000 MHz; nftables added for firewalld; RPM 26 MB. 2026-09-09: `A7A_PVR=1` builds the open-stack flavour (`kernel-a7apvr`), both flavours are install-only packages that keep the previous kernel as the extlinux fallback |
| packages | `tools/build-tools-rpm.sh`, `tools/build-mesa-rpm.sh`, `tools/payload-from-board.sh`, `tools/rpm/*.spec` | 2026-09-09: `a7a-extlinux`, `a7a-gpu-tools`, `mesa-pvr-a7a` (driver built on the board by `tools/mesa/build-pvr-vulkan.sh`), `kernel-a7a` 1931 and `kernel-a7apvr` 1821 (the soaked binaries, fetched back from the board); `dnf install` of all five on the running board without a re-login; docs/PACKAGING-2026-09-09.md |
| rootfs | `src/stages/rootfs.py` (curl, sha256, gpg, xz) | Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw.xz: checksum verified, CHECKSUM signature verified (Fedora 44 key) |
| image | `tools/assemble-image.sh` via `tools/priv.sh` | `output/a7a-fedora-44-kde-20260906.img` (12 GB sparse): firmware region, the Radxa card layout, ext4 root, extlinux default + recovery. 2026-09-09: `a7a-fedora-44-kde-20260909.img` = the five RPMs installed with dnf inside the image (open-stack kernel as the default entry, DDK kernel as entry 2, Fedora's PowerVR ICD masked) |
| emulate | `tools/emulate.sh` (qemu virt, -snapshot) | PASS with the Rabs9 kernel: login prompt, sshd, dnf5, Plasma installed, graphical.target, DRM node, sleep masked, firewalld's nftables table; 1 failed unit (gssproxy). 2026-09-09: PASS on the packaged image with its own default kernel (`<image>.kernel` sidecar), the five packages, the extlinux default/entry 2 and the ICD mask checked |
| flash | `tools/priv.sh flash` (+ `read-card`, `inspect`, `boot-type`, `grow-rootfs`) | 2026-09-07: Debian card backed up (deb-13-a7a.img.xz), image written, verified, p3 set to EFI, rootfs grown to 124 GB |
| serial | `src/stages/serial.py` (termios) | 2026-09-07 09:54: the board booted Fedora 44 KDE on 6.6.98+ (login at +38 s); `serial --probe` and ssh confirmed |
| update | `src/stages/update.py` (kernel RPM over ssh, `A7A_DEFAULT=1 dnf install`) | tested with a fake ssh runner; 2026-09-10: ships the kernel package (+ a7a-extlinux for a board without it), `--rpm`, `--force` (reinstall), `--prune-keep N`; see docs/PACKAGING-2026-09-09.md for the board run |

`registry.json` holds every run, every command with its exit status and log, the images with
their sha256 and verdicts. `output/logs/` has one log per command.

## Using it

    python3 -m src.cli --help
    python3 -m src.cli status                      # what ran, images and their tests
    python3 -m src.cli --dry-run all               # every command the chain would run
    python3 -m src.cli all                         # sources -> kernel -> rootfs -> image -> emulate (never flashes)
    python3 -m src.cli emulate --mode interactive  # console attached, ssh -p 2222 radxa@127.0.0.1, VNC 127.0.0.1:5
    python3 -m src.cli candidates                  # removable disks the flash stage would accept
    python3 -m src.cli flash --device /dev/sdX     # backs the card up, asks you to type sdX, writes, verifies

Packages (output/rpms, docs/PACKAGING-2026-09-09.md): `tools/build-tools-rpm.sh` (a7a-extlinux, a7a-board-tools,
a7a-gpu-tools from tools/), `tools/build-rpm.sh` (the kernel in output/payload; `A7A_PAYLOAD=output/payload-<rel>` after
`tools/payload-from-board.sh <rel>` packages the exact kernel a board runs), `tools/build-mesa-rpm.sh` (fetches the
staging tarball `tools/mesa/build-pvr-vulkan.sh` leaves on the board). `python3 -m src.cli image` installs the newest
build of each inside the image; on a board: `sudo dnf install output/rpms/<pkg>.rpm` (`A7A_DEFAULT=1 dnf install` makes a
kernel the default entry, the previous default becomes entry 2). Fedora's own kernels stay excluded in dnf.conf.
    python3 -m src.cli serial                      # watch the first boot on /dev/ttyACM0; --fallback picks the recovery entry
    python3 -m src.cli update [--rpm PKG] [--force]  # the payload's kernel RPM onto a running a7a-fedora board, previous kernel kept as entry 2
    python3 -m src.cli dashboard                   # PySide6: pipeline, images, serial console, logs; buttons show their commands first

`--json` prints the stage's result dict and nothing else; exit codes: 0 passed, 1 failed,
2 refused by a gate, 3 skipped. `a7a-fedora.json` names the serial port and the board.
`sudo` is needed only by `tools/priv.sh` (loop devices, mounts, chroot, the card); the sudoers
line is in its header, and it refuses anything outside `output/` and any disk that is not removable.

## For an LLM

The same stages are MCP tools over stdio:

    {"mcpServers": {"a7a-fedora": {"command": "python3", "args": ["-m", "src.mcp_server", "--config", "/path/to/a7a-fedora/a7a-fedora.json"], "cwd": "/path/to/a7a-fedora"}}}

(`a7a-fedora.json` holds the local settings: serial port, board address, ssh key; copy `a7a-fedora.example.json`
to it and edit it.)

Tools: `status`, `run_stage` (sources inline; kernel/rootfs/image/emulate/update as jobs), `job`,
`jobs`, `candidates`, `flash` (needs `confirm` = the device's name, refuses untested images),
`serial_watch`, `images`, `log_tail`, `config`. Every result is JSON with `status` and `reason`;
dry runs list the exact commands. The gates are the stages' own, so a model cannot flash an
untested image or a mounted disk by asking nicely.

## The card in the reader (2026-09-06)

`tools/priv.sh inspect /dev/sda`: the card's GPT header says 244,342,784 sectors (128 GB) and it
holds a Radxa layout (config, boot, 63.5 GB rootfs); the generic USB 2.0 "Card-Reader" (serial
2010...) exposes 22 GB and cannot read past 21 GB. A backup through it would be incomplete and a
write would be unverifiable beyond 22 GB. Use an SDXC-capable reader for the flash.

## Two traps the card taught (both now checked by the tools)

- The vendor firmware region (first 16 MB of a Radxa card) carries that card's own GPT. Written after the fresh
  table it silently replaced it: a 9 GB rootfs and partition 3 typed 8300. Allwinner's U-Boot only scans EFI-typed
  partitions for extlinux.conf (HARDWARE.md), so the first boot stopped in U-Boot. Now: region first, table second,
  assembly aborts unless p2/p3 are EFI System; `priv.sh flash` re-checks after writing; `inspect` warns.
- Fedora 44 KDE's display manager is plasmalogin.service, not sddm.
- systemd-repart must be masked: at first boot it partitions and formats the unpartitioned firmware area in front of p1 and
  erases boot0 (the board then boots from the empty SPI NOR and loops). `priv.sh restore-firmware` repairs a card.

## Publishing (the owner's steps; nothing here pushes by itself)

    tools/public-export.sh                      # the public copy of this repo: output/public/radxa-a7a-fedora44, one commit
    tools/repo-key.sh                           # ONCE: the package signing key (asks for a passphrase)
    tools/repo-publish.py --first --image output/<image>.img   # the first time: the image's packages, signed
    tools/repo-publish.py --image output/<image>.img   # later releases (output/public/radxa-a7a-fedora44-packages)
    tools/repo-publish.py output/rpms/<fix>.rpm        # a fix after the release

`public-export.sh` takes the committed tree minus `tools/public-exclude.list` (session logs, mail drafts, scratch
scripts, one machine's run log and settings) and refuses to write anything if `tools/public-check.py` finds an
e-mail or hardware address, a home path, a Tailscale address, this machine's names and addresses, or a string of the
builder's private list (`~/.config/a7a-public/private.txt`, outside every repository). The copy is one commit by the
public identity, dated in UTC; development history stays on the private remote.
`repo-publish.py` builds the package repository the image's `a7a-repo` package points at
(https://rabs9.github.io/radxa-a7a-fedora44-packages/44/aarch64/, GitHub Pages of a repository of its own, branch main:
on a branch of this one its packages would be in every clone). Every run starts from what is published (it fetches
the branch; nothing published yet: `--first`), so a lost local folder or a file put there by hand changes nothing.
Every package is looked through for the builder's details, signed with the key in `tools/repo/RPM-GPG-KEY-a7a-fedora`
and checked against it; a published file is never replaced; the index lists the two highest versions of each package,
and one taken out stays on the server two days after the push that took it out; dnf must read the result; one commit
replaces the last. Needs `rpm-sign` and `createrepo_c`. Boards then get the fixes through Discover or `dnf upgrade`.
Kernels are not in it: they come with a new image (a kernel package does not carry the modules the image adds to each
kernel: exFAT, ISO 9660, uinput, the DSU's).
Once, on GitHub: create Rabs9/radxa-a7a-fedora44-packages (empty) and, in its Settings, Pages, deploy from the branch
main (root). After each publish, right away: `git -C output/public/radxa-a7a-fedora44-packages push --force origin main`
(the script sets origin: fetched over https, pushed over ssh).

## On the board

`a7a-gpu-check [--bench]` (package a7a-gpu-tools): inventory and pass/warn checklist of whichever GPU stack is
running; `prepare616.sh` / `backtoddk.sh` make the newest kernel-a7apvr (open stack) / kernel-a7a (vendor DDK) entry
the default and reboot; `a7a-gpu-session.service` (same package) reads which driver bound to the GPU at boot and
applies the matching KWin session config before the display manager (`a7a-gpu-session status` shows it;
`/etc/a7a/gpu-session.manual` switches it off for experiments), `pvrwatch.sh` / `fwtrace-tail.sh` /
`clockcheck616.sh` watch the open driver. Package a7a-board-tools: `a7a-clock` (its window is the A7A Panel's Controls tab): clocks, voltages, temperatures, CPU caps, DSU, RAM, NPU, WiFi band,
GPU boot entries and profiles. `pvr-run <app>` runs one program on the PowerVR (Vulkan + GLES 3.2); `pvr-desktop`
switches the desktop to the GPU stack (`all` for an X11 session; the Wayland `on` path panics kernel 6.6.98+ and is refused by default). `npu-run <program>` runs a VIPLite program on the NPU (after `sudo dnf install opencv` and the soname aliases in its header). `tools/stage-extras.sh <mounted debian root>` stages the closed DDK,
firmware, DSU modules and NPU SDK from a Debian card so `assemble-image.sh` bakes them into the next image.

## Known gaps

- gssproxy.service fails under the Rabs9 kernel (starts under Fedora's); Kerberos NFS only. logrotate and plocate-updatedb failed once on first boot.
- Tailscale is not in the image; WiFi is loaded but unconfigured; the PowerVR DDK and NPU runtime are not copied in (software rendering).
