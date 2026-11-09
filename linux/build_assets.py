#!/usr/bin/python3
"""Copy upstream resources into the standalone application; no downloads."""

from pathlib import Path
import sys

from rufus_linux.assets import BOOT_ARRAYS, freedos_files, source_asset

target = Path(sys.argv[1]) / "rufus_linux/assets"
target.mkdir(parents=True, exist_ok=True)
for name in (*BOOT_ARRAYS, "uefi-ntfs.img"):
    (target / name).write_bytes(source_asset(name))
for name, data in freedos_files().items():
    (target / "freedos").mkdir(exist_ok=True)
    (target / "freedos" / name).write_bytes(data)
(target / "freedos/readme.txt").write_bytes(source_asset("freedos/readme.txt"))
