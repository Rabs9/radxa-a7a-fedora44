"""Configuration for a7a-fedora."""

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional


@dataclass
class Config:
    """Build configuration."""

    project_root: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent)
    mirror_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "mirror")
    work_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "work")
    output_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "output")
    registry_path: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "registry.json")
    kernel_source: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "mirror")
    cross_compile: str = "aarch64-linux-gnu-"
    arch: str = "arm64"
    bsp_top: str = "bsp/"
    kernel_branch: str = "allwinner-aiot-linux-6.6"
    bsp_branch: str = "cubie-aiot-v1.4.8"
    device_branch: str = "device-a733-v1.4.8"
    target_branch: str = "target-a733-v1.4.6"
    mirror_repo: str = "https://github.com/Rabs9/radxa-cubie-a7a-kernel"
    kernel_repo: str = "https://github.com/radxa/kernel.git"
    bsp_repo: str = "https://github.com/radxa/allwinner-bsp.git"
    device_repo: str = "https://github.com/radxa/allwinner-device.git"
    target_repo: str = "https://github.com/radxa/allwinner-target.git"
    fedora_version: str = "44"
    fedora_variant: str = "KDE-Desktop"
    fedora_arch: str = "aarch64"
    fedora_mirror: str = "https://download.fedoraproject.org/pub/fedora/linux/releases"
    fedora_image_pattern: str = "Fedora-KDE-Desktop-*-aarch64.raw.xz"
    board_name: str = "cubie_a7a"
    dtb_name: str = "sun60i-a733-cubie-a7a.dtb"
    dtb_source: str = "sun60i-a733-cubie-a7a"
    serial_baud: int = 115200
    # Nothing defaults to the real serial port, card reader or board (FEATURES.md rules):
    # they come from the CLI flags or a7a-fedora.json (Config.load).
    serial_port: Optional[str] = None
    sd_card: Optional[str] = None
    board_host: Optional[str] = None
    board_user: str = "radxa"
    board_tailscale: Optional[str] = None
    ssh_identity: Optional[str] = None      # a private key for the board (e.g. ~/.ssh/a7a_key)
    tools_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent.parent / "tools")
    fedora_raw_name: str = "Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw"
    fedora_url: str = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/KDE/aarch64/images/Fedora-KDE-Desktop-Disk-44-1.7.aarch64.raw.xz"
    fedora_checksum_url: str = "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/KDE/aarch64/images/Fedora-KDE-44-1.7-aarch64-CHECKSUM"
    firmware_region_name: str = "sd-firmware-region.img"
    emulate_timeout: int = 900
    sudo_prefix: list = field(default_factory=lambda: ["sudo", "-n"])
    lsblk_argv: Optional[list] = None            # tests point this at a stub that prints lsblk -J output
    root_device: Optional[str] = None            # the disk holding /, found with findmnt unless set
    device_path_override: Optional[dict] = None  # tests map /dev/sdz to a plain file
    fedora_gpg_key: Optional[Path] = field(default_factory=lambda: Path("/etc/pki/rpm-gpg/RPM-GPG-KEY-fedora-44-primary"))
    serial_hang_seconds: int = 120
    serial_boot_timeout: int = 420
    serial_wait_seconds: int = 900       # how long to wait for the first byte (the person has to power the board)
    update_poll_timeout: float = 300
    update_poll_seconds: float = 10
    kbuild_build_user: str = "a7a-builder"
    kbuild_build_host: str = "fedora-laptop"
    qemu_machine: str = "virt"
    qemu_firmware: str = "edk2-aarch64-code.fd"
    rootfs_label: str = "rootfs"
    boot_label: str = "BOOT"
    config_partition_size: int = 16 * 1024 * 1024
    boot_partition_size: int = 300 * 1024 * 1024
    privileged_helper: str = "privileged_helper.py"
    _image_path: Optional[Path] = None
    _image_xz_path: Optional[Path] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        """Defaults, then the keys of a JSON file (a7a-fedora.json) that name Config fields."""
        cfg = cls()
        path = Path(path) if path else cfg.project_root / "a7a-fedora.json"
        if path.exists():
            names = {f.name: f.type for f in fields(cls)}
            for key, value in json.loads(path.read_text()).items():
                if key in names and not key.startswith("_"):
                    if isinstance(getattr(cfg, key), Path) and value is not None:
                        value = Path(value)
                    if key == "ssh_identity" and value:
                        value = os.path.expanduser(value)       # "~/.ssh/a7a_key", as a7a-fedora.example.json has it
                    if key in ("ssh_identity", "board_host", "board_tailscale") and value == "":
                        value = None                            # the example's empty field: not set
                    setattr(cfg, key, value)
        return cfg

    def to_dict(self) -> dict:
        return {f.name: (str(v) if isinstance(v := getattr(self, f.name), Path) else v)
                for f in fields(self) if not f.name.startswith("_")}

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def payload_dir(self) -> Path:
        return self.output_dir / "payload"

    @property
    def priv_helper(self) -> Path:
        return self.tools_dir / "priv.sh"

    @property
    def fedora_raw(self) -> Path:
        return self.output_dir / self.fedora_raw_name

    @property
    def firmware_region(self) -> Path:
        return self.output_dir / self.firmware_region_name

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "manifest.json"

    @property
    def kernel_dir(self) -> Path:
        return self.work_dir / "kernel-6.6"

    @property
    def bsp_dir(self) -> Path:
        return self.work_dir / "allwinner-bsp-1.4.8"

    @property
    def device_dir(self) -> Path:
        return self.work_dir / "allwinner-device-1.4.8"

    @property
    def target_dir(self) -> Path:
        return self.work_dir / "allwinner-target"

    @property
    def output_boot(self) -> Path:
        return self.output_dir / "boot"

    @property
    def output_rootfs(self) -> Path:
        return self.output_dir / "rootfs"

    @property
    def image_path(self) -> Path:
        return self._image_path or (self.output_dir / "a7a-fedora-44.raw")

    @image_path.setter
    def image_path(self, value: Path) -> None:
        self._image_path = value

    @property
    def image_xz_path(self) -> Path:
        return self._image_xz_path or (self.output_dir / "a7a-fedora-44.raw.xz")

    @image_xz_path.setter
    def image_xz_path(self, value: Path) -> None:
        self._image_xz_path = value
