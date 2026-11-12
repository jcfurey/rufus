import ctypes
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib

from rufus_linux import assets, boot, core, layout, media


def ntfs_available():
    if not (shutil.which("wimlib-imagex") and shutil.which("mkfs.ntfs") and shutil.which("ntfscat")):
        return False
    try:
        ctypes.CDLL("libwim.so.15")
    except OSError:
        return False
    return True


class LayoutTests(unittest.TestCase):
    def test_gpt_headers_arrays_and_backup_match_independent_sfdisk(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "disk.img"
            size = 128 * boot.MIB
            table, parts = media.windows_partitions(size, "ntfs", "uefi")
            with image.open("w+b") as disk:
                disk.truncate(size)
                layout.write_layout(disk.fileno(), size, parts, table)
                for current, other in ((1, size // 512 - 1), (size // 512 - 1, 1)):
                    disk.seek(current * 512)
                    header = bytearray(disk.read(512))
                    checksum = struct.unpack_from("<I", header, 16)[0]
                    struct.pack_into("<I", header, 16, 0)
                    self.assertEqual(zlib.crc32(header[:92]), checksum)
                    self.assertEqual(struct.unpack_from("<Q", header, 32)[0], other)
            result = subprocess.run(["sfdisk", "--json", str(image)], text=True, capture_output=True, check=True)
            parsed = json.loads(result.stdout)["partitiontable"]
            self.assertEqual(parsed["label"], "gpt")
            self.assertEqual([p["start"] for p in parsed["partitions"]], [p.start for p in parts])
            self.assertEqual([p["size"] for p in parsed["partitions"]], [p.sectors for p in parts])

    def test_overlapping_partitions_are_rejected(self):
        with tempfile.TemporaryFile() as disk:
            with self.assertRaisesRegex(core.RufusError, "overlap"):
                layout.write_layout(disk.fileno(), 64 * boot.MIB,
                                    [layout.Partition(2048, 4096), layout.Partition(4096, 4096)])


@unittest.skipUnless(shutil.which("xorriso"), "xorriso is not installed")
class WindowsMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tree = self.root / "source"
        (self.tree / "EFI/BOOT").mkdir(parents=True)
        (self.tree / "sources").mkdir()
        (self.tree / "boot").mkdir()
        (self.tree / "EFI/BOOT/BOOTX64.EFI").write_bytes(b"EFI fixture; not a Windows loader")
        (self.tree / "sources/boot.wim").write_bytes(b"Windows PE fixture; not bootable")
        (self.tree / "bootmgr").write_bytes(b"BIOS loader fixture; not bootable")
        (self.tree / "boot/BCD").write_bytes(b"BCD fixture")
        self.iso = self.root / "fixture.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-J", "-R", "-o", str(self.iso), str(self.tree)],
                        check=True, capture_output=True)

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipUnless(shutil.which("mcopy"), "mtools is not installed")
    def test_fat32_bios_and_uefi_images(self):
        for firmware in ("uefi", "bios"):
            with self.subTest(firmware=firmware):
                output = self.root / f"{firmware}.img"
                result = media.create_windows_image(self.iso, output, size=128 * boot.MIB, firmware=firmware)
                self.assertEqual(result["firmware"], firmware)
                copied = subprocess.run(["mtype", "-i", f"{output}@@{boot.PARTITION_OFFSET}", "::/sources/boot.wim"],
                                        check=True, capture_output=True).stdout
                self.assertEqual(copied, (self.tree / "sources/boot.wim").read_bytes())
                with output.open("rb") as disk:
                    if firmware == "bios":
                        self.assertEqual(disk.read(440), assets.asset("mbr-win7")[:440])
                        disk.seek(boot.PARTITION_OFFSET + 0x1800)
                        self.assertEqual(disk.read(len(assets.asset("fat32-windows-loader"))), assets.asset("fat32-windows-loader"))

    @unittest.skipUnless(ntfs_available(), "wimlib/NTFS tools are not installed")
    def test_ntfs_bios_and_uefi_images_preserve_files(self):
        for firmware in ("uefi", "bios"):
            with self.subTest(firmware=firmware):
                output = self.root / f"ntfs-{firmware}.img"
                result = media.create_windows_image(self.iso, output, size=128 * boot.MIB,
                                                     filesystem="ntfs", firmware=firmware)
                table, parts = media.windows_partitions(result["size"], "ntfs", firmware)
                volume = self.root / f"volume-{firmware}.img"
                with output.open("rb") as source, volume.open("wb") as target:
                    source.seek(parts[0].start * 512)
                    left = parts[0].sectors * 512
                    while left:
                        data = source.read(min(core.CHUNK, left))
                        target.write(data)
                        left -= len(data)
                for relative in ("sources/boot.wim", "EFI/BOOT/BOOTX64.EFI", "boot/BCD", "bootmgr"):
                    result = subprocess.run(["ntfscat", str(volume), "/" + relative], check=True, capture_output=True)
                    self.assertEqual(result.stdout, (self.tree / relative).read_bytes())
                # Clearing the legacy NTFS root's system/hidden flags causes
                # CHKDSK errors even though ntfs-3g can read the volume.
                info = subprocess.check_output(["ntfscat", "--inode", "5", "--attribute", "16", str(volume)])
                self.assertEqual(struct.unpack_from("<I", info, 32)[0] & 0x6, 0x6)
                if firmware == "uefi":
                    with output.open("rb") as source:
                        source.seek(parts[1].start * 512)
                        self.assertEqual(source.read(parts[1].sectors * 512), assets.asset("uefi-ntfs.img"))


if __name__ == "__main__":
    unittest.main()
