#!/usr/bin/python3
"""Boot fresh portable Windows copies and verify live portable policies.

Requires a locally prepared Windows To Go image. The original stays read-only.
The test replaces an answer file ONLY inside private copies, adds a specialize
commands to record real OS boot/policies, and stops the private VM on success.
"""

import argparse
import errno
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, windows, windows_togo
from rufus_linux.boot import copy_sparse


def resident_root_file(image, offset, filename):
    """Read our small evidence file directly, without mounting a live test disk.

    This intentionally supports only resident root files and the initial MFT
    data runs; it is not a general NTFS implementation. Missing/unfinished records
    return None. A filename match inside the answer XML cannot count as evidence.
    """
    with core.source_file(image) as stream:
        fd = stream.fileno()
        boot = os.pread(fd, 512, offset)
        if boot[3:11] != b"NTFS    ":
            return None
        sector = struct.unpack_from("<H", boot, 11)[0]
        cluster = sector * boot[13]
        scale = struct.unpack_from("<b", boot, 64)[0]
        record_size = 1 << -scale if scale < 0 else scale * cluster
        if sector != 512 or not 512 <= record_size <= 65536 or record_size > cluster:
            return None
        mft = offset + struct.unpack_from("<Q", boot, 48)[0] * cluster

        def attributes(record):
            data = bytearray(record)
            if len(data) != record_size or data[:4] != b"FILE" or not struct.unpack_from("<H", data, 22)[0] & 1:
                return
            usa, count = struct.unpack_from("<HH", data, 4)
            if count != record_size // sector + 1 or usa + count * 2 > record_size:
                return
            for number in range(1, count):
                position = number * sector - 2
                if data[position:position + 2] != data[usa:usa + 2]:
                    return
                data[position:position + 2] = data[usa + number * 2:usa + number * 2 + 2]
            position = struct.unpack_from("<H", data, 20)[0]
            while position + 24 <= record_size:
                kind, amount = struct.unpack_from("<II", data, position)
                if kind == 0xffffffff or amount < 24 or position + amount > record_size:
                    return
                yield kind, bytes(data[position:position + amount])
                position += amount

        data_attribute = next((value for kind, value in attributes(os.pread(fd, record_size, mft))
                               if kind == 0x80 and value[8] == 1 and value[9] == 0), None)
        if data_attribute is None:
            return None
        run_offset = struct.unpack_from("<H", data_attribute, 32)[0]
        remaining = min(struct.unpack_from("<Q", data_attribute, 48)[0], 512 * 1024**2)
        runs, cursor, lcn = [], run_offset, 0
        while cursor < len(data_attribute) and data_attribute[cursor] and remaining:
            sizes = data_attribute[cursor]
            cursor += 1
            count_bytes, offset_bytes = sizes & 15, sizes >> 4
            if not 1 <= count_bytes <= 8 or offset_bytes > 8 or cursor + count_bytes + offset_bytes > len(data_attribute):
                return None
            amount = int.from_bytes(data_attribute[cursor:cursor + count_bytes], "little") * cluster
            cursor += count_bytes
            lcn += int.from_bytes(data_attribute[cursor:cursor + offset_bytes], "little", signed=True) if offset_bytes else 0
            cursor += offset_bytes
            amount = min(amount, remaining)
            if offset_bytes:
                runs.append((offset + lcn * cluster, amount))
            remaining -= amount
        needle = filename.encode("utf-16-le")
        for start, amount in runs:
            if start < offset or start + amount > os.fstat(fd).st_size:
                return None
            for position in range(0, amount, 4 * 1024**2):
                chunk = os.pread(fd, min(4 * 1024**2, amount - position), start + position)
                found = chunk.find(needle)
                while found >= 0:
                    record = chunk[found // record_size * record_size:(found // record_size + 1) * record_size]
                    named, content = False, None
                    for kind, value in attributes(record):
                        if value[8] != 0:
                            continue
                        length, data_start = struct.unpack_from("<IH", value, 16)
                        body = value[data_start:data_start + length]
                        if kind == 0x30 and len(body) >= 66:
                            name = body[66:66 + body[64] * 2].decode("utf-16-le", errors="replace")
                            parent = struct.unpack_from("<Q", body)[0] & 0xffffffffffff
                            named |= name.casefold() == filename.casefold() and parent == 5
                        elif kind == 0x80 and value[9] == 0:
                            content = body
                    if named and content is not None:
                        return content
                    found = chunk.find(needle, found + len(needle))
    return None


def valid_evidence(data):
    text = data.replace(b"\0", b"").decode(errors="replace")
    return ("RUFUS_WINDOWS_TOGO_BOOT_OK" in text and
            re.search(r"SanPolicy\s+REG_DWORD\s+0x4\b", text, re.I) and
            re.search(r"PortableOperatingSystem\s+REG_DWORD\s+0x1\b", text, re.I))


def copy_region(source, target, source_offset, target_offset, size):
    position, finish = source_offset, source_offset + size
    while position < finish:
        try:
            start = os.lseek(source, position, os.SEEK_DATA)
            if start >= finish:
                break
            end = min(finish, os.lseek(source, start, os.SEEK_HOLE))
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
            start, end = position, finish
        while start < end:
            data = os.pread(source, min(core.CHUNK, end - start), start)
            if not data:
                raise RuntimeError("Test image ended during partition copy")
            core.write_at(target, data, target_offset + start - source_offset)
            start += len(data)
        position = end


def test_answer(tree, architecture):
    """Keep each specialize command within Windows' 259-character Path limit."""
    ns = {"u": windows.UNATTEND}
    settings = tree.find("u:settings[@pass='specialize']", ns)
    if settings is None:
        settings = windows.element(tree, "settings", **{"pass": "specialize"})
    component = settings.find("u:component[@name='Microsoft-Windows-Deployment']", ns)
    if component is None:
        component = windows.element(settings, "component", name="Microsoft-Windows-Deployment", processorArchitecture=architecture,
            publicKeyToken="31bf3856ad364e35", language="neutral", versionScope="nonSxS")
    commands = component.find("u:RunSynchronous", ns)
    if commands is None:
        commands = windows.element(component, "RunSynchronous")
    order = 1 + max((int(node.text) for node in commands.findall("u:RunSynchronousCommand/u:Order", ns)), default=0)
    marker = r"%SystemDrive%\rufus-wtg-test.txt"
    paths = (f'cmd.exe /d /c "echo RUFUS_WINDOWS_TOGO_BOOT_OK>{marker}"',
             rf'cmd.exe /d /c "reg query HKLM\SYSTEM\CurrentControlSet\Services\partmgr\Parameters /v SanPolicy >>{marker}"',
             rf'cmd.exe /d /c "reg query HKLM\SYSTEM\CurrentControlSet\Control /v PortableOperatingSystem >>{marker}"')
    for number, text in enumerate(paths, order):
        if number > 500 or len(text) > 259:
            raise RuntimeError("Boot test command exceeds Windows answer-file limits")
        command = windows.element(commands, "RunSynchronousCommand", **{f"{{{windows.WCM}}}action": "add"})
        windows.element(command, "Order", number)
        windows.element(command, "Path", text)
    return ET.tostring(tree, encoding="utf-8", xml_declaration=True)


def instrument(source, work, architecture):
    table = json.loads(subprocess.check_output(["sfdisk", "--json", str(source)]))["partitiontable"]
    if table.get("sectorsize", 512) != 512:
        raise RuntimeError("Test supports 512-byte-sector Windows To Go images")
    part = table["partitions"][-1]
    offset, length = part["start"] * 512, part["size"] * 512
    image, volume = work / "test.img", work / "windows.ntfs"
    with core.source_file(source) as original, image.open("xb") as target, volume.open("xb") as os_volume:
        size = os.fstat(original.fileno()).st_size
        target.truncate(size)
        os_volume.truncate(length)
        copy_sparse(original.fileno(), target.fileno(), 0, size)
        copy_region(original.fileno(), os_volume.fileno(), offset, 0, length)
    with volume.open("rb") as stream:
        if stream.read(11)[3:] != b"NTFS    ":
            raise RuntimeError("Last partition is not the portable Windows NTFS volume")
    read = subprocess.run(["ntfscat", str(volume), "/Windows/Panther/unattend.xml"], capture_output=True)
    tree = ET.fromstring(read.stdout) if read.returncode == 0 else ET.Element(f"{{{windows.UNATTEND}}}unattend")
    answer = work / "test-unattend.xml"
    answer.write_bytes(test_answer(tree, architecture))
    core.run_tool(["ntfscp", str(volume), str(answer), "/Windows/Panther/unattend.xml"])
    with volume.open("rb") as data, image.open("r+b") as target:
        copy_region(data.fileno(), target.fileno(), 0, offset, length)
        os.fsync(target.fileno())
    return image, volume, offset, length


class QMP:
    """One connection for commands and events; QEMU accepts one monitor client."""
    def __init__(self, path, events=None):
        self.path, self.events = path, events
        self.connection = self.stream = None
        self.serial = 0

    def close(self):
        if self.stream is not None:
            self.stream.close()
        if self.connection is not None:
            self.connection.close()
        self.connection = self.stream = None

    def _request(self, command, arguments):
        number = self.serial
        self.serial += 1
        self.stream.write(json.dumps({"execute": command, "arguments": arguments or {}, "id": number}).encode() + b"\n")
        while True:
            line = self.stream.readline(1024 * 1024)
            if not line:
                raise OSError("QEMU closed its control connection")
            response = json.loads(line)
            if "event" in response and self.events:
                with self.events.open("a") as log:
                    log.write(json.dumps(response) + "\n")
            if response.get("id") == number:
                return response

    def command(self, command, arguments=None):
        try:
            if self.stream is None:
                self.connection = socket.socket(socket.AF_UNIX)
                self.connection.settimeout(15)
                self.connection.connect(str(self.path))
                self.stream = self.connection.makefile("rwb", buffering=0)
                if "QMP" not in json.loads(self.stream.readline(1024 * 1024)):
                    raise ValueError("Invalid QEMU control greeting")
                enabled = self._request("qmp_capabilities", None)
                if "error" in enabled:
                    return enabled
            return self._request(command, arguments)
        except (OSError, ValueError) as exc:
            self.close()
            return {"error": {"desc": str(exc)}}


def qmp_command(path, command, arguments=None):
    if isinstance(path, QMP):
        return path.command(command, arguments)
    client = QMP(path)
    try:
        return client.command(command, arguments)
    finally:
        client.close()


def screenshot(path, destination):
    qmp_command(path, "screendump", {"filename": str(destination)})


def console_probe(path, work):
    """Query the live OS through Setup's diagnostic console (US keyboard).

    This changes no registry values and runs only in a disposable test VM.
    Setup can defer console startup; queued keys may take time to execute.
    """
    marker = r"%SystemDrive%\rufus-wtg-test.txt"
    commands = [f"ver >{marker}", f"echo %SystemRoot% >>{marker}",
                f"echo RUFUS_WINDOWS_TOGO_BOOT_OK>>{marker}",
                rf"reg query hklm\system\currentcontrolset\services\partmgr\parameters /v sanpolicy >>{marker}",
                rf"reg query hklm\system\currentcontrolset\control /v portableoperatingsystem >>{marker}"]
    plain = {" ": "spc", "\\": "backslash", "/": "slash", ".": "dot", "-": "minus", "\n": "ret"}
    shift = {">": "dot", "_": "minus", "%": "5"}
    client = path if isinstance(path, QMP) else QMP(path)
    def keys(values):
        result = client.command("send-key", {"keys": [{"type": "qcode", "data": value} for value in values],
                                               "hold-time": 30})
        if result is None or "error" in result:
            raise RuntimeError(f"Cannot send the Windows diagnostic-console probe: {result}")
    try:
        for command in commands:
            for char in command + "\n":
                key = shift.get(char) or plain.get(char) or char.lower()
                keys((["shift"] if char in shift or char.isupper() else []) + [key])
                time.sleep(0.075)
            time.sleep(0.4)
    finally:
        if client is not path:
            client.close()
    (work / "console-probe.json").write_text(json.dumps({
        "method": "live Windows setup diagnostic console via emulated US keyboard", "commands": commands}, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW artifact directory")
    parser.add_argument("--architecture", choices=("amd64", "x86", "arm64"), default="amd64")
    parser.add_argument("--firmware", choices=("bios", "uefi", "both"), default="uefi")
    parser.add_argument("--timeout", type=int, default=2400)
    parser.add_argument("--usb", action="store_true", help="boot the private test disk through virtual USB storage")
    parser.add_argument("--console-probe", action="store_true",
                        help="also query live policies through Setup's diagnostic prompt (x86, US keyboard)")
    parser.add_argument("--console-delay", type=int, default=360, help="seconds before opening the optional diagnostic console")
    parser.add_argument("--console-warmup", type=int, default=120, help="seconds to allow the diagnostic console to initialize")
    parser.add_argument("--keep-images", action="store_true", help="retain successful private test disks as well as diagnostics")
    args = parser.parse_args()
    if args.console_probe and (args.architecture == "arm64" or args.console_delay < 30 or args.console_warmup < 30):
        parser.error("Diagnostic-console probing requires x86/amd64 and delays of at least 30 seconds")
    root = args.output_dir.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    before = args.image.stat()
    modes = ("bios", "uefi") if args.firmware == "both" else (args.firmware,)
    results = {}
    for mode in modes:
        work = root / mode
        work.mkdir()
        print(f"Preparing private {mode} Windows boot/policy test copy", flush=True)
        image, volume, offset, length = instrument(args.image.resolve(), work, args.architecture)
        command = windows_togo.vm_command(image, args.architecture, mode, work)
        # Fresh Windows images can reboot while installing virtual hardware
        # before specialize reaches our policy recording commands.
        command.remove("-no-reboot")
        if args.usb and args.architecture != "arm64":
            for index, value in enumerate(command):
                if value.startswith("if=ide,format=raw,file="):
                    command[index] = value.replace("if=ide,", "if=none,id=stick,", 1)
                    break
            command += ["-device", "qemu-xhci", "-device", "usb-storage,drive=stick"]
        socket_path = work / "qmp.sock"
        command += ["-qmp", f"unix:{socket_path},server=on,wait=off"]
        qmp = QMP(socket_path, work / "events.jsonl")
        start, last_frame, opened, probed = time.monotonic(), 0, None, False
        def progress(stage, done, total):
            nonlocal last_frame, opened, probed
            now = time.monotonic()
            if args.console_probe and opened is None and now - start >= args.console_delay:
                print(f"{mode}: opening the Windows diagnostic console", flush=True)
                result = qmp_command(qmp, "send-key", {"keys": [
                    {"type": "qcode", "data": "shift"}, {"type": "qcode", "data": "f10"}], "hold-time": 100})
                if result is None or "error" in result:
                    raise RuntimeError("Cannot open the Windows diagnostic console")
                opened = now
            if opened is not None and not probed and now - opened >= args.console_warmup:
                print(f"{mode}: querying live policies through the Windows diagnostic console", flush=True)
                console_probe(qmp, work)
                probed = True
            if now - last_frame > 30:
                screenshot(qmp, work / "latest-frame.ppm")
                evidence = resident_root_file(image, offset, "rufus-wtg-test.txt")
                if evidence and valid_evidence(evidence):
                    # The test image is disposable. Stop only after real Windows
                    # wrote both registry values; no shutdown inside specialize.
                    qmp_command(qmp, "quit")
                else:
                    print(f"{mode}: waiting for live Windows policy evidence ({int(now - start)}s)", flush=True)
                last_frame = now
        print(f"Booting real Windows via {mode} {'USB' if args.usb else 'disk'} media", flush=True)
        try:
            with (work / "qemu.log").open("xb") as log:
                windows_togo.run_vm(command, log, args.timeout, progress=progress)
        finally:
            qmp.close()
        evidence = resident_root_file(image, offset, "rufus-wtg-test.txt")
        if evidence is None:
            with image.open("rb") as data, volume.open("r+b") as target:
                copy_region(data.fileno(), target.fileno(), offset, 0, length)
            evidence = subprocess.check_output(["ntfscat", str(volume), "/rufus-wtg-test.txt"])
        (work / "boot-evidence.txt").write_bytes(evidence)
        if not valid_evidence(evidence):
            raise AssertionError(f"Portable Windows did not verify its policies; inspect {work}")
        results[mode] = {"result": "booted real Windows and verified portable policies",
                         "probe": "setup diagnostic console" if (work / "console-probe.json").exists() else "specialize answer file"}
        print(f"PASS: {mode} Windows startup and SAN/portable flags", flush=True)
        if not args.keep_images:
            image.unlink()
            volume.unlink()
    after = args.image.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise AssertionError("Original image changed during the boot test")
    (root / "result.json").write_text(json.dumps({"image": str(args.image.resolve()), "tests": results,
        "virtual_usb": args.usb, "original_modified": False,
        "instrumentation": "private specialize answer file and optional diagnostic console"}, indent=2) + "\n")


if __name__ == "__main__":
    main()
