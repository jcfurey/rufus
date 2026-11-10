from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from rufus_linux import devices
from rufus_linux.core import RufusError


def node(path="/dev/sdz", kind="disk", transport="usb", removable=True, mounts=None,
         number="8:240", children=None, readonly=False):
    return {"path": path, "type": kind, "tran": transport, "rm": removable,
            "size": 8 * 1024**3, "model": "Test USB", "serial": "serial-1",
            "maj:min": number, "mountpoints": mounts or [], "ro": readonly,
            "children": children or []}


def usb():
    return devices.parse_devices({"blockdevices": [node()]})[0]


class DriveSafetyTests(unittest.TestCase):
    def test_device_capacity_uses_native_32_or_64_bit_linux_ioctl_abi(self):
        import struct
        original = struct.calcsize
        for pointer_size, request in ((4, 0x80041272), (8, 0x80081272)):
            with patch.object(devices.struct, "calcsize", side_effect=lambda fmt: pointer_size if fmt == "P" else original(fmt)), \
                 patch.object(devices.fcntl, "ioctl", return_value=struct.pack("Q", 8 * 1024**3)) as ioctl:
                self.assertEqual(devices.disk_capacity(99), 8 * 1024**3)
                ioctl.assert_called_once_with(99, request, b"\0" * 8)

    def test_discovery_explicitly_requests_tree_for_partition_safety(self):
        import json
        result = MagicMock(returncode=0, stdout=json.dumps({"blockdevices": [node(children=[
            node("/dev/sdz1", kind="part", mounts=["/media/user/USB"], number="8:241")])]}))
        with patch.object(devices.subprocess, "run", return_value=result) as run:
            found = devices.discover_devices()
            self.assertIn("--tree", run.call_args.args[0])
            self.assertIn("8:241", found[0].nodes)
            self.assertEqual(found[0].mounts, ("/media/user/USB",))

    def test_internal_disks_and_loop_devices_are_excluded(self):
        data = {"blockdevices": [node("/dev/nvme0n1", transport="nvme", removable=False),
                                  node("/dev/loop0", kind="loop"), node()]}
        self.assertEqual([d.path for d in devices.parse_devices(data)], ["/dev/sdz"])

    def test_usb_ssds_with_removable_bit_unset_are_supported(self):
        found = devices.parse_devices({"blockdevices": [node(removable=False)]})
        self.assertEqual(len(found), 1)

    def test_system_and_swap_mounts_protect_usb_boot_drives(self):
        for mount in ("/", "/home", "/boot/efi", "[SWAP]", "/var", "/mnt"):
            data = {"blockdevices": [node(children=[node("/dev/sdz1", kind="part", mounts=[mount])])]}
            self.assertTrue(devices.parse_devices(data)[0].reason, mount)

    def test_media_mounts_may_be_unmounted(self):
        data = {"blockdevices": [node(children=[node("/dev/sdz1", kind="part", mounts=["/media/user/USB"])])]}
        device = devices.parse_devices(data)[0]
        self.assertFalse(device.reason)
        self.assertEqual(device.mounts, ("/media/user/USB",))

    def test_readonly_drive_rejected(self):
        device = devices.parse_devices({"blockdevices": [node(readonly=True)]})[0]
        self.assertIn("read-only", device.reason)

    def test_layered_storage_rejected(self):
        device = devices.parse_devices({"blockdevices": [node(children=[node(kind="crypt")])]})[0]
        self.assertTrue(device.reason)

    def test_removed_or_replaced_drive_rejected(self):
        device = usb()
        for current in ([], [replace(device, serial="replacement")], [replace(device, size=1024)]):
            with patch.object(devices, "discover_devices", return_value=current):
                with self.assertRaisesRegex(RufusError, "removed or changed"):
                    devices.guard(device)

    def test_source_image_disk_rejected(self):
        device = usb()
        def ancestors(number):
            return {number, device.number}
        with patch.object(devices, "discover_devices", return_value=[device]), \
             patch.object(devices, "storage_numbers", side_effect=ancestors):
            with self.assertRaisesRegex(RufusError, "Refusing to erase"):
                devices.guard(device, __file__)

    def test_checksum_failure_never_opens_a_device(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.img"
            image.write_bytes(b"fixture")
            with patch.object(devices, "UDisks") as service:
                with self.assertRaisesRegex(RufusError, "mismatch"):
                    devices.write_to_device(image, usb(), expected="0" * 64)
                service.assert_not_called()

    def test_corrupt_compressed_image_never_opens_a_device(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.gz"
            image.write_bytes(b"\x1f\x8bcorrupt gzip")
            with patch.object(devices, "UDisks") as service:
                with self.assertRaises(RufusError):
                    devices.write_to_device(image, usb())
                service.assert_not_called()

    def test_windows_staging_failure_never_formats_a_device(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.iso"
            image.write_bytes(b"not an ISO")
            with patch.object(devices, "guard", return_value=usb()), \
                 patch.object(devices, "UDisks") as service:
                with self.assertRaises(RufusError):
                    devices.windows_installer(image, usb())
                service.assert_not_called()

    def test_live_preparation_failure_never_requests_drive_access(self):
        with patch.object(devices, "guard", return_value=usb()), \
             patch("rufus_linux.live.create_live_image", side_effect=RufusError("unsupported ISO")), \
             patch.object(devices, "UDisks") as service, \
             patch.object(devices, "write_to_device") as write:
            with self.assertRaisesRegex(RufusError, "unsupported ISO"):
                devices.live_device("fixture.iso", usb())
            service.assert_not_called()
            write.assert_not_called()

    def test_live_media_rechecks_source_drive_after_preparation(self):
        guards = []
        def guard(device, source):
            guards.append(source)
            if len(guards) == 3:
                raise RufusError("drive replaced")
            return device
        with patch.object(devices, "guard", side_effect=guard), \
             patch("rufus_linux.live.create_live_image", return_value={"sha256": "0" * 64}), \
             patch.object(devices, "write_to_device") as write:
            with self.assertRaisesRegex(RufusError, "drive replaced"):
                devices.live_device("fixture.iso", usb())
            self.assertEqual(guards[0], "fixture.iso")
            self.assertEqual(guards[-1], "fixture.iso")
            write.assert_not_called()

    def test_live_size_and_sector_validation_never_build_or_open_storage(self):
        with patch.object(devices, "guard", return_value=usb()), \
             patch("rufus_linux.live.create_live_image") as build, \
             patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(RufusError, "at least 64 MiB"):
                devices.live_device("fixture.iso", usb(), 63 * 1024**2)
            with self.assertRaisesRegex(RufusError, "512-byte"):
                devices.live_device("fixture.iso", replace(usb(), logical_sector=4096))
            build.assert_not_called()
            service.assert_not_called()

    def test_format_parameters_checked_before_storage_access(self):
        for filesystem, label, table in (("vfat", "TOO-LONG-LABEL", "gpt"),
                                         ("vfat", "bad:name", "gpt"),
                                         ("invalid", "RUFUS", "gpt"),
                                         ("ntfs", "RUFUS", "invalid")):
            with patch.object(devices, "UDisks") as service:
                with self.assertRaises(RufusError):
                    devices.format_device(usb(), filesystem, label, table)
                service.assert_not_called()

    def test_invalid_windows_checksum_rejected_before_storage_access(self):
        with patch.object(devices, "UDisks") as service:
            with self.assertRaises(RufusError):
                devices.windows_installer("nonexistent.iso", usb(), expected="bad checksum")
            service.assert_not_called()

    def test_windows_iso_change_during_extraction_never_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "source.iso"
            image.write_bytes(b"original ISO contents")
            def stage(*_, **_kwargs):
                image.write_bytes(b"replaced during extraction")
                return []
            with patch.object(devices, "guard", return_value=usb()), \
                 patch("rufus_linux.media.stage_windows_iso", side_effect=stage), \
                 patch("rufus_linux.media.shutil.which", return_value="/not-executed"), \
                 patch.object(devices, "UDisks") as service:
                with self.assertRaisesRegex(RufusError, "changed during extraction"):
                    devices.windows_installer(image, usb())
                service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
