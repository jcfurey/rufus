"""Command-line interface. Run with no arguments to open the GTK application."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import signal
import sys
import threading
import time

from . import __version__
from .core import Cancelled, HASH_ALGORITHMS, RufusError, checksums, human_size, write_to_file
from .devices import (backup_device, discover_devices, format_device, freedos_device, live_device, windows_installer,
                      write_to_device)
from .windows import WindowsOptions, answer_file, prepare_windows


def windows_arguments(command, *, installer=True):
    command.set_defaults(bypass_hardware=False)
    if installer:
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
    write = commands.add_parser("write", help="restore disk images, compressed images, ZIP, VHD, or VHDX")
    write.add_argument("image")
    destination = write.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="whole USB drive, e.g. /dev/sdb")
    destination.add_argument("--output", help="create a NEW image file for testing without a USB drive")
    write.add_argument("--confirm", help="repeat the exact device path to authorize erasing it")
    write.add_argument("--sha256", default="", help="expected SHA-256 of the source/downloaded file")
    write.add_argument("--no-verify", action="store_true", help="skip read-back verification")
    live = commands.add_parser("linux", help="create hybrid or extracted FAT32 Linux live media, including ARM UEFI")
    live.add_argument("image")
    destination = live.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="USB drive to erase")
    destination.add_argument("--output", help="NEW bootable image file; no root or drive access")
    live.add_argument("--confirm", help="repeat the USB drive path to authorize erasing it")
    live.add_argument("--mode", choices=("hybrid", "extract"), default="hybrid")
    live.add_argument("--architecture", choices=("auto", "x86", "x86_64", "arm64", "arm"), default="auto",
                      help="extraction only; auto detects the Linux kernel architecture")
    live.add_argument("--bootloader", choices=("auto", "grub", "syslinux"), default="auto",
                      help="extraction only; BIOS loader (auto follows the source menu); UEFI uses GRUB")
    live.add_argument("--firmware", choices=("bios", "uefi", "both"),
                      help="extraction only; defaults to UEFI on ARM and BIOS+UEFI on x86-64")
    live.add_argument("--size-mib", type=int, help="extracted output image size; default fits the live files")
    live.add_argument("--persistence-mib", type=int,
                      help="ext4 size in MiB (minimum 64); defaults to 1024 for hybrid, 0 for extraction")
    live.add_argument("--casper-label", choices=("writable", "casper-rw"), default="writable",
                      help="use casper-rw for older Ubuntu/Casper images; Debian is detected automatically")
    live.add_argument("--sha256", default="")
    live.add_argument("--no-verify", action="store_true", help="skip USB read-back verification")
    from .jetson import MODELS
    jetson = commands.add_parser("jetson", help="prepare NVIDIA installer USB/legacy SD media or open recovery setup")
    jetson.add_argument("image", nargs="?")
    jetson.add_argument("--model", choices=tuple(MODELS), required=True)
    jetson.add_argument("--storage", choices=("nvme", "sd", "emmc", "usb"), required=True,
                        help="installation target attached to the Jetson; distinct from the host's installer USB")
    jetson.add_argument("--method", choices=("installer", "sd", "direct", "recovery"), default="installer")
    jetson.add_argument("--plan", action="store_true", help="show model/storage instructions without writing anything")
    jetson.add_argument("--launch-sdkmanager", action="store_true", help="open NVIDIA's recovery installation wizard")
    destination = jetson.add_mutually_exclusive_group()
    destination.add_argument("--device", help="host's removable installer USB or SD card to erase")
    destination.add_argument("--output", help="NEW image file, for testing with no physical drive")
    jetson.add_argument("--confirm", help="repeat the host removable drive path to authorize erasing it")
    jetson.add_argument("--sha256", default="")
    jetson.add_argument("--no-verify", action="store_true")
    commands.add_parser("jetson-devices", help="inspect NVIDIA USB devices and recovery candidates; read-only")
    bsp = commands.add_parser("jetson-bsp", help="inspect a prepared Linux_for_Tegra BSP without running its scripts")
    bsp.add_argument("directory")
    flash = commands.add_parser("jetson-flash", help="review or execute a direct NVIDIA BSP recovery job; no SDK Manager")
    flash.add_argument("--bsp", required=True, help="prepared Linux_for_Tegra directory")
    flash.add_argument("--model", choices=tuple(MODELS), required=True)
    flash.add_argument("--storage", choices=("nvme", "sd", "emmc", "usb"), required=True)
    flash.add_argument("--board", required=True, help="exact BSP profile, without .conf; match module and carrier")
    flash.add_argument("--usb-port", default="", help="recovery USB port from jetson-devices, e.g. 1-2.4")
    flash.add_argument("--external-device", default="", help="device name on the Jetson, e.g. nvme0n1p1; never a host /dev path")
    flash.add_argument("--layout", default="", help="external partition XML path relative to the BSP")
    flash.add_argument("--skip-firmware", action="store_true", help="external storage only; require already compatible firmware")
    flash.add_argument("--action", choices=("flash", "probe"), default="flash",
                       help="probe uses flash.sh --read-info when supported; still requires authorization")
    flash.add_argument("--execute", action="store_true", help="execute only after reviewing the default JSON plan")
    flash.add_argument("--confirm", help="exact USB-port and plan token from the reviewed JSON")
    flash.add_argument("--log", help="NEW log filename; required with --execute")
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
    editions = commands.add_parser("windows-images", help="list Windows editions/indexes from an installation ISO")
    editions.add_argument("image")
    togo = commands.add_parser("windows-to-go", help="deploy portable Windows and configure boot in a private WinPE VM")
    togo.add_argument("image")
    destination = togo.add_mutually_exclusive_group(required=True)
    destination.add_argument("--device", help="whole removable drive to erase after successful preparation")
    destination.add_argument("--output", help="NEW portable Windows image; no root or physical drive required")
    togo.add_argument("--confirm", help="repeat the exact removable drive path to authorize erasing it")
    togo.add_argument("--index", type=int, required=True, help="edition index from windows-images")
    togo.add_argument("--size-gib", type=int, help="output image capacity; default 64 GiB; physical drives use their whole capacity")
    togo.add_argument("--firmware", choices=("uefi", "bios", "both"), default="uefi")
    togo.add_argument("--vm-timeout", type=int, default=900, help="private WinPE VM timeout in seconds (30-7200)")
    togo.add_argument("--log", help="NEW log filename; defaults beside the output image or in the working directory")
    togo.add_argument("--sha256", default="")
    togo.add_argument("--no-verify", action="store_true", help="skip USB read-back verification")
    windows_arguments(togo, installer=False)
    prepare = commands.add_parser("prepare-windows", help="extract/customize Windows ISO into a NEW directory; no drive needed")
    prepare.add_argument("image")
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--sha256", default="")
    windows_arguments(prepare)
    answer = commands.add_parser("windows-config", help="generate a Windows answer file without touching a drive")
    answer.add_argument("--output", required=True)
    windows_arguments(answer)
    backup = commands.add_parser("backup", help="save and verify an entire USB drive as raw IMG, fixed VHD, or fixed VHDX")
    sources = backup.add_mutually_exclusive_group(required=True)
    sources.add_argument("--device", help="USB/removable source drive; opened read-only")
    sources.add_argument("--source", help="regular file source for backup/restore testing")
    backup.add_argument("--output", required=True, help="NEW destination filename; existing files are never overwritten")
    backup.add_argument("--format", choices=("raw", "vhd", "vhdx"), default="raw")
    backup.add_argument("--sector-size", type=int, choices=(512, 4096),
                        help="VHDX --source file's logical sector size (default 512); drives use their reported size")
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
        if stage != last[1] or now - last[0] >= 0.25 or (total and done == total):
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
        elif args.command == "linux":
            from .live import create_live_image
            persistence = args.persistence_mib if args.persistence_mib is not None else (1024 if args.mode == "hybrid" else 0)
            size = persistence * 1024**2
            if args.mode == "hybrid" and (args.architecture != "auto" or args.bootloader != "auto" or args.firmware or args.size_mib is not None):
                raise RufusError("Architecture, bootloader, firmware, and image size options apply to --mode extract.")
            if args.output:
                if args.mode == "extract":
                    from .linux_iso import create_extracted_image
                    capacity = args.size_mib * 1024**2 if args.size_mib is not None else None
                    result = create_extracted_image(args.image, args.output, capacity, size, args.casper_label,
                                                    args.architecture, args.firmware, args.sha256, cancel, progress,
                                                    bootloader=args.bootloader)
                else:
                    result = create_live_image(args.image, args.output, size, args.casper_label,
                                               expected=args.sha256, cancel=cancel, progress=progress)
                print(json.dumps(result, indent=2))
            else:
                if args.size_mib is not None:
                    raise RufusError("--size-mib applies to output files; USB media uses the whole drive.")
                result = live_device(args.image, select_device(args.device, args.confirm), size,
                                     args.casper_label, args.sha256, not args.no_verify, cancel, progress,
                                     mode=args.mode, architecture=args.architecture, firmware=args.firmware,
                                     bootloader=args.bootloader)
                print(f"Linux ISO SHA-256: {result}")
        elif args.command == "jetson-devices":
            from .jetson import recovery_devices
            from .jetson_flash import connected_devices
            known = {device["usb_port"]: device for device in connected_devices()}
            print(json.dumps([{**device, **known.get(device["usb_port"], {})} for device in recovery_devices()], indent=2))
        elif args.command == "jetson-bsp":
            from .jetson_flash import inspect_bsp
            print(json.dumps(inspect_bsp(args.directory), indent=2))
        elif args.command == "jetson-flash":
            from .jetson_flash import FlashRequest, execute_plan, make_plan
            plan = make_plan(FlashRequest(args.bsp, args.model, args.storage, args.board, args.usb_port,
                                          args.external_device, args.layout, not args.skip_firmware, args.action))
            if not args.execute:
                if args.confirm or args.log:
                    raise RufusError("--confirm and --log require --execute; omit all three to review the plan.")
                print(json.dumps(plan, indent=2))
            else:
                if not args.confirm or not args.log:
                    raise RufusError("Execution requires --confirm with the reviewed plan token and --log with a NEW filename.")
                result = execute_plan(plan, args.confirm, Path(args.log), cancel, progress)
                print(json.dumps(result, indent=2))
        elif args.command == "jetson":
            from .jetson import installation_plan, launch_sdkmanager, write_media
            plan = installation_plan(args.model, args.storage, args.method)
            if args.plan:
                print(json.dumps(plan, indent=2))
            elif args.method == "direct":
                raise RufusError("Use jetson-flash --bsp ... --board ... to review a direct BSP recovery plan.")
            elif args.method == "recovery":
                if args.image or args.device or args.output:
                    raise RufusError("Recovery flashing uses NVIDIA SDK Manager, without a host disk-image destination.")
                print(json.dumps(launch_sdkmanager() if args.launch_sdkmanager else plan, indent=2))
            else:
                if args.launch_sdkmanager or not args.image or not (args.device or args.output):
                    raise RufusError("Select a source image and a destination for Jetson media, or use --plan.")
                device = select_device(args.device, args.confirm) if args.device else None
                result = write_media(args.image, model=args.model, storage=args.storage, method=args.method,
                                     output=args.output, device=device, expected=args.sha256,
                                     verify=not args.no_verify, cancel=cancel, progress=progress)
                print(json.dumps(result, indent=2))
        elif args.command == "windows-images":
            from .windows_togo import inspect_iso
            print(json.dumps(inspect_iso(args.image, cancel, progress), indent=2))
        elif args.command == "windows-to-go":
            from .windows_togo import GIB, create_windows_to_go
            if args.output:
                result = create_windows_to_go(args.image, args.output, index=args.index,
                    size=(args.size_gib if args.size_gib is not None else 64) * GIB, firmware=args.firmware,
                    options=windows_options(args), expected=args.sha256, log=args.log,
                    timeout=args.vm_timeout, cancel=cancel, progress=progress)
            else:
                if args.size_gib is not None:
                    raise RufusError("--size-gib applies to output files; a removable drive uses its whole capacity.")
                from .devices import windows_to_go_device
                log = args.log or str(Path.cwd() / f"rufus-windows-to-go-{time.time_ns()}.log")
                result = windows_to_go_device(args.image, select_device(args.device, args.confirm), index=args.index,
                    firmware=args.firmware, options=windows_options(args), expected=args.sha256,
                    verify=not args.no_verify, log=log, timeout=args.vm_timeout, cancel=cancel, progress=progress)
            print(json.dumps(result, indent=2))
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
            if args.sector_size is not None and (not args.source or args.format != "vhdx"):
                raise RufusError("--sector-size applies to VHDX file sources; physical drives use their reported sector size.")
            if args.source:
                from .backup import backup_file
                result = backup_file(args.source, args.output, args.format, not args.no_verify, cancel, progress,
                                     logical_sector=args.sector_size or 512)
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
