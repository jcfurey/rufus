import contextlib
from dataclasses import replace
import gzip
import hashlib
import io
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
import zipfile

from rufus_linux import backup, core, devices, images, vhdx
from rufus_linux.cli import main
from test_devices import usb


class VHDXTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rufus-vhdx-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.image, self.output = (self.root / name for name in ("source.img", "disk.vhdx", "restored.img"))
        self.data = b"A" * 4096 + bytes(2 * vhdx.MIB) + b"B" * 4096
        self.source.write_bytes(self.data)

    def create(self, sector=512):
        return backup.backup_file(self.source, self.image, "vhdx", logical_sector=sector)

    def edit(self, offset, data):
        with self.image.open("r+b") as stream:
            stream.seek(offset)
            stream.write(data)

    def header(self, offset, change):
        with self.image.open("rb") as stream:
            stream.seek(offset)
            data = bytearray(stream.read(4096))
        change(data)
        self.edit(offset, vhdx._seal(data))

    def assert_invalid(self, message=None):
        before = hashlib.sha256(self.image.read_bytes()).digest()
        with patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, message or "VHDX"):
                devices.write_to_device(self.image, usb())
            service.assert_not_called()
        self.assertEqual(hashlib.sha256(self.image.read_bytes()).digest(), before)
        self.assertFalse(self.output.exists())

    def test_crc_and_native_backup_restore_for_both_sector_sizes(self):
        self.assertEqual(vhdx.crc32c(b"123456789"), 0xe3069283)
        for sector in (512, 4096):
            with self.subTest(sector=sector):
                self.assertEqual(self.create(sector), hashlib.sha256(self.data).hexdigest())
                before = self.image.stat()
                with core.prepare_image(self.image) as image:
                    self.assertEqual(image.size, len(self.data))
                    self.assertEqual(image.logical_sector, sector)
                    self.assertEqual(image.sha256, hashlib.sha256(self.data).hexdigest())
                    self.assertEqual(image.stream.read(), self.data)
                    image.stream.seek(-4096, os.SEEK_END)
                    self.assertEqual(image.stream.read(), b"B" * 4096)
                core.write_to_file(self.image, self.output)
                self.assertEqual(self.output.read_bytes(), self.data)
                self.assertEqual(self.image.stat().st_mtime_ns, before.st_mtime_ns)
                self.image.unlink()
                self.output.unlink()

    def test_4k_metadata_sector_mismatch_is_rejected_before_drive_authorization(self):
        self.create(4096)
        with patch.object(devices, "guard", return_value=usb()), patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "4096.*512"):
                devices.write_to_device(self.image, usb())
            service.assert_not_called()

    def test_physical_backup_preserves_reported_4k_geometry_and_opens_source_read_only(self):
        device = replace(usb(), logical_sector=4096)
        fd = os.open(self.source, os.O_RDONLY)
        opened = SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 240))
        with patch.object(devices, "guard", return_value=device), patch.object(devices, "UDisks") as service, \
             patch.object(devices.os, "fstat", return_value=opened), patch.object(devices, "disk_capacity", return_value=device.size), \
             patch.object(backup, "save_image", return_value="saved") as save:
            service.return_value.open_disk.return_value = fd
            self.assertEqual(devices.backup_device(device, self.image, "vhdx"), "saved")
            service.return_value.open_disk.assert_called_once_with(device, "r")
            self.assertEqual(save.call_args.kwargs["logical_sector"], 4096)

    def test_fixed_vhdx_padding_space_is_checked_before_requesting_source_access(self):
        device = replace(usb(), size=64 * vhdx.MIB)
        with patch.object(devices, "guard", return_value=device), patch.object(devices, "UDisks") as service, \
             patch.object(devices.shutil, "disk_usage", return_value=SimpleNamespace(free=device.size)):
            with self.assertRaisesRegex(core.RufusError, "enough space"):
                devices.backup_device(device, self.image, "vhdx")
            service.assert_not_called()

    def test_backup_invalid_geometry_and_existing_destinations_are_protected(self):
        for size, sector in ((0, 512), (513, 512), (4097, 4096), (4096, 2048), (vhdx.MAX_SIZE + 4096, 4096)):
            with self.assertRaises(core.RufusError):
                vhdx.fixed_layout(size, sector)
        for output in (self.output, self.image):
            if output == self.output:
                output.write_bytes(b"preserve")
            else:
                output.symlink_to(self.output)
            with self.assertRaises(FileExistsError):
                backup.backup_file(self.source, output, "vhdx")
            self.assertEqual(self.output.read_bytes(), b"preserve")
        with patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "VHD requires 512"):
                devices.backup_device(replace(usb(), logical_sector=4096), self.root / "new.vhd", "vhd")
            service.assert_not_called()

    def test_redundant_headers_and_region_table_recovery(self):
        self.create()
        self.edit(131072 + 100, b"\x01")  # Bad newer checksum: use older header.
        self.edit(192 * 1024 + 100, b"\x01")  # Bad first region table: use second.
        core.write_to_file(self.image, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.output.unlink()
        self.edit(65536 + 100, b"\x01")
        self.assert_invalid("header checksums")

    def test_active_header_sequence_and_pending_log_gate(self):
        self.create()
        self.header(131072, lambda data: data.__setitem__(slice(48, 64), uuid.uuid4().bytes_le))
        self.assert_invalid("log replay")
        self.header(131072, lambda data: data.__setitem__(slice(48, 64), bytes(16)))
        self.header(65536, lambda data: struct.pack_into("<Q", data, 8, 2))
        self.assert_invalid("ambiguous")

    def test_damaged_checkpoint_extension_cannot_fall_back_to_raw_writing(self):
        checkpoint = self.root / "bad.avhdx"
        checkpoint.write_bytes(b"corrupted checkpoint header")
        with self.assertRaisesRegex(core.RufusError, "VHDX"):
            core.write_to_file(checkpoint, self.output)
        self.assertFalse(self.output.exists())

    def test_vhdx_signature_wins_over_vhd_footer_inside_logical_data(self):
        size = vhdx.BLOCK_SIZE
        footer = images.vhd_footer(vhdx.fixed_layout(size)["file_size"] - 512)
        with self.source.open("wb") as source:
            source.truncate(size)
            source.seek(-512, os.SEEK_END)
            source.write(footer)
        backup.backup_file(self.source, self.image, "vhdx")
        with core.prepare_image(self.image) as view:
            self.assertEqual(view.size, size)
            self.assertEqual(view.sha256, core.checksum(self.source))
            view.stream.seek(-512, os.SEEK_END)
            self.assertEqual(view.stream.read(), footer)

    def test_parent_and_required_unknown_metadata_are_rejected_without_following_paths(self):
        self.create()
        layout = vhdx.fixed_layout(len(self.data))
        self.edit(layout["metadata"] + 65540, struct.pack("<I", 3))
        self.assert_invalid("Differencing")
        self.edit(layout["metadata"] + 65540, struct.pack("<I", 1))
        self.edit(layout["metadata"] + 10, struct.pack("<H", 6))
        self.edit(layout["metadata"] + 32 + 5 * 32,
                  struct.pack("<16sIIII", vhdx.PARENT, 65576, 8, 4, 0))
        self.assert_invalid("Differencing")
        self.edit(layout["metadata"] + 32 + 5 * 32,
                  struct.pack("<16sIIII", uuid.uuid4().bytes_le, 65576, 8, 4, 0))
        self.assert_invalid("required VHDX metadata")
        self.edit(layout["metadata"] + 32 + 5 * 32 + 24, struct.pack("<I", 0))
        core.write_to_file(self.image, self.output)  # Unknown optional metadata is safe to ignore.
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_corrupt_regions_and_duplicate_or_overlapping_metadata(self):
        self.create()
        self.edit(192 * 1024, b"bad!")
        self.edit(256 * 1024, b"bad!")
        self.assert_invalid("region table")
        self.image.unlink()
        self.create()
        metadata = vhdx.fixed_layout(len(self.data))["metadata"]
        self.edit(metadata + 32 + 3 * 32, vhdx.DISK_SIZE)
        self.assert_invalid("duplicate")
        self.image.unlink()
        self.create()
        self.edit(metadata + 32 + 3 * 32 + 16, struct.pack("<I", 65536))
        self.assert_invalid("metadata overlaps")

    def test_system_metadata_cannot_be_disguised_as_user_metadata(self):
        self.create()
        metadata = vhdx.fixed_layout(len(self.data))["metadata"]
        self.edit(metadata + 32 + 3 * 32 + 24, struct.pack("<I", 7))
        self.assert_invalid("system metadata flags")

    def test_bad_allocation_states_and_metadata_aliases_are_rejected(self):
        self.create()
        for entry in (4, 5, 7, 6, vhdx.MIB | 6, 4 * vhdx.MIB | 0x16, 128 * vhdx.MIB | 6):
            self.edit(2 * vhdx.MIB, struct.pack("<Q", entry))
            self.assert_invalid()

    def test_legacy_short_final_block_is_read_but_missing_logical_data_is_rejected(self):
        self.create()
        payload = vhdx.fixed_layout(len(self.data))["payload"]
        with self.image.open("r+b") as stream:
            stream.truncate(payload + len(self.data))
        core.write_to_file(self.image, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.output.unlink()
        with self.image.open("r+b") as stream:
            stream.truncate(payload + len(self.data) - 1)
        self.assert_invalid("outside")

    def test_compressed_vhdx_and_download_checksum_are_preserved(self):
        self.create(4096)
        wrapped = self.root / "disk.vhdx.gz"
        wrapped.write_bytes(gzip.compress(self.image.read_bytes()))
        core.write_to_file(wrapped, self.output, hashlib.sha256(wrapped.read_bytes()).hexdigest())
        self.assertEqual(self.output.read_bytes(), self.data)
        self.output.unlink()
        archive = self.root / "disk.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(self.image, "image.VHDX")
        with core.prepare_image(archive) as image:
            self.assertEqual(image.logical_sector, 4096)
            self.assertEqual(image.stream.read(), self.data)

    def test_corrupt_compressed_containers_and_nested_archives_cannot_be_written_as_raw(self):
        payload = b"damaged VHDX signature" + bytes(1024)
        for name, data in (("bad.vhdx.gz", gzip.compress(payload)),
                           ("bad.avhdx.xz", lzma.compress(payload))):
            source = self.root / name
            source.write_bytes(data)
            with self.assertRaisesRegex(core.RufusError, "VHDX"):
                core.write_to_file(source, self.output)
            self.assertFalse(self.output.exists())
        nested = self.root / "disk.tar.gz"
        with tarfile.open(nested, "w:gz") as archive:
            archive.add(self.source, arcname="disk.img")
        with self.assertRaisesRegex(core.RufusError, "archive"):
            core.write_to_file(nested, self.output)
        self.assertFalse(self.output.exists())
        nested = self.root / "nested.zip"
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("disk.img.gz", gzip.compress(self.data))
        with self.assertRaisesRegex(core.RufusError, "archive"):
            core.write_to_file(nested, self.output)
        self.assertFalse(self.output.exists())

    def test_sparse_512_and_4k_chunk_boundaries_and_large_capacity_preflight(self):
        for sector, boundary, left_entry, right_entry in ((512, 4 * 1024**3, 127, 129),
                                                          (4096, 32 * 1024**3, 1023, 1025)):
            image = self.root / f"boundary-{sector}.vhdx"
            size = boundary + vhdx.BLOCK_SIZE
            with image.open("xb") as output:
                vhdx.write_fixed_header(output.fileno(), size, sector)
                output.truncate(68 * vhdx.MIB)
                output.seek(2 * vhdx.MIB)
                output.write(bytes(vhdx.MIB))  # Dynamic, zero/unallocated blocks.
                output.seek(3 * vhdx.MIB + 65540)
                output.write(struct.pack("<I", 0))
                for entry, offset in ((left_entry, 4 * vhdx.MIB), (right_entry, 36 * vhdx.MIB)):
                    output.seek(2 * vhdx.MIB + entry * 8)
                    output.write(struct.pack("<Q", offset | 6))
                output.seek(36 * vhdx.MIB - 4096)
                output.write(b"A" * 4096)
                output.seek(36 * vhdx.MIB)
                output.write(b"B" * 4096)
            with image.open("rb") as source:
                stream = vhdx.VHDXStream(source, image.stat().st_size)
                stream.seek(boundary - 4096)
                self.assertEqual(stream.read(8192), b"A" * 4096 + b"B" * 4096)
                stream.seek(0)
                self.assertEqual(stream.read(8192), bytes(8192))
            if sector == 512 and shutil.which("qemu-io"):
                subprocess.run(["qemu-io", "-r", "-f", "vhdx", "-c", f"read -P 0x41 {boundary - 4096} 4096",
                                "-c", f"read -P 0x42 {boundary} 4096", str(image)], check=True, capture_output=True)
            with patch.object(devices, "UDisks") as service:
                with self.assertRaisesRegex(core.RufusError, "larger"):
                    devices.write_to_device(image, replace(usb(), size=64 * vhdx.MIB))
                service.assert_not_called()

    def test_fixed_writer_preserves_bitmap_gaps_and_multimegabyte_tables(self):
        for sector, boundary in ((512, 4 * 1024**3), (4096, 32 * 1024**3), (512, 4 * 1024**4)):
            image = self.root / f"fixed-{sector}-{boundary}.vhdx"
            with image.open("xb") as output:
                payload = vhdx.write_fixed_header(output.fileno(), boundary + vhdx.BLOCK_SIZE, sector)
                output.seek(payload + boundary - 4096)
                output.write(b"A" * 4096 + b"B" * 4096)
            with image.open("rb") as source:
                view = vhdx.VHDXStream(source, image.stat().st_size)
                view.seek(boundary - 4096)
                self.assertEqual(view.read(8192), b"A" * 4096 + b"B" * 4096)
            if sector == 512 and shutil.which("qemu-io"):
                subprocess.run(["qemu-io", "-r", "-f", "vhdx", "-c", f"read -P 0x41 {boundary - 4096} 4096",
                                "-c", f"read -P 0x42 {boundary} 4096", str(image)], check=True, capture_output=True)

    def test_cancellation_and_source_mutation_remove_incomplete_backups(self):
        for mutate in (False, True):
            cancel = threading.Event()
            def progress(stage, *_):
                if stage == "Saving drive image":
                    if mutate:
                        self.source.write_bytes(self.data[:-4096] + b"X" * 4096)
                    else:
                        cancel.set()
            with self.assertRaises(core.RufusError):
                backup.backup_file(self.source, self.image, "vhdx", cancel=cancel, progress=progress)
            self.assertFalse(self.image.exists())
        cancel = threading.Event()
        self.create()
        def report(stage, *_):
            if stage == "Validating VHDX allocation table":
                cancel.set()
        with self.assertRaises(core.Cancelled):
            core.write_to_file(self.image, self.output, cancel=cancel, progress=report)
        self.assertFalse(self.output.exists())

    def test_cli_backup_restore_and_invalid_sector_override_never_discover_drives(self):
        with patch("rufus_linux.cli.discover_devices") as discover, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["backup", "--source", str(self.source), "--output", str(self.image),
                                   "--format", "vhdx", "--sector-size", "4096"]), 0)
            self.assertEqual(main(["write", str(self.image), "--output", str(self.output)]), 0)
            self.assertEqual(main(["backup", "--device", "/dev/TEST_ONLY", "--format", "vhdx",
                                   "--sector-size", "4096", "--output", str(self.root / "new.vhdx")]), 1)
            discover.assert_not_called()
        self.assertEqual(self.output.read_bytes(), self.data)

    @unittest.skipUnless(shutil.which("qemu-img"), "QEMU interoperability tools are missing")
    def test_independent_qemu_fixed_dynamic_and_native_writer_interoperability(self):
        self.create()
        subprocess.run(["qemu-img", "convert", "-f", "vhdx", "-O", "raw", str(self.image), str(self.output)], check=True)
        self.assertEqual(self.output.read_bytes(), self.data)
        for kind in ("fixed", "dynamic"):
            image = self.root / f"qemu-{kind}.vhdx"
            subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "vhdx",
                            "-o", f"subformat={kind},block_size=1048576", str(self.source), str(image)], check=True)
            self.output.unlink()
            core.write_to_file(image, self.output)
            self.assertEqual(self.output.read_bytes(), self.data)


if __name__ == "__main__":
    unittest.main()
