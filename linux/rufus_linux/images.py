"""Bounded-memory archive decoding and native fixed/dynamic VHD support."""

from contextlib import contextmanager
import bz2
import gzip
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile
import time
import uuid
import zipfile

from .core import CHUNK, RufusError, check_cancel, emit, run_tool


def copy_stream(source, target, cancel=None, progress=None, stage="Decompressing image", total=0):
    done = 0
    while True:
        check_cancel(cancel)
        chunk = source.read(CHUNK)
        if not chunk:
            break
        target.write(chunk)
        done += len(chunk)
        emit(progress, stage, done, total)
    return done


def checksum_metadata(data, offset):
    data = bytearray(data)
    data[offset:offset + 4] = b"\0" * 4
    return (~sum(data)) & 0xffffffff


def validate_vhd_size(size):
    if not size or size % 512 or size > 2040 * 1024**3:
        raise RufusError("VHD size must be a positive multiple of 512 bytes, at most 2040 GiB.")


def vhd_footer(size):
    validate_vhd_size(size)
    # CHS calculation from Microsoft's VHD specification, also used by QEMU.
    sectors = min(size // 512, 65535 * 16 * 255)
    if sectors >= 65535 * 16 * 63:
        sectors_per_track, heads = 255, 16
        cylinders = sectors // (sectors_per_track * heads)
    else:
        sectors_per_track = 17
        cylinder_heads = sectors // sectors_per_track
        heads = max(4, (cylinder_heads + 1023) // 1024)
        if cylinder_heads >= heads * 1024 or heads > 16:
            sectors_per_track, heads = 31, 16
            cylinder_heads = sectors // sectors_per_track
        if cylinder_heads >= heads * 1024:
            sectors_per_track, heads = 63, 16
            cylinder_heads = sectors // sectors_per_track
        cylinders = cylinder_heads // heads
    footer = bytearray(512)
    struct.pack_into(">8sIIQI4sI4sQQHBBII16s", footer, 0,
                     b"conectix", 2, 0x10000, 0xffffffffffffffff,
                     max(0, int(time.time()) - 946684800), b"RufL", 0x10000, b"Wi2k",
                     size, size, cylinders, heads, sectors_per_track, 2, 0, uuid.uuid4().bytes)
    struct.pack_into(">I", footer, 64, checksum_metadata(footer, 64))
    return bytes(footer)


def vhd_info(source, length):
    if length < 512:
        raise RufusError("Truncated VHD footer.")
    source.seek(length - 512)
    footer = source.read(512)
    if len(footer) != 512 or footer[:8] != b"conectix" or struct.unpack_from(">I", footer, 64)[0] != checksum_metadata(footer, 64):
        raise RufusError("VHD footer signature/checksum is invalid.")
    if struct.unpack_from(">I", footer, 12)[0] != 0x10000:
        raise RufusError("Unsupported VHD format version.")
    size = struct.unpack_from(">Q", footer, 48)[0]
    kind = struct.unpack_from(">I", footer, 60)[0]
    if not size or size % 512 or size > 2040 * 1024**3:
        raise RufusError("Invalid VHD virtual disk size.")
    if kind not in (2, 3):
        raise RufusError("Differencing VHDs need a parent disk and cannot be restored independently.")
    if kind == 2 and length != size + 512:
        raise RufusError("Fixed VHD file length does not match its virtual disk size.")
    return size, kind, footer


def unpack_vhd(source, length, target, cancel=None, progress=None):
    size, kind, footer = vhd_info(source, length)
    if kind == 2:
        source.seek(0)
        remaining = size
        while remaining:
            check_cancel(cancel)
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise RufusError("Truncated fixed VHD payload.")
            target.write(chunk)
            remaining -= len(chunk)
            emit(progress, "Reading VHD image", size - remaining, size)
        return size
    header_offset = struct.unpack_from(">Q", footer, 16)[0]
    if header_offset < 512 or header_offset % 512 or header_offset + 1024 > length - 512:
        raise RufusError("Invalid dynamic VHD header offset.")
    source.seek(header_offset)
    header = source.read(1024)
    if (len(header) != 1024 or header[:8] != b"cxsparse" or struct.unpack_from(">I", header, 36)[0] != checksum_metadata(header, 36)
            or struct.unpack_from(">I", header, 24)[0] != 0x10000):
        raise RufusError("Dynamic VHD header signature/version/checksum is invalid.")
    table = struct.unpack_from(">Q", header, 16)[0]
    entries, block_size = struct.unpack_from(">II", header, 28)
    required = (size + block_size - 1) // block_size if block_size else 0
    if (not block_size or block_size < 512 or block_size > 256 * 1024**2 or block_size & (block_size - 1)
            or required > entries or entries > 4 * 1024**2 or table % 512
            or table < header_offset + 1024 or table + entries * 4 > length - 512):
        raise RufusError("Invalid dynamic VHD allocation table or block size.")
    source.seek(table)
    raw_table = source.read(entries * 4)
    if len(raw_table) != entries * 4:
        raise RufusError("Truncated dynamic VHD allocation table.")
    allocation = struct.unpack(f">{entries}I", raw_table)
    bitmap_size = ((block_size // 512 + 7) // 8 + 511) // 512 * 512
    metadata_end = (table + entries * 4 + 511) // 512 * 512
    ranges = []
    for entry in allocation[:required]:
        if entry != 0xffffffff:
            start, end = entry * 512, entry * 512 + bitmap_size + block_size
            if start < metadata_end or end > length - 512:
                raise RufusError("Dynamic VHD block points outside its data area.")
            ranges.append((start, end))
    ranges.sort()
    if any(left[1] > right[0] for left, right in zip(ranges, ranges[1:])):
        raise RufusError("Dynamic VHD has overlapping data blocks.")
    target.truncate(size)
    for index, entry in enumerate(allocation[:required]):
        check_cancel(cancel)
        base = index * block_size
        amount = min(block_size, size - base)
        if entry != 0xffffffff:
            source.seek(entry * 512)
            bitmap = source.read(bitmap_size)
            if len(bitmap) != bitmap_size:
                raise RufusError("Truncated dynamic VHD sector bitmap.")
            sector = 0
            while sector < amount // 512:
                present = bool(bitmap[sector // 8] & (0x80 >> (sector % 8)))
                end = sector + 1
                while end < amount // 512 and bool(bitmap[end // 8] & (0x80 >> (end % 8))) == present:
                    end += 1
                if present:
                    source.seek(entry * 512 + bitmap_size + sector * 512)
                    target.seek(base + sector * 512)
                    left = (end - sector) * 512
                    while left:
                        check_cancel(cancel)
                        data = source.read(min(CHUNK, left))
                        if not data:
                            raise RufusError("Truncated dynamic VHD data block.")
                        target.write(data)
                        left -= len(data)
                sector = end
        emit(progress, "Reading VHD image", base + amount, size)
    return size


@contextmanager
def payload_stream(source, length, filename, cancel=None, progress=None):
    source.seek(0)
    magic = source.read(8)
    source.seek(0)
    suffix = Path(filename).suffix.lower()
    transformed = False
    with tempfile.TemporaryFile() as unpacked, tempfile.TemporaryFile() as disk:
        decoder = (gzip.open if magic.startswith(b"\x1f\x8b") or suffix == ".gz" else
                   bz2.BZ2File if magic.startswith(b"BZh") or suffix == ".bz2" else
                   lzma.LZMAFile if magic.startswith(b"\xfd7zXZ\0") or suffix in (".lzma", ".xz") else None)
        try:
            if suffix == ".zip" or magic.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                with zipfile.ZipFile(source) as archive:
                    members = [item for item in archive.infolist() if not item.is_dir()]
                    if len(members) != 1:
                        raise RufusError("ZIP disk-image archives must contain exactly one regular file.")
                    member = members[0]
                    mode = member.external_attr >> 16
                    if (member.flag_bits & 1 or (stat.S_IFMT(mode) not in (0, stat.S_IFREG))):
                        raise RufusError("ZIP disk image cannot be encrypted or a link/special file.")
                    with archive.open(member) as stream:
                        length = copy_stream(stream, unpacked, cancel, progress, total=member.file_size)
                    if length != member.file_size:
                        raise RufusError("ZIP image length does not match its archive metadata.")
                    suffix = Path(member.filename).suffix.lower()
                transformed = True
            elif decoder:
                with decoder(source, "rb") as stream:
                    length = copy_stream(stream, unpacked, cancel, progress)
                transformed = True
            elif magic.startswith(b"\x28\xb5\x2f\xfd") or suffix in (".zst", ".zstd"):
                try:
                    from compression import zstd
                except ImportError:
                    if not shutil.which("zstd"):
                        raise RufusError("Zstandard images require Python 3.14 or the zstd package.")
                    os.lseek(source.fileno(), 0, os.SEEK_SET)
                    run_tool(["zstd", "--decompress", "--stdout"], cancel, stdin=source, stdout=unpacked)
                    length = unpacked.tell()
                else:
                    with zstd.open(source, "rb") as stream:
                        length = copy_stream(stream, unpacked, cancel, progress)
                transformed = True
            elif magic.startswith(b"\x1f\x9d") or suffix == ".z":
                if not shutil.which("gzip"):
                    raise RufusError("Unix compress (.Z) images require gzip.")
                os.lseek(source.fileno(), 0, os.SEEK_SET)
                run_tool(["gzip", "--decompress", "--stdout"], cancel, stdin=source, stdout=unpacked)
                length = unpacked.tell()
                transformed = True
            elif magic.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!")) or suffix in (".7z", ".rar", ".tar"):
                raise RufusError("This archive format is not supported. Extract its disk image before writing.")
        except RufusError:
            raise
        except Exception as exc:
            raise RufusError(f"Cannot decompress this image: {exc}") from exc
        stream = unpacked if transformed else source
        stream.seek(0)
        header = stream.read(16)
        stream.seek(max(0, length - 512))
        footer = stream.read(512)
        if suffix == ".vhd" or header.startswith(b"conectix") or footer[:8] == b"conectix":
            length = unpack_vhd(stream, length, disk, cancel, progress)
            stream, transformed = disk, True
        elif header.startswith(b"vhdxfile") or suffix in (".vhdx", ".ffu", ".vtsi") or header[4:] == b"SignedImage ":
            raise RufusError("This disk container is not supported yet. Convert it to a raw IMG before writing.")
        stream.seek(0)
        yield stream, length, transformed
