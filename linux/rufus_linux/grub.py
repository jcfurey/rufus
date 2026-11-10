"""Build matching GRUB loaders for new image files, including ARM UEFI."""

import os
from pathlib import Path
import re
import shutil
import struct

from .core import RufusError, run_tool, write_at
from .windows import case_path

EFI = {"x86_64": ("x86_64-efi", "BOOTX64.EFI"), "arm64": ("arm64-efi", "BOOTAA64.EFI"),
       "arm": ("arm-efi", "BOOTARM.EFI")}
PREFIX = "/boot/rufus"


def module_directory(target):
    executable = shutil.which("grub-mkimage")
    if not executable:
        raise RufusError("FAT32 Linux extraction requires grub-mkimage (grub-common).")
    directories = [Path(executable).resolve().parents[1] / "lib/grub", Path("/usr/lib/grub")]
    if os.environ.get("RUFUS_USE_LOCAL_TOOLS", "1") != "0":
        directories.insert(0, Path(__file__).resolve().parents[1] / "build/tools/root/usr/lib/grub")
    if os.environ.get("RUFUS_GRUB_MODULES"):
        directories.insert(0, Path(os.environ["RUFUS_GRUB_MODULES"]).expanduser())
    for directory in directories:
        candidate = directory / target
        if (candidate / "modinfo.sh").is_file() and (candidate / "kernel.img").is_file():
            return candidate
    package = {"i386-pc": "grub-pc-bin", "x86_64-efi": "grub-efi-amd64-bin",
               "arm64-efi": "grub-efi-arm64-bin", "arm-efi": "grub-efi-arm-bin"}[target]
    raise RufusError(f"{target} GRUB modules are missing. Install {package}, or use hybrid mode.")


def target_modules(architecture, firmware, *, bios="grub"):
    if bios not in ("grub", "syslinux"):
        raise RufusError("Select GRUB or Syslinux for BIOS extraction.")
    if architecture not in ("x86", "x86_64", "arm64", "arm"):
        raise RufusError("Unsupported Linux kernel architecture.")
    firmware = firmware or ("uefi" if architecture in ("arm64", "arm") else "bios" if architecture == "x86" else "both")
    if firmware not in ("bios", "uefi", "both"):
        raise RufusError("Select BIOS, UEFI, or both for extracted Linux media.")
    if architecture in ("arm64", "arm") and firmware != "uefi":
        raise RufusError("ARM Linux media requires UEFI; BIOS is available only for x86.")
    if architecture == "x86" and firmware != "bios":
        raise RufusError("32-bit x86 Linux extraction currently supports BIOS firmware.")
    targets = {}
    if firmware in ("bios", "both") and bios == "grub":
        targets["i386-pc"] = module_directory("i386-pc")
    if firmware in ("uefi", "both"):
        target, _ = EFI[architecture]
        targets[target] = module_directory(target)
    return firmware, targets


def kernel_architecture(path):
    with path.open("rb") as stream:
        header = stream.read(1024 * 1024)
    if len(header) >= 0x238 and header[0x202:0x206] == b"HdrS" and header[510:512] == b"\x55\xaa":
        return "x86_64" if struct.unpack_from("<H", header, 0x236)[0] & 1 else "x86"
    if len(header) >= 64 and header[56:60] == b"ARM\x64":
        return "arm64"
    if len(header) >= 40 and header[36:40] == b"\x18\x28\x6f\x01":
        return "arm"
    # ARM64 EFI zboot images may have a PE header instead of an Image header.
    if header[:2] == b"MZ" and len(header) >= 64:
        pe = struct.unpack_from("<I", header, 60)[0]
        if pe + 6 <= len(header) and header[pe:pe + 4] == b"PE\0\0":
            if struct.unpack_from("<H", header, pe + 4)[0] == 0xaa64:
                return "arm64"
            if struct.unpack_from("<H", header, pe + 4)[0] in (0x1c0, 0x1c2, 0x1c4):
                return "arm"
    raise RufusError("Linux extraction needs a supported x86, ARM64, or 32-bit ARM Linux kernel.")


def build_loaders(root, work, filesystem_uuid, architecture, targets, cancel=None):
    destination = case_path(root, PREFIX.lstrip("/"), create=True)
    bootstrap = work / "bootstrap.cfg"
    bootstrap.write_text(f"search --no-floppy --fs-uuid --set=root {filesystem_uuid}\n"
                         f"set prefix=($root){PREFIX}\n", encoding="utf-8")
    versions = {}
    bios_core = None
    for target, modules in targets.items():
        # Every external module is copied from the SAME directory whose kernel
        # grub-mkimage embeds. Never reuse the ISO's potentially incompatible
        # modules, and never execute modinfo.sh as a shell script.
        copied = destination / target
        copied.mkdir()
        for path in modules.iterdir():
            if path.is_file() and (path.suffix in (".mod", ".lst") or path.name == "modinfo.sh"):
                shutil.copyfile(path, copied / path.name)
        metadata = (modules / "modinfo.sh").read_text()
        version = re.search(r'^grub_package_version=["\']([^"\']+)', metadata, re.MULTILINE)
        versions[target] = version[1] if version else "unknown"
        names = ["normal", "linux", "configfile", "search", "search_fs_uuid", "part_gpt", "part_msdos",
                 "fat", "echo", "test", "regexp", "terminal", "halt", "reboot", "sleep", "gzio"]
        names += [name for name in ("serial", "all_video") if (modules / (name + ".mod")).is_file()]
        if target == "i386-pc":
            names.append("biosdisk")
            output = work / "core.img"
            bios_core = output
        else:
            case_path(root, "EFI/BOOT", create=True)
            output = case_path(root, "EFI/BOOT/" + EFI[architecture][1])
        run_tool(["grub-mkimage", "-O", target, "-d", str(modules), "-p", PREFIX,
                  "-c", str(bootstrap), "-o", str(output), *names], cancel)
    return bios_core, versions


def embed_bios(fd, modules, core):
    """GRUB util/setup.c's contiguous embedding, only in a NEW MBR image.

    Offsets come from GRUB include/grub/i386/pc/boot.h and util/setup.c:
    https://gitlab.freedesktop.org/gnu-grub/grub
    The core occupies reserved sectors starting at 1, before the FAT partition
    at sector 2048. It never depends on a filesystem file's block allocation.
    """
    boot = bytearray((modules / "boot.img").read_bytes())
    payload = bytearray(core.read_bytes())
    sectors = (len(payload) + 511) // 512
    if (len(boot) != 512 or boot[510:512] != b"\x55\xaa" or len(payload) < 1024 or
            sectors >= 2048 or len(payload) > 0x78000 - 0x8200):
        raise RufusError("GRUB BIOS loader does not fit its reserved embedding area.")
    # Validate the generated diskboot format before using its block-list ABI.
    start, count, segment = struct.unpack_from("<QHH", payload, 500)
    if start != 2 or count != sectors - 1 or segment != 0x820 or any(payload[488:500]):
        raise RufusError("Unsupported GRUB BIOS diskboot format.")
    struct.pack_into("<Q", boot, 0x5c, 1)
    boot[0x64] = 0xff
    boot[0x66:0x68] = b"\x90\x90"
    struct.pack_into("<QHH", payload, 500, 2, sectors - 1, 0x820)
    payload.extend(b"\0" * (-len(payload) % 512))
    # Preserve the disk identity, partition table, and signature at byte 440+.
    write_at(fd, boot[:440], 0)
    write_at(fd, payload, 512)
