"""Read-only standalone VHDX disks and fixed VHDX creation (MS-VHDX).

All integers are explicitly little-endian. Metadata checks and an on-disk
allocation-range index finish before a logical disk stream is exposed.
Differencing disks and logs requiring replay are deliberately rejected.
"""

import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import uuid

from .core import CHUNK, RufusError, check_cancel, emit, write_at

MIB = 1024**2
MAX_SIZE = 64 * 1024**4
BLOCK_SIZE = 32 * MIB
BAT = uuid.UUID("2dc27766-f623-4200-9d64-115e9bfd4a08").bytes_le
METADATA = uuid.UUID("8b7ca206-4790-4b9a-b8fe-575f050f886e").bytes_le
PARAMETERS = uuid.UUID("caa16737-fa36-4d43-b3b6-33f0aa44e76b").bytes_le
DISK_SIZE = uuid.UUID("2fa54224-cd1b-4876-b211-5dbed83bf4b8").bytes_le
DISK_ID = uuid.UUID("beca12ab-b2e6-4523-93ef-c309e000c746").bytes_le
LOGICAL_SECTOR = uuid.UUID("8141bf1d-a96f-4709-ba47-f233a8faab5f").bytes_le
PHYSICAL_SECTOR = uuid.UUID("cda348c7-445d-4471-9cc9-e9885251c556").bytes_le
PARENT = uuid.UUID("a8d35f2d-b30b-454d-abf7-d3d84834ab0c").bytes_le


def _crc_table():
    result = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0x82f63b78 if value & 1 else 0)
        result.append(value)
    return tuple(result)


_CRC = _crc_table()


def crc32c(data):
    value = 0xffffffff
    for byte in data:
        value = _CRC[(value ^ byte) & 255] ^ (value >> 8)
    return value ^ 0xffffffff


def _checksum(data):
    return crc32c(data[:4] + b"\0" * 4 + data[8:])


def _seal(data):
    struct.pack_into("<I", data, 4, _checksum(data))
    return data


def fixed_layout(size, logical_sector=512):
    if (type(size) is not int or logical_sector not in (512, 4096) or
            size <= 0 or size > MAX_SIZE or size % logical_sector):
        raise RufusError("VHDX requires 512-byte or 4096-byte sectors, an aligned size, and at most 64 TiB.")
    blocks = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
    ratio = (1 << 23) * logical_sector // BLOCK_SIZE
    entries = blocks + (blocks - 1) // ratio
    bat_length = ((entries * 8 + MIB - 1) // MIB) * MIB
    metadata = 2 * MIB + bat_length
    payload = metadata + MIB
    return {"blocks": blocks, "ratio": ratio, "entries": entries, "bat_length": bat_length,
            "metadata": metadata, "payload": payload, "file_size": payload + blocks * BLOCK_SIZE}


def write_fixed_header(fd, size, logical_sector=512, cancel=None, progress=None):
    layout = fixed_layout(size, logical_sector)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise RufusError("VHDX backup destination must be a new regular file.")
    check_cancel(cancel)
    os.ftruncate(fd, layout["file_size"])
    identifier = bytearray(64 * 1024)
    identifier[:8] = b"vhdxfile"
    creator = "Rufus for Linux".encode("utf-16-le")
    identifier[8:8 + len(creator)] = creator
    write_at(fd, identifier, 0)
    file_id, data_id = uuid.uuid4().bytes_le, uuid.uuid4().bytes_le
    for sequence, offset in ((1, 65536), (2, 131072)):
        header = bytearray(4096)
        struct.pack_into("<4sIQ16s16s16sHHIQ", header, 0, b"head", 0, sequence,
                         file_id, data_id, bytes(16), 0, 1, MIB, MIB)
        write_at(fd, _seal(header), offset)
    regions = bytearray(65536)
    struct.pack_into("<4sIII", regions, 0, b"regi", 0, 2, 0)
    struct.pack_into("<16sQII", regions, 16, BAT, 2 * MIB, layout["bat_length"], 1)
    struct.pack_into("<16sQII", regions, 48, METADATA, layout["metadata"], MIB, 1)
    _seal(regions)
    for offset in (192 * 1024, 256 * 1024):
        write_at(fd, regions, offset)
    metadata = bytearray(65536 + 40)
    struct.pack_into("<8sHH20s", metadata, 0, b"metadata", 0, 5, bytes(20))
    items = ((PARAMETERS, struct.pack("<II", BLOCK_SIZE, 1), 4),
             (DISK_SIZE, struct.pack("<Q", size), 6),
             (DISK_ID, uuid.uuid4().bytes_le, 6),
             (LOGICAL_SECTOR, struct.pack("<I", logical_sector), 6),
             (PHYSICAL_SECTOR, struct.pack("<I", logical_sector), 6))
    position = 65536
    for number, (key, value, flags) in enumerate(items):
        struct.pack_into("<16sIIII", metadata, 32 + number * 32, key, position, len(value), flags, 0)
        metadata[position:position + len(value)] = value
        position += len(value)
    write_at(fd, metadata, layout["metadata"])
    for first in range(0, layout["entries"], CHUNK // 8):
        check_cancel(cancel)
        amount = min(CHUNK // 8, layout["entries"] - first)
        values = bytearray(amount * 8)
        for local in range(amount):
            index = first + local
            if (index + 1) % (layout["ratio"] + 1):
                block = index - index // (layout["ratio"] + 1)
                struct.pack_into("<Q", values, local * 8, layout["payload"] + block * BLOCK_SIZE | 6)
        write_at(fd, values, 2 * MIB + first * 8)
        emit(progress, "Creating VHDX allocation table", first + amount, layout["entries"])
    return layout["payload"]


class VHDXStream:
    """A validated, seekable logical disk view with bounded working memory."""

    def __init__(self, source, length, cancel=None, progress=None, max_size=None):
        self.source, self.length, self.position = source, length, 0
        self.cancel = cancel
        check_cancel(cancel)
        if length < MIB or self._read(0, 8) != b"vhdxfile":
            raise RufusError("VHDX file identifier or header area is invalid/truncated.")
        headers = []
        for offset in (65536, 131072):
            data = self._read(offset, 4096)
            if data[:4] == b"head" and struct.unpack_from("<I", data, 4)[0] == _checksum(data):
                headers.append(data)
        if not headers:
            raise RufusError("Both VHDX header checksums are invalid.")
        if len(headers) == 2 and headers[0][8:16] == headers[1][8:16]:
            raise RufusError("VHDX headers have ambiguous sequence numbers.")
        header = max(headers, key=lambda value: struct.unpack_from("<Q", value, 8)[0])
        if struct.unpack_from("<H", header, 66)[0] != 1:
            raise RufusError("Unsupported VHDX version.")
        if header[48:64] != bytes(16):
            raise RufusError("VHDX requires log replay. Cleanly close it in its originating hypervisor before restoring.")
        log_length, log_offset = struct.unpack_from("<IQ", header, 68)
        ranges = [(0, MIB)]
        if log_length:
            self._range(log_offset, log_length)
            ranges.append((log_offset, log_offset + log_length))
        tables = []
        for offset in (192 * 1024, 256 * 1024):
            data = self._read(offset, 65536)
            if data[:4] != b"regi" or struct.unpack_from("<I", data, 4)[0] != _checksum(data):
                continue
            count = struct.unpack_from("<I", data, 8)[0]
            if not 1 <= count <= 2047:
                raise RufusError("Invalid VHDX region entry count.")
            entries = {}
            for index in range(count):
                key, start, size, flags = struct.unpack_from("<16sQII", data, 16 + index * 32)
                if key in entries or flags & ~1 or (flags & 1 and key not in (BAT, METADATA)):
                    raise RufusError("Duplicate or unsupported required VHDX region.")
                self._range(start, size)
                entries[key] = (start, size, flags)
            tables.append(entries)
        if not tables or any(table != tables[0] for table in tables[1:]):
            raise RufusError("VHDX region table checksums are invalid or the copies disagree.")
        regions = tables[0]
        if BAT not in regions or METADATA not in regions:
            raise RufusError("VHDX is missing its allocation or metadata region.")
        ranges.extend((start, start + size) for start, size, _ in regions.values())
        metadata, metadata_size, _ = regions[METADATA]
        data = self._read(metadata, 65536)
        count = struct.unpack_from("<H", data, 10)[0]
        if data[:8] != b"metadata" or not 1 <= count <= 2047:
            raise RufusError("Invalid VHDX metadata table.")
        entries, item_ranges = {}, []
        known = {PARAMETERS: 8, DISK_SIZE: 8, DISK_ID: 16, LOGICAL_SECTOR: 4, PHYSICAL_SECTOR: 4}
        for index in range(count):
            key, offset, size, flags, reserved = struct.unpack_from("<16sIIII", data, 32 + index * 32)
            if key == PARENT:
                raise RufusError("Differencing VHDX images require a parent and cannot be restored independently.")
            if (key in entries or flags & ~7 or reserved or (flags & 4 and key not in known) or
                    offset < 65536 or not size or offset + size > metadata_size):
                raise RufusError("Invalid, duplicate, or unsupported required VHDX metadata.")
            if key in known and size != known[key]:
                raise RufusError("Invalid VHDX metadata item length.")
            if key in known and flags != (4 if key == PARAMETERS else 6):
                raise RufusError("Invalid VHDX system metadata flags.")
            entries[key] = (offset, size)
            item_ranges.append((offset, offset + size))
        item_ranges.sort()
        if any(a[1] > b[0] for a, b in zip(item_ranges, item_ranges[1:])) or not known.keys() <= entries.keys():
            raise RufusError("VHDX metadata overlaps or lacks required disk information.")
        def value(key, form):
            offset, size = entries[key]
            return struct.unpack(form, self._read(metadata + offset, size))
        self.block_size, flags = value(PARAMETERS, "<II")
        if flags & 2:
            raise RufusError("Differencing VHDX images require a parent and cannot be restored independently.")
        if flags & ~3 or not MIB <= self.block_size <= 256 * MIB or self.block_size & (self.block_size - 1):
            raise RufusError("Invalid VHDX file parameters or block size.")
        self.size, = value(DISK_SIZE, "<Q")
        self.logical_sector, = value(LOGICAL_SECTOR, "<I")
        physical, = value(PHYSICAL_SECTOR, "<I")
        if (self.logical_sector not in (512, 4096) or physical not in (512, 4096) or
                not 0 < self.size <= MAX_SIZE or self.size % self.logical_sector):
            raise RufusError("Invalid VHDX disk size or sector geometry.")
        if max_size is not None and self.size > max_size:
            raise RufusError("The image is larger than the selected drive.")
        self.ratio = (1 << 23) * self.logical_sector // self.block_size
        blocks = (self.size + self.block_size - 1) // self.block_size
        self.bat_count = blocks + (blocks - 1) // self.ratio
        self.bat_offset, bat_length, _ = regions[BAT]
        if self.bat_count * 8 > bat_length:
            raise RufusError("VHDX allocation table is too short for the virtual disk.")
        self._validate_blocks(ranges, cancel, progress)

    def _read(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.length:
            raise RufusError("VHDX structure points outside the container.")
        self.source.seek(offset)
        data = self.source.read(size)
        if len(data) != size:
            raise RufusError("Truncated VHDX structure or data.")
        return data

    def _range(self, start, size):
        if start < MIB or start % MIB or not size or size % MIB or start + size > self.length:
            raise RufusError("Invalid VHDX region/log range or alignment.")

    def _validate_blocks(self, ranges, cancel, progress):
        # SQLite's primary-key B-tree sorts ranges on disk. This avoids a list
        # proportional to every allocated block on a multi-terabyte disk.
        with tempfile.TemporaryDirectory(prefix="rufus-vhdx-index-") as directory:
            database = sqlite3.connect(str(Path(directory) / "ranges.sqlite"))
            try:
                database.execute("PRAGMA journal_mode=OFF")
                database.execute("PRAGMA synchronous=OFF")
                database.execute("PRAGMA cache_size=-2048")
                database.execute("CREATE TABLE ranges (start INTEGER PRIMARY KEY, finish INTEGER)")
                database.executemany("INSERT INTO ranges VALUES (?,?)", ranges)
                for first in range(0, self.bat_count, CHUNK // 8):
                    check_cancel(cancel)
                    count = min(CHUNK // 8, self.bat_count - first)
                    data = self._read(self.bat_offset + first * 8, count * 8)
                    pending = []
                    for local, (entry,) in enumerate(struct.iter_unpack("<Q", data)):
                        index = first + local
                        if not local % 1024:
                            check_cancel(cancel)
                        if (index + 1) % (self.ratio + 1) == 0:
                            if entry:
                                raise RufusError("Standalone VHDX has an unexpected sector bitmap.")
                            continue
                        state, offset = entry & 7, entry & ~0xfffff
                        if entry & 0xffff8 or state not in (0, 1, 2, 3, 6):
                            raise RufusError("VHDX has a reserved or parent-dependent allocation state.")
                        if state == 6 and not offset:
                            raise RufusError("VHDX present block has no file offset.")
                        if offset:
                            block = index - index // (self.ratio + 1)
                            needed = min(self.block_size, self.size - block * self.block_size)
                            if offset < MIB or offset + needed > self.length:
                                raise RufusError("VHDX data block points outside the container.")
                            # Older QEMU versions stored only the final block's
                            # logical bytes. Never permit missing logical data.
                            pending.append((offset, offset + min(self.block_size, self.length - offset)))
                        if len(pending) == 1024:
                            database.executemany("INSERT INTO ranges VALUES (?,?)", pending)
                            pending.clear()
                    database.executemany("INSERT INTO ranges VALUES (?,?)", pending)
                    emit(progress, "Validating VHDX allocation table", first + count, self.bat_count)
                finish = 0
                for number, (start, end) in enumerate(database.execute("SELECT start,finish FROM ranges ORDER BY start")):
                    if not number % 1024:
                        check_cancel(cancel)
                    if start < finish:
                        raise RufusError("VHDX data, log, or metadata ranges overlap.")
                    finish = end
            except sqlite3.IntegrityError as exc:
                raise RufusError("VHDX has duplicate or overlapping allocated ranges.") from exc
            except sqlite3.DatabaseError as exc:
                raise RufusError("Cannot validate VHDX allocation ranges: " + str(exc)) from exc
            finally:
                database.close()

    def seek(self, offset, whence=os.SEEK_SET):
        position = offset if whence == os.SEEK_SET else self.position + offset if whence == os.SEEK_CUR else (
            self.size + offset if whence == os.SEEK_END else -1)
        if position < 0:
            raise ValueError("Invalid VHDX logical seek")
        self.position = position
        return position

    def tell(self):
        return self.position

    def read(self, size=-1):
        size = min(self.size - self.position, size if size >= 0 else self.size)
        if size <= 0:
            return b""
        result = bytearray()
        while size:
            check_cancel(self.cancel)
            block, within = divmod(self.position, self.block_size)
            entry, = struct.unpack("<Q", self._read(self.bat_offset + (block + block // self.ratio) * 8, 8))
            amount = min(size, self.block_size - within)
            result.extend(self._read((entry & ~0xfffff) + within, amount) if entry & 7 == 6 else bytes(amount))
            self.position += amount
            size -= amount
        return bytes(result)
