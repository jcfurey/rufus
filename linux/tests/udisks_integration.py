#!/usr/bin/python3
"""Opt-in real UDisks integration using ONLY a newly allocated loop device.

Never accepts an existing block device. Requires one explicitly elevated run;
unprivileged runs exit before D-Bus access so no Polkit dialogs are generated.
The normal backend deliberately excludes loop devices from its USB selector;
this harness substitutes its own device guard only for the loop it creates.
"""

from pathlib import Path
from dataclasses import replace
import json
import os
import sys
import tempfile
import subprocess
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import devices
from rufus_linux.core import copy_windows_tree, validate_windows_tree


def main():
    if os.geteuid() != 0:
        print("Integration tests need one administrator authorization for the whole run.\n"
              "Run: sudo make -C linux integration\n"
              "No disk operations were attempted. File-backed and GUI tests need no elevation.",
              file=sys.stderr)
        return 77
    service = devices.UDisks()
    Gio, GLib = service.Gio, service.GLib
    loop = None
    partition = None
    mounted = False
    with tempfile.TemporaryDirectory(prefix="rufus-loop-test-") as directory:
        backing = Path(directory) / "disposable.img"
        fd = os.open(backing, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.ftruncate(fd, 256 * 1024 * 1024)
            descriptors = Gio.UnixFDList.new()
            index = descriptors.append(fd)
            result, _ = service.bus.call_with_unix_fd_list_sync(
                "org.freedesktop.UDisks2", "/org/freedesktop/UDisks2/Manager",
                "org.freedesktop.UDisks2.Manager", "LoopSetup",
                GLib.Variant("(ha{sv})", (index, {})), None,
                Gio.DBusCallFlags.NONE, -1, descriptors, None)
            loop = result.unpack()[0]
        finally:
            os.close(fd)
        try:
            interfaces = service.objects()[loop]
            block = interfaces[devices.BLOCK]
            path = bytes(block["Device"]).rstrip(b"\0").decode()
            number = block["DeviceNumber"]
            # Verify the real kernel loop backing file before bypassing discovery.
            actual_backing = Path("/sys/class/block") / Path(path).name / "loop/backing_file"
            if not path.startswith("/dev/loop") or actual_backing.read_text().strip() != str(backing):
                raise RuntimeError("Loop backing file does not match this test. Refusing any write.")
            service.call(loop, "org.freedesktop.UDisks2.Loop", "SetAutoclear", "(ba{sv})", (False, {}))
            key = f"{os.major(number)}:{os.minor(number)}"
            device = devices.Device(path, 256 * 1024 * 1024, "Disposable test loop", "", key, (key,), ())

            def guard_fixture(candidate, source=None):
                if (candidate.identity != device.identity or
                        actual_backing.read_text().strip() != str(backing)):
                    raise RuntimeError("Attempt to write outside this test's disposable loop.")
                # Include newly created partitions and desktop auto-mounts, just
                # as production discovery refreshes them before each operation.
                result = subprocess.run(["lsblk", "--json", "--tree", "--output", "MAJ:MIN,MOUNTPOINTS", path],
                                        check=True, text=True, capture_output=True)
                nodes = list(devices.descendants(json.loads(result.stdout)["blockdevices"][0]))
                return replace(device, nodes=tuple(n["maj:min"] for n in nodes),
                               mounts=tuple(m for n in nodes for m in n.get("mountpoints", []) if m))

            def check_fixture_object(object_path):
                # UDisks flags loop devices as system devices. Bypass that flag
                # only for the exact loop whose backing file we just verified.
                if object_path != loop:
                    raise RuntimeError("Unexpected UDisks object in disposable loop test.")
                guard_fixture(device)

            def unmount_fixture():
                # A desktop indexer can keep a just-created test volume busy.
                # Forced unmount is permitted ONLY in this harness, after
                # validating this exact disposable backing file again.
                for _ in range(30):
                    if not actual_backing.exists():
                        return
                    guard_fixture(device)
                    for name, interfaces in service.objects().items():
                        if name.startswith(loop + 'p') and interfaces.get(devices.FILESYSTEM, {}).get('MountPoints'):
                            service.call(name, devices.FILESYSTEM, "Unmount", "(a{sv})",
                                         ({"force": GLib.Variant("b", True)},))
                    if not guard_fixture(device).mounts:
                        return
                    time.sleep(0.1)
                raise RuntimeError("Could not cleanly release the disposable test volume.")

            # Exercise D-Bus FD passing, write/flush/read-back, and fd closure.
            image = Path(directory) / "source.img"
            image.write_bytes(bytes(range(256)) * 4096)
            with patch.object(devices, "guard", side_effect=guard_fixture), \
                 patch.object(devices.UDisks, "check_system_hint", side_effect=check_fixture_object):
                devices.write_to_device(image, device)
            print("PASS: UDisks OpenDevice + image write/read-back")

            with patch.object(devices, "guard", side_effect=guard_fixture), \
                 patch.object(devices.UDisks, "check_system_hint", side_effect=check_fixture_object):
                for table in ("gpt", "dos"):
                    for filesystem in devices.FS_TOOLS:
                        unmount_fixture()
                        partition = service.format(device, filesystem, "RUFUS_TEST", table)
                        actual = service.objects()[partition][devices.BLOCK]["IdType"]
                        expected = "vfat" if filesystem == "fat16" else filesystem
                        if actual != expected:
                            raise RuntimeError(f"Expected {expected}, found {actual}.")
                        if filesystem == "fat16" and service.objects()[partition][devices.BLOCK]["IdVersion"] != "FAT16":
                            raise RuntimeError("Explicit FAT16 format did not produce FAT16.")
                        print(f"PASS: UDisks {table} + {filesystem}", flush=True)
                unmount_fixture()
                partition = service.format(device, "vfat", "RUFUS_TEST", "gpt", efi=True)
            print("PASS: UDisks GPT + EFI partition + FAT32 format", flush=True)
            destination = service.mount(partition)
            mounted = True
            if os.stat(destination).st_dev != service.objects()[partition][devices.BLOCK]["DeviceNumber"]:
                raise RuntimeError("Unexpected mounted device.")
            tree = Path(directory) / "installer"
            (tree / "efi/boot").mkdir(parents=True)
            (tree / "sources").mkdir()
            (tree / "efi/boot/bootx64.efi").write_bytes(b"TEST FIXTURE - not a bootloader")
            (tree / "sources/boot.wim").write_bytes(b"TEST FIXTURE - not a bootable WIM")
            copy_windows_tree(validate_windows_tree(tree), destination)
            service.call(partition, devices.FILESYSTEM, "Unmount", "(a{sv})", ({},))
            mounted = False
            print("PASS: UDisks mount + FAT32 file copy/read-back + unmount")
            iso = Path(directory) / "windows-fixture.iso"
            subprocess.run(["xorriso", "-as", "mkisofs", "-iso-level", "3", "-J", "-R",
                            "-o", str(iso), str(tree)], check=True, capture_output=True)
            with patch.object(devices, "guard", side_effect=guard_fixture), \
                 patch.object(devices.UDisks, "check_system_hint", side_effect=check_fixture_object):
                unmount_fixture()
                devices.windows_installer(iso, device)
            print("PASS: complete Windows ISO extraction/format/copy/verify workflow", flush=True)
        finally:
            try:
                if 'unmount_fixture' in locals():
                    unmount_fixture()
            finally:
                if loop:
                    try:
                        service.call(loop, "org.freedesktop.UDisks2.Loop", "Delete", "(a{sv})", ({},))
                    except devices.RufusError as exc:
                        if 'No such device or address' not in str(exc):
                            raise
                    print("Deleted disposable loop device.")


if __name__ == "__main__":
    sys.exit(main())
