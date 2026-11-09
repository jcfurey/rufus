"""Small, typed Linux wimlib binding for unmounted NTFS image files."""

import ctypes
import os
from pathlib import Path
import stat

from .core import RufusError, check_cancel, emit


def apply_ntfs_image(wim_path, index, target, cancel=None, progress=None):
    target = Path(target)
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RufusError("Native WIM extraction requires a regular NTFS image file, not a device or link.")
    with target.open("rb") as stream:
        if stream.read(11)[3:11] != b"NTFS    ":
            raise RufusError("WIM destination is not a formatted NTFS image.")
    check_cancel(cancel)
    try:
        library = ctypes.CDLL("libwim.so.15")
    except OSError as exc:
        raise RufusError("NTFS media creation requires the system wimtools/libwim package.") from exc
    pointer = ctypes.c_void_p
    callback_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, pointer, pointer)
    library.wimlib_open_wim.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(pointer)]
    library.wimlib_open_wim.restype = ctypes.c_int
    library.wimlib_extract_image.argtypes = [pointer, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    library.wimlib_extract_image.restype = ctypes.c_int
    library.wimlib_free.argtypes = [pointer]
    library.wimlib_free.restype = None
    library.wimlib_get_error_string.argtypes = [ctypes.c_int]
    library.wimlib_get_error_string.restype = ctypes.c_char_p
    library.wimlib_register_progress_function.argtypes = [pointer, callback_type, pointer]
    library.wimlib_register_progress_function.restype = None
    errors = []

    @callback_type
    def report(_message, _info, _context):
        try:
            check_cancel(cancel)
            emit(progress, "Populating NTFS image")
            return 0
        except BaseException as exc:
            if not errors:
                errors.append(exc)
            return 1

    def checked(status):
        if errors:
            raise errors[0]
        check_cancel(cancel)
        if status:
            message = library.wimlib_get_error_string(status).decode(errors="replace")
            raise RufusError("wimlib NTFS extraction: " + message)

    handle = pointer()
    status = library.wimlib_open_wim(os.fsencode(wim_path), 0, ctypes.byref(handle))
    if status:
        checked(status)
    try:
        checked(0)
        library.wimlib_register_progress_function(handle, report, None)
        # WIMLIB_EXTRACT_FLAG_NTFS: preserve native Windows/NTFS metadata.
        checked(library.wimlib_extract_image(handle, index, os.fsencode(target), 0x01))
    finally:
        library.wimlib_free(handle)
