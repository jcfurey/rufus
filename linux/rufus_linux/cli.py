"""Command-line interface. Run with no arguments to open the GTK application."""

import argparse
from dataclasses import asdict
import json
import signal
import sys
import threading
import time

from . import __version__
from .core import Cancelled, RufusError, checksum, human_size, write_to_file
from .devices import (discover_devices, format_device, windows_installer,
                      write_to_device)


def parser():
    root = argparse.ArgumentParser(description="Rufus for Linux — native bootable USB utility")
    root.add_argument("--version", action="version", version=f"Rufus for Linux {__version__}")
    commands = root.add_subparsers(dest="command")
    gui = commands.add_parser("gui", help="open the GTK 4 desktop application")
    gui.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    devices = commands.add_parser("devices", help="list USB/removable drives; never lists internal disks")
    devices.add_argument("--json", action="store_true")
    digest = commands.add_parser("checksum", help="compute a file's SHA-256")
    digest.add_argument("image")
    write = commands.add_parser("write", help="write a raw/hybrid ISO, IMG, gzip, xz, or bzip2 image")
    write.add_argument("image")
    destination = write.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="whole USB drive, e.g. /dev/sdb")
    destination.add_argument("--output", help="create a NEW image file for testing without a USB drive")
    write.add_argument("--confirm", help="repeat the exact device path to authorize erasing it")
    write.add_argument("--sha256", default="", help="expected SHA-256 of the source/downloaded file")
    write.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    windows = commands.add_parser("windows", help="create a GPT/FAT32 UEFI Windows installer from an ISO")
    windows.add_argument("image")
    windows.add_argument("--device", required=True)
    windows.add_argument("--confirm", required=True)
    windows.add_argument("--sha256", default="")
    windows.add_argument("--no-verify", action="store_true")
    fmt = commands.add_parser("format", help="erase and format a USB drive with one partition")
    fmt.add_argument("--device", required=True)
    fmt.add_argument("--confirm", required=True)
    fmt.add_argument("--filesystem", choices=("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4"), default="vfat")
    fmt.add_argument("--table", choices=("gpt", "dos"), default="gpt")
    fmt.add_argument("--label", default="RUFUS")
    return root


def select_device(path, confirmation):
    if path != confirmation:
        raise RufusError("Erasing a drive requires --confirm with the same exact path as --device.")
    device = next((d for d in discover_devices() if d.path == path), None)
    if device is None:
        raise RufusError("Select a whole USB/removable drive listed by the devices command.")
    if device.reason:
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
            print(f"{checksum(args.image, cancel, progress)}  {args.image}")
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
            result = windows_installer(args.image, select_device(args.device, args.confirm),
                                       args.sha256, not args.no_verify, cancel, progress)
            print(f"Windows ISO SHA-256: {result}")
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
