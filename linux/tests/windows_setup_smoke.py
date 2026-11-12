#!/usr/bin/python3
"""Complete Windows Setup/OOBE in disposable file-only VMs, using local media.

All destructive answer-file disk selection is test-only and paired with a newly
created VM disk. No network, physical device, or host directory reaches the VM.
"""

import argparse
import base64
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, media, windows, windows_togo
from rufus_linux.boot import copy_sparse
from windows_togo_smoke import QMP, copy_region, screenshot

NS = {"u": windows.UNATTEND}
# TCG does not implement RDPMC. This Windows fixture executed it with QEMU's
# default AMD family-15 identity despite pmu being disabled. Intel family 6
# exposes the explicit, empty architectural PMU enumeration instead.
ACCEPTANCE_CPU = "max,vendor=GenuineIntel,family=6,model=60,stepping=1,pmu=off"


def component(tree, config_pass, name):
    settings = tree.find(f"u:settings[@pass='{config_pass}']", NS)
    if settings is None:
        settings = windows.element(tree, "settings", **{"pass": config_pass})
    result = settings.find(f"u:component[@name='{name}']", NS)
    if result is None:
        result = windows.element(settings, "component", name=name, processorArchitecture="amd64",
                                 publicKeyToken="31bf3856ad364e35", language="neutral", versionScope="nonSxS")
    return result


def instrument_answer(data, firmware, index, *, control=False):
    """Retain production choices; add only VM target selection and evidence."""
    tree = ET.fromstring(data)
    setup = component(tree, "windowsPE", "Microsoft-Windows-Setup")
    for name in ("DiskConfiguration", "ImageInstall"):
        if setup.find("u:" + name, NS) is not None:
            raise RuntimeError("Refusing existing installation target selection.")
    configuration = windows.element(setup, "DiskConfiguration")
    disk = windows.element(configuration, "Disk", **{f"{{{windows.WCM}}}action": "add"})
    windows.element(disk, "DiskID", 0)
    windows.element(disk, "WillWipeDisk", "true")
    create = windows.element(disk, "CreatePartitions")
    parts = [("EFI", 260), ("MSR", 16), ("Primary", None)] if firmware == "uefi" else [("Primary", 500), ("Primary", None)]
    for number, (kind, size) in enumerate(parts, 1):
        part = windows.element(create, "CreatePartition", **{f"{{{windows.WCM}}}action": "add"})
        windows.element(part, "Order", number)
        windows.element(part, "Type", kind)
        windows.element(part, "Size" if size else "Extend", size or "true")
    modify = windows.element(disk, "ModifyPartitions")
    for order, number in enumerate((1, len(parts)), 1):
        part = windows.element(modify, "ModifyPartition", **{f"{{{windows.WCM}}}action": "add"})
        windows.element(part, "Order", order)
        windows.element(part, "PartitionID", number)
        windows.element(part, "Format", "FAT32" if firmware == "uefi" and number == 1 else "NTFS")
        windows.element(part, "Label", "SYSTEM" if number == 1 else "WINDOWS")
        if number == len(parts):
            windows.element(part, "Letter", "C")
        elif firmware == "bios":
            windows.element(part, "Active", "true")
    windows.element(configuration, "WillShowUI", "OnError")
    install = windows.element(windows.element(setup, "ImageInstall"), "OSImage")
    metadata = windows.element(windows.element(install, "InstallFrom"), "MetaData", **{f"{{{windows.WCM}}}action": "add"})
    windows.element(metadata, "Key", "/IMAGE/INDEX")
    windows.element(metadata, "Value", index)
    target = windows.element(install, "InstallTo")
    windows.element(target, "DiskID", 0)
    windows.element(target, "PartitionID", len(parts))
    windows.element(install, "WillShowUI", "OnError")
    user = setup.find("u:UserData", NS)
    if user is None:
        user = windows.element(setup, "UserData")
        windows.element(user, "AcceptEula", "true")
    if user.find("u:ProductKey", NS) is None:
        windows.element(windows.element(user, "ProductKey"), "Key", "")
    dynamic = windows.element(setup, "DynamicUpdate")
    windows.element(dynamic, "Enable", "false")
    international = component(tree, "windowsPE", "Microsoft-Windows-International-Core-WinPE")
    windows.element(windows.element(international, "SetupUILanguage"), "UILanguage", "en-US")
    for name in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
        windows.element(international, name, "en-US")
    if control:
        if setup.find("u:RunSynchronous", NS) is not None or setup.find("u:RunAsynchronous", NS) is not None:
            raise RuntimeError("Hardware control cannot retain WinPE setup commands.")
        commands = windows.element(setup, "RunAsynchronous")
        command = windows.element(commands, "RunAsynchronousCommand", **{f"{{{windows.WCM}}}action": "add"})
        windows.element(command, "Order", 1)
        windows.element(command, "Path", r'cmd.exe /d /c start "" /min cmd.exe /d /c X:\RufusHardwareControl.cmd')
    add_logon_checker(tree, r"C:\RufusAcceptance\launch.cmd")
    ET.indent(tree, space="  ")
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


def add_logon_checker(tree, launcher):
    shell = component(tree, "oobeSystem", "Microsoft-Windows-Shell-Setup")
    commands = shell.find("u:FirstLogonCommands", NS)
    if commands is None:
        commands = windows.element(shell, "FirstLogonCommands")
    order = 1 + max((int(n.text) for n in commands.findall("u:SynchronousCommand/u:Order", NS)), default=0)
    command = windows.element(commands, "SynchronousCommand", **{f"{{{windows.WCM}}}action": "add"})
    windows.element(command, "Order", order)
    windows.element(command, "CommandLine", "cmd.exe /d /c " + launcher)


def validate_evidence(data, nonce, options, *, portable=False):
    if data.get("nonce") != nonce or data.get("errors"):
        raise AssertionError("Guest evidence has the wrong identity or reports checker errors.")
    account = data.get("account", {})
    sid = data.get("sid", "")
    if (data.get("username", "").casefold() != options.local_account.casefold() or
            not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", sid) or account.get("sid") != sid or
            account.get("source") != "Local" or account.get("enabled") is not True):
        raise AssertionError("Expected the actual local account created by Windows Setup.")
    if (data.get("interactive") is not True or data.get("session", 0) <= 0 or
            not data.get("explorerPids") or sid not in data.get("explorerOwners", [])):
        raise AssertionError("No interactive Explorer session for the expected user.")
    if data.get("setup") != {"imageState": "IMAGE_STATE_COMPLETE", "systemSetupInProgress": 0, "oobeInProgress": 0}:
        raise AssertionError("Windows Setup/OOBE is not complete.")
    if data.get("userMarkerReadback") != nonce:
        raise AssertionError("The real user session did not write/read its profile marker.")
    for key, value in (("culture", options.locale), ("systemLocale", options.locale),
                       ("uiLanguage", options.locale), ("timezone", options.timezone)):
        if value and data.get(key) != value:
            raise AssertionError(f"Installed {key} does not match the requested setting.")
    if options.offline_account and data.get("bypassNRO") != 1:
        raise AssertionError("Offline-account configuration was not applied.")
    if options.disable_encryption and data.get("preventDeviceEncryption") != 1:
        raise AssertionError("Automatic device-encryption prevention was not applied.")
    if options.privacy:
        privacy = data.get("privacy", {})
        if privacy.get("advertising") != 0 or privacy.get("tailored") != 0 or privacy.get("telemetry") not in (0, 1):
            raise AssertionError("Installed privacy settings did not disable advertising/tailoring and optional diagnostics.")
    if portable and (data.get("portableOperatingSystem") != 1 or data.get("sanPolicy") != 4):
        raise AssertionError("Portable Windows policies did not survive OOBE.")


def validate_control(marker, activity, nonce):
    if f"RUFUS_HARDWARE_CONTROL {nonce}" not in marker.splitlines():
        raise AssertionError("Hardware control has no matching guest nonce.")
    absent = "ERROR: The system was unable to find the specified registry key or value." in marker
    if not absent and r"HKEY_LOCAL_MACHINE\SYSTEM\Setup\LabConfig" not in marker:
        raise AssertionError("Hardware control did not query the live bypass settings.")
    if any(int(value, 16) for value in re.findall(r"Bypass\w+Check\s+REG_DWORD\s+0x([0-9a-f]+)", marker, re.I)):
        raise AssertionError("Hardware control had a hardware bypass enabled.")
    for requirement in ("TpmVersion", "Memory", "UefiSecureBoot"):
        if f"CHWReqChecker: checked {requirement}, found HardBlock." not in activity:
            raise AssertionError(f"Windows did not report the expected {requirement} hardware block.")


def collect_control(root, nonce):
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(root / "installer-test.img")]))["partitiontable"]
    offset = table["partitions"][0]["start"] * 512
    volume = root / "control-diagnostics.ntfs"
    if volume.exists():
        raise RuntimeError("Refusing to overwrite existing control diagnostic volume.")
    overlay = str(root / "installer-overlay.qcow2").replace(",", ",,")
    try:
        core.run_tool(["qemu-img", "convert", "--image-opts",
                       f"driver=raw,offset={offset},file.driver=qcow2,file.file.filename={overlay}",
                       "-O", "raw", str(volume)])
        files = {}
        for name in ("rufus-hardware-control.txt", "rufus-control-errors.log", "rufus-control-activity.log"):
            data = subprocess.check_output(["ntfscat", str(volume), "/" + name])
            (root / name).write_bytes(data)
            files[name] = data.decode("utf-8-sig", errors="replace")
    finally:
        volume.unlink(missing_ok=True)
    validate_control(files["rufus-hardware-control.txt"], files["rufus-control-activity.log"], nonce)
    return {"nonce": nonce, "hardware_control": "blocked_as_expected",
            "blocked_requirements": ["TpmVersion", "Memory", "UefiSecureBoot"],
            "labconfig": files["rufus-hardware-control.txt"]}


def instrument_image(prepared, work, options, firmware, index, *, control_nonce=None):
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(prepared)]))["partitiontable"]
    part = table["partitions"][0]
    offset, length = part["start"] * 512, part["size"] * 512
    volume, image = work / "installer.ntfs", work / "installer-test.img"
    with prepared.open("rb") as source, image.open("xb") as target, volume.open("xb") as fs:
        target.truncate(prepared.stat().st_size)
        fs.truncate(length)
        copy_sparse(source.fileno(), target.fileno(), 0, prepared.stat().st_size)
        copy_region(source.fileno(), fs.fileno(), offset, 0, length)
    boot = work / "boot.wim"
    with boot.open("xb") as stream:
        core.run_tool(["ntfscat", str(volume), "/sources/boot.wim"], stdout=stream)
    boot_index = windows.wim_boot_index(boot)
    extracted = work / "original-answer"
    extracted.mkdir()
    if options.bypass_hardware:
        core.run_tool(["wimlib-imagex", "extract", str(boot), str(boot_index), "/Autounattend.xml",
                       "--dest-dir=" + str(extracted), "--no-acls"])
        original = (extracted / "Autounattend.xml").read_bytes()
    else:
        original = subprocess.check_output(["ntfscat", str(volume), "/sources/$OEM$/$$/Panther/unattend.xml"])
        (extracted / "Autounattend.xml").write_bytes(original)
    answer = work / "test-answer.xml"
    answer.write_bytes(instrument_answer(original, firmware, index, control=control_nonce is not None))
    core.run_tool(["wimlib-imagex", "update", str(boot), str(boot_index), "--check",
                   "--command=add " + windows.quote_wim_path(answer) + " /Autounattend.xml"])
    if control_nonce is not None:
        observer = work / "hardware-control.cmd"
        observer.write_text((Path(__file__).parent / "fixtures/windows-hardware-control.cmd").read_text()
                            .replace("@NONCE@", control_nonce), newline="\r\n")
        core.run_tool(["wimlib-imagex", "update", str(boot), str(boot_index), "--check",
                       "--command=add " + windows.quote_wim_path(observer) + " /RufusHardwareControl.cmd"])
        delay = work / "wait.vbs"
        delay.write_text("WScript.Sleep CLng(WScript.Arguments(0))\n", newline="\r\n")
        core.run_tool(["wimlib-imagex", "update", str(boot), str(boot_index), "--check",
                       "--command=add " + windows.quote_wim_path(delay) + " /RufusWait.vbs"])
    core.run_tool(["ntfscp", str(volume), str(boot), "/sources/boot.wim"])
    with volume.open("rb") as fs, image.open("r+b") as target:
        copy_region(fs.fileno(), target.fileno(), 0, offset, length)
        os.fsync(target.fileno())
    volume.unlink()
    return image


def run_prepared(root, timeout):
    """Resume an owned test fixture without rebuilding or resetting its disks."""
    if (root / "result.json").exists():
        raise RuntimeError("This run already passed; use a new output directory for another test.")
    inputs = json.loads((root / "inputs.json").read_text())
    command = json.loads((root / "command.json").read_text())
    socket_path = Path(command[command.index("-qmp") + 1].split(",", 1)[0].removeprefix("unix:"))
    qmp = QMP(socket_path, root / "events.jsonl")
    start, last_frame = time.monotonic(), 0
    usb_first = "usb-storage,id=installer-usb,drive=installer,removable=on,bootindex=1"
    select_target = inputs["firmware"] == "bios" and usb_first in command
    def progress(stage, done, total):
        nonlocal last_frame, select_target
        elapsed = time.monotonic() - start
        if select_target and elapsed >= 30:
            # SeaBIOS tries only its first hard disk, not the next disk when a
            # blank target fails. Boot USB first, then change the firmware list
            # for the next reset. This does not change the current BIOS mapping.
            for device, index in (("installer-usb", 3), ("target-disk", 1), ("installer-usb", 2)):
                reply = qmp.command("qom-set", {"path": "/machine/peripheral/" + device,
                                    "property": "bootindex", "value": index})
                if "error" in reply:
                    raise RuntimeError(f"Cannot select the installed disk for the next BIOS boot: {reply}")
            command[command.index(usb_first)] = usb_first[:-1] + "2"
            old_target = "ide-hd,id=target-disk,drive=target,bootindex=2"
            command[command.index(old_target)] = old_target[:-1] + "1"
            (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")
            (root / "bios-boot-switch.json").write_text(json.dumps({
                "elapsed_seconds": elapsed, "first_boot": "installer-usb", "later_boots": "target-disk"}, indent=2) + "\n")
            select_target = False
        requests = root / "keyboard.json"
        if requests.exists():
            pending = requests.with_suffix(".running.json")
            requests.rename(pending)
            actions = json.loads(pending.read_text())
            for action in actions:
                if "keys" in action:
                    send_keys(qmp, action["keys"])
                elif "text" in action:
                    type_text(qmp, action["text"])
                else:
                    raise ValueError("Keyboard requests accept keys or text only.")
            with (root / "keyboard-history.jsonl").open("a") as history:
                history.write(json.dumps(actions) + "\n")
            pending.unlink()
        elapsed = time.monotonic() - start
        if elapsed - last_frame >= 30:
            frame = root / "next-frame.ppm"
            screenshot(qmp, frame)
            if frame.exists():
                frame.replace(root / "latest-frame.ppm")
            waiting = "hardware-block evidence" if inputs.get("control") else "completed OOBE/user session"
            print(f"Windows {inputs['firmware']}: waiting for {waiting} ({int(elapsed)}s)", flush=True)
            last_frame = elapsed
    print("Booting the private Windows acceptance VM", flush=True)
    with (root / "runs.jsonl").open("a") as history:
        history.write(json.dumps({"started_unix": time.time(), "timeout_seconds": timeout, "command": command}) + "\n")
    try:
        with (root / "qemu.log").open("ab") as log:
            windows_togo.run_vm(command, log, timeout, progress=progress)
    finally:
        qmp.close()
        socket_path.unlink(missing_ok=True)
    verify_run(root)


def verify_run(root):
    """Verify retained evidence after the private VM has exited."""
    inputs = json.loads((root / "inputs.json").read_text())
    options = windows.WindowsOptions(**inputs["options"])
    nonce = inputs["nonce"]
    serial = (root / "serial.log").read_text(errors="replace")
    records = re.findall(r"RUFUS_OOBE_EVIDENCE ([A-Za-z0-9+/=]+)", serial)
    if inputs.get("control"):
        if records:
            raise AssertionError("The unsupported-hardware control unexpectedly reached first login.")
        evidence = collect_control(root, nonce)
        status = {"hardware_control": "blocked_as_expected", "setup_oobe": "not_run", "first_logon": "not_run"}
    else:
        if len(records) != 1:
            raise AssertionError("No unique first-logon evidence; inspect screenshots, serial output and the private target disk.")
        evidence = json.loads(base64.b64decode(records[0], validate=True))
        (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        validate_evidence(evidence, nonce, options, portable=inputs.get("mode") == "windows-to-go")
        status = {"setup_oobe": "passed", "first_logon": "passed"}
    (root / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    for path, sha in inputs["readonly_inputs"].items():
        if core.checksum(Path(path)) != sha:
            raise AssertionError(f"Read-only test input changed: {path}")
    if "source_tree" in inputs:
        source = Path(inputs["source_tree"])
        if any(core.checksum(source / name) != sha for name, sha in inputs["source_files_sha256"].items()):
            raise AssertionError("Cached source tree changed.")
    result = {**inputs, **status, "evidence": evidence,
              "physical_storage_used": False, "source_images_modified": False}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("PASS: Windows rejected unsupported hardware without bypass" if inputs.get("control") else
          "PASS: Windows Setup/OOBE, real local-user Explorer session and requested settings", flush=True)


def send_keys(qmp, keys):
    result = qmp.command("send-key", {"keys": [{"type": "qcode", "data": key} for key in keys], "hold-time": 30})
    if "error" in result:
        raise RuntimeError(f"Cannot send test keyboard input: {result}")
    time.sleep(0.075)


def type_text(qmp, value):
    """Type diagnostic commands through the guest's emulated US keyboard."""
    plain = {" ": "spc", "\\": "backslash", "/": "slash", ".": "dot", "-": "minus", "\n": "ret",
             ":": "semicolon", ";": "semicolon", "=": "equal", ",": "comma", "'": "apostrophe",
             "[": "bracket_left", "]": "bracket_right", "`": "grave_accent"}
    shifted = dict(zip('!@#$%^&*()_+{}|:\"<>?~',
        ['1','2','3','4','5','6','7','8','9','0','minus','equal','bracket_left','bracket_right',
         'backslash','semicolon','apostrophe','comma','dot','slash','grave_accent']))
    for char in value:
        shift = char in shifted or char.isupper()
        key = shifted.get(char) or plain.get(char) or char.lower()
        if not (char.isascii() and (char.isalnum() or char in plain or char in shifted)):
            raise ValueError("Test keyboard input must use printable US ASCII or newline.")
        send_keys(qmp, (["shift"] if shift else []) + [key])


def prepare_togo(source, root, firmware):
    """Add only the first-logon observer to a private production To Go copy."""
    source = source.resolve()
    options = windows.WindowsOptions(offline_account=True, local_account="rufus-test", privacy=True,
        disable_encryption=True, locale="en-US", timezone="Central Standard Time")
    nonce = secrets.token_hex(16)
    sha = core.checksum(source)
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(source)]))["partitiontable"]
    if table.get("sectorsize", 512) != 512:
        raise RuntimeError("The test requires a 512-byte-sector Windows To Go image.")
    part = table["partitions"][-1]
    offset, length = part["start"] * 512, part["size"] * 512
    image, volume = root / "test.img", root / "windows.ntfs"
    with core.source_file(source) as original, image.open("xb") as target, volume.open("xb") as fs:
        target.truncate(os.fstat(original.fileno()).st_size)
        fs.truncate(length)
        copy_sparse(original.fileno(), target.fileno(), 0, os.fstat(original.fileno()).st_size)
        copy_region(original.fileno(), fs.fileno(), offset, 0, length)
    with volume.open("rb") as stream:
        if stream.read(11)[3:] != b"NTFS    ":
            raise RuntimeError("The last Windows To Go partition must be NTFS.")
    original = subprocess.check_output(["ntfscat", str(volume), "/Windows/Panther/unattend.xml"])
    (root / "original-answer.xml").write_bytes(original)
    tree = ET.fromstring(original)
    add_logon_checker(tree, r"C:\Windows\Temp\rufus-oobe-launch.cmd")
    answer = root / "test-answer.xml"
    answer.write_bytes(ET.tostring(tree, encoding="utf-8", xml_declaration=True))
    checker, launcher = root / "check.ps1", root / "launch.cmd"
    checker.write_text((Path(__file__).parent / "fixtures/windows-oobe-check.ps1").read_text()
        .replace("@NONCE@", nonce).replace("@USER@", options.local_account))
    launcher.write_text('@echo off\nstart "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass '
                        '-File C:\\Windows\\Temp\\rufus-oobe-check.ps1\n', newline="\r\n")
    for src, dest in ((answer, "/Windows/Panther/unattend.xml"),
                      (checker, "/Windows/Temp/rufus-oobe-check.ps1"),
                      (launcher, "/Windows/Temp/rufus-oobe-launch.cmd")):
        core.run_tool(["ntfscp", str(volume), str(src), dest])
    with volume.open("rb") as fs, image.open("r+b") as target:
        copy_region(fs.fileno(), target.fileno(), 0, offset, length)
        os.fsync(target.fileno())
    volume.unlink()
    inputs = {"mode": "windows-to-go", "options": asdict(options), "nonce": nonce,
              "firmware": firmware, "readonly_inputs": {str(source): sha},
              "hardware": {"architecture": "x86_64", "cpu": ACCEPTANCE_CPU,
                           "ram_mib": 4096, "tpm": False, "secure_boot": False,
                           "acceleration": "tcg", "network": "none", "physical_storage": False}}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    command = windows_togo.vm_command(image, "amd64", firmware, root)
    command.remove("-no-reboot")
    command[command.index("-cpu") + 1] = ACCEPTANCE_CPU
    command[command.index("-serial") + 1] = "file:" + str(root / "serial.log")
    for i, value in enumerate(command):
        if value.startswith("if=ide,format=raw,file="):
            command[i] = value.replace("if=ide,", "if=none,id=stick,", 1)
    command += ["-device", "qemu-xhci", "-device", "usb-storage,drive=stick",
                "-qmp", f"unix:/tmp/rufus-oobe-{nonce}.sock,server=on,wait=off"]
    (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tree", type=Path)
    parser.add_argument("--togo-image", type=Path, help="fresh Windows To Go image prepared with the test options")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--firmware", choices=("uefi", "bios"), default="uefi")
    parser.add_argument("--timeout", type=int, default=14400)
    parser.add_argument("--control", action="store_true", help="omit hardware bypass on the same unsupported virtual hardware")
    parser.add_argument("--resume", action="store_true", help="resume this existing harness-owned output directory")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds.")
    root = args.output_dir.resolve()
    if args.resume:
        run_prepared(root, args.timeout)
        return
    if args.togo_image:
        if args.source_tree or args.index or args.control:
            parser.error("--togo-image cannot be combined with installer-source/control arguments.")
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        prepare_togo(args.togo_image, root, args.firmware)
        run_prepared(root, args.timeout)
        return
    if args.source_tree is None or args.index is None:
        parser.error("Supply --source-tree and --index when creating a new fixture.")
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    source = args.source_tree.resolve()
    core.validate_windows_tree(source)
    editions = windows_togo.images(windows_togo.install_archive(source))
    selected = next((entry for entry in editions if entry["index"] == args.index), None)
    if selected is None or selected["architecture"] != "amd64":
        parser.error("Select an existing x64 Windows image index for this harness.")
    options = windows.WindowsOptions(bypass_hardware=not args.control, offline_account=True,
        local_account="rufus-test", privacy=True, disable_encryption=True, locale="en-US", timezone="Central Standard Time")
    ram_mib = 3072 if args.firmware == "uefi" or args.control else 4096
    nonce = secrets.token_hex(16)
    payload = root / "payload"
    payload.mkdir()
    checker = Path(__file__).parent / "fixtures/windows-oobe-check.ps1"
    (payload / "check.ps1").write_text(checker.read_text().replace("@NONCE@", nonce).replace("@USER@", options.local_account))
    (payload / "launch.cmd").write_text('@echo off\nstart "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\RufusAcceptance\\check.ps1\n', newline="\r\n")
    files = {str(p.relative_to(source)): core.checksum(p) for p in source.rglob("*") if p.is_file()}
    inputs = {"source_tree": str(source), "source_files_sha256": files, "options": asdict(options),
              "nonce": nonce, "firmware": args.firmware, "control": args.control, "edition": selected,
              "hardware": {"architecture": "x86_64", "cpu": ACCEPTANCE_CPU,
                           "ram_mib": ram_mib, "tpm": False, "secure_boot": False,
                           "acceleration": "tcg", "network": "none", "physical_storage": False}}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    iso, prepared = root / "test-source.iso", root / "prepared.img"
    print("Building private source ISO and production Windows installer image", flush=True)
    core.run_tool(["xorriso", "-outdev", str(iso), "-compliance", "iso_9660_level=3", "-map", str(source), "/",
                   "-map", str(payload), "/sources/$OEM$/$1/RufusAcceptance", "-volid", "RUFUS_WIN_TEST", "-end"])
    preparation = media.create_windows_image(iso, prepared, filesystem="ntfs", firmware=args.firmware, options=options)
    (root / "preparation.json").write_text(json.dumps(preparation, indent=2) + "\n")
    installer = instrument_image(prepared, root, options, args.firmware, args.index,
                                 control_nonce=nonce if args.control else None)
    inputs.update({"source_iso_sha256": core.checksum(iso), "prepared_sha256": core.checksum(prepared),
                   "instrumented_installer_sha256": core.checksum(installer)})
    inputs["readonly_inputs"] = {str(iso): inputs["source_iso_sha256"], str(prepared): inputs["prepared_sha256"],
                                 str(installer): inputs["instrumented_installer_sha256"]}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    target = root / "target.qcow2"
    core.run_tool(["qemu-img", "create", "-f", "qcow2", str(target), "64G"])
    # Windows may update NTFS metadata when it first mounts Linux-created media.
    # Model a writable USB stick while retaining the prepared source unchanged.
    installer_overlay = root / "installer-overlay.qcow2"
    core.run_tool(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(installer), str(installer_overlay)])
    command = windows_togo.vm_command(target, "amd64", args.firmware, root)
    command.remove("-no-reboot")
    command[command.index("-cpu") + 1] = ACCEPTANCE_CPU
    command[command.index("-m") + 1] = str(ram_mib)
    command[command.index("-serial") + 1] = "file:" + str(root / "serial.log")
    for i, value in enumerate(command):
        if value.startswith("if=ide,format=raw,file="):
            command[i] = value.replace("if=ide,format=raw,", "if=none,id=target,format=qcow2,", 1)
    target_index, usb_index = (2, 1) if args.firmware == "bios" else (1, 2)
    command += ["-device", f"ide-hd,id=target-disk,drive=target,bootindex={target_index}", "-device", "qemu-xhci",
                "-drive", "if=none,id=installer,format=qcow2,file=" + str(installer_overlay).replace(",", ",,"),
                "-device", f"usb-storage,id=installer-usb,drive=installer,removable=on,bootindex={usb_index}"]
    socket_path = Path("/tmp") / ("rufus-oobe-" + nonce + ".sock")
    command += ["-qmp", f"unix:{socket_path},server=on,wait=off"]
    (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    print(f"Booting Windows Setup with a blank 64-GiB target, {ram_mib}-MiB RAM, no TPM or network", flush=True)
    run_prepared(root, args.timeout)


if __name__ == "__main__":
    main()
