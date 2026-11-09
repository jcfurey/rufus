import os
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rufus_linux import assets, boot, core


class AssetTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gcc"), "C compiler not installed")
    def test_boot_assets_match_actual_c_initializers(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / "resource.c", Path(directory) / "resource"
            for name, (header, variable) in assets.BOOT_ARRAYS.items():
                with self.subTest(asset=name):
                    source.write_text('#include <stdio.h>\n#include ' + json.dumps(str(root / "src/ms-sys/inc" / header)) +
                                      f'\nint main(void) {{ return fwrite({variable}, 1, sizeof({variable}), stdout) != sizeof({variable}); }}\n')
                    subprocess.run(["gcc", str(source), "-o", str(binary)], check=True, capture_output=True)
                    expected = subprocess.run([str(binary)], check=True, capture_output=True).stdout
                    self.assertEqual(assets.source_asset(name), expected)


@unittest.skipUnless(shutil.which("mcopy"), "mtools is not installed")
class BootMediaTests(unittest.TestCase):
    def test_freedos_fat16_and_fat32_images_preserve_upstream_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for fat in (16, 32):
                with self.subTest(fat=fat):
                    image = root / f"freedos-{fat}.img"
                    with patch("rufus_linux.devices.UDisks") as service:
                        boot.create_freedos(image, fat_bits=fat)
                        service.assert_not_called()
                    with image.open("rb") as disk:
                        mbr = disk.read(512)
                        self.assertEqual(mbr[510:512], b"\x55\xaa")
                        self.assertEqual(mbr[446], 0x80)
                        self.assertEqual(struct.unpack_from("<I", mbr, 454)[0], 2048)
                        disk.seek(boot.PARTITION_OFFSET)
                        sector = disk.read(512)
                        self.assertEqual(struct.unpack_from("<I", sector, 28)[0], 2048)
                        self.assertEqual(sector[510:512], b"\x55\xaa")
                        self.assertEqual(sector[0:11], assets.asset("fat32-head" if fat == 32 else "fat16-head"))
                    for name in ("KERNEL.SYS", "COMMAND.COM"):
                        extracted = root / f"{fat}-{name}"
                        subprocess.run(["mcopy", "-i", f"{image}@@{boot.PARTITION_OFFSET}",
                                        "::/" + name, str(extracted)], check=True, capture_output=True)
                        self.assertEqual(extracted.read_bytes(), assets.asset("freedos/" + name))

    def test_existing_image_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "existing.img"
            image.write_bytes(b"preserve")
            with self.assertRaisesRegex(core.RufusError, "already exists"):
                boot.create_freedos(image)
            self.assertEqual(image.read_bytes(), b"preserve")

    def test_invalid_sizes_and_labels_fail_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "new.img"
            for size, fat, label in ((16 * boot.MIB, 32, "FREEDOS"), (4 * 1024**3, 16, "FREEDOS"),
                                      (64 * boot.MIB, 32, "BAD:LABEL")):
                with self.assertRaises(core.RufusError):
                    boot.create_freedos(image, size, fat, label)
                self.assertFalse(image.exists())


if __name__ == "__main__":
    unittest.main()
