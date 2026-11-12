"""Build Windows BIOS/UEFI disk images without mounts or administrator access."""

import os
from pathlib import Path
import shutil
import struct
import tempfile

from .assets import asset
from .backup import new_output
from .boot import MIB, copy_sparse
from .core import (FAT_MAX_FILE, RufusError, check_cancel, checksum, emit, expected_sha256,
                   run_tool, stage_windows_iso, write_at)
from .layout import Partition, write_layout
from .windows import WindowsOptions, apply_windows_options


def windows_partitions(size, filesystem="vfat", firmware="uefi"):
    if filesystem not in ("vfat", "ntfs") or firmware not in ("uefi", "bios"):
        raise RufusError("Windows media requires FAT32 or NTFS and UEFI or BIOS firmware.")
    if size < 64 * MIB or size % 512:
        raise RufusError("Windows disk-image size must be at least 64 MiB with 512-byte sectors.")
    sectors = size // 512
    table = "gpt" if firmware == "uefi" else "dos"
    end = ((sectors - 34) // 2048) * 2048 if table == "gpt" else sectors
    if firmware == "uefi" and filesystem == "ntfs":
        secondary_sectors = (len(asset("uefi-ntfs.img")) + 511) // 512
        secondary = ((end - secondary_sectors) // 2048) * 2048
        main_end = secondary
    else:
        secondary, secondary_sectors, main_end = None, 0, end
    parts = [Partition(2048, main_end - 2048, "esp" if filesystem == "vfat" and firmware == "uefi" else "basic",
                       0x0c if filesystem == "vfat" else 0x07, "Windows", firmware == "bios")]
    if secondary is not None:
        # Match upstream CreatePartition in src/drive.c: Windows Setup can pick
        # the USB helper as its system partition if it is declared as an ESP.
        # UEFI still discovers its FAT loader; bit 63 suppresses a drive letter.
        parts.append(Partition(secondary, secondary_sectors, "basic", 0xef, "UEFI_NTFS",
                               gpt_attributes=1 << 63))
    if parts[0].sectors <= 0 or (filesystem == "vfat" and parts[0].sectors > 0xffffffff):
        raise RufusError("Windows partition size is outside the selected filesystem's limits.")
    return table, parts


def windows_bootcode(volume, filesystem):
    with volume.open("r+b") as disk:
        header = disk.read(512)
        if filesystem == "ntfs":
            if header[3:11] != b"NTFS    ":
                raise RufusError("Expected an NTFS filesystem for Windows boot code.")
            disk.seek(0)
            disk.write(asset("ntfs-head"))
            disk.seek(0x54)
            disk.write(asset("ntfs-boot"))
            disk.seek(0)
            primary = disk.read(512)
            disk.seek(-512, os.SEEK_END)
            disk.write(primary)
        else:
            if header[82:90] != b"FAT32   ":
                raise RufusError("Expected a FAT32 filesystem for Windows boot code.")
            backup = struct.unpack_from("<H", header, 50)[0]
            fsinfo = struct.unpack_from("<H", header, 48)[0]
            for base in (0, backup * 512) if backup not in (0, 0xffff) else (0,):
                # Upstream Windows boot code includes part of FSInfo. Preserve
                # Linux's known free/next-cluster hints so mtools can allocate.
                disk.seek(base + fsinfo * 512 + 488)
                hints = disk.read(8)
                for offset, name in ((0, "fat32-head"), (0x52, "fat32-windows"),
                                     (0x3f0, "fat32-windows-extra"), (0x1800, "fat32-windows-loader")):
                    disk.seek(base + offset)
                    disk.write(asset(name))
                disk.seek(base + fsinfo * 512 + 488)
                disk.write(hints)
        disk.flush()
        os.fsync(disk.fileno())


def populate_volume(root, volume, partition, filesystem, firmware, cancel=None, progress=None):
    if filesystem == "vfat":
        run_tool(["mkfs.vfat", "-F", "32", "-h", str(partition.start), "-n", "WINDOWS", str(volume)], cancel)
        if firmware == "bios":
            windows_bootcode(volume, filesystem)
        children = sorted(root.iterdir())
        for index, child in enumerate(children):
            emit(progress, "Copying Windows files to FAT32", index, len(children))
            run_tool(["mcopy", "-s", "-i", str(volume), str(child), "::/"], cancel)
        run_tool(["fsck.fat", "-n", str(volume)], cancel)
    else:
        from .wim import apply_ntfs_image
        capture = volume.with_suffix(".wim")
        emit(progress, "Capturing installer files for NTFS")
        run_tool(["wimlib-imagex", "capture", str(root), str(capture), "Windows installer files",
                  "--compress=none", "--check"], cancel)
        run_tool(["mkfs.ntfs", "-F", "-Q", "-s", "512", "-p", str(partition.start),
                  "-H", "255", "-S", "63", "-L", "WINDOWS", str(volume)], cancel)
        # This WIM transports files captured from a Linux directory. Its root
        # has no Windows system/hidden attributes, so retain mkntfs's metadata
        # instead of clearing the flags on the NTFS root directory record.
        apply_ntfs_image(capture, 1, volume, cancel, progress, preserve_attributes=False)
        windows_bootcode(volume, filesystem)


def create_windows_image(source, output, size=None, filesystem="vfat", firmware="uefi",
                         options=None, expected="", cancel=None, progress=None):
    options = options or WindowsOptions()
    options.validate()
    expected = expected_sha256(expected)
    if os.path.lexists(output):
        raise RufusError("Image destination already exists. Select a new filename.")
    if filesystem not in ("vfat", "ntfs") or firmware not in ("uefi", "bios"):
        raise RufusError("Select FAT32/NTFS and UEFI/BIOS for Windows media.")
    tools = ("mkfs.vfat", "mcopy", "fsck.fat") if filesystem == "vfat" else ("mkfs.ntfs", "wimlib-imagex")
    for tool in tools:
        if not shutil.which(tool):
            raise RufusError(f"Windows image creation requires {tool}.")
    before = os.stat(source)
    source_hash = checksum(source, cancel, progress)
    if expected and source_hash != expected:
        raise RufusError("Windows ISO SHA-256 does not match the expected checksum.")
    with tempfile.TemporaryDirectory(prefix="rufus-windows-image-") as directory:
        directory = Path(directory)
        root = directory / "files"
        root.mkdir()
        files = stage_windows_iso(source, root, cancel, progress, require_uefi=firmware == "uefi",
                                  split_wim=filesystem == "vfat")
        if options.enabled:
            files = apply_windows_options(root, options, cancel, progress, require_uefi=firmware == "uefi")
        if filesystem == "vfat" and any(amount > FAT_MAX_FILE for _, _, amount in files):
            raise RufusError("Customized installer files exceed FAT32's file size limit.")
        after = os.stat(source)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RufusError("Windows ISO changed during extraction.")
        content_size = sum(amount for _, _, amount in files)
        minimum = content_size + len(files) * 32768 + 32 * MIB
        if size is None:
            size = max(128 * MIB, ((content_size * 11 // 10 + len(files) * 32768 + 128 * MIB + MIB - 1) // MIB) * MIB)
        if size < minimum:
            raise RufusError("Installer files will not fit in the requested image size.")
        table, partitions = windows_partitions(size, filesystem, firmware)
        main = partitions[0]
        if main.sectors * 512 < minimum:
            raise RufusError("The installer partition is too small for the selected ISO.")
        volume = directory / "volume.img"
        with volume.open("xb") as stream:
            stream.truncate(main.sectors * 512)
        populate_volume(root, volume, main, filesystem, firmware, cancel, progress)
        with new_output(output) as target, volume.open("rb") as payload:
            os.ftruncate(target, size)
            write_layout(target, size, partitions, table)
            copy_sparse(payload.fileno(), target, main.start * 512, main.sectors * 512, cancel, progress)
            if len(partitions) > 1:
                driver = asset("uefi-ntfs.img")
                write_at(target, driver, partitions[1].start * 512)
            os.fsync(target)
            image_hash = checksum(output, cancel, progress)
            check_cancel(cancel)
            emit(progress, "Complete", size, size)
    return {"sha256": image_hash, "source_sha256": source_hash, "size": size,
            "filesystem": filesystem, "firmware": firmware, "table": table}
