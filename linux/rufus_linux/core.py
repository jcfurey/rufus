"""Image operations shared by the GTK frontend and CLI. No elevated process."""

from contextlib import contextmanager
from dataclasses import dataclass
import bz2
import gzip
import hashlib
import lzma
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

CHUNK = 4 * 1024 * 1024
FAT_MAX_FILE = 2**32 - 1


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
    with source_file(path) as stream:
        return digest_stream(stream, os.fstat(stream.fileno()).st_size, cancel, progress)


@dataclass(frozen=True)
class PreparedImage:
    stream: object
    size: int
    sha256: str
    is_iso: bool
    hybrid: bool


@contextmanager
def prepare_image(path, expected="", cancel=None, progress=None):
    """Validate and size the *entire* decompressed image before opening a target.

    SHA-256 refers to the downloaded file, including its compression wrapper.
    Compressed data is staged on disk, keeping memory use bounded and ensuring a
    bad gzip trailer or oversized image cannot destroy a drive before detection.
    """
    expected = expected_sha256(expected)
    with source_file(path) as source:
        original = os.fstat(source.fileno())
        file_hash = digest_stream(source, original.st_size, cancel, progress)
        if expected and file_hash != expected:
            raise RufusError(f"SHA-256 mismatch: expected {expected}, got {file_hash}.")
        source.seek(0)
        magic = source.read(6)
        source.seek(0)
        decoder = (gzip.open if magic.startswith(b"\x1f\x8b") else
                   lzma.LZMAFile if magic == b"\xfd7zXZ\x00" else
                   bz2.BZ2File if magic.startswith(b"BZh") else None)
        with tempfile.TemporaryFile() as staged:
            if decoder:
                emit(progress, "Decompressing image")
                try:
                    with decoder(source, "rb") as uncompressed:
                        while True:
                            check_cancel(cancel)
                            chunk = uncompressed.read(CHUNK)
                            if not chunk:
                                break
                            staged.write(chunk)
                            emit(progress, "Decompressing image", source.tell(), original.st_size)
                except (OSError, EOFError, lzma.LZMAError) as exc:
                    raise RufusError(f"Cannot decompress this image: {exc}") from exc
                stream = staged
                size = stream.tell()
                stream.seek(0)
                image_hash = digest_stream(stream, size, cancel, progress)
            else:
                stream, size, image_hash = source, original.st_size, file_hash
            if not size:
                raise RufusError("The decompressed image is empty.")
            stream.seek(0)
            header = stream.read(65536)
            is_iso = header[32769:32774] == b"CD001"
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


def run_tool(args, cancel=None):
    """Bounded output, cancellation, and no shell interpolation."""
    with tempfile.TemporaryFile() as log:
        with subprocess.Popen(args, stdout=log, stderr=log) as process:
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


def validate_windows_tree(root):
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
    if not any(f"efi/boot/boot{arch}.efi" in names for arch in ("x64", "aa64", "ia32", "arm")):
        raise RufusError("ISO has no UEFI removable-media bootloader in EFI/BOOT.")
    if "sources/boot.wim" not in names:
        raise RufusError("ISO is not Windows installation media (sources/boot.wim is missing).")
    return files


def stage_windows_iso(image, staging, cancel=None, progress=None):
    import shutil

    if not shutil.which("xorriso"):
        raise RufusError("Windows ISO extraction requires xorriso: sudo apt install xorriso")
    with source_file(image) as stream:
        stream.seek(32769)
        if stream.read(5) != b"CD001":
            raise RufusError("Select an uncompressed Windows installation ISO.")
    emit(progress, "Extracting Windows ISO")
    run_tool(["xorriso", "-abort_on", "FAILURE", "-osirrox", "on", "-indev",
              str(Path(image).resolve()), "-extract", "/", str(staging)], cancel)
    files = validate_windows_tree(staging)
    for path, relative, size in files:
        if size <= FAT_MAX_FILE:
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
                  "3800", "--check"], cancel)
        path.unlink()
    files = validate_windows_tree(staging)
    if any(size > FAT_MAX_FILE for _, _, size in files):
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
