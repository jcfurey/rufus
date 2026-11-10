#!/usr/bin/python3
"""Boot a real ARM kernel through Rufus's FAT32 UEFI media in QEMU.

Kernel, static BusyBox, and UEFI firmware must be provided locally. The test
never downloads files, installs packages, or opens a physical storage device.
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
from live_smoke import archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("arm64", "arm"), required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, required=True, help="static BusyBox built for the guest architecture")
    parser.add_argument("--firmware-code", type=Path, required=True)
    parser.add_argument("--firmware-vars", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if grub.kernel_architecture(args.kernel) != args.architecture:
        raise RuntimeError("Test kernel architecture does not match the requested guest.")
    qemu = shutil.which("qemu-system-aarch64" if args.architecture == "arm64" else "qemu-system-arm")
    if not qemu:
        raise RuntimeError("Install qemu-system-arm for ARM firmware tests.")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    tree = root / "source"
    (tree / "casper").mkdir(parents=True)
    (tree / "boot/grub").mkdir(parents=True)
    shutil.copyfile(args.kernel, tree / "casper/vmlinuz")
    marker = f"RUFUS_{args.architecture.upper()}_KERNEL_OK"
    init = ("#!/bin/busybox sh\n"
            "/bin/busybox mount -t devtmpfs devtmpfs /dev\n"
            "/bin/busybox mount -t proc proc /proc\n"
            "exec >/dev/ttyAMA0 2>&1\n"
            "/bin/busybox uname -m\n"
            f"echo {marker}\n"
            "/bin/busybox sync\n"
            "/bin/busybox poweroff -f\n").encode()
    files = [(name, b"", stat.S_IFDIR | 0o755) for name in ("bin", "dev", "proc", "sys")]
    files += [("bin/busybox", args.busybox.read_bytes(), stat.S_IFREG | 0o755),
              ("init", init, stat.S_IFREG | 0o755)]
    (tree / "casper/initrd").write_bytes(gzip.compress(archive(files)))
    (tree / "boot/grub/grub.cfg").write_text(
        "menuentry 'ARM kernel validation' {\n"
        "linux /casper/vmlinuz boot=casper console=ttyAMA0 rdinit=/init panic=-1\n"
        "initrd /casper/initrd\n}\n")
    iso, image = root / "fixture.iso", root / "arm-live.img"
    core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(iso), str(tree)])
    result = linux_iso.create_extracted_image(iso, image, architecture=args.architecture)
    variables = root / "variables.fd"
    shutil.copyfile(args.firmware_vars, variables)
    share = Path(qemu).resolve().parents[1] / "share/qemu"
    drive = lambda path: str(path).replace(",", ",,")
    command = [qemu, "-machine", "virt,accel=tcg", "-cpu", "cortex-a57" if args.architecture == "arm64" else "cortex-a15",
               "-m", "1024", "-smp", "2", "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio",
               "-monitor", "none", "-no-reboot", "-L", str(share),
               "-drive", f"if=pflash,format=raw,readonly=on,file={drive(args.firmware_code.resolve())}",
               "-drive", f"if=pflash,format=raw,file={drive(variables)}",
               "-device", "qemu-xhci", "-drive", f"if=none,id=stick,format=raw,snapshot=on,file={drive(image)}",
               "-device", "usb-storage,drive=stick"]
    log = root / "boot.log"
    print(f"Booting {args.architecture}: UEFI -> USB -> FAT32 -> GRUB -> real Linux kernel", flush=True)
    with log.open("xb") as stream:
        run = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                             timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
    data = log.read_bytes()
    if run.returncode or marker.encode() not in data:
        raise AssertionError(f"ARM firmware/kernel boot failed; inspect {log}: " + data[-3000:].decode(errors="replace"))
    result.update({"kernel_boot": "passed", "test_kernel": str(args.kernel.resolve()), "log": str(log)})
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS: {marker}", flush=True)


if __name__ == "__main__":
    main()
