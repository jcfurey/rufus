"""MBR/GPT partition tables for newly created disk-image files."""

from dataclasses import dataclass
import os
import struct
import uuid
import zlib

from .assets import asset
from .boot import chs
from .core import RufusError, write_at

GUID_TYPES = {"esp": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
              "msr": "e3c9e316-0b5c-4db8-817d-f92df00215ae",
              "basic": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7",
              "linux": "0fc63daf-8483-4772-8e79-3d69d8477de4"}


@dataclass(frozen=True)
class Partition:
    start: int
    sectors: int
    kind: str = "basic"
    mbr_type: int = 0x07
    name: str = "Rufus"
    bootable: bool = False
    gpt_attributes: int = 0

    @property
    def end(self):
        return self.start + self.sectors - 1


def write_layout(fd, size, partitions, table="gpt", *, sector=512):
    if sector not in (512, 1024, 2048, 4096) or size % sector or size < 2 * 1024**2 or table not in ("gpt", "dos"):
        raise RufusError("Partition image requires a valid size and MBR/GPT scheme.")
    sectors = size // sector
    array_sectors = 128 * 128 // sector
    first, last = (2 + array_sectors, sectors - array_sectors - 2) if table == "gpt" else (1, sectors - 1)
    maximum = 128 if table == "gpt" else 4
    if not 1 <= len(partitions) <= maximum:
        raise RufusError("Invalid partition count.")
    ordered = sorted(partitions, key=lambda p: p.start)
    if any(p.sectors <= 0 or p.start < first or p.end > last or p.kind not in GUID_TYPES for p in ordered):
        raise RufusError("Partition lies outside the image's usable sectors.")
    if any(type(p.gpt_attributes) is not int or not 0 <= p.gpt_attributes < 2**64 for p in ordered):
        raise RufusError("GPT partition attributes must be an unsigned 64-bit integer.")
    if any(a.end >= b.start for a, b in zip(ordered, ordered[1:])):
        raise RufusError("Image partitions overlap.")
    master = bytearray(512)
    master[510:512] = b"\x55\xaa"
    if table == "dos":
        if sectors > 2**32:
            raise RufusError("MBR images cannot exceed 2**32 logical sectors.")
        code = asset("mbr-win7")
        master[:len(code)] = code
        master[440:444] = os.urandom(4)
        for index, partition in enumerate(partitions):
            start = 446 + index * 16
            master[start:start + 16] = struct.pack("<B3sB3sII", 0x80 if partition.bootable else 0,
                chs(partition.start), partition.mbr_type, chs(partition.end), partition.start, partition.sectors)
        write_at(fd, master, 0)
        return
    master[446:462] = struct.pack("<B3sB3sII", 0, chs(1), 0xee, b"\xff\xff\xff", 1, min(sectors - 1, 0xffffffff))
    write_at(fd, master, 0)
    entries = bytearray(128 * 128)
    for index, partition in enumerate(partitions):
        name = partition.name.encode("utf-16-le")
        if len(name) > 72:
            raise RufusError("GPT partition name exceeds 36 UTF-16 code units.")
        struct.pack_into("<16s16sQQQ72s", entries, index * 128, uuid.UUID(GUID_TYPES[partition.kind]).bytes_le,
                         uuid.uuid4().bytes_le, partition.start, partition.end, partition.gpt_attributes, name)
    disk_guid = uuid.uuid4().bytes_le
    entries_crc = zlib.crc32(entries)

    def header(current, alternate, array):
        value = bytearray(sector)
        struct.pack_into("<8sIIIIQQQQ16sQIII", value, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         current, alternate, first, last, disk_guid, array, 128, 128, entries_crc)
        struct.pack_into("<I", value, 16, zlib.crc32(value[:92]))
        return value

    backup_array = sectors - array_sectors - 1
    write_at(fd, entries, 2 * sector)
    write_at(fd, entries, backup_array * sector)
    write_at(fd, header(1, sectors - 1, 2), sector)
    write_at(fd, header(sectors - 1, 1, backup_array), (sectors - 1) * sector)
