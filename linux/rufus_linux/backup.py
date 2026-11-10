"""Read-only drive backup and file-backed backup/restore validation."""

from contextlib import contextmanager
import hashlib
import os

from .core import CHUNK, RufusError, check_cancel, emit, source_file, write_all
from .images import vhd_footer


@contextmanager
def new_output(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    created = os.fstat(fd)
    try:
        yield fd
        os.fsync(fd)
    except BaseException:
        try:
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)


def save_image(source_fd, size, output, image_format="raw", verify=True, cancel=None, progress=None, *, logical_sector=512):
    if image_format not in ("raw", "vhd", "vhdx"):
        raise RufusError("Backup format must be raw, vhd, or vhdx.")
    if size <= 0:
        raise RufusError("Cannot save an empty drive.")
    footer = vhd_footer(size) if image_format == "vhd" else b""
    if image_format == "vhd" and logical_sector != 512:
        raise RufusError("VHD requires 512-byte logical sectors. Use VHDX for a 4 KiB-sector source.")
    if image_format == "vhdx":
        from .vhdx import fixed_layout
        fixed_layout(size, logical_sector)
    check_cancel(cancel)
    with new_output(output) as target:
        payload = 0
        if image_format == "vhdx":
            from .vhdx import write_fixed_header
            payload = write_fixed_header(target, size, logical_sector, cancel, progress)
            os.lseek(target, payload, os.SEEK_SET)
        os.lseek(source_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        done = 0
        while done < size:
            check_cancel(cancel)
            data = os.read(source_fd, min(CHUNK, size - done))
            if not data:
                raise RufusError("Source ended before the entire drive was backed up.")
            write_all(target, data)
            digest.update(data)
            done += len(data)
            emit(progress, "Saving drive image", done, size)
        write_all(target, footer)
        os.fsync(target)
        if verify:
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(target, 0, 0, os.POSIX_FADV_DONTNEED)
            if image_format == "vhdx":
                from .vhdx import VHDXStream
                reader = os.fdopen(os.dup(target), "rb")
                try:
                    view = VHDXStream(reader, os.fstat(target).st_size, cancel, progress, size)
                    if view.size != size or view.logical_sector != logical_sector:
                        raise RufusError("VHDX backup disk geometry verification failed.")
                except BaseException:
                    reader.close()
                    raise
            else:
                reader, view = None, None
                os.lseek(target, 0, os.SEEK_SET)
            actual = hashlib.sha256()
            done = 0
            try:
                while done < size:
                    check_cancel(cancel)
                    data = view.read(min(CHUNK, size - done)) if view else os.read(target, min(CHUNK, size - done))
                    if not data:
                        raise RufusError("Backup verification ended before the full image was read.")
                    actual.update(data)
                    done += len(data)
                    emit(progress, "Verifying saved image", done, size)
                if actual.hexdigest() != digest.hexdigest() or (not view and os.read(target, 512) != footer):
                    raise RufusError("Saved image verification failed.")
            finally:
                if reader:
                    reader.close()
        check_cancel(cancel)
        emit(progress, "Complete", size, size)
    return digest.hexdigest()


def backup_file(source, output, image_format="raw", verify=True, cancel=None, progress=None, *, logical_sector=512):
    """Exercise exactly the backup copier on an ordinary file; requires no root."""
    with source_file(source) as stream:
        before = os.fstat(stream.fileno())
        # Keep a source-change failure inside new_output's cleanup boundary.
        # save_image owns that boundary, so detect mutations from its callback.
        def report(stage, done, total):
            if stage == "Complete":
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise RufusError("Source changed during backup.")
            emit(progress, stage, done, total)
        return save_image(stream.fileno(), before.st_size, output, image_format, verify, cancel, report,
                          logical_sector=logical_sector)
