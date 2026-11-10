#!/usr/bin/python3
"""Boot a real x86 Linux kernel through Syslinux BIOS and GRUB UEFI USB media.

All disks are NEW files or QEMU snapshots. No root or physical hardware needed.
"""

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, grub, linux_iso
from boot_smoke import firmware_paths
from live_smoke import archive
from uefi_smoke import ovmf_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, default=Path("/usr/bin/busybox"), help="static x86-64 BusyBox")
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if grub.kernel_architecture(args.kernel) != "x86_64":
        raise RuntimeError("Supply an x86-64 Linux kernel for BIOS and UEFI boot tests.")
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        raise RuntimeError("Install qemu-system-x86, seabios, and ovmf.")
    bios, firmware = firmware_paths(qemu)
    ovmf, variables = ovmf_paths(qemu)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    tree = root / "source"
    (tree / "isolinux/menus").mkdir(parents=True)
    (tree / "casper").mkdir()
    shutil.copyfile(args.kernel, tree / "casper/vmlinuz")
    init = b'''#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mount -t proc proc /proc
exec >/dev/ttyS0 2>&1
if [ "$(/bin/busybox cat /early-marker)" = early-initrd-ok ]; then
    echo RUFUS_SYSLINUX_MULTI_INITRD_KERNEL_OK
else
    echo RUFUS_SYSLINUX_MISSING_EARLY_INITRD
fi
/bin/busybox uname -m
/bin/busybox poweroff -f
'''
    early = archive([("early-marker", b"early-initrd-ok\n", stat.S_IFREG | 0o644)])
    (tree / "casper/early.img").write_bytes(gzip.compress(early))
    files = [(name, b"", stat.S_IFDIR | 0o755) for name in ("bin", "dev", "proc")]
    files += [("bin/busybox", args.busybox.read_bytes(), stat.S_IFREG | 0o755), ("init", init, stat.S_IFREG | 0o755)]
    (tree / "casper/initrd").write_bytes(gzip.compress(archive(files)))
    (tree / "isolinux/isolinux.cfg").write_text("SERIAL 0 115200\nDEFAULT live\nTIMEOUT 1\nINCLUDE menus/live.cfg\n")
    (tree / "isolinux/menus/live.cfg").write_text(
        "LABEL live\nMENU LABEL Real Linux kernel validation\nKERNEL /casper/vmlinuz\n"
        "APPEND initrd=/casper/early.img,/casper/initrd boot=casper console=ttyS0 rdinit=/init panic=-1\n")
    iso, image = root / "syslinux-fixture.iso", root / "syslinux-live.img"
    core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(iso), str(tree)])
    result = linux_iso.create_extracted_image(iso, image)
    if result["bios_bootloader"] != "syslinux":
        raise AssertionError("Test did not install the native Syslinux BIOS path.")
    nvram = root / "variables.fd"
    shutil.copyfile(variables, nvram)
    drive = lambda path: str(path).replace(",", ",,")
    for mode in ("bios", "uefi"):
        boot = ["-bios", str(bios)] if mode == "bios" else [
            "-drive", f"if=pflash,format=raw,readonly=on,file={drive(ovmf)}",
            "-drive", f"if=pflash,format=raw,file={drive(nvram)}"]
        command = [qemu, "-machine", "pc,accel=tcg" if mode == "bios" else "q35,accel=tcg", "-cpu", "max",
                   "-m", "1024", "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio",
                   "-monitor", "none", "-no-reboot", "-L", str(firmware),
                   "-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"), *boot,
                   "-device", "qemu-xhci", "-drive", f"if=none,id=stick,format=raw,snapshot=on,file={drive(image)}",
                   "-device", "usb-storage,drive=stick", "-boot", "c"]
        log = root / f"{mode}.log"
        print(f"Booting {mode}: firmware -> USB FAT32 -> {'Syslinux' if mode == 'bios' else 'GRUB'} -> real Linux", flush=True)
        with log.open("xb") as stream:
            run = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                 timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
        data = log.read_bytes()
        if run.returncode or b"RUFUS_SYSLINUX_MULTI_INITRD_KERNEL_OK" not in data:
            raise AssertionError(f"{mode} kernel boot failed; inspect {log}: " + data[-3000:].decode(errors="replace"))
        print(f"PASS: {mode} kernel handoff and both initrd archives", flush=True)
    result["kernel_boot"] = {"bios": "passed", "uefi": "passed"}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
