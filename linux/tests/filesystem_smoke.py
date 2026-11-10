#!/usr/bin/python3
"""Mount/write/reboot/read FAT16 and UDF using private QEMU data disks only."""

import argparse
import gzip
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, formatting, grub, linux_iso
from live_smoke import archive
from boot_smoke import firmware_paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--modules", type=Path, required=True, help="matching kernel module directory")
    parser.add_argument("--busybox", type=Path, default=Path("/usr/bin/busybox"), help="static guest BusyBox")
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory")
    parser.add_argument("--firmware-code", type=Path, help="ARM: matching AAVMF UEFI code")
    parser.add_argument("--firmware-vars", type=Path, help="ARM: matching AAVMF variables template")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    architecture = grub.kernel_architecture(args.kernel)
    if architecture not in ("x86_64", "arm64", "arm"):
        raise RuntimeError("Supply an x86-64, ARM64 or ARM32 kernel and matching modules/BusyBox.")
    qemu = shutil.which({"x86_64": "qemu-system-x86_64", "arm64": "qemu-system-aarch64", "arm": "qemu-system-arm"}[architecture])
    if not qemu:
        raise RuntimeError("Install the matching qemu-system package.")
    if architecture != "x86_64" and (args.firmware_code is None or args.firmware_vars is None):
        raise RuntimeError("ARM guests require --firmware-code and --firmware-vars.")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    files = [(name, b"", stat.S_IFDIR | 0o755) for name in ("bin", "dev", "proc", "sys", "mnt", "modules")]
    files.append(("bin/busybox", args.busybox.read_bytes(), stat.S_IFREG | 0o755))
    modules = []
    for name in ("crc-itu-t", "nls_cp437", "nls_iso8859-1", "nls_utf8", "fat", "vfat", "udf"):
        found = [path for path in args.modules.rglob(name + ".ko*") if path.is_file()]
        if len(found) > 1:
            raise RuntimeError(f"Ambiguous {name} modules; supply one kernel's module directory.")
        if found:
            path = found[0]
            data = path.read_bytes()
            if path.suffix == ".zst":
                data = subprocess.run(["zstd", "-d", "-c", str(path)], check=True, capture_output=True).stdout
            elif path.suffix == ".xz":
                data = lzma.decompress(data)
            elif path.suffix == ".gz":
                data = gzip.decompress(data)
            files.append((f"modules/{name}.ko", data, stat.S_IFREG | 0o644))
            modules.append(name)
    disks, manifest = [], []
    for filesystem in ("fat16", "udf"):
        for table in ("dos", "gpt"):
            for sector in (512, 4096):
                image = root / f"{filesystem}-{table}-{sector}.img"
                formatting.create_formatted_image(image, 64 * formatting.MIB, filesystem, table=table, sector=sector)
                manifest.append(f"RUFUS_{len(disks)} {'vfat' if filesystem == 'fat16' else filesystem}")
                disks.append((image, sector))
    files.append(("manifest", ("\n".join(manifest) + "\n").encode(), stat.S_IFREG | 0o644))
    load = "\n".join(f"/bin/busybox insmod /modules/{name}.ko || fail" for name in modules)
    init = '''#!/bin/busybox sh
exec >/dev/console 2>&1
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
fail() { echo RUFUS_FILESYSTEM_FAILED; /bin/busybox poweroff -f; while :; do :; done; }
''' + load + '''
phase=write
/bin/busybox grep -q rufus_phase=read /proc/cmdline && phase=read
while read identity fs; do
    node=
    for disk in /sys/block/vd*; do
        if [ "$(/bin/busybox cat "$disk/serial")" = "$identity" ]; then node=${disk##*/}; fi
    done
    [ -n "$node" ] || fail
    /bin/busybox mount -t "$fs" "/dev/${node}1" /mnt || fail
    if [ "$phase" = write ]; then
        echo "Rufus-$identity-persistent-data" > /mnt/rufus-marker.txt || fail
        /bin/busybox sync
    else
        [ "$(/bin/busybox cat /mnt/rufus-marker.txt)" = "Rufus-$identity-persistent-data" ] || fail
    fi
    /bin/busybox umount /mnt || fail
    echo "RUFUS_FILESYSTEM_OK $phase $identity $node $fs"
done < /manifest
echo "RUFUS_FILESYSTEM_ALL_OK $phase"
/bin/busybox poweroff -f
'''
    files.append(("init", init.encode(), stat.S_IFREG | 0o755))
    initrd = root / "initrd.gz"
    initrd.write_bytes(gzip.compress(archive(files)))
    kernel = args.kernel.resolve()
    if kernel.read_bytes()[:2] == b"\x1f\x8b":
        expanded = root / "kernel"
        expanded.write_bytes(gzip.decompress(kernel.read_bytes()))
        kernel = expanded
    if architecture == "x86_64":
        bios, firmware = firmware_paths(qemu)
        machine = ["-machine", "pc,accel=tcg", "-cpu", "max", "-L", str(firmware), "-bios", str(bios)]
    else:
        variables = root / "variables.fd"
        shutil.copyfile(args.firmware_vars, variables)
        machine = ["-machine", "virt,accel=tcg", "-cpu", "cortex-a57" if architecture == "arm64" else "cortex-a15",
                   "-L", str(Path(qemu).resolve().parents[1] / "share/qemu"),
                   "-drive", f"if=pflash,format=raw,readonly=on,file={str(args.firmware_code.resolve()).replace(',', ',,')}",
                   "-drive", f"if=pflash,format=raw,file={str(variables).replace(',', ',,')}"]
    block = "virtio-blk-pci" if architecture == "x86_64" else "virtio-blk-device"
    for phase in ("write", "read"):
        command = [qemu, *machine, "-m", "1024", "-nodefaults", "-nic", "none", "-display", "none",
                   "-serial", "stdio", "-monitor", "none", "-no-reboot"]
        if architecture == "x86_64":
            command += ["-kernel", str(kernel), "-initrd", str(initrd), "-append",
                        f"console=ttyS0 rdinit=/init panic=-1 rufus_phase={phase}"]
        else:
            tree = root / ("source-" + phase)
            (tree / "casper").mkdir(parents=True)
            (tree / "boot/grub").mkdir(parents=True)
            shutil.copyfile(kernel, tree / "casper/vmlinuz")
            shutil.copyfile(initrd, tree / "casper/initrd")
            (tree / "boot/grub/grub.cfg").write_text("menuentry 'Filesystem test' {\n"
                f"linux /casper/vmlinuz boot=casper console=ttyAMA0 rdinit=/init panic=-1 rufus_phase={phase}\n"
                "initrd /casper/initrd\n}\n")
            iso, boot = root / f"boot-{phase}.iso", root / f"boot-{phase}.img"
            core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(iso), str(tree)])
            linux_iso.create_extracted_image(iso, boot, architecture=architecture)
            command += ["-device", "qemu-xhci", "-drive", f"if=none,id=boot,format=raw,readonly=on,file={str(boot).replace(',', ',,')}",
                        "-device", "usb-storage,drive=boot,bootindex=1"]
        for index, (image, sector) in enumerate(disks):
            command += ["-drive", f"if=none,id=d{index},format=raw,file={str(image).replace(',', ',,')}",
                        "-device", f"{block},drive=d{index},serial=RUFUS_{index},logical_block_size={sector},physical_block_size={sector}"]
        log = root / f"{phase}.log"
        print(f"Booting {architecture}: {phase} across eight FAT16/UDF MBR/GPT 512/4Kn disks", flush=True)
        with log.open("xb") as stream:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
        output = log.read_bytes()
        if result.returncode or b"RUFUS_FILESYSTEM_FAILED" in output or f"RUFUS_FILESYSTEM_ALL_OK {phase}".encode() not in output:
            raise AssertionError(f"{phase} failed; inspect {log}: " + output[-3000:].decode(errors="replace"))
        print(f"PASS: {phase}, eight volumes", flush=True)
    (root / "result.json").write_text(json.dumps({"status": "passed", "architecture": architecture,
        "physical_storage_used": False, "volumes": [path.name for path, _ in disks],
        "mount_write_reboot_read": "passed"}, indent=2) + "\n")


if __name__ == "__main__":
    main()
