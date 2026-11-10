#!/usr/bin/python3
"""Read our 4Kn VHDX with Microsoft's driver in an isolated x64 WinPE VM.

Opt-in, requires an extracted Windows ISO tree. Only newly generated regular
files enter QEMU; source Windows files and physical storage are never changed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import backup, boot, core, layout, media, windows, windows_togo


def fixture(root, token, filesystem):
    raw, volume, marker = (root / name for name in ("fourkn.img", "fourkn." + filesystem, "marker.txt"))
    size, start = 64 * boot.MIB, 256
    sectors = (size - 2 * boot.MIB) // 4096
    with raw.open("xb") as stream:
        stream.truncate(size)
    script = f"label: gpt\nunit: sectors\n\nstart={start},size={sectors},type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7,name=VHDX4K\n"
    subprocess.run(["sfdisk", "--sector-size", "4096", "--no-reread", "--no-tell-kernel", str(raw)],
                   input=script.encode(), check=True, capture_output=True)
    with volume.open("xb") as stream:
        stream.truncate(sectors * 4096)
    marker.write_bytes(("RUFUS_NATIVE_VHDX_4KN_" + token + "\r\n").encode())
    if filesystem == "ntfs":
        core.run_tool(["mkfs.ntfs", "-F", "-Q", "-s", "4096", "-c", "4096", "-p", str(start),
                       "-L", "VHDX4K", str(volume)])
        core.run_tool(["ntfscp", str(volume), str(marker), "/sector-test.txt"])
    else:
        core.run_tool(["mkfs.vfat", "-F", "16", "-S", "4096", "-s", "1", "-h", str(start), "-n", "VHDX4K", str(volume)])
        core.run_tool(["mcopy", "-i", str(volume), str(marker), "::/sector-test.txt"])
    with raw.open("r+b") as target, volume.open("rb") as source:
        boot.copy_sparse(source.fileno(), target.fileno(), start * 4096, sectors * 4096)
    image = root / "fourkn.vhdx"
    digest = backup.backup_file(raw, image, "vhdx", logical_sector=4096)
    return image, marker.read_bytes(), digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-tree", type=Path, required=True, help="extracted x64 Windows ISO, used read-only")
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory (about 2 GiB temporary space)")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--filesystem", choices=("fat16", "ntfs"), default="fat16")
    parser.add_argument("--readonly", action="store_true", help="attach the disposable VHDX copy read-only")
    args = parser.parse_args()
    work = args.output_dir.resolve()
    work.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = args.windows_tree.resolve()
    core.validate_fat_tree(root)
    original = windows.case_path(root, "sources/boot.wim")
    original_hash = core.checksum(original)
    index = windows.wim_boot_index(original)
    selected = next(item for item in windows_togo.images(original) if item["index"] == index)
    if selected["architecture"] != "amd64":
        raise RuntimeError("This optional Microsoft-reader check requires an x64 Windows ISO tree")
    token = uuid.uuid4().hex
    image, expected, digest = fixture(work, token, args.filesystem)
    tree = work / "system-files"
    tree.mkdir()
    for name in ("boot", "efi"):
        shutil.copytree(windows.case_path(root, name), tree / name)
    shutil.copyfile(windows.case_path(root, "bootmgr"), tree / "bootmgr")
    (tree / "sources").mkdir()
    staged = tree / "sources/boot.wim"
    shutil.copyfile(original, staged)
    (tree / "system.marker").write_text(token)
    guest = work / "guest"
    guest.mkdir()
    (guest / "initial.txt").write_text(
        "select disk 0\nselect partition 1\nremove all noerr\nassign letter=S\n"
        "select partition 2\nremove all noerr\nassign letter=H\nexit\n")
    (guest / "attach.txt").write_text(
        "select vdisk file=H:\\fourkn.vhdx\nattach vdisk" + (" readonly" if args.readonly else "") + "\n"
        "select disk 1\nonline disk noerr\nselect partition 1\nassign letter=V\ndetail vdisk\ndetail volume\nlist volume\nexit\n")
    geometry_command = "ntfsinfo" if args.filesystem == "ntfs" else "sectorinfo"
    script = rf"""@echo off
wpeinit
diskpart /s X:\RufusVhdx\initial.txt >X:\RufusVhdx\initial.log 2>&1
if not exist S:\system.marker goto shutdown
diskpart /s X:\RufusVhdx\attach.txt >S:\attach.log 2>&1
fsutil fsinfo {geometry_command} V: >S:\geometry.txt 2>&1
dir V:\ /a >S:\directory.log 2>&1
set attempts=0
:waitvolume
if exist V:\sector-test.txt goto readvolume
set /a attempts+=1
if %attempts% GEQ 30 goto failed
ping -n 2 127.0.0.1 >nul
goto waitvolume
:readvolume
type V:\sector-test.txt >S:\readback.txt
fsutil fsinfo {geometry_command} V: >S:\geometry.txt 2>&1
if errorlevel 1 goto failed
echo {token}>S:\status.txt
goto shutdown
:failed
echo FAILED>S:\status.txt
:shutdown
wpeutil shutdown
"""
    (guest / "test.cmd").write_bytes(script.replace("\n", "\r\n").encode())
    shell = guest / "winpeshl.ini"
    shell.write_text("[LaunchApps]\r\n%SYSTEMROOT%\\System32\\cmd.exe, /D /C X:\\RufusVhdx\\test.cmd\r\n")
    updates = ("add " + windows.quote_wim_path(guest) + " /RufusVhdx\nadd " +
               windows.quote_wim_path(shell) + " /Windows/System32/winpeshl.ini\n")
    with tempfile.TemporaryFile() as commands:
        commands.write(updates.encode())
        commands.seek(0)
        core.run_tool(["wimlib-imagex", "update", str(staged), str(index), "--check"], stdin=commands)
    core.run_tool(["wimlib-imagex", "info", str(staged), str(index), "--boot"])
    parts = [layout.Partition(2048, 1536 * boot.MIB // 512, "basic", 0x0c, "WinPE", True),
             layout.Partition(2048 + 1536 * boot.MIB // 512, 256 * boot.MIB // 512, "basic", 0x07, "VHDX data")]
    disk_size = (parts[-1].end + 1) * 512
    fat, ntfs, disk = (work / name for name in ("system.fat", "bridge.ntfs", "winpe-test.img"))
    for path, size in ((fat, parts[0].sectors * 512), (ntfs, parts[1].sectors * 512)):
        with path.open("xb") as stream:
            stream.truncate(size)
    media.populate_volume(tree, fat, parts[0], "vfat", "bios")
    core.run_tool(["mkfs.ntfs", "-F", "-Q", "-s", "512", "-p", str(parts[1].start), "-L", "VHDX_DATA", str(ntfs)])
    core.run_tool(["ntfscp", str(ntfs), str(image), "/fourkn.vhdx"])
    with disk.open("xb") as target:
        target.truncate(disk_size)
        layout.write_layout(target.fileno(), disk_size, parts, "dos")
        for path, part in ((fat, parts[0]), (ntfs, parts[1])):
            with path.open("rb") as source:
                boot.copy_sparse(source.fileno(), target.fileno(), part.start * 512, part.sectors * 512)
        os.fsync(target.fileno())
    command = windows_togo.vm_command(disk, "amd64", "bios", work)
    last = [""]
    def progress(stage, done, total):
        if stage != last[0]:
            print(stage, flush=True)
            last[0] = stage
    print("Booting isolated WinPE to attach and read our 4Kn VHDX through Microsoft's driver", flush=True)
    with (work / "qemu.log").open("xb") as log:
        windows_togo.run_vm(command, log, args.timeout, progress=progress)
    files = {}
    for name in ("status.txt", "attach.log", "readback.txt", "geometry.txt", "directory.log"):
        result = subprocess.run(["mtype", "-i", f"{disk}@@{parts[0].start * 512}", "::/" + name], capture_output=True)
        files[name] = result.stdout
        (work / name).write_bytes(result.stdout)
    if files["status.txt"].strip() != token.encode() or files["readback.txt"] != expected:
        raise AssertionError(f"Microsoft could not mount/read the 4Kn VHDX; inspect {work}")
    geometry = files["geometry.txt"].replace(b"\0", b"").decode(errors="replace")
    if not re.search(r"(?:Bytes Per Sector|LogicalBytesPerSector)\s*:\s*4096\b", geometry, re.I):
        raise AssertionError("Windows did not report 4096-byte logical sectors: " + geometry)
    if core.checksum(original) != original_hash or core.checksum(work / "fourkn.img") != digest:
        raise AssertionError("An original validation input changed")
    (work / "result.json").write_text(json.dumps({"test": "Microsoft WinPE VHDX reader",
        "logical_sector": 4096, "gpt": True, "filesystem": args.filesystem, "marker_read": True,
        "disposable_copy_readonly": args.readonly,
        "source_windows_unchanged": True, "physical_storage_used": False}, indent=2) + "\n")
    for path in (fat, ntfs, disk, work / ("fourkn." + args.filesystem)):
        path.unlink()
    shutil.rmtree(tree)
    print(f"PASS: Microsoft VHDX driver -> 4Kn GPT -> {args.filesystem} -> expected marker; geometry verified", flush=True)


if __name__ == "__main__":
    main()
