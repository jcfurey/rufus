import contextlib
from dataclasses import replace
import io
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zlib

from rufus_linux import core, devices, formatting
from rufus_linux.cli import main

MIB = 1024**2


class FormattingSafetyTests(unittest.TestCase):
    def test_invalid_geometry_and_labels_fail_before_device_or_output_access(self):
        device = devices.Device("/dev/TEST_ONLY", 64 * MIB, "Test", "", "999:999", (), ())
        cases = [("fat16", "RUFUS", replace(device, size=4 * 1024**3)),
                 ("udf", "bad\x7flabel", device), ("fat16", "bad.label", device),
                 ("udf", "a" * 127, device), ("udf", "漢" * 64, device),
                 ("udf", "😀", device), ("udf", "RUFUS", replace(device, logical_sector=8192)),
                 ("vfat", "RUFUS", replace(device, logical_sector=4096)),
                 ("vfat", "RUFUS", replace(device, size=(2**32 + 4096) * 512)),
                 ("udf", "RUFUS", replace(device, size=(2**32 + 4096) * 512))]
        for filesystem, label, candidate in cases:
            with self.subTest(filesystem=filesystem, label=label), patch.object(devices, "UDisks") as service, \
                    patch.object(devices, "guard") as guard:
                with self.assertRaises(core.RufusError):
                    devices.format_device(candidate, filesystem, label)
                guard.assert_not_called()
                service.assert_not_called()

    def test_sector_aware_mbr_capacity_and_unicode_udf_labels(self):
        formatting.plan_format(3 * 1024**4, "ntfs", table="dos", sector=4096)
        with self.assertRaisesRegex(core.RufusError, "MBR"):
            formatting.plan_format(3 * 1024**4, "ntfs", table="dos")
        for label in ("é" * 126, "漢" * 63):
            self.assertEqual(formatting.plan_format(64 * MIB, "udf", label).label, label)

    def service(self, available=True):
        service = devices.UDisks.__new__(devices.UDisks)
        service.GLib = SimpleNamespace(Variant=lambda signature, value: value)
        service.call = MagicMock(side_effect=lambda path, interface, method, *_:
                                 ((available, "mkudffs"),) if method == "CanFormat" else ("/partition",))
        service.object_for = MagicMock(return_value="/drive")
        service.check_system_hint = MagicMock()
        service.ensure_unmounted = MagicMock()
        return service

    def test_daemon_missing_tool_or_backend_never_unmounts_or_erases(self):
        for error in (False, core.RufusError("Unsupported filesystem")):
            service = self.service(available=False)
            if error:
                service.call.side_effect = error
            device = devices.Device("/dev/TEST_ONLY", 64 * MIB, "Test", "", "999:999", (), ())
            with patch.object(formatting.shutil, "which", return_value="/local/mkudffs"), \
                    patch.object(devices, "guard") as guard:
                with self.assertRaisesRegex(core.RufusError, "UDisks cannot format"):
                    service.format(device, "udf", "RUFUS", "gpt")
                guard.assert_not_called()
            service.ensure_unmounted.assert_not_called()
            service.object_for.assert_not_called()
            self.assertEqual(service.call.call_count, 1)

    def test_udisks_uses_vfat_backend_with_explicit_fat16_and_lba_type(self):
        for filesystem, sector, table in (("fat16", 512, "dos"), ("fat16", 4096, "gpt"),
                                          ("vfat", 4096, "dos"), ("udf", 4096, "gpt")):
            service = self.service()
            device = devices.Device("/dev/TEST_ONLY", 512 * MIB, "Test", "", "999:999", (), (), logical_sector=sector)
            with patch.object(formatting.shutil, "which", return_value="/not-executed"), \
                    patch.object(devices, "guard", return_value=device):
                self.assertEqual(service.format(device, filesystem, "test", table), "/partition")
            calls = service.call.call_args_list
            self.assertEqual([call.args[2] for call in calls], ["CanFormat", "Format", "CreatePartitionAndFormat"])
            offset, length, kind, _, _, backend, options = calls[-1].args[4]
            self.assertEqual((offset, length), (MIB, 510 * MIB))
            self.assertEqual(backend, "vfat" if filesystem == "fat16" else filesystem)
            if filesystem == "fat16":
                self.assertIn("16", options["mkfs-args"])
                self.assertEqual(options["label"], "TEST")
                if table == "dos":
                    self.assertEqual(kind, "0x0e")
            if filesystem == "udf":
                self.assertIn("--blocksize=4096", options["mkfs-args"])

    def test_drive_geometry_cannot_be_overridden_from_cli(self):
        with patch("rufus_linux.cli.discover_devices") as discover, contextlib.redirect_stderr(io.StringIO()):
            for option in (("--sector-size", "512"), ("--size-mib", "64")):
                self.assertEqual(main(["format", "--device", "/dev/sdz", "--confirm", "/dev/sdz", *option]), 1)
            self.assertEqual(main(["format", "--device", "/dev/sdz"]), 1)
            discover.assert_not_called()


@unittest.skipUnless(all(shutil.which(tool) for tool in ("mkfs.vfat", "mkudffs", "udfinfo", "blkid")),
                     "dosfstools, udftools and util-linux are required")
class FormattingImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def probe(self, image):
        result = subprocess.run(["blkid", "-d", "-p", "-O", str(MIB), "-o", "export", str(image)],
                                check=True, text=True, capture_output=True)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def test_fat16_and_udf_mbr_gpt_and_512_4kn_geometry(self):
        for filesystem in ("fat16", "udf"):
            for table in ("dos", "gpt"):
                for sector in (512, 1024, 2048, 4096):
                    with self.subTest(filesystem=filesystem, table=table, sector=sector):
                        image = self.root / f"{filesystem}-{table}-{sector}.img"
                        digest = formatting.create_formatted_image(image, 64 * MIB, filesystem, "RUFUS_TEST", table, sector)
                        self.assertEqual(digest, core.checksum(image))
                        info = self.probe(image)
                        self.assertEqual(info["TYPE"], "vfat" if filesystem == "fat16" else filesystem)
                        self.assertEqual(info["VERSION"], "FAT16" if filesystem == "fat16" else "2.01")
                        self.assertEqual(info["BLOCK_SIZE"], str(sector))
                        self.assertEqual(info["LABEL"], "RUFUS_TEST")
                        with image.open("rb") as stream:
                            master = stream.read(512)
                            if table == "dos":
                                self.assertEqual(master[450], 0x0e if filesystem == "fat16" else 0x07)
                                self.assertEqual(struct.unpack_from("<II", master, 454), (MIB // sector, 62 * MIB // sector))
                            else:
                                stream.seek(sector)
                                header = bytearray(stream.read(92))
                                stored = struct.unpack_from("<I", header, 16)[0]
                                struct.pack_into("<I", header, 16, 0)
                                self.assertEqual(zlib.crc32(header), stored)
                                self.assertEqual(header[:8], b"EFI PART")
                                backup = struct.unpack_from("<Q", header, 32)[0]
                                stream.seek(backup * sector)
                                self.assertEqual(stream.read(8), b"EFI PART")

    def test_other_filesystems_still_format_and_fat32_4kn_has_valid_cluster_count(self):
        for filesystem in ("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4"):
            if not shutil.which(formatting.FS_TOOLS[filesystem]):
                continue
            for sector in ((512,) if filesystem == "exfat" else (512, 4096)):
                with self.subTest(filesystem=filesystem, sector=sector):
                    image = self.root / f"{filesystem}-{sector}.img"
                    size = 512 * MIB if filesystem == "vfat" else 64 * MIB
                    formatting.create_formatted_image(image, size, filesystem, sector=sector)
                    info = self.probe(image)
                    self.assertEqual(info["TYPE"], filesystem)
                    if filesystem == "vfat":
                        self.assertEqual(info["VERSION"], "FAT32")
                        self.assertEqual(info["BLOCK_SIZE"], str(sector))

    def test_udf_label_roundtrips_without_utf8_byte_truncation(self):
        for index, label in enumerate(("é" * 126, "漢" * 63, "Long logical volume identifier" * 3)):
            image = self.root / f"unicode-{index}.img"
            formatting.create_formatted_image(image, 64 * MIB, "udf", label)
            self.assertEqual(self.probe(image)["LABEL"], label)

    def test_fat16_three_gib_uses_64k_clusters(self):
        plan = formatting.plan_format(3 * 1024**3, "fat16")
        volume = self.root / "large-fat16.img"
        with volume.open("xb") as stream:
            stream.truncate(plan.partition.sectors * plan.sector)
        core.run_tool(formatting.volume_command(plan, volume))
        with volume.open("rb") as stream:
            sector = stream.read(64)
        self.assertEqual(struct.unpack_from("<H", sector, 11)[0] * sector[13], 65536)
        core.run_tool(["fsck.fat", "-n", str(volume)])

    def test_fat32_4kn_minimum_has_valid_clusters_and_smaller_sizes_fail_preflight(self):
        sector = 4096
        minimum = 65525 + 32 + 2 * ((65527 * 4 + sector - 1) // sector)
        size = minimum * sector + 2 * MIB
        with self.assertRaisesRegex(core.RufusError, "FAT32 needs more space"):
            formatting.plan_format(size - sector, sector=sector)
        plan = formatting.plan_format(size, sector=sector)
        volume = self.root / "minimum-fat32.img"
        with volume.open("xb") as stream:
            stream.truncate(plan.partition.sectors * sector)
        core.run_tool(formatting.volume_command(plan, volume))
        core.run_tool(["fsck.fat", "-n", str(volume)])

    def test_cli_file_format_never_discovers_or_opens_a_drive(self):
        image = self.root / "from-cli.img"
        with patch("rufus_linux.cli.discover_devices") as discover, patch.object(devices, "UDisks") as service, \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["format", "--output", str(image), "--size-mib", "64", "--filesystem", "udf",
                                   "--sector-size", "4096"]), 0)
            self.assertIn(core.checksum(image), output.getvalue())
            discover.assert_not_called()
            service.assert_not_called()

    def test_existing_output_symlinks_cancellation_and_tool_failure_are_safe(self):
        image = self.root / "protected.img"
        image.write_bytes(b"keep this")
        link = self.root / "link.img"
        link.symlink_to(image)
        for output in (image, link):
            with self.assertRaisesRegex(core.RufusError, "already exists"):
                formatting.create_formatted_image(output, 64 * MIB, "udf")
        self.assertEqual(image.read_bytes(), b"keep this")
        cancel = threading.Event()
        def progress(stage, *_):
            if stage == "Calculating checksums":
                cancel.set()
        cancelled = self.root / "cancelled.img"
        with self.assertRaises(core.Cancelled):
            formatting.create_formatted_image(cancelled, 64 * MIB, "udf", cancel=cancel, progress=progress)
        self.assertFalse(cancelled.exists())
        failed = self.root / "failed.img"
        with patch.object(formatting, "run_tool", side_effect=core.RufusError("formatter failed")):
            with self.assertRaisesRegex(core.RufusError, "formatter failed"):
                formatting.create_formatted_image(failed, 64 * MIB, "udf")
        self.assertFalse(failed.exists())
