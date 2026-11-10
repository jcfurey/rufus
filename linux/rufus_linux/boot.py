"""Unprivileged boot-media construction from upstream Rufus resources."""

import errno
import os
from pathlib import Path
import shutil
import struct
import tempfile

from .assets import asset, freedos_files
from .backup import new_output
from .core import CHUNK, RufusError, check_cancel, checksum, emit, run_tool, write_all

MIB = 1024 * 1024
PARTITION_OFFSET = MIB


def chs(lba):
    cylinder, remainder = divmod(lba, 255 * 63)
    head, sector = divmod(remainder, 63)
    sector += 1
    if cylinder > 1023:
        cylinder, head, sector = 1023, 254, 63
    return bytes((head, sector | ((cylinder >> 2) & 0xc0), cylinder & 255))


def mbr(size, partition_type=0x0c):
    sectors = size // 512
    if size % 512 or not 2048 < sectors <= 2**32:
        raise RufusError("MBR image must have 512-byte sectors and fit in 2 TiB.")
    result = bytearray(512)
    code = asset("mbr-win7")
    if not 0 < len(code) <= 446:
        raise RufusError("Invalid bundled MBR boot code.")
    result[:len(code)] = code
    result[440:444] = os.urandom(4)
    result[446:462] = struct.pack("<B3sB3sII", 0x80, chs(2048), partition_type, chs(sectors - 1),
                                  2048, sectors - 2048)
    result[510:512] = b"\x55\xaa"
    return bytes(result)


def install_freedos_bootcode(volume, fat_bits):
    with volume.open("r+b") as disk:
        header = disk.read(512)
        expected = b"FAT32   " if fat_bits == 32 else b"FAT16   "
        location = 0x52 if fat_bits == 32 else 0x36
        if header[location:location + 8] != expected or struct.unpack_from("<H", header, 11)[0] != 512:
            raise RufusError("Formatter did not create the requested 512-byte FAT filesystem.")
        disk.seek(0)
        disk.write(asset("fat32-head" if fat_bits == 32 else "fat16-head"))
        disk.seek(0x52 if fat_bits == 32 else 0x3e)
        disk.write(asset("fat32-freedos" if fat_bits == 32 else "fat16-freedos"))
        if fat_bits == 32:
            disk.seek(0x3f0)
            disk.write(asset("fat32-freedos-extra"))
            # Keep the FAT32 backup boot sector consistent with the primary.
            backup = struct.unpack_from("<H", header, 50)[0]
            if backup not in (0, 0xffff):
                disk.seek(0)
                primary = disk.read(512)
                disk.seek(backup * 512)
                disk.write(primary)
        disk.flush()
        os.fsync(disk.fileno())


def copy_sparse(source_fd, target_fd, offset, size, cancel=None, progress=None, *, stage="Assembling bootable image"):
    position = 0
    while position < size:
        check_cancel(cancel)
        try:
            start = os.lseek(source_fd, position, os.SEEK_DATA)
            end = min(size, os.lseek(source_fd, start, os.SEEK_HOLE))
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            if exc.errno != errno.EINVAL:
                raise
            start, end = position, size
        os.lseek(source_fd, start, os.SEEK_SET)
        os.lseek(target_fd, offset + start, os.SEEK_SET)
        while start < end:
            check_cancel(cancel)
            data = os.read(source_fd, min(CHUNK, end - start))
            if not data:
                raise RufusError("Filesystem image ended during assembly.")
            write_all(target_fd, data)
            start += len(data)
            emit(progress, stage, start, size)
        position = end


def validate_freedos(size, fat_bits=32, label="FREEDOS"):
    if fat_bits not in (16, 32):
        raise RufusError("FreeDOS supports FAT16 or FAT32.")
    minimum, maximum = (32 * MIB, 2 * 1024**3) if fat_bits == 16 else (64 * MIB, 2 * 1024**4)
    if size % MIB or not minimum <= size <= maximum:
        raise RufusError(f"FAT{fat_bits} image size must be a whole number of MiB, from {minimum // MIB} to {maximum // MIB} MiB.")
    if not label or len(label) > 11 or not label.isascii() or any(
            ord(c) < 32 or c in '*?\\/:<>|"+.,;=[]' for c in label):
        raise RufusError("FAT volume label must be 1–11 ASCII characters without reserved punctuation.")
    for tool in ("mkfs.vfat", "mcopy", "mmd", "fsck.fat"):
        if not shutil.which(tool):
            raise RufusError(f"FreeDOS image creation requires {tool}; install dosfstools and mtools.")


def create_freedos(output, size=64 * MIB, fat_bits=32, label="FREEDOS", cancel=None, progress=None):
    validate_freedos(size, fat_bits, label)
    if os.path.lexists(output):
        raise RufusError("Image destination already exists. Select a new filename.")
    payload = freedos_files()
    check_cancel(cancel)
    with tempfile.TemporaryDirectory(prefix="rufus-freedos-") as directory:
        directory = Path(directory)
        volume = directory / "fat.img"
        with volume.open("xb") as stream:
            stream.truncate(size - PARTITION_OFFSET)
        emit(progress, "Creating FreeDOS filesystem")
        run_tool(["mkfs.vfat", "-F", str(fat_bits), "-h", "2048", "-n", label, str(volume)], cancel)
        install_freedos_bootcode(volume, fat_bits)
        for filename in ("KERNEL.SYS", "COMMAND.COM"):
            source = directory / filename
            source.write_bytes(payload[filename])
            run_tool(["mcopy", "-i", str(volume), str(source), "::/"], cancel)
        locale = directory / "LOCALE"
        locale.mkdir()
        for filename, data in payload.items():
            if filename not in ("KERNEL.SYS", "COMMAND.COM"):
                (locale / filename).write_bytes(data)
        run_tool(["mcopy", "-s", "-i", str(volume), str(locale), "::/"], cancel)
        autoexec = directory / "AUTOEXEC.BAT"
        autoexec.write_bytes(b"@echo off\r\nset PATH=.;\\;\\LOCALE\r\nset TEMP=C:\\\r\n"
                             b"echo Welcome to FreeDOS\r\nver\r\n")
        run_tool(["mcopy", "-i", str(volume), str(autoexec), "::/"], cancel)
        emit(progress, "Checking FreeDOS filesystem")
        run_tool(["fsck.fat", "-n", str(volume)], cancel)
        with new_output(output) as target, volume.open("rb") as source:
            os.ftruncate(target, size)
            write_all(target, mbr(size, 0x0e if fat_bits == 16 else 0x0c))
            copy_sparse(source.fileno(), target, PARTITION_OFFSET, size - PARTITION_OFFSET, cancel, progress)
            os.fsync(target)
            digest = checksum(output, cancel, progress)
            emit(progress, "Complete", size, size)
    return digest
