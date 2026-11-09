import bz2
import gzip
import hashlib
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from rufus_linux import core


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = bytes(range(256)) * 20001  # Crosses a 4 MiB chunk boundary.
        self.source = self.root / "source.img"
        self.source.write_bytes(self.data)
        self.output = self.root / "written.img"

    def tearDown(self):
        self.temp.cleanup()

    def test_raw_image_write_and_readback(self):
        stages = []
        result = core.write_to_file(self.source, self.output,
                                   progress=lambda stage, *_: stages.append(stage))
        self.assertEqual(result, hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertIn("Verifying written image", stages)

    def test_all_compression_formats_use_download_checksum(self):
        for name, compress in (("gzip", gzip.compress), ("xz", lzma.compress), ("bzip2", bz2.compress)):
            with self.subTest(name=name):
                compressed = self.root / name
                compressed.write_bytes(compress(self.data))
                output = self.root / f"{name}.img"
                result = core.write_to_file(compressed, output,
                    expected=hashlib.sha256(compressed.read_bytes()).hexdigest())
                self.assertEqual(result, hashlib.sha256(self.data).hexdigest())
                self.assertEqual(output.read_bytes(), self.data)

    def test_checksum_mismatch_never_creates_destination(self):
        with self.assertRaisesRegex(core.RufusError, "SHA-256 mismatch"):
            core.write_to_file(self.source, self.output, expected="0" * 64)
        self.assertFalse(self.output.exists())

    def test_existing_destination_is_never_overwritten(self):
        self.output.write_bytes(b"precious data")
        with self.assertRaises(FileExistsError):
            core.write_to_file(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), b"precious data")

    def test_symlink_destination_is_never_followed(self):
        precious = self.root / "precious"
        precious.write_bytes(b"preserve me")
        self.output.symlink_to(precious)
        with self.assertRaises(FileExistsError):
            core.write_to_file(self.source, self.output)
        self.assertEqual(precious.read_bytes(), b"preserve me")

    def test_self_write_is_rejected(self):
        with self.assertRaises(FileExistsError):
            core.write_to_file(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_empty_image_rejected(self):
        self.source.write_bytes(b"")
        with self.assertRaisesRegex(core.RufusError, "nonempty"):
            core.write_to_file(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_corrupt_compressed_images_fail_before_destination(self):
        for compress in (gzip.compress, lzma.compress, bz2.compress):
            self.source.write_bytes(compress(self.data)[:-9])
            with self.assertRaises(core.RufusError):
                core.write_to_file(self.source, self.output)
            self.assertFalse(self.output.exists())

    def test_preflight_cancellation_does_not_create_output(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(core.Cancelled):
            core.write_to_file(self.source, self.output, cancel=cancel)
        self.assertFalse(self.output.exists())

    def test_mid_write_cancellation_removes_partial_test_file(self):
        cancel = threading.Event()
        def progress(stage, *_):
            if stage == "Writing image":
                cancel.set()
        with self.assertRaises(core.Cancelled):
            core.write_to_file(self.source, self.output, cancel=cancel, progress=progress)
        self.assertFalse(self.output.exists())

    def test_capacity_failure_does_not_touch_destination(self):
        self.output.write_bytes(b"sentinel")
        with core.prepare_image(self.source) as image, self.output.open("r+b") as output:
            with self.assertRaisesRegex(core.RufusError, "target has"):
                core.write_image(image, output.fileno(), 10)
        self.assertEqual(self.output.read_bytes(), b"sentinel")

    def test_short_writes_are_retried(self):
        original_write = os.write
        def short_write(fd, data):
            return original_write(fd, data[:8191])
        with patch("rufus_linux.core.os.write", side_effect=short_write):
            core.write_to_file(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_readback_corruption_is_detected(self):
        original_read = os.read
        def bad_read(fd, size):
            data = original_read(fd, size)
            return bytes([data[0] ^ 1]) + data[1:] if data else data
        with patch("rufus_linux.core.os.read", side_effect=bad_read):
            with self.assertRaisesRegex(core.RufusError, "verification failed"):
                core.write_to_file(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_nonhybrid_iso_is_not_silently_written_as_unbootable_media(self):
        data = bytearray(65536)
        data[32769:32774] = b"CD001"
        self.source.write_bytes(data)
        with self.assertRaisesRegex(core.RufusError, "DD mode"):
            core.write_to_file(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_hybrid_iso_is_accepted(self):
        data = bytearray(65536)
        data[32769:32774] = b"CD001"
        data[510:512] = b"\x55\xaa"
        data[450] = 0x17
        self.source.write_bytes(data)
        core.write_to_file(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), data)

    def test_tail_signature_clear_keeps_written_image_intact(self):
        capacity = len(self.data) + 2 * 1024 * 1024
        with self.output.open("w+b") as output, core.prepare_image(self.source) as image:
            output.truncate(capacity)
            output.seek(capacity - 8)
            output.write(b"EFI PART")
            output.flush()
            core.write_image(image, output.fileno(), capacity, block=True)
            output.seek(capacity - 8)
            self.assertEqual(output.read(8), b"\0" * 8)
            output.seek(0)
            self.assertEqual(output.read(len(self.data)), self.data)

    def test_invalid_expected_checksum(self):
        for value in ("123", "z" * 64, "../" * 22):
            with self.assertRaises(core.RufusError):
                core.expected_sha256(value)

    def test_source_change_during_preflight_never_opens_output(self):
        changed = False
        def progress(stage, *_):
            nonlocal changed
            if stage == "Checking image" and not changed:
                changed = True
                self.source.write_bytes(b"changed during hashing")
        with self.assertRaisesRegex(core.RufusError, "changed during preflight"):
            core.write_to_file(self.source, self.output, progress=progress)
        self.assertFalse(self.output.exists())

    def test_source_change_at_completion_removes_test_output(self):
        def progress(stage, *_):
            if stage == "Complete":
                self.source.write_bytes(b"changed at completion")
        with self.assertRaisesRegex(core.RufusError, "changed during the operation"):
            core.write_to_file(self.source, self.output, progress=progress)
        self.assertFalse(self.output.exists())


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree"
        (self.tree / "EFI/BOOT").mkdir(parents=True)
        (self.tree / "sources").mkdir()
        (self.tree / "EFI/BOOT/BOOTX64.EFI").write_bytes(b"fixture EFI file, not a real bootloader")
        (self.tree / "sources/boot.wim").write_bytes(b"fixture boot.wim")
        (self.tree / "setup.exe").write_bytes(b"fixture installer")

    def tearDown(self):
        self.temp.cleanup()

    def test_windows_tree_copy_and_verify(self):
        destination = self.root / "destination"
        destination.mkdir()
        files = core.validate_windows_tree(self.tree)
        stages = []
        core.copy_windows_tree(files, destination, progress=lambda stage, *_: stages.append(stage))
        for source, relative, _ in files:
            self.assertEqual(source.read_bytes(), (destination / relative).read_bytes())
        self.assertIn("Verifying Windows installer", stages)

    def test_symlinks_rejected(self):
        (self.tree / "escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(core.RufusError, "link or special"):
            core.validate_windows_tree(self.tree)

    def test_directory_symlinks_rejected(self):
        (self.tree / "escape").symlink_to("/tmp", target_is_directory=True)
        with self.assertRaises(core.RufusError):
            core.validate_windows_tree(self.tree)

    def test_fat_case_collisions_rejected(self):
        (self.tree / "SETUP.EXE").write_bytes(b"collision")
        with self.assertRaisesRegex(core.RufusError, "conflicting"):
            core.validate_windows_tree(self.tree)

    def test_missing_bootloader_rejected(self):
        (self.tree / "EFI/BOOT/BOOTX64.EFI").unlink()
        with self.assertRaisesRegex(core.RufusError, "bootloader"):
            core.validate_windows_tree(self.tree)

    def test_non_windows_media_rejected(self):
        (self.tree / "sources/boot.wim").unlink()
        with self.assertRaisesRegex(core.RufusError, "not Windows"):
            core.validate_windows_tree(self.tree)

    def test_fat_incompatible_filename_rejected(self):
        (self.tree / "bad:name").touch()
        with self.assertRaisesRegex(core.RufusError, "incompatible"):
            core.validate_windows_tree(self.tree)

    @unittest.skipUnless(shutil.which("xorriso"), "xorriso is not installed")
    def test_real_iso_extract_copy_and_verify(self):
        iso = self.root / "windows-fixture.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-iso-level", "3", "-J", "-R",
                        "-o", str(iso), str(self.tree)], check=True, capture_output=True)
        staging = self.root / "staging"
        staging.mkdir()
        files = core.stage_windows_iso(iso, staging)
        destination = self.root / "destination"
        destination.mkdir()
        core.copy_windows_tree(files, destination)
        self.assertEqual((destination / "sources/boot.wim").read_bytes(), b"fixture boot.wim")


if __name__ == "__main__":
    unittest.main()
