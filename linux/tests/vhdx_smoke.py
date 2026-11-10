#!/usr/bin/python3
"""Boot native-fixed and QEMU-dynamic VHDX round trips without physical storage."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import backup, boot, core, layout, media
from boot_smoke import firmware_paths
from uefi_smoke import compile_marker, ovmf_paths


def x86_sources(work):
    source = work / "bios-source.img"
    boot.create_freedos(source)
    program = work / "EXITVM.COM"
    program.write_bytes(bytes.fromhex("ba f4 00 b0 10 ee f4"))
    script = work / "AUTOEXEC.BAT"
    script.write_bytes(b"@echo off\r\nset PATH=.;\\;\\LOCALE\r\nMODE COM1:9600,N,8,1 >NUL\r\n"
                       b"echo RUFUS_FREEDOS_BOOT_OK>COM1\r\nEXITVM.COM\r\n")
    for path in (program, script):
        core.run_tool(["mcopy", "-o", "-i", f"{source}@@{boot.PARTITION_OFFSET}", str(path), "::/"])
    efi = compile_marker(work)
    tree = work / "efi-files/EFI/BOOT"
    tree.mkdir(parents=True)
    shutil.copyfile(efi, tree / "BOOTX64.EFI")
    size = 128 * boot.MIB
    table, parts = media.windows_partitions(size)
    volume, uefi = work / "efi.fat", work / "uefi-source.img"
    with volume.open("xb") as stream:
        stream.truncate(parts[0].sectors * 512)
    media.populate_volume(work / "efi-files", volume, parts[0], "vfat", "uefi")
    with uefi.open("xb") as output, volume.open("rb") as data:
        output.truncate(size)
        layout.write_layout(output.fileno(), size, parts, table)
        boot.copy_sparse(data.fileno(), output.fileno(), parts[0].start * 512, parts[0].sectors * 512)
    volume.unlink()
    return [("bios", source), ("uefi", uefi)]


def boot_x86(image, mode, work):
    qemu = shutil.which("qemu-system-x86_64")
    if not qemu:
        raise RuntimeError("Install qemu-system-x86 for this optional boot test")
    bios, share = firmware_paths(qemu)
    command = [qemu, "-machine", ("pc" if mode == "bios" else "q35") + ",accel=tcg", "-m", "256",
               "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio", "-monitor", "none",
               "-no-reboot", "-L", str(share), "-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"),
               "-device", "isa-debug-exit,iobase=0xf4,iosize=0x04",
               "-drive", f"if=ide,format=raw,file={str(image).replace(',', ',,')}"]
    if mode == "bios":
        command += ["-bios", str(bios), "-boot", "c"]
    else:
        code, variables = ovmf_paths(qemu)
        local = work / (image.stem + "-vars.fd")
        shutil.copyfile(variables, local)
        command += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}",
                    "-drive", f"if=pflash,format=raw,file={local}"]
    result = subprocess.run(command, capture_output=True, timeout=45, env={**os.environ, "TMPDIR": str(work)})
    marker = b"RUFUS_FREEDOS_BOOT_OK" if mode == "bios" else b"RUFUS_UEFI_BOOT_OK"
    if result.returncode != (33 if mode == "bios" else 37) or marker not in result.stdout:
        raise AssertionError((result.stdout + result.stderr)[-4000:].decode(errors="replace"))
    return result.stdout + result.stderr


def boot_arm(image, mode, work):
    qemu = shutil.which("qemu-system-aarch64" if mode == "arm64" else "qemu-system-arm")
    if not qemu:
        raise RuntimeError("Install qemu-system-arm for ARM boot tests")
    share = Path(qemu).resolve().parents[1] / "share"
    family = "AAVMF" if mode == "arm64" else "AAVMF32"
    code, variables = (share / "AAVMF" / (family + suffix + ".fd") for suffix in ("_CODE", "_VARS"))
    if not code.is_file():
        code, variables = (Path("/usr/share/AAVMF") / (family + suffix + ".fd") for suffix in ("_CODE", "_VARS"))
    local = work / (image.stem + "-vars.fd")
    shutil.copyfile(variables, local)
    command = [qemu, "-machine", "virt,accel=tcg", "-cpu", "cortex-a57" if mode == "arm64" else "cortex-a15",
               "-m", "512", "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio",
               "-monitor", "none", "-no-reboot", "-L", str(share / "qemu"),
               "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
               "-drive", f"if=pflash,format=raw,file={local}",
               "-device", "qemu-xhci", "-drive", f"if=none,id=stick,format=raw,snapshot=on,file={str(image).replace(',', ',,')}",
               "-device", "usb-storage,drive=stick"]
    result = subprocess.run(command, capture_output=True, timeout=120, env={**os.environ, "TMPDIR": str(work)})
    if result.returncode or f"RUFUS_{mode.upper()}_KERNEL_OK".encode() not in result.stdout:
        raise AssertionError((result.stdout + result.stderr)[-4000:].decode(errors="replace"))
    return result.stdout + result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory")
    parser.add_argument("--arm64-image", type=Path, help="read-only image from arm_smoke.py, with its kernel marker")
    parser.add_argument("--arm32-image", type=Path, help="read-only ARM32 arm_smoke.py image")
    parser.add_argument("--skip-x86", action="store_true")
    args = parser.parse_args()
    root = args.output_dir.resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    sources = [] if args.skip_x86 else x86_sources(root)
    sources += [(mode, path.resolve()) for mode, path in (("arm64", args.arm64_image), ("arm", args.arm32_image)) if path]
    if not sources:
        parser.error("Select at least one boot image/test architecture")
    results = {}
    for mode, source in sources:
        original = core.checksum(source)
        for producer in ("native-fixed", "qemu-dynamic"):
            container, restored = (root / f"{mode}-{producer}{suffix}" for suffix in (".vhdx", ".img"))
            if producer == "native-fixed":
                backup.backup_file(source, container, "vhdx")
            else:
                core.run_tool(["qemu-img", "convert", "-f", "raw", "-O", "vhdx",
                               "-o", "subformat=dynamic,block_state_zero=on", str(source), str(container)])
            restored_hash = core.write_to_file(container, restored)
            if restored_hash != original:
                raise AssertionError("VHDX restoration changed the original bootable disk bytes")
            output = boot_x86(restored, mode, root) if mode in ("bios", "uefi") else boot_arm(restored, mode, root)
            (root / f"{mode}-{producer}.log").write_bytes(output)
            results[f"{mode}-{producer}"] = {"booted": True, "restored_sha256": restored_hash}
            (root / "result.json").write_text(json.dumps({
                "status": "in-progress", "physical_storage_used": False, "tests": results}, indent=2) + "\n")
            restored.unlink()
            container.unlink()
            print(f"PASS: {mode} {producer} -> native VHDX restore -> verified bytes -> boot marker", flush=True)
        if core.checksum(source) != original:
            raise AssertionError("Original boot source changed")
    (root / "result.json").write_text(json.dumps({
        "status": "passed", "physical_storage_used": False, "tests": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
