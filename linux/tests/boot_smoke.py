#!/usr/bin/python3
"""Unprivileged BIOS boot tests. Only newly created image files enter the VM."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import boot


def firmware_paths(qemu):
    share = Path(qemu).resolve().parents[1] / "share"
    for prefix in (share, Path("/usr/share")):
        bios = prefix / "seabios/bios-256k.bin"
        if bios.is_file() and (prefix / "qemu").is_dir():
            return bios, prefix / "qemu"
    raise RuntimeError("Install seabios and qemu-system-data to run file-backed BIOS boot tests.")


def main():
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        raise RuntimeError("Install qemu-system-x86 to run file-backed BIOS boot tests.")
    bios, firmware = firmware_paths(qemu)
    with tempfile.TemporaryDirectory(prefix="rufus-boot-test-") as directory:
        root = Path(directory)
        # This DOS COM program exits QEMU through the explicitly installed test
        # device. It is test instrumentation, never part of generated user media.
        exit_vm = root / "EXITVM.COM"
        exit_vm.write_bytes(bytes.fromhex("ba f4 00 b0 10 ee f4"))
        autoexec = root / "AUTOEXEC.BAT"
        autoexec.write_bytes(b"@echo off\r\nset PATH=.;\\;\\LOCALE\r\n"
                             b"MODE COM1:9600,N,8,1 >NUL\r\n"
                             b"echo RUFUS_FREEDOS_BOOT_OK>COM1\r\nEXITVM.COM\r\n")
        for fat in (16, 32):
            image = root / f"freedos-{fat}.img"
            boot.create_freedos(image, fat_bits=fat)
            for source in (exit_vm, autoexec):
                subprocess.run(["mcopy", "-o", "-i", f"{image}@@{boot.PARTITION_OFFSET}",
                                str(source), "::/"], check=True, capture_output=True)
            args = [qemu, "-machine", "pc,accel=tcg", "-m", "128", "-nodefaults", "-nic", "none",
                    "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot",
                    "-L", str(firmware), "-bios", str(bios), "-device",
                    "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"),
                    "-device", "isa-debug-exit,iobase=0xf4,iosize=0x04", "-boot", "c",
                    "-drive", f"file={image},format=raw,if=ide"]
            try:
                result = subprocess.run(args, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(f"FAT{fat} FreeDOS did not finish booting: " +
                                     (exc.stdout or b"")[-2000:].decode(errors="replace")) from exc
            if result.returncode != 33 or b"RUFUS_FREEDOS_BOOT_OK" not in result.stdout:
                raise AssertionError(f"FAT{fat} boot failed ({result.returncode}): " +
                                     (result.stdout + result.stderr)[-3000:].decode(errors="replace"))
            print(f"PASS: BIOS -> MBR -> FAT{fat} boot sector -> FreeDOS kernel -> COMMAND.COM", flush=True)


if __name__ == "__main__":
    main()
