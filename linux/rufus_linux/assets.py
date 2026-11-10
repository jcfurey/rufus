"""Reuse the same boot code and FreeDOS payload as the Windows application."""

from importlib.resources import files
from pathlib import Path
import re

from .core import RufusError

BOOT_ARRAYS = {
    "mbr-win7": ("mbr_win7.h", "mbr_win7_0x0"),
    "mbr-syslinux": ("mbr_syslinux.h", "mbr_syslinux_0x0"),
    "fat16-head": ("br_fat16_0x0.h", "br_fat16_0x0"),
    "fat16-freedos": ("br_fat16fd_0x3e.h", "br_fat16_0x3e"),
    "fat32-head": ("br_fat32_0x0.h", "br_fat32_0x0"),
    "fat32-freedos": ("br_fat32fd_0x52.h", "br_fat32_0x52"),
    "fat32-freedos-extra": ("br_fat32fd_0x3f0.h", "br_fat32_0x3f0"),
    "fat32-windows": ("br_fat32pe_0x52.h", "br_fat32pe_0x52"),
    "fat32-windows-extra": ("br_fat32pe_0x3f0.h", "br_fat32pe_0x3f0"),
    "fat32-windows-loader": ("br_fat32pe_0x1800.h", "br_fat32pe_0x1800"),
    "ntfs-head": ("br_ntfs_0x0.h", "br_ntfs_0x0"),
    "ntfs-boot": ("br_ntfs_0x54.h", "br_ntfs_0x54"),
}


def source_asset(name):
    root = Path(__file__).resolve().parents[2]
    if name in BOOT_ARRAYS:
        header, variable = BOOT_ARRAYS[name]
        text = (root / "src/ms-sys/inc" / header).read_text()
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"//[^\n]*", "", text)
        match = re.search(r"unsigned\s+char\s+" + re.escape(variable) + r"\s*\[\s*\]\s*=\s*\{([^}]+)\}", text, re.S)
        if not match:
            raise RufusError(f"Cannot read bundled boot code: {name}")
        return bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{1,2})\b", match.group(1)))
    if name == "uefi-ntfs.img":
        return (root / "res/uefi/uefi-ntfs.img").read_bytes()
    if name.startswith("freedos/") and len(Path(name).parts) == 2 and ".." not in Path(name).parts:
        return (root / "res" / name).read_bytes()
    raise RufusError(f"Unknown bundled asset: {name}")


def asset(name):
    if name.startswith("/") or ".." in Path(name).parts:
        raise RufusError("Invalid asset path.")
    packaged = files("rufus_linux").joinpath("assets", name)
    if packaged.is_file():
        return packaged.read_bytes()
    return source_asset(name)


def freedos_files():
    # This list matches ExtractFreeDOS in src/dos.c, including locale utilities.
    names = ["COMMAND.COM", "KERNEL.SYS", "DISPLAY.EXE", "KEYB.EXE", "KEYBOARD.SYS",
             "KEYBRD2.SYS", "KEYBRD3.SYS", "KEYBRD4.SYS", "MODE.COM", "EGA.CPX"]
    names.extend(f"EGA{number}.CPX" for number in range(2, 19))
    return {name: asset("freedos/" + name) for name in names}
