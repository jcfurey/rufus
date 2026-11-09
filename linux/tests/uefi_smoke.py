#!/usr/bin/python3
"""Boot generated FAT32 and UEFI:NTFS media using OVMF and a test EFI app."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import boot, media
from boot_smoke import firmware_paths


def compile_marker(root):
    fixture = Path(__file__).parent / "fixtures/uefi_marker.c"
    obj, coff, executable = root / "marker.o", root / "marker.obj", root / "BOOTX64.EFI"
    subprocess.run(["gcc", "-c", "-ffreestanding", "-fshort-wchar", "-mno-red-zone", "-fno-stack-protector",
                    "-fpic", "-fno-asynchronous-unwind-tables", "-o", str(obj), str(fixture)], check=True)
    subprocess.run(["objcopy", "-R", ".comment", "-R", ".note.GNU-stack", "-O", "pe-x86-64", str(obj), str(coff)], check=True)
    subprocess.run(["ld", "-m", "i386pep", "--subsystem", "10", "--entry", "efi_main", "--image-base", "0x10000000",
                    "--enable-reloc-section", "--no-insert-timestamp", "-o", str(executable), str(coff)], check=True)
    return executable


def ovmf_paths(qemu):
    for share in (Path(qemu).resolve().parents[1] / "share", Path("/usr/share")):
        code, variables = share / "OVMF/OVMF_CODE_4M.fd", share / "OVMF/OVMF_VARS_4M.fd"
        if code.is_file() and variables.is_file():
            return code, variables
    raise RuntimeError("Install ovmf/ovmf-generic to run file-backed UEFI boot tests.")


def main():
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        raise RuntimeError("Install qemu-system-x86 to run UEFI boot tests.")
    bios, firmware = firmware_paths(qemu)
    code, variables = ovmf_paths(qemu)
    with tempfile.TemporaryDirectory(prefix="rufus-uefi-test-") as directory:
        root = Path(directory)
        marker = compile_marker(root)
        tree = root / "iso"
        (tree / "EFI/BOOT").mkdir(parents=True)
        (tree / "sources").mkdir()
        shutil.copyfile(marker, tree / "EFI/BOOT/BOOTX64.EFI")
        (tree / "sources/boot.wim").write_bytes(b"Not a Windows image: firmware boot test fixture only.")
        iso = root / "fixture.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-J", "-R", "-o", str(iso), str(tree)], check=True, capture_output=True)
        for filesystem in ("vfat", "ntfs"):
            image = root / f"{filesystem}.img"
            media.create_windows_image(iso, image, size=128 * boot.MIB, filesystem=filesystem)
            nvram = root / f"{filesystem}-vars.fd"
            shutil.copyfile(variables, nvram)
            args = [qemu, "-machine", "q35,accel=tcg", "-m", "256", "-nodefaults", "-nic", "none",
                    "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot", "-L", str(firmware),
                    "-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"),
                    "-device", "isa-debug-exit,iobase=0xf4,iosize=0x04",
                    "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                    "-drive", f"if=pflash,format=raw,file={nvram}",
                    "-drive", f"file={image},format=raw,if=ide"]
            try:
                result = subprocess.run(args, capture_output=True, timeout=30)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError(f"{filesystem} UEFI boot timed out: " +
                                     (exc.stdout or b"")[-3000:].decode(errors="replace")) from exc
            if result.returncode != 37 or b"RUFUS_UEFI_BOOT_OK" not in result.stdout:
                raise AssertionError(f"{filesystem} UEFI boot failed ({result.returncode}): " +
                                     (result.stdout + result.stderr)[-3000:].decode(errors="replace"))
            print(f"PASS: OVMF -> GPT -> {'FAT32' if filesystem == 'vfat' else 'UEFI:NTFS driver -> NTFS'} -> EFI application", flush=True)


if __name__ == "__main__":
    main()
