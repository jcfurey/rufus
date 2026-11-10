"""Unmounted Windows deployment, finalized by the ISO's WinPE in a private VM.

The guest sees one newly created disk image, no network, no physical devices,
and no host filesystem share. Microsoft BCDBoot constructs its own BCD stores.
Only a successfully finalized image may enter the ordinary USB writer.
"""

import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

from .backup import new_output
from .boot import MIB, copy_sparse
from .core import (FAT_MAX_FILE, RufusError, check_cancel, checksum, emit, expected_sha256,
                   run_tool, source_file, stage_windows_iso)
from .layout import Partition, write_layout
from .media import populate_volume
from .windows import ARCH_BOOT, WindowsOptions, answer_file, case_path, quote_wim_path, wim_boot_index
from .wim import apply_ntfs_image

GIB = 1024**3
ARCHITECTURES = {"0": "x86", "9": "amd64", "12": "arm64"}


def images(path, cancel=None):
    with tempfile.TemporaryFile() as stream:
        run_tool(["wimlib-imagex", "info", str(path), "--xml"], cancel, stdout=stream)
        if stream.tell() > 4 * MIB:
            raise RufusError("Windows image metadata exceeds the supported size.")
        stream.seek(0)
        data = stream.read()
    text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise RufusError("Unsupported declarations in Windows image metadata.")
    try:
        root = ET.fromstring(text)
        result = []
        for entry in root.findall("IMAGE"):
            result.append({"index": int(entry.attrib["INDEX"]), "name": entry.findtext("NAME") or "Windows",
                           "architecture": ARCHITECTURES.get(entry.findtext("WINDOWS/ARCH"), "unsupported"),
                           "expanded_size": int(entry.findtext("TOTALBYTES", "0")),
                           "major": int(entry.findtext("WINDOWS/VERSION/MAJOR", "0")),
                           "minor": int(entry.findtext("WINDOWS/VERSION/MINOR", "0")),
                           "build": int(entry.findtext("WINDOWS/VERSION/BUILD", "0")),
                           "edition": entry.findtext("WINDOWS/EDITIONID") or ""})
        if (not result or len(result) > 128 or len({entry["index"] for entry in result}) != len(result) or
                any(entry["index"] < 1 or entry["expanded_size"] < 0 for entry in result)):
            raise ValueError("Missing or duplicate images")
    except (ET.ParseError, KeyError, ValueError) as exc:
        raise RufusError("Cannot identify Windows installation images.") from exc
    return result


def pe_architecture(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise RufusError("Windows To Go requires a valid Microsoft EFI loader.")
        offset = struct.unpack_from("<I", header, 60)[0]
        if not 64 <= offset <= MIB:
            raise RufusError("Unsupported EFI loader header.")
        stream.seek(offset)
        pe = stream.read(6)
    if len(pe) != 6 or pe[:4] != b"PE\0\0":
        raise RufusError("Windows To Go requires a valid Microsoft EFI loader.")
    return {0x14c: "x86", 0x8664: "amd64", 0xaa64: "arm64"}.get(struct.unpack_from("<H", pe, 4)[0], "unsupported")


def install_archive(root):
    archives = [case_path(root, "sources/" + name) for name in ("install.wim", "install.esd")]
    present = [path for path in archives if path.is_file()]
    if len(present) != 1:
        raise RufusError("Windows To Go requires one complete sources/install.wim or install.esd in the ISO.")
    return present[0]


def inspect_iso(source, cancel=None, progress=None):
    with tempfile.TemporaryDirectory(prefix="rufus-wtg-inspect-") as directory:
        root = Path(directory)
        stage_windows_iso(source, root, cancel, progress, split_wim=False)
        return images(install_archive(root), cancel)


def partitions(size, firmware):
    if type(size) is not int or firmware not in ("uefi", "bios", "both") or size < 32 * GIB or size % 512:
        raise RufusError("Windows To Go requires UEFI/BIOS/both and at least 32 GiB with 512-byte sectors.")
    table = "gpt" if firmware == "uefi" else "dos"
    end = ((size // 512 - (34 if table == "gpt" else 0)) // 2048) * 2048
    # The provisioning WinPE is removed after BCDBoot succeeds. Leave a roomy
    # system partition so contemporary boot.wim files fit during preparation.
    system = Partition(2048, 1536 * MIB // 512, "esp" if table == "gpt" else "basic", 0x0c, "WTG_SYSTEM", table == "dos")
    parts = [system]
    start = system.end + 1
    if table == "gpt":
        parts.append(Partition(start, 16 * MIB // 512, "msr", 0, "Microsoft reserved"))
        start = parts[-1].end + 1
    parts.append(Partition(start, end - start, "basic", 0x07, "Windows To Go"))
    if table == "dos" and size > 2**41:
        raise RufusError("BIOS/MBR Windows To Go cannot exceed 2 TiB.")
    return table, parts


def validate_selection(selected, size, firmware, architecture=""):
    """Share edition, CPU, firmware, and capacity checks with the desktop."""
    target = selected["architecture"]
    if target not in ("amd64", "x86", "arm64") or (selected["major"], selected["minor"]) < (6, 2):
        raise RufusError("Windows To Go requires Windows 8 or newer for x86/x86-64, or compatible ARM64 Windows.")
    if architecture and architecture != target:
        raise RufusError("Selected Windows architecture does not match the installation image.")
    if target == "arm64" and firmware != "uefi":
        raise RufusError("ARM64 Windows To Go requires UEFI firmware.")
    if target == "x86" and firmware == "uefi":
        raise RufusError("32-bit Windows To Go supports BIOS or BIOS+UEFI creation; select one of those targets.")
    table, parts = partitions(size, firmware)
    if parts[-1].sectors * 512 < selected["expanded_size"] * 11 // 10 + 8 * GIB:
        raise RufusError("The selected Windows image needs more space; allow its expanded size plus at least 8 GiB.")
    return table, parts


def firmware_files(architecture, mode, executable):
    if mode == "bios":
        for base in (Path(executable).resolve().parents[1] / "share", Path("/usr/share")):
            path = base / "seabios/bios-256k.bin"
            if path.is_file():
                return path, None, base / "qemu"
        raise RufusError("Windows To Go BIOS preparation requires seabios and qemu-system-data.")
    for base in (Path(executable).resolve().parents[1] / "share", Path("/usr/share")):
        pair = ("AAVMF/AAVMF_CODE.fd", "AAVMF/AAVMF_VARS.fd") if architecture == "arm64" else (
            "OVMF/OVMF_CODE_4M.fd", "OVMF/OVMF_VARS_4M.fd")
        code, variables = (base / name for name in pair)
        if code.is_file() and variables.is_file():
            return code, variables, base / "qemu"
    raise RufusError("Windows To Go UEFI preparation requires ovmf (x86-64) or qemu-efi-aarch64 (ARM64).")


def vm_command(image, architecture, firmware, work):
    if architecture not in ("amd64", "x86", "arm64"):
        raise RufusError("Unsupported Windows provisioning architecture.")
    if architecture == "arm64" and firmware != "uefi":
        raise RufusError("ARM64 Windows To Go requires UEFI firmware.")
    info = image.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RufusError("The Windows provisioning VM accepts only a newly prepared regular image file.")
    mode = "uefi" if architecture == "arm64" or architecture == "amd64" and firmware != "bios" else "bios"
    if architecture == "x86" and firmware == "uefi":
        raise RufusError("32-bit Windows To Go currently supports BIOS or BIOS+UEFI creation, not UEFI-only preparation.")
    if architecture != "x86" and struct.calcsize("P") < 8:
        raise RufusError("Preparing 64-bit Windows in a private VM requires a 64-bit Linux host.")
    name = "qemu-system-aarch64" if architecture == "arm64" else "qemu-system-x86_64"
    executable = shutil.which(name)
    if not executable:
        raise RufusError(f"Windows To Go boot configuration requires {name}.")
    code, variables, share = firmware_files(architecture, mode, executable)
    drive = lambda path: str(path).replace(",", ",,")
    command = [executable, "-machine", "virt,accel=tcg,gic-version=3" if architecture == "arm64" else (
               "q35,accel=tcg" if mode == "uefi" else "pc,accel=tcg"),
               "-cpu", "max", "-m", "2048" if architecture == "x86" else "4096", "-smp", "2", "-nodefaults", "-nic", "none",
               "-display", "none", "-monitor", "none", "-serial", "none", "-no-reboot", "-L", str(share)]
    if variables:
        nvram = work / "variables.fd"
        shutil.copyfile(variables, nvram)
        command += ["-drive", f"if=pflash,format=raw,readonly=on,file={drive(code)}",
                    "-drive", f"if=pflash,format=raw,file={drive(nvram)}"]
    else:
        command += ["-bios", str(code)]
    if architecture == "arm64":
        command += ["-device", "ramfb", "-device", "qemu-xhci", "-drive",
                    f"if=none,id=target,format=raw,file={drive(image)}", "-device", "usb-storage,drive=target"]
    else:
        rom = next((path for path in (share / "vgabios-stdvga.bin", share.parent / "seabios/vgabios-stdvga.bin",
                                     Path("/usr/share/seabios/vgabios-stdvga.bin")) if path.is_file()), None)
        if rom is None:
            raise RufusError("Windows To Go preparation requires the SeaBIOS VGA option ROM.")
        command += ["-device", "VGA,romfile=" + drive(rom),
                    "-drive", f"if=ide,format=raw,file={drive(image)}"]
    return command


def provisioning_script(token, firmware, options, architecture="amd64"):
    if not re.fullmatch(r"[a-f0-9]{32}", token) or architecture not in ("amd64", "x86", "arm64"):
        raise RufusError("Invalid Windows To Go job identity.")
    job = "rufus-wtg-" + token
    # Disk 0 is safe ONLY inside vm_command's single-disk, unshared VM. This
    # script is deleted with the temporary WinPE and never runs on user hardware.
    script = rf'''@echo off
setlocal EnableExtensions EnableDelayedExpansion
set PATH=X:\Windows\System32;X:\Windows
wpeinit
diskpart /s X:\Rufus\diskpart.txt >X:\Rufus\diskpart.log 2>&1
if not exist S:\{job}\system.marker goto shutdown
if not exist W:\.{job}.marker goto shutdown
call :configure >S:\{job}\provision.log 2>&1
if errorlevel 1 (
    echo FAILED>{'S:'}\{job}\status.txt
) else (
    echo {token}>S:\{job}\status.txt
)
goto shutdown

:configure
if not exist W:\Windows\System32\config\SYSTEM exit /b 1
reg load HKLM\RufusWTG W:\Windows\System32\config\SYSTEM
if errorlevel 1 exit /b 1
set count=0
for %%V in (Current Default LastKnownGood) do (
    for /f "tokens=2,3" %%T in ('reg query HKLM\RufusWTG\Select /v %%V') do (
        if /i "%%T"=="REG_DWORD" (
            set /a selected=%%U
            if !selected! GEQ 1 if !selected! LEQ 999 (
                set padded=00!selected!
                call :portable_policy !padded:~-3!
                if errorlevel 1 goto unload_failed
            )
        )
    )
)
if !count! EQU 0 goto unload_failed
reg unload HKLM\RufusWTG
if errorlevel 1 exit /b 1
'''
    if options.enabled:
        script += rf'''
if exist W:\Windows\Panther\unattend.xml exit /b 1
if exist W:\Windows\Panther\Unattend\unattend.xml exit /b 1
if exist W:\Windows\System32\Sysprep\unattend.xml exit /b 1
if not exist W:\Windows\Panther mkdir W:\Windows\Panther
copy /y S:\{job}\unattend.xml W:\Windows\Panther\unattend.xml
if errorlevel 1 exit /b 1
'''
    script += rf'''
if not exist S:\EFI\Microsoft\Boot mkdir S:\EFI\Microsoft\Boot
del /f /q /a S:\EFI\Microsoft\Boot\BCD >nul 2>&1
del /f /q /a S:\Boot\BCD >nul 2>&1
if exist S:\EFI\Microsoft\Boot\BCD exit /b 1
if exist S:\Boot\BCD exit /b 1
bcdboot W:\Windows /s S: /f {"ALL" if firmware == "both" else firmware.upper()} /c /v /offline
if errorlevel 1 bcdboot W:\Windows /s S: /f {"ALL" if firmware == "both" else firmware.upper()} /v
if errorlevel 1 exit /b 1
'''
    if firmware != "bios":
        script += rf'''if not exist S:\EFI\Boot mkdir S:\EFI\Boot
copy /y S:\EFI\Microsoft\Boot\bootmgfw.efi S:\EFI\Boot\{ARCH_BOOT[architecture]}
if errorlevel 1 exit /b 1
'''
    stores = [r"S:\EFI\Microsoft\Boot\BCD"] if firmware == "uefi" else [r"S:\Boot\BCD"] if firmware == "bios" else [
        r"S:\EFI\Microsoft\Boot\BCD", r"S:\Boot\BCD"]
    for store in stores:
        script += f'bcdedit /store "{store}" /set {{default}} recoveryenabled no\nif errorlevel 1 exit /b 1\n'
    script += f'del /q W:\\.{job}.marker\nif exist W:\\.{job}.marker exit /b 1\n'
    script += r'''exit /b 0
:portable_policy
reg query HKLM\RufusWTG\ControlSet%1 >nul 2>&1
if errorlevel 1 exit /b 0
reg add HKLM\RufusWTG\ControlSet%1\Services\partmgr\Parameters /v SanPolicy /t REG_DWORD /d 4 /f
if errorlevel 1 exit /b 1
reg add HKLM\RufusWTG\ControlSet%1\Control /v PortableOperatingSystem /t REG_DWORD /d 1 /f
if errorlevel 1 exit /b 1
reg add HKLM\RufusWTG\ControlSet%1\Control\Power /v HibernateEnabled /t REG_DWORD /d 0 /f
if errorlevel 1 exit /b 1
reg add "HKLM\RufusWTG\ControlSet%1\Control\Session Manager\Power" /v HiberbootEnabled /t REG_DWORD /d 0 /f
if errorlevel 1 exit /b 1
set /a count+=1
exit /b 0
:unload_failed
reg unload HKLM\RufusWTG
exit /b 1
:shutdown
wpeutil shutdown
'''
    return script.replace("\n", "\r\n").encode("ascii")


def run_vm(command, log, timeout, cancel=None, progress=None):
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
    try:
        while process.poll() is None:
            check_cancel(cancel)
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                raise RufusError("Windows boot configuration timed out. Check the provisioning log and ISO compatibility.")
            emit(progress, f"Configuring Windows boot in private VM ({int(elapsed)}s)")
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        if process.returncode:
            raise RufusError(f"Windows provisioning VM failed ({process.returncode}); log: {log.name}")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def provision(command, image, part, token, log, timeout, cancel=None, progress=None):
    job = "rufus-wtg-" + token
    fat = f"{image}@@{part.start * 512}"
    with log.open("xb") as diagnostic:
        os.fchmod(diagnostic.fileno(), 0o600)
        run_vm(command, diagnostic, timeout, cancel, progress)
        try:
            with tempfile.TemporaryFile() as stream:
                run_tool(["mtype", "-i", fat, "::/" + job + "/provision.log"], stdout=stream)
                stream.seek(0)
                shutil.copyfileobj(stream, diagnostic)
            with tempfile.TemporaryFile() as stream:
                run_tool(["mtype", "-i", fat, "::/" + job + "/status.txt"], stdout=stream)
                stream.seek(0)
                status = stream.read(256).decode("ascii", errors="replace").strip()
        except RufusError as exc:
            raise RufusError(f"WinPE did not finish Windows To Go configuration; log: {log}") from exc
        if status != token:
            raise RufusError(f"Windows To Go boot/policy configuration failed; log: {log}")
    # Remove the temporary recovery/provisioning entry point before release.
    run_tool(["mdel", "-i", fat, "::/sources/boot.wim"], cancel)
    run_tool(["mdeltree", "-i", fat, "::/" + job], cancel)


def _build(root, work, index, size, firmware, options, log, timeout, cancel=None, progress=None):
    archive = install_archive(root)
    choices = images(archive, cancel)
    selected = next((entry for entry in choices if entry["index"] == index), None)
    if selected is None:
        raise RufusError("Select an installation image index listed by windows-images.")
    architecture = selected["architecture"]
    table, parts = validate_selection(selected, size, firmware, options.architecture)
    if firmware != "bios":
        loader = case_path(root, "efi/boot/" + ARCH_BOOT[architecture])
        if not loader.is_file() or pe_architecture(loader) != architecture:
            raise RufusError("The ISO EFI loader does not match the selected Windows architecture.")
    if shutil.disk_usage(work).free < selected["expanded_size"] * 2 + 3 * GIB:
        raise RufusError("Not enough temporary disk space for Windows To Go. Set TMPDIR to a roomy Linux filesystem.")
    boot = case_path(root, "sources/boot.wim")
    boot_index = wim_boot_index(boot, options.boot_index)
    boot_info = next((entry for entry in images(boot, cancel) if entry["index"] == boot_index), None)
    if boot_info is None or boot_info["architecture"] != architecture:
        raise RufusError("The ISO's WinPE architecture does not match the selected Windows image.")
    # Resolve dependencies before applying any potentially large Windows image.
    image = work / "provision.img"
    with image.open("xb") as stream:
        stream.truncate(size)
        write_layout(stream.fileno(), size, parts, table)
    command = vm_command(image, architecture, firmware, work)
    token = uuid.uuid4().hex
    job = "rufus-wtg-" + token
    system_root = work / "system-files"
    system_root.mkdir()
    for name in ("boot", "efi"):
        source = case_path(root, name)
        if source.is_dir():
            shutil.copytree(source, system_root / name)
    if case_path(root, "bootmgr").is_file():
        shutil.copyfile(case_path(root, "bootmgr"), system_root / "bootmgr")
    (system_root / "sources").mkdir()
    staged_boot = system_root / "sources/boot.wim"
    shutil.copyfile(boot, staged_boot)
    job_root = system_root / job
    job_root.mkdir()
    (job_root / "system.marker").write_text(token, encoding="ascii")
    if options.enabled:
        (job_root / "unattend.xml").write_bytes(answer_file(options, architecture))
    guest = work / "guest"
    guest.mkdir()
    (guest / "provision.cmd").write_bytes(provisioning_script(token, firmware, options, architecture))
    os_partition = len(parts)
    (guest / "diskpart.txt").write_text(
        f"select disk 0\nselect partition 1\nremove all noerr\nassign letter=S\n"
        f"select partition {os_partition}\nremove all noerr\nassign letter=W\nexit\n", encoding="ascii")
    shell = guest / "winpeshl.ini"
    shell.write_text("[LaunchApps]\r\n%SYSTEMROOT%\\System32\\cmd.exe, /D /C X:\\Rufus\\provision.cmd\r\n", encoding="ascii")
    updates = ["add " + quote_wim_path(guest) + " /Rufus",
               "add " + quote_wim_path(shell) + " /Windows/System32/winpeshl.ini"]
    with tempfile.TemporaryFile() as commands:
        commands.write(("\n".join(updates) + "\n").encode())
        commands.seek(0)
        run_tool(["wimlib-imagex", "update", str(staged_boot), str(boot_index), "--check"], cancel, stdin=commands)
    run_tool(["wimlib-imagex", "info", str(staged_boot), str(boot_index), "--boot"], cancel)
    if staged_boot.stat().st_size > FAT_MAX_FILE or sum(p.stat().st_size for p in system_root.rglob("*") if p.is_file()) + 32 * MIB > parts[0].sectors * 512:
        raise RufusError("This ISO's WinPE boot environment does not fit the Windows To Go system partition.")
    volume = work / "windows.ntfs"
    with volume.open("xb") as stream:
        stream.truncate(parts[-1].sectors * 512)
    emit(progress, "Applying selected Windows image to NTFS")
    run_tool(["mkfs.ntfs", "-F", "-Q", "-s", "512", "-p", str(parts[-1].start), "-L", "WINDOWS", str(volume)], cancel)
    apply_ntfs_image(archive, index, volume, cancel, progress)
    marker = work / "os.marker"
    marker.write_text(token, encoding="ascii")
    run_tool(["ntfscp", str(volume), str(marker), "/." + job + ".marker"], cancel)
    system = work / "system.fat"
    with system.open("xb") as stream:
        stream.truncate(parts[0].sectors * 512)
    populate_volume(system_root, system, parts[0], "vfat", "bios", cancel, progress)
    with image.open("r+b") as destination:
        for source, part in ((system, parts[0]), (volume, parts[-1])):
            with source.open("rb") as data:
                copy_sparse(data.fileno(), destination.fileno(), part.start * 512, part.sectors * 512, cancel, progress)
        os.fsync(destination.fileno())
    provision(command, image, parts[0], token, log, timeout, cancel, progress)
    return image, selected, parts, table


def create_windows_to_go(source, output, *, index, size=64 * GIB, firmware="uefi", options=None,
                         expected="", log=None, timeout=900, cancel=None, progress=None):
    options = options or WindowsOptions()
    options.validate()
    if options.bypass_hardware:
        raise RufusError("WinPE hardware-check bypass is an installer option, not a Windows To Go deployment setting.")
    if type(index) is not int or index < 1 or type(timeout) is not int or not 30 <= timeout <= 7200:
        raise RufusError("Select a positive Windows edition index and a VM timeout from 30 to 7200 seconds.")
    partitions(size, firmware)
    output = Path(output)
    log = Path(log) if log is not None else Path(str(output) + ".log")
    if output.resolve() == log.resolve() or os.path.lexists(output) or os.path.lexists(log):
        raise RufusError("Windows To Go image and log destinations must both be new files.")
    if not output.parent.is_dir() or not log.parent.is_dir():
        raise RufusError("Select existing directories for the new Windows To Go image and log.")
    for tool in ("wimlib-imagex", "mkfs.ntfs", "ntfscp", "mkfs.vfat", "mcopy", "mtype", "mdel", "mdeltree", "fsck.fat"):
        if not shutil.which(tool):
            raise RufusError(f"Windows To Go creation requires {tool}.")
    expected = expected_sha256(expected)
    if os.environ.get("TMPDIR") and not Path(os.environ["TMPDIR"]).is_dir():
        raise RufusError("TMPDIR must name an existing, roomy temporary directory.")
    with source_file(source) as stream:
        before = os.fstat(stream.fileno())
    source_hash = checksum(source, cancel, progress)
    if expected and source_hash != expected:
        raise RufusError("Windows ISO SHA-256 does not match the expected checksum.")
    with tempfile.TemporaryDirectory(prefix="rufus-windows-togo-") as directory:
        work = Path(directory)
        root = work / "source"
        root.mkdir()
        if shutil.disk_usage(work).free < before.st_size + 2 * GIB:
            raise RufusError("Not enough temporary space to extract this Windows ISO; select another TMPDIR.")
        stage_windows_iso(source, root, cancel, progress, require_uefi=firmware != "bios", split_wim=False)
        image, selected, parts, table = _build(root, work, index, size, firmware, options, log, timeout, cancel, progress)
        after = os.stat(source)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RufusError("Windows ISO changed during deployment.")
        if shutil.disk_usage(output.parent).free < image.stat().st_blocks * 512 + 64 * MIB:
            raise RufusError("Not enough free space at the Windows To Go output destination.")
        with new_output(output) as target, image.open("rb") as payload:
            os.ftruncate(target, size)
            copy_sparse(payload.fileno(), target, 0, size, cancel, progress)
            os.fsync(target)
            digest = checksum(output, cancel, progress)
            check_cancel(cancel)
    emit(progress, "Complete", size, size)
    return {"sha256": digest, "source_sha256": source_hash, "size": size, "edition": selected,
            "firmware": firmware, "table": table, "filesystem": "ntfs", "mode": "windows-to-go",
            "boot_configuration": "Microsoft BCDBoot in isolated WinPE VM", "internal_disk_policy": "offline",
            "log": str(log)}
