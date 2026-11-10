import contextlib
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch
from types import SimpleNamespace

from rufus_linux import backup, core, devices, images
from rufus_linux.cli import main


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = bytes(range(256)) * 4096
        self.source = self.root / "source.img"
        self.source.write_bytes(self.data)
        self.output = self.root / "output.img"

    def tearDown(self):
        self.temp.cleanup()

    def test_four_hashes_match_independent_known_values(self):
        self.source.write_bytes(b"abc")
        result = core.checksums(self.source)
        self.assertEqual(result["md5"], "900150983cd24fb0d6963f7d28e17f72")
        self.assertEqual(result["sha1"], "a9993e364706816aba3e25717850c26c9cd0d89d")
        self.assertEqual(result["sha256"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(result["sha512"], hashlib.sha512(b"abc").hexdigest())

    def test_all_hashes_use_one_source_read(self):
        with patch.object(core, "source_file", wraps=core.source_file) as opened:
            result = core.checksums(self.source)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(len(result), 4)

    def test_checksum_cancellation_and_changed_source_are_errors(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(core.Cancelled):
            core.checksums(self.source, cancel=cancel)
        def progress(*_):
            self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(core.RufusError, "changed"):
            core.checksums(self.source, progress=progress)

    def test_checksum_cli_all_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["checksum", str(self.source), "--all", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), core.checksums(self.source))

    def test_zip_roundtrip_checks_download_hash_and_never_extracts_names(self):
        archive = self.root / "payload.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            zipped.writestr("../../must-not-be-extracted.img", self.data)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        core.write_to_file(archive, self.output, expected=digest)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.assertFalse((self.root.parent / "must-not-be-extracted.img").exists())

    def test_ambiguous_zip_rejected_before_output(self):
        archive = self.root / "payload.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("first.img", b"first")
            zipped.writestr("second.img", b"second")
        with self.assertRaisesRegex(core.RufusError, "exactly one"):
            core.write_to_file(archive, self.output)
        self.assertFalse(self.output.exists())

    def test_zip_symlink_is_not_a_disk_image(self):
        archive = self.root / "payload.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            info = zipfile.ZipInfo("image.img")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zipped.writestr(info, "/etc/passwd")
        with self.assertRaisesRegex(core.RufusError, "link/special"):
            core.write_to_file(archive, self.output)

    def test_zip_crc_failure_never_creates_output(self):
        archive = self.root / "payload.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zipped:
            zipped.writestr("disk.img", self.data)
        encoded = bytearray(archive.read_bytes())
        encoded[40] ^= 1
        archive.write_bytes(encoded)
        with self.assertRaisesRegex(core.RufusError, "decompress"):
            core.write_to_file(archive, self.output)
        self.assertFalse(self.output.exists())

    def test_lzma_alone_roundtrip(self):
        source = self.root / "disk.lzma"
        source.write_bytes(lzma.compress(self.data, format=lzma.FORMAT_ALONE))
        core.write_to_file(source, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)

    @unittest.skipUnless(shutil.which("gzip"), "gzip is not installed")
    def test_unix_compress_literal_code_fixture(self):
        # A short standard .Z stream using only 9-bit literal LZW codes.
        # It stays below the dictionary-size transition and uses no CLEAR code.
        data = bytes(range(100))
        packed, bits, value = bytearray(b"\x1f\x9d\x90"), 0, 0
        for code in data:
            value |= code << bits
            bits += 9
            while bits >= 8:
                packed.append(value & 255)
                value >>= 8
                bits -= 8
        if bits:
            packed.append(value & 255)
        source = self.root / "disk.Z"
        source.write_bytes(packed)
        core.write_to_file(source, self.output)
        self.assertEqual(self.output.read_bytes(), data)

    def test_corrupt_archive_headers_are_not_treated_as_raw(self):
        for suffix in (".gz", ".bz2", ".xz", ".lzma", ".zip", ".zst", ".Z", ".7z", ".rar"):
            source = self.root / ("broken" + suffix)
            source.write_bytes(b"This is not a valid compressed image.")
            with self.subTest(suffix=suffix), self.assertRaises(core.RufusError):
                core.write_to_file(source, self.output)
            self.assertFalse(self.output.exists())

    @unittest.skipUnless(shutil.which("zstd"), "zstd encoder not installed")
    def test_zstandard_roundtrip_and_truncation(self):
        source = self.root / "disk.zst"
        subprocess.run(["zstd", "-q", str(self.source), "-o", str(source)], check=True)
        core.write_to_file(source, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)
        self.output.unlink()
        source.write_bytes(source.read_bytes()[:-8])
        with self.assertRaises(core.RufusError):
            core.write_to_file(source, self.output)
        self.assertFalse(self.output.exists())

    def test_raw_and_fixed_vhd_backup_restore_roundtrip(self):
        for fmt in ("raw", "vhd"):
            with self.subTest(format=fmt):
                saved = self.root / f"saved.{fmt}"
                restored = self.root / f"restored-{fmt}.img"
                digest = backup.backup_file(self.source, saved, fmt)
                self.assertEqual(digest, hashlib.sha256(self.data).hexdigest())
                core.write_to_file(saved, restored)
                self.assertEqual(restored.read_bytes(), self.data)
                self.assertEqual(saved.stat().st_size, len(self.data) + (512 if fmt == "vhd" else 0))

    def test_gzip_wrapped_vhd_restores_disk_not_footer(self):
        vhd = self.root / "disk.vhd"
        backup.backup_file(self.source, vhd, "vhd")
        compressed = self.root / "disk.vhd.gz"
        compressed.write_bytes(gzip.compress(vhd.read_bytes()))
        core.write_to_file(compressed, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)

    def test_bad_vhd_footer_is_rejected_before_output(self):
        source = self.root / "disk.vhd"
        backup.backup_file(self.source, source, "vhd")
        data = bytearray(source.read_bytes())
        data[-10] ^= 1
        source.write_bytes(data)
        with self.assertRaisesRegex(core.RufusError, "checksum"):
            core.write_to_file(source, self.output)
        self.assertFalse(self.output.exists())

    def test_unsupported_container_is_never_written_as_raw(self):
        self.source.write_bytes(b"\0" * 4 + b"SignedImage " + b"\0" * 4096)
        with self.assertRaisesRegex(core.RufusError, "not supported"):
            core.write_to_file(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_backup_existing_file_and_link_are_protected(self):
        precious = self.root / "precious"
        precious.write_bytes(b"preserve")
        for output in (precious, self.output):
            if output == self.output:
                output.symlink_to(precious)
            with self.assertRaises(FileExistsError):
                backup.backup_file(self.source, output)
            self.assertEqual(precious.read_bytes(), b"preserve")

    def test_backup_cancellation_removes_partial_output(self):
        cancel = threading.Event()
        def progress(stage, *_):
            if stage == "Saving drive image":
                cancel.set()
        with self.assertRaises(core.Cancelled):
            backup.backup_file(self.source, self.output, cancel=cancel, progress=progress)
        self.assertFalse(self.output.exists())

    def test_backup_existing_output_does_not_contact_udisks(self):
        self.output.write_bytes(b"preserve")
        with patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "already exists"):
                devices.backup_device(None, self.output)
            service.assert_not_called()

    def test_vhd_capacity_failure_does_not_request_authentication(self):
        with patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "2040 GiB"):
                devices.backup_device(SimpleNamespace(size=3 * 1024**4), self.output, "vhd")
            service.assert_not_called()

    def test_backup_cli_roundtrip(self):
        vhd = self.root / "saved.vhd"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["backup", "--source", str(self.source), "--output", str(vhd), "--format", "vhd"]), 0)
            self.assertEqual(main(["write", str(vhd), "--output", str(self.output)]), 0)
        self.assertEqual(self.output.read_bytes(), self.data)

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not available")
    def test_qemu_can_read_our_fixed_vhd(self):
        vhd = self.root / "disk.vhd"
        backup.backup_file(self.source, vhd, "vhd")
        subprocess.run(["qemu-img", "convert", "-f", "vpc", "-O", "raw", str(vhd), str(self.output)], check=True)
        self.assertEqual(self.output.read_bytes(), self.data)

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not available")
    def test_dynamic_vhd_from_qemu_roundtrip(self):
        vhd = self.root / "dynamic.vhd"
        subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "vpc", "-o", "force_size=on",
                        str(self.source), str(vhd)], check=True)
        core.write_to_file(vhd, self.output)
        self.assertEqual(self.output.read_bytes(), self.data)

    @unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is not available")
    def test_dynamic_vhd_unallocated_blocks_are_zero(self):
        self.source.write_bytes(self.data + b"\0" * (5 * 1024 * 1024) + self.data)
        self.dynamic_vhd_roundtrip_expected_source()

    def dynamic_vhd_roundtrip_expected_source(self):
        if not shutil.which("qemu-img"):
            self.skipTest("qemu-img is not available")
        vhd = self.root / "sparse.vhd"
        subprocess.run(["qemu-img", "convert", "-f", "raw", "-O", "vpc", "-o", "force_size=on",
                        str(self.source), str(vhd)], check=True)
        core.write_to_file(vhd, self.output)
        self.assertEqual(self.output.read_bytes(), self.source.read_bytes())


if __name__ == "__main__":
    unittest.main()
