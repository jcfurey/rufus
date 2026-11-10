#!/usr/bin/python3
"""Opt-in real Casper reboot test in private VM overlays.

The source ISO and prepared image remain read-only. A test-only initramfs hook
runs after Casper mounts the real live root, writes a marker through that root,
and checks it on a second boot. Firmware tests remaster a private image with
that hook, serial console output, and a zero-second GRUB menu timeout.
"""

import argparse
import gzip
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, live
from boot_smoke import firmware_paths
from uefi_smoke import ovmf_paths


def archive(files):
    """Small newc archive appended to a COPY of the distribution's initramfs."""
    data = bytearray()
    for index, (path, value, mode) in enumerate([*files, ("TRAILER!!!", b"", 0)]):
        name = path.encode() + b"\0"
        fields = (index + 1, mode, 0, 0, 1, 0, len(value), 0, 0, 0, 0, len(name), 0)
        data.extend(b"070701" + "".join(f"{n:08x}" for n in fields).encode() + name)
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(value)
        data.extend(b"\0" * (-len(data) % 4))
    data.extend(b"\0" * (-len(data) % 512))
    return bytes(data)


HOOK = b'''#!/bin/sh
if [ "$1" = prereqs ]; then exit 0; fi
exec >/dev/ttyS0 2>&1
if ! grep -q ' /cow ext4 ' /proc/mounts; then
    echo RUFUS_PERSISTENCE_FAIL_NO_EXT4_COW
    cat /proc/mounts
elif ! grep -q " ${rootmnt} overlay " /proc/mounts; then
    echo RUFUS_PERSISTENCE_FAIL_NO_ROOT_OVERLAY
    cat /proc/mounts
elif [ -e "${rootmnt}/root/rufus-persistence-test" ]; then
    if [ "$(cat "${rootmnt}/root/rufus-persistence-test")" = rufus-reboot-marker ]; then
        echo RUFUS_PERSISTENCE_SECOND_BOOT_OK
    else
        echo RUFUS_PERSISTENCE_FAIL_WRONG_MARKER
    fi
else
    echo rufus-reboot-marker >"${rootmnt}/root/rufus-persistence-test"
    sync
    echo RUFUS_PERSISTENCE_FIRST_BOOT_OK
fi
sync
poweroff -f
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path)
    parser.add_argument("--prepared", type=Path, help="existing Rufus live image, used as a read-only backing file")
    parser.add_argument("--output-dir", required=True, type=Path, help="NEW directory for image, overlay, and logs")
    parser.add_argument("--kernel-path", default="/casper/vmlinuz")
    parser.add_argument("--initrd-path", default="/casper/initrd.gz")
    parser.add_argument("--timeout", type=int, default=240, help="seconds per boot")
    parser.add_argument("--firmware", choices=("direct", "bios", "uefi", "all"), default="direct")
    args = parser.parse_args()
    qemu, qemu_img = shutil.which("qemu-system-x86_64"), shutil.which("qemu-img")
    if not qemu or not qemu_img:
        raise RuntimeError("Install qemu-system-x86 and qemu-utils for the live reboot test.")
    bios, firmware = firmware_paths(qemu)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    prepared = args.prepared.resolve() if args.prepared else root / "live.img"
    if not args.prepared:
        live.create_live_image(args.iso, prepared, 256 * live.MIB)
    kernel, initrd = root / "vmlinuz", root / "test-initrd"
    core.run_tool(live.iso_command(args.iso.resolve(), "-osirrox", "on", "-extract", args.kernel_path, str(kernel),
                                   "-extract", args.initrd_path, str(initrd)))
    hook_path = "scripts/casper-bottom/99-rufus-persistence-test"
    order = f'/{hook_path} "$@"\n'.encode()
    with initrd.open("ab") as stream:
        stream.write(gzip.compress(archive([(hook_path, HOOK, stat.S_IFREG | 0o755),
                                           ("scripts/casper-bottom/ORDER", order, stat.S_IFREG | 0o644)])))
    modes = ("bios", "uefi") if args.firmware == "all" else (args.firmware,)
    backing = prepared
    if args.firmware != "direct":
        config = root / "grub.cfg"
        core.run_tool(live.iso_command(prepared, "-osirrox", "on", "-extract", "/boot/grub/grub.cfg", str(config)))
        text = config.read_text()
        text, count = re.subn(r"(?<!\S)boot=casper(?=\s|$)", "boot=casper console=ttyS0 ip=frommedia", text)
        if not count:
            raise RuntimeError("Firmware harness needs a Casper GRUB configuration.")
        config.write_text("serial --unit=0 --speed=115200\nterminal_input serial\nterminal_output serial\n"
                          "set timeout=0\nset timeout_style=hidden\n" + text)
        backing = root / "firmware-test.img"
        core.run_tool(live.iso_command(prepared, "-outdev", str(backing), "-map", str(config), "/boot/grub/grub.cfg",
                                       "-map", str(initrd), args.initrd_path, "-boot_image", "any", "replay", "-end"))
    for mode in modes:
        overlay = root / f"{mode}-persistence.qcow2"
        core.run_tool([qemu_img, "create", "-f", "qcow2", "-F", "raw", "-b", str(backing), str(overlay)])
        if mode == "uefi":
            code, variables = ovmf_paths(qemu)
            nvram = root / "uefi-vars.fd"
            shutil.copyfile(variables, nvram)
            machine = "q35,accel=tcg"
            boot_args = ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                         "-drive", f"if=pflash,format=raw,file={nvram}"]
        else:
            machine, boot_args = "pc,accel=tcg", ["-bios", str(bios)]
            if mode == "direct":
                boot_args += ["-kernel", str(kernel), "-initrd", str(initrd), "-append",
                              "boot=casper persistent console=ttyS0 fsck.mode=skip ip=frommedia panic=-1"]
        for number, expected in ((1, b"RUFUS_PERSISTENCE_FIRST_BOOT_OK"), (2, b"RUFUS_PERSISTENCE_SECOND_BOOT_OK")):
            print(f"Booting real Casper live root via {mode}, pass {number}", flush=True)
            log = root / f"{mode}-boot-{number}.log"
            command = [qemu, "-machine", machine, "-m", "2048", "-smp", "2", "-nodefaults", "-nic", "none",
                       "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot",
                       "-L", str(firmware), "-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"),
                       *boot_args, "-drive", f"if=ide,format=qcow2,file={overlay}"]
            with log.open("xb") as stream:
                try:
                    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                            timeout=args.timeout)
                except subprocess.TimeoutExpired as exc:
                    raise AssertionError(f"Casper {mode} boot {number} timed out; inspect {log}") from exc
            data = log.read_bytes()
            if result.returncode or expected not in data or b"RUFUS_PERSISTENCE_FAIL" in data:
                raise AssertionError(f"Casper {mode} boot {number} failed; inspect {log}: " +
                                     data[-3000:].decode(errors="replace"))
            print(f"PASS: {mode}: {expected.decode()}", flush=True)
    (root / "result.json").write_text(json.dumps({"iso": str(args.iso.resolve()), "prepared": str(prepared),
                                                  "boots": 2 * len(modes), "persistence": "passed", "boot_method": modes,
                                                  "source_images_modified": False}, indent=2) + "\n")


if __name__ == "__main__":
    main()
