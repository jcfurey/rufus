"""Data-volume formatting plans shared by UDisks and unprivileged images."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import tempfile

from .backup import new_output
from .boot import copy_sparse
from .core import RufusError, check_cancel, checksum, emit, run_tool
from .layout import GUID_TYPES, Partition, write_layout

MIB = 1024**2
FS_TOOLS = {"vfat": "mkfs.vfat", "exfat": "mkfs.exfat", "ntfs": "mkfs.ntfs",
            "ext2": "mkfs.ext2", "ext3": "mkfs.ext3", "ext4": "mkfs.ext4",
            "fat16": "mkfs.vfat", "udf": "mkudffs"}
FS_NAMES = ("FAT32", "exFAT", "NTFS", "ext2", "ext3", "ext4", "FAT16", "UDF")


@dataclass(frozen=True)
class FormatPlan:
    filesystem: str
    label: str
    table: str
    size: int
    sector: int
    partition: Partition
    args: tuple

    @property
    def backend(self):
        return "vfat" if self.filesystem == "fat16" else self.filesystem

    @property
    def kind(self):
        return GUID_TYPES[self.partition.kind] if self.table == "gpt" else f"0x{self.partition.mbr_type:02x}"


def plan_format(size, filesystem="vfat", label="RUFUS", table="gpt", sector=512):
    if filesystem not in FS_TOOLS or table not in ("gpt", "dos"):
        raise RufusError("Unsupported filesystem or partition table.")
    if sector not in (512, 1024, 2048, 4096) or size % sector:
        raise RufusError("Formatting requires aligned 512–4096-byte logical sectors.")
    if size < 64 * MIB:
        raise RufusError("Formatting requires a drive or image of at least 64 MiB.")
    if table == "dos" and size // sector > 2**32:
        raise RufusError("Use GPT when the drive exceeds MBR's 32-bit logical-sector limit.")
    if filesystem == "fat16" and size >= 4 * 1024**3:
        raise RufusError("FAT16 requires a drive or image smaller than 4 GiB. Select FAT32 or exFAT.")
    # Keep a MiB free at each end, including GPT's backup metadata.
    start, end = MIB // sector, (size - MIB) // sector
    count = end - start
    if filesystem == "vfat":
        # FAT type is determined by cluster count, not the FAT32 boot label.
        minimum = 65525 + 32 + 2 * ((65527 * 4 + sector - 1) // sector)
        if count < minimum:
            raise RufusError("FAT32 needs more space for this logical sector size. Use a larger drive or FAT16.")
        if count > 0xffffffff:
            raise RufusError("FAT32 exceeds its 32-bit sector count. Select exFAT or another filesystem.")
    if filesystem == "udf" and count > 0xffffffff:
        raise RufusError("UDF exceeds its 32-bit block count for this logical sector size.")
    if not label or any(ord(c) < 32 or ord(c) == 127 or c in '\\/:*?"<>|' for c in label):
        raise RufusError("Volume label must be nonempty and contain no control or reserved characters.")
    if filesystem == "udf":
        limit = 126 if all(ord(c) <= 255 for c in label) else 63
        if len(label) > limit or any(ord(c) > 0xffff or 0xd800 <= ord(c) <= 0xdfff for c in label):
            raise RufusError("UDF labels allow up to 126 Latin-1 or 63 BMP characters; supplementary characters are unsupported.")
    else:
        limit = {"fat16": 11, "vfat": 11, "ntfs": 32, "exfat": 15}.get(filesystem, 16)
        if len(label.encode("utf-8")) > limit:
            raise RufusError(f"Volume label must contain 1–{limit} bytes without reserved characters.")
    args = ()
    if filesystem in ("vfat", "fat16"):
        if not label.isascii() or any(c in '+.,;=[]' for c in label):
            raise RufusError("FAT labels require ASCII and cannot contain + . , ; = [ ].")
        label = label.upper()
        args = ("-F", "16" if filesystem == "fat16" else "32", "-S", str(sector))
        if filesystem == "fat16":
            cluster = 1
            while (count + cluster - 1) // cluster > 65520:
                cluster *= 2
            args += ("-s", str(cluster))
    elif filesystem == "udf":
        args = ("--media-type=hd", "--udfrev=0x0201", f"--blocksize={sector}", "--bootarea=erase")
    linux = filesystem.startswith("ext")
    partition = Partition(start, count, "linux" if linux else "basic",
                          0x83 if linux else 0x0e if filesystem == "fat16" else 0x0c if filesystem == "vfat" else 0x07)
    return FormatPlan(filesystem, label, table, size, sector, partition, args)


def require_formatter(plan):
    tool = FS_TOOLS[plan.filesystem]
    if not shutil.which(tool):
        package = " (package udftools)" if plan.filesystem == "udf" else ""
        raise RufusError(f"Install {tool}{package} before formatting this filesystem.")


def volume_command(plan, path):
    """Only call with a newly allocated, private regular volume file."""
    fs, label = plan.filesystem, plan.label
    if fs in ("vfat", "fat16"):
        args = (*plan.args, "-h", str(plan.partition.start), "-n", label)
    elif fs == "udf":
        # UDF's logical label is longer than its ancillary Volume Identifier.
        vid_limit = 30 if all(ord(c) <= 255 for c in label) else 15
        args = ("--utf8", *plan.args, f"--lvid={label}", f"--vid={label[:vid_limit]}")
    elif fs == "ntfs":
        args = ("-F", "-Q", "-s", str(plan.sector), "-p", str(plan.partition.start), "-L", label)
    elif fs == "exfat":
        args = ("-L", label)  # Older exfatprogs cannot override regular-file sector geometry.
    else:
        args = ("-F", "-b", "4096", "-L", label)
    return [FS_TOOLS[fs], *args, str(path)]


def create_formatted_image(output, size, filesystem="vfat", label="RUFUS", table="gpt", sector=512,
                           cancel=None, progress=None):
    plan = plan_format(size, filesystem, label, table, sector)
    if filesystem == "exfat" and sector != 512:
        raise RufusError("exFAT file images currently require 512-byte sectors; physical drives use their reported geometry.")
    require_formatter(plan)
    if os.path.lexists(output):
        raise RufusError("Image destination already exists. Select a new filename.")
    check_cancel(cancel)
    with tempfile.TemporaryDirectory(prefix="rufus-format-") as directory:
        volume = Path(directory) / "volume.img"
        amount = plan.partition.sectors * sector
        with volume.open("xb") as stream:
            stream.truncate(amount)
        emit(progress, "Creating data filesystem")
        run_tool(volume_command(plan, volume), cancel)
        with new_output(output) as target, volume.open("rb") as source:
            os.ftruncate(target, size)
            write_layout(target, size, [plan.partition], table, sector=sector)
            copy_sparse(source.fileno(), target, plan.partition.start * sector, amount, cancel, progress,
                        stage="Assembling data image")
            os.fsync(target)
            digest = checksum(output, cancel, progress)
            emit(progress, "Complete", size, size)
    return digest
