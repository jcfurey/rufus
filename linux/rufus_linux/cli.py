"""Command-line interface. Run with no arguments to open the GTK application."""

import argparse
from dataclasses import asdict
import json
import signal
import sys
import threading
import time

from . import __version__
from .core import Cancelled, HASH_ALGORITHMS, RufusError, checksums, human_size, write_to_file
from .devices import (backup_device, discover_devices, format_device, freedos_device, windows_installer,
                      write_to_device)
from .windows import WindowsOptions, answer_file, prepare_windows


def windows_arguments(command):
    command.add_argument("--bypass-hardware-checks", dest="bypass_hardware", action="store_true",
                         help="add Rufus's WinPE TPM/Secure Boot/RAM check bypass")
    command.add_argument("--offline-account", action="store_true", help="request offline Windows account setup")
    command.add_argument("--local-account", default="", help="create a local administrator and require a password change")
    command.add_argument("--privacy", action="store_true", help="disable express data-collection choices during setup")
    command.add_argument("--disable-device-encryption", dest="disable_encryption", action="store_true")
    command.add_argument("--locale", default="", help="Windows locale, e.g. en-US")
    command.add_argument("--timezone", default="", help="Windows time zone ID, e.g. Central Standard Time")
    command.add_argument("--architecture", choices=("amd64", "x86", "arm64", "arm"), default="")
    command.add_argument("--boot-index", type=int, default=0, help="override the boot.wim Setup image index")


def windows_options(args):
    return WindowsOptions(**{name: getattr(args, name) for name in WindowsOptions.__dataclass_fields__})


def parser():
    root = argparse.ArgumentParser(description="Rufus for Linux — native bootable USB utility")
    root.add_argument("--version", action="version", version=f"Rufus for Linux {__version__}")
    commands = root.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="open the GTK 4 desktop application")
    gui.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    devices = commands.add_parser("devices", help="list USB/removable drives; never lists internal disks")
    devices.add_argument("--json", action="store_true")
    digest = commands.add_parser("checksum", help="compute MD5, SHA-1, SHA-256, or SHA-512 in one read")
    digest.add_argument("image")
    digest.add_argument("--algorithm", choices=HASH_ALGORITHMS + ("all",), default="sha256")
    digest.add_argument("--all", dest="algorithm", action="store_const", const="all")
    digest.add_argument("--json", action="store_true")
    write = commands.add_parser("write", help="restore disk images, compressed images, ZIP, or VHD")
    write.add_argument("image")
    destination = write.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="whole USB drive, e.g. /dev/sdb")
    destination.add_argument("--output", help="create a NEW image file for testing without a USB drive")
    write.add_argument("--confirm", help="repeat the exact device path to authorize erasing it")
    write.add_argument("--sha256", default="", help="expected SHA-256 of the source/downloaded file")
    write.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    windows = commands.add_parser("windows", help="create BIOS/UEFI Windows media with FAT32 or NTFS")
    windows.add_argument("image")
    destination = windows.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="USB drive to erase")
    destination.add_argument("--output", help="NEW bootable image file, with no root or drive access")
    windows.add_argument("--confirm", help="repeat the USB drive path to authorize erasing it")
    windows.add_argument("--filesystem", choices=("fat32", "ntfs"), default="fat32")
    windows.add_argument("--firmware", choices=("uefi", "bios"), default="uefi")
    windows.add_argument("--size-mib", type=int, help="output image size; default automatically fits the installer")
    windows.add_argument("--sha256", default="")
    windows.add_argument("--no-verify", action="store_true")
    windows_arguments(windows)
    prepare = commands.add_parser("prepare-windows", help="extract/customize Windows ISO into a NEW directory; no drive needed")
    prepare.add_argument("image")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--sha256", default="")
    windows_arguments(prepare)
    answer = commands.add_parser("windows-config", help="generate a Windows answer file without touching a drive")
    answer.add_argument("--output", required=True)
    windows_arguments(answer)
    backup = commands.add_parser("backup", help="save and verify an entire USB drive as raw IMG or fixed VHD")
    sources = backup.add_mutually_exclusive_group(required=True)
    sources.add_argument("--device", help="USB/removable source drive; opened read-only")
    sources.add_argument("--source", help="regular file source for backup/restore testing")
    backup.add_argument("--output", required=True, help="NEW destination filename; existing files are never overwritten")
    backup.add_argument("--format", choices=("raw", "vhd"), default="raw")
    backup.add_argument("--no-verify", action="store_true")
    dos = commands.add_parser("freedos", help="create BIOS-bootable FreeDOS media from bundled Rufus files")
    destination = dos.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", help="NEW bootable image file; no drive or root access needed")
    destination.add_argument("--device", help="USB drive to erase and make bootable")
    dos.add_argument("--confirm", help="repeat the exact USB drive path to authorize erasing it")
    dos.add_argument("--size-mib", type=int, help="image size; default 64 MiB for files or whole USB capacity")
    dos.add_argument("--fat", type=int, choices=(16, 32), default=32)
    dos.add_argument("--label", default="FREEDOS")
    dos.add_argument("--no-verify", action="store_true")
    fmt = commands.add_parser("format", help="erase and format a USB drive with one partition")
    fmt.add_argument("--device", required=True)
    fmt.add_argument("--confirm", required=True)
    fmt.add_argument("--filesystem", choices=("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4"), default="vfat")
    fmt.add_argument("--table", choices=("gpt", "dos"), default="gpt")
    fmt.add_argument("--label", default="RUFUS")
    return root


def select_device(path, confirmation=None, destructive=True):
    if destructive and path != confirmation:
        raise RufusError("Erasing a drive requires --confirm with the same exact path as --device.")
    device = next((d for d in discover_devices() if d.path == path), None)
    if device is None:
        raise RufusError("Select a whole USB/removable drive listed by the devices command.")
    if device.reason and not (not destructive and device.reason == "Drive is read-only."):
        raise RufusError(device.reason)
    return device


def main(argv=None):
    args = parser().parse_args(argv)
    cancel = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: cancel.set())
    last = [0.0, ""]

    def progress(stage, done, total):
        now = time.monotonic()
        if stage != last[1] or now - last[0] >= 0.25 or done == total:
            suffix = f" {100 * done / total:.1f}%" if total else ""
            print(f"{stage}{suffix}", file=sys.stderr)
            last[:] = now, stage

    try:
        if args.command in (None, "gui"):
            try:
                from .gui import run
            except ImportError as exc:
                raise RufusError("The desktop interface needs python3-gi and gir1.2-gtk-4.0. "
                                 "Install them with apt, or use the command-line subcommands.") from exc
            return run(getattr(args, "smoke_test", False))
        if args.command == "devices":
            devices = discover_devices()
            if args.json:
                print(json.dumps([asdict(d) for d in devices], indent=2))
            elif not devices:
                print("No USB/removable drives found. Connect a USB drive and refresh.")
            for device in [] if args.json else devices:
                state = device.reason or "Ready"
                print(f"{device.path}\t{human_size(device.size)}\t{device.model}\t{state}")
        elif args.command == "checksum":
            algorithms = HASH_ALGORITHMS if args.algorithm == "all" else (args.algorithm,)
            result = checksums(args.image, algorithms, cancel, progress)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                for name, digest in result.items():
                    prefix = f"{name.upper()} " if len(result) > 1 else ""
                    print(f"{prefix}{digest}  {args.image}")
        elif args.command == "write":
            if args.output:
                result = write_to_file(args.image, args.output, args.sha256,
                                       not args.no_verify, cancel, progress)
            else:
                device = select_device(args.device, args.confirm)
                result = write_to_device(args.image, device, args.sha256,
                                         not args.no_verify, cancel, progress)
            print(f"Image SHA-256: {result}")
        elif args.command == "windows":
            filesystem = "vfat" if args.filesystem == "fat32" else "ntfs"
            if args.output:
                from .media import create_windows_image
                size = args.size_mib * 1024**2 if args.size_mib is not None else None
                result = create_windows_image(args.image, args.output, size, filesystem, args.firmware,
                                              windows_options(args), args.sha256, cancel, progress)
                print(json.dumps(result, indent=2))
            else:
                if args.size_mib is not None:
                    raise RufusError("--size-mib applies to --output images; USB media uses the drive's capacity.")
                result = windows_installer(args.image, select_device(args.device, args.confirm),
                                           args.sha256, not args.no_verify, cancel, progress, windows_options(args),
                                           filesystem, args.firmware)
                print(f"Windows ISO SHA-256: {result}")
        elif args.command == "prepare-windows":
            result = prepare_windows(args.image, args.output, windows_options(args), args.sha256, cancel, progress)
            print(json.dumps(result, indent=2))
        elif args.command == "windows-config":
            from .backup import new_output
            from .core import write_all
            data = answer_file(windows_options(args))
            with new_output(args.output) as fd:
                write_all(fd, data)
            print(f"Created Windows answer file: {args.output}")
        elif args.command == "backup":
            if args.source:
                from .backup import backup_file
                result = backup_file(args.source, args.output, args.format, not args.no_verify, cancel, progress)
            else:
                result = backup_device(select_device(args.device, destructive=False), args.output,
                                       args.format, not args.no_verify, cancel, progress)
            print(f"Backed-up disk SHA-256: {result}")
        elif args.command == "freedos":
            from .boot import MIB, create_freedos
            if args.output:
                size = (args.size_mib if args.size_mib is not None else 64) * MIB
                result = create_freedos(args.output, size, args.fat, args.label, cancel, progress)
            else:
                size = args.size_mib * MIB if args.size_mib is not None else None
                result = freedos_device(select_device(args.device, args.confirm), args.fat, args.label, size,
                                        not args.no_verify, cancel, progress)
            print(f"FreeDOS image SHA-256: {result}")
        elif args.command == "format":
            format_device(select_device(args.device, args.confirm), args.filesystem,
                          args.label, args.table, cancel, progress)
    except Cancelled as exc:
        print(str(exc), file=sys.stderr)
        return 130
    except (RufusError, OSError, ValueError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def entrypoint():
    sys.exit(main())


if __name__ == "__main__":
    entrypoint()
