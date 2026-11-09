"""512-byte MBR/GPT partition tables for newly created disk-image files."""

from dataclasses import dataclass
import os
import struct
import uuid
import zlib

from .assets import asset
from .boot import chs
from .core import RufusError, write_at

GUID_TYPES = {"esp": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b",
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

    @property
    def end(self):
        return self.start + self.sectors - 1


def write_layout(fd, size, partitions, table="gpt"):
    if size % 512 or size < 2 * 1024**2 or table not in ("gpt", "dos"):
        raise RufusError("Partition image requires a valid size and MBR/GPT scheme.")
    sectors = size // 512
    first, last = (34, sectors - 34) if table == "gpt" else (1, sectors - 1)
    maximum = 128 if table == "gpt" else 4
    if not 1 <= len(partitions) <= maximum:
        raise RufusError("Invalid partition count.")
    ordered = sorted(partitions, key=lambda p: p.start)
    if any(p.sectors <= 0 or p.start < first or p.end > last or p.kind not in GUID_TYPES for p in ordered):
        raise RufusError("Partition lies outside the image's usable sectors.")
    if any(a.end >= b.start for a, b in zip(ordered, ordered[1:])):
        raise RufusError("Image partitions overlap.")
    master = bytearray(512)
    master[510:512] = b"\x55\xaa"
    if table == "dos":
        if sectors > 2**32:
            raise RufusError("MBR images cannot exceed 2 TiB with 512-byte sectors.")
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
                         uuid.uuid4().bytes_le, partition.start, partition.end, 0, name)
    disk_guid = uuid.uuid4().bytes_le
    entries_crc = zlib.crc32(entries)

    def header(current, alternate, array):
        value = bytearray(512)
        struct.pack_into("<8sIIIIQQQQ16sQIII", value, 0, b"EFI PART", 0x10000, 92, 0, 0,
                         current, alternate, first, last, disk_guid, array, 128, 128, entries_crc)
        struct.pack_into("<I", value, 16, zlib.crc32(value[:92]))
        return value

    write_at(fd, entries, 2 * 512)
    write_at(fd, entries, (sectors - 33) * 512)
    write_at(fd, header(1, sectors - 1, 2), 512)
    write_at(fd, header(sectors - 1, 1, sectors - 33), (sectors - 1) * 512)
