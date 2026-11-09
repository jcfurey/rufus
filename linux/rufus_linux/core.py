"""Image operations shared by the GTK frontend and CLI. No elevated process."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

CHUNK = 4 * 1024 * 1024
FAT_MAX_FILE = 2**32 - 1
HASH_ALGORITHMS = ("md5", "sha1", "sha256", "sha512")


class RufusError(Exception):
    """An actionable operation failure."""


class Cancelled(RufusError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled. A partially written drive must be rewritten before use.")


def emit(progress, stage, done=0, total=0):
    if progress:
        progress(stage, done, total)


def human_size(size):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024


def expected_sha256(value):
    value = value.strip().lower()
    if value and not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RufusError("Expected SHA-256 must contain exactly 64 hexadecimal characters.")
    return value


@contextmanager
def source_file(path):
    # O_NONBLOCK also prevents a mistakenly selected FIFO from hanging the UI.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise RufusError("Select a nonempty, regular image file.")
        yield stream


def digest_stream(stream, size, cancel=None, progress=None, stage="Checking image"):
    digest = hashlib.sha256()
    done = 0
    while True:
        check_cancel(cancel)
        chunk = stream.read(CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        done += len(chunk)
        emit(progress, stage, done, size)
    return digest.hexdigest()


def checksum(path, cancel=None, progress=None):
    return checksums(path, ("sha256",), cancel, progress)["sha256"]


def checksums(path, algorithms=HASH_ALGORITHMS, cancel=None, progress=None):
    algorithms = tuple(dict.fromkeys(algorithms))
    if not algorithms or any(name not in HASH_ALGORITHMS for name in algorithms):
        raise RufusError("Supported checksums: md5, sha1, sha256, sha512.")
    digests = {name: hashlib.new(name, usedforsecurity=False) for name in algorithms}
    with source_file(path) as stream:
        original = os.fstat(stream.fileno())
        done = 0
        while True:
            check_cancel(cancel)
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            for digest in digests.values():
                digest.update(chunk)
            done += len(chunk)
            emit(progress, "Calculating checksums", done, original.st_size)
        current = os.fstat(stream.fileno())
        if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                original.st_size, original.st_mtime_ns, original.st_ctime_ns):
            raise RufusError("Source changed while calculating checksums.")
    return {name: digest.hexdigest() for name, digest in digests.items()}


@dataclass(frozen=True)
class PreparedImage:
    stream: object
    size: int
    sha256: str
    is_iso: bool
    hybrid: bool


def image_filesystems(header):
    identifiers = {header[sector * 2048 + 1:sector * 2048 + 6] for sector in range(16, 32)}
    return b"CD001" in identifiers, bool(identifiers & {b"NSR02", b"NSR03"})


@contextmanager
def prepare_image(path, expected="", cancel=None, progress=None):
    """Validate and size the *entire* decompressed image before opening a target.

    SHA-256 refers to the downloaded file, including its compression wrapper.
    Compressed data is staged on disk, keeping memory use bounded and ensuring a
    bad gzip trailer or oversized image cannot destroy a drive before detection.
    """
    expected = expected_sha256(expected)
    from .images import payload_stream

    with source_file(path) as source:
        original = os.fstat(source.fileno())
        file_hash = digest_stream(source, original.st_size, cancel, progress)
        if expected and file_hash != expected:
            raise RufusError(f"SHA-256 mismatch: expected {expected}, got {file_hash}.")
        with payload_stream(source, original.st_size, str(path), cancel, progress) as (stream, size, transformed):
            if transformed:
                stream.seek(0)
                image_hash = digest_stream(stream, size, cancel, progress)
            else:
                image_hash = file_hash
            if not size:
                raise RufusError("The decompressed image is empty.")
            stream.seek(0)
            header = stream.read(65536)
            iso9660, udf = image_filesystems(header)
            is_iso = iso9660 or udf
            hybrid = (header[510:512] == b"\x55\xaa" and
                      any(header[446 + 16 * i + 4] for i in range(4)))
            stream.seek(0)
            current = os.fstat(source.fileno())
            if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                    original.st_size, original.st_mtime_ns, original.st_ctime_ns):
                raise RufusError("The source image changed during preflight. Wait for the download to finish.")
            yield PreparedImage(stream, size, image_hash, is_iso, hybrid)
            current = os.fstat(source.fileno())
            if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
                    original.st_size, original.st_mtime_ns, original.st_ctime_ns):
                raise RufusError("The source image changed during the operation. Rewrite the target.")


def write_all(fd, data):
    data = memoryview(data)
    while data:
        written = os.write(fd, data)
        if written <= 0:
            raise RufusError("The destination stopped accepting data.")
        data = data[written:]


def write_at(fd, data, offset):
    data = memoryview(data)
    while data:
        written = os.pwrite(fd, data, offset)
        if written <= 0:
            raise RufusError("The destination stopped accepting image metadata.")
        offset += written
        data = data[written:]


def write_image(image, fd, capacity, verify=True, cancel=None, progress=None, block=False):
    if image.size > capacity:
        raise RufusError(f"Image needs {human_size(image.size)}; target has {human_size(capacity)}.")
    if image.is_iso and not image.hybrid:
        raise RufusError("This ISO has no disk partition table and cannot boot in DD mode. "
                         "For Windows installation media, choose Windows installer (UEFI).")
    check_cancel(cancel)
    if block and capacity - image.size >= 1024 * 1024:
        # Remove stale backup GPT/filesystem signatures at the end of a larger disk.
        os.lseek(fd, capacity - 1024 * 1024, os.SEEK_SET)
        write_all(fd, b"\0" * (1024 * 1024))
    os.lseek(fd, 0, os.SEEK_SET)
    image.stream.seek(0)
    done = 0
    written_hash = hashlib.sha256()
    while True:
        check_cancel(cancel)
        chunk = image.stream.read(CHUNK)
        if not chunk:
            break
        if done + len(chunk) > capacity:
            raise RufusError("Source grew beyond the target capacity.")
        write_all(fd, chunk)
        written_hash.update(chunk)
        done += len(chunk)
        emit(progress, "Writing image", done, image.size)
    emit(progress, "Flushing writes", done, image.size)
    os.fsync(fd)
    if done != image.size or written_hash.hexdigest() != image.sha256:
        raise RufusError("Source image changed while writing. Rewrite the target.")
    if verify:
        check_cancel(cancel)
        if hasattr(os, "posix_fadvise"):
            os.posix_fadvise(fd, 0, done, os.POSIX_FADV_DONTNEED)
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        verified = 0
        while verified < image.size:
            check_cancel(cancel)
            chunk = os.read(fd, min(CHUNK, image.size - verified))
            if not chunk:
                raise RufusError("Read-back ended before the image was fully verified.")
            digest.update(chunk)
            verified += len(chunk)
            emit(progress, "Verifying written image", verified, image.size)
        if digest.hexdigest() != image.sha256:
            raise RufusError("Read-back verification failed. The drive may be faulty.")
    emit(progress, "Complete", image.size, image.size)
    return image.sha256


def write_to_file(source, output, expected="", verify=True, cancel=None, progress=None):
    """Use the same image writer on a new regular file; never overwrite a file."""
    fd = None
    try:
        with prepare_image(source, expected, cancel, progress) as image:
            fd = os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            result = write_image(image, fd, image.size, verify, cancel, progress)
        return result
    except BaseException:
        if fd is not None:
            created = os.fstat(fd)
            try:
                current = os.stat(output, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(output)
            except FileNotFoundError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def run_tool(args, cancel=None, *, stdin=None, stdout=None):
    """Bounded output, cancellation, and no shell interpolation."""
    with tempfile.TemporaryFile() as log:
        with subprocess.Popen(args, stdin=stdin, stdout=log if stdout is None else stdout, stderr=log) as process:
            try:
                while True:
                    check_cancel(cancel)
                    try:
                        status = process.wait(timeout=0.15)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if status:
            length = log.tell()
            log.seek(max(0, length - 4096))
            raise RufusError(f"{Path(args[0]).name} failed: " + log.read().decode(errors="replace"))


def validate_windows_tree(root, require_uefi=True):
    """Reject links, special files, FAT name collisions, and non-Windows ISOs."""
    files = []
    names = {}
    for directory, dirs, children in os.walk(root, followlinks=False):
        for name in dirs + children:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise RufusError(f"ISO contains a link or special file: {path.relative_to(root)}")
            relative = path.relative_to(root).as_posix()
            if (any(ord(c) < 32 or c in '<>:"\\|?*' for c in name) or
                    name.endswith((".", " "))):
                raise RufusError(f"ISO name is incompatible with FAT32: {relative}")
            key = relative.casefold()
            if key in names:
                raise RufusError(f"ISO has conflicting FAT32 names: {relative}")
            names[key] = path
            if stat.S_ISREG(info.st_mode):
                files.append((path, relative, info.st_size))
    if require_uefi and not any(f"efi/boot/boot{arch}.efi" in names for arch in ("x64", "aa64", "ia32", "arm")):
        raise RufusError("ISO has no UEFI removable-media bootloader in EFI/BOOT.")
    if "sources/boot.wim" not in names:
        raise RufusError("ISO is not Windows installation media (sources/boot.wim is missing).")
    if not require_uefi and not {"bootmgr", "boot/bcd"} <= names.keys():
        raise RufusError("BIOS Windows media requires bootmgr and boot/BCD in the ISO.")
    return files


def stage_windows_iso(image, staging, cancel=None, progress=None, *, require_uefi=True, split_wim=True):
    import shutil

    with source_file(image) as stream:
        iso9660, udf = image_filesystems(stream.read(65536))
        if not (iso9660 or udf):
            raise RufusError("Select an uncompressed Windows installation ISO.")
    emit(progress, "Extracting Windows ISO")
    sevenzip = shutil.which("7zz") or shutil.which("7z")
    if udf or not shutil.which("xorriso"):
        if not sevenzip:
            raise RufusError("This Windows ISO uses UDF and requires the 7zip package for extraction." if udf else
                             "ISO extraction requires xorriso or 7zip.")
        run_tool([sevenzip, "x", "-tudf" if udf else "-tiso", "-y", "-o" + str(staging), "--",
                  str(Path(image).resolve())], cancel)
    else:
        run_tool(["xorriso", "-abort_on", "FAILURE", "-osirrox", "on", "-indev",
                  str(Path(image).resolve()), "-extract", "/", str(staging)], cancel)
    files = validate_windows_tree(staging, require_uefi)
    return ensure_fat32_files(staging, files, cancel, progress, require_uefi=require_uefi) if split_wim else files


def ensure_fat32_files(staging, files, cancel=None, progress=None, *, file_limit=FAT_MAX_FILE, part_size_mb=3800, require_uefi=True):
    """Split WIM resources before formatting; smaller limits allow bounded tests."""
    import shutil

    if file_limit <= 0 or part_size_mb <= 0 or part_size_mb * 1024**2 >= file_limit:
        raise RufusError("WIM split size must be positive and smaller than the file limit.")
    for path, relative, size in files:
        if size <= file_limit:
            continue
        if relative.casefold() != "sources/install.wim":
            raise RufusError(f"{relative} exceeds FAT32's 4 GiB file limit.")
        if not shutil.which("wimlib-imagex"):
            raise RufusError("This ISO needs WIM splitting: sudo apt install wimtools")
        if any(p.name.casefold().startswith("install") and p.suffix.casefold() == ".swm"
               for p in path.parent.iterdir()):
            raise RufusError("ISO already contains split WIM files alongside install.wim.")
        emit(progress, "Splitting install.wim for FAT32")
        run_tool(["wimlib-imagex", "split", str(path), str(path.with_name("install.swm")),
                  str(part_size_mb), "--check"], cancel)
        path.unlink()
    files = validate_windows_tree(staging, require_uefi)
    if any(size > file_limit for _, _, size in files):
        raise RufusError("A staged file still exceeds FAT32's file size limit.")
    return files


def copy_windows_tree(files, destination, verify=True, cancel=None, progress=None):
    total = sum(size for _, _, size in files)
    done = 0
    digests = []
    for source, relative, size in files:
        check_cancel(cancel)
        target = Path(destination) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with source.open("rb") as src, target.open("xb") as dst:
            while chunk := src.read(CHUNK):
                check_cancel(cancel)
                dst.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                emit(progress, "Copying Windows installer", done, total)
            dst.flush()
            os.fsync(dst.fileno())
        digests.append((target, size, digest.hexdigest()))
    if verify:
        done = 0
        for target, size, expected in digests:
            check_cancel(cancel)
            with target.open("rb") as stream:
                if hasattr(os, "posix_fadvise"):
                    os.posix_fadvise(stream.fileno(), 0, size, os.POSIX_FADV_DONTNEED)
                actual = digest_stream(stream, size, cancel)
            if actual != expected:
                raise RufusError(f"Read-back verification failed for {target.name}.")
            done += size
            emit(progress, "Verifying Windows installer", done, total)
