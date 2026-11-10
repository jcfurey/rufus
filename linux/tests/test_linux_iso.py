import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from rufus_linux import core, grub, linux_iso


def kernel_fixture(architecture):
    data = bytearray(4096)
    if architecture in ("x86", "x86_64"):
        data[510:512] = b"\x55\xaa"
        data[0x202:0x206] = b"HdrS"
        struct.pack_into("<H", data, 0x236, architecture == "x86_64")
    elif architecture == "arm64":
        data[56:60] = b"ARM\x64"
    elif architecture == "arm":
        data[36:40] = b"\x18\x28\x6f\x01"
    return bytes(data)


class ArchitectureTests(unittest.TestCase):
    def test_cross_architecture_module_data_can_be_supplied_without_system_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "arm64-efi"
            target.mkdir()
            (target / "modinfo.sh").write_text('grub_package_version="test-data"\n')
            (target / "kernel.img").write_bytes(b"test module directory")
            with patch.dict("os.environ", {"RUFUS_GRUB_MODULES": str(root), "RUFUS_USE_LOCAL_TOOLS": "1"}), \
                 patch.object(grub.shutil, "which", return_value="/usr/bin/grub-mkimage"):
                self.assertEqual(grub.module_directory("arm64-efi"), target)

    def test_real_kernel_header_formats_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vmlinuz"
            for architecture in ("x86", "x86_64", "arm64", "arm"):
                path.write_bytes(kernel_fixture(architecture))
                self.assertEqual(grub.kernel_architecture(path), architecture)
            for machine, architecture in ((0xaa64, "arm64"), (0x1c4, "arm")):
                data = bytearray(1024)
                data[:2] = b"MZ"
                struct.pack_into("<I", data, 60, 128)
                data[128:132] = b"PE\0\0"
                struct.pack_into("<H", data, 132, machine)
                path.write_bytes(data)
                self.assertEqual(grub.kernel_architecture(path), architecture)
            path.write_bytes(b"not a Linux kernel")
            with self.assertRaises(core.RufusError):
                grub.kernel_architecture(path)

    def test_arm_bios_is_rejected_before_tools_are_consulted(self):
        with patch.object(grub, "module_directory") as lookup:
            for architecture in ("arm64", "arm"):
                for firmware in ("bios", "both"):
                    with self.assertRaisesRegex(core.RufusError, "requires UEFI"):
                        grub.target_modules(architecture, firmware)
            lookup.assert_not_called()

    def test_extraction_does_not_follow_other_directory_links_or_change_their_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.mkdir(mode=0o500)
            (root / "debian").symlink_to(outside, target_is_directory=True)
            linux_iso.prepare_extracted_tree(root)
            with self.assertRaisesRegex(core.RufusError, "link or special file"):
                core.validate_fat_tree(root)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o500)

    def test_debian_media_checks_do_not_gain_persistence(self):
        entry = linux_iso.LiveEntry("Verify medium", "/live/vmlinuz", ("boot=live", "verify-checksums"),
                                   ("/live/initrd.img",), "debian")
        for enabled in (False, True):
            self.assertEqual(linux_iso.boot_arguments(entry, enabled), ["boot=live", "verify-checksums"])

    def test_external_file_links_broken_links_and_cycles_are_not_materialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tree"
            root.mkdir()
            outside = Path(directory) / "outside"
            outside.write_bytes(b"must not be copied or modified")
            outside.chmod(0o400)
            link = root / "unsafe"
            for target in (str(outside), "missing", "unsafe"):
                link.symlink_to(target)
                linux_iso.prepare_extracted_tree(root)
                with self.assertRaises(core.RufusError):
                    core.validate_fat_tree(root)
                self.assertTrue(link.is_symlink())
                link.unlink()
            self.assertEqual(outside.read_bytes(), b"must not be copied or modified")
            self.assertEqual(outside.stat().st_mode & 0o777, 0o400)

    def test_arm_uefi_uses_correct_platform_and_removable_loader(self):
        with patch.object(grub, "module_directory", side_effect=lambda target: Path(target)):
            for architecture, target, name in (("arm64", "arm64-efi", "BOOTAA64.EFI"),
                                                ("arm", "arm-efi", "BOOTARM.EFI")):
                self.assertEqual(grub.target_modules(architecture, None), ("uefi", {target: Path(target)}))
                self.assertEqual(grub.EFI[architecture], (target, name))


TOOLS = all(shutil.which(tool) for tool in ("xorriso", "mcopy", "mtype", "mkfs.vfat", "fsck.fat", "grub-mkimage"))


@unittest.skipUnless(TOOLS, "Linux extraction tools are not installed")
class ExtractedImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree"
        (self.tree / "boot/grub").mkdir(parents=True)
        (self.tree / "EFI/boot").mkdir(parents=True)
        (self.tree / "casper").mkdir()
        (self.tree / "casper/initrd").write_bytes(b"initrd fixture; not bootable")
        (self.tree / "casper/filesystem.squashfs").write_bytes(b"live root fixture; not bootable")
        (self.tree / "md5sum.txt").write_text("0" * 32 + "  ./md5sum.txt\n")
        self.iso, self.output = self.root / "source.iso", self.root / "output.img"

    def fixture(self, architecture="x86_64", kind="casper", extra=""):
        (self.tree / "casper/vmlinuz").write_bytes(kernel_fixture(architecture))
        (self.tree / "boot/grub/grub.cfg").write_text(
            "menuentry 'Live system' {\n"
            f"linux /casper/vmlinuz boot={kind} iso-scan/filename=${{iso_path}} quiet ---\n"
            "initrd /casper/initrd\n}\n" + extra)
        core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(self.iso), str(self.tree)])

    def modules(self, architecture, firmware=None):
        try:
            grub.target_modules(architecture, firmware)
        except core.RufusError as exc:
            self.skipTest(str(exc))

    def read_fat(self, path):
        return subprocess.run(["mtype", "-i", f"{self.output}@@{linux_iso.MIB}", "::" + path],
                              check=True, capture_output=True).stdout

    def test_x86_bios_and_uefi_fat_image_and_complete_manifest(self):
        self.modules("x86_64")
        self.fixture()
        source_hash = core.checksum(self.iso)
        result = linux_iso.create_extracted_image(self.iso, self.output, size=160 * linux_iso.MIB, expected=source_hash)
        self.assertEqual((result["architecture"], result["firmware"], result["table"]), ("x86_64", "both", "dos"))
        config = self.read_fat("/boot/rufus/grub.cfg").decode()
        self.assertNotIn("iso-scan", config)
        self.assertIn('"nopersistent" "---"', config)
        self.assertEqual(self.read_fat("/casper/filesystem.squashfs"), b"live root fixture; not bootable")
        self.assertEqual(self.read_fat("/EFI/BOOT/BOOTX64.EFI")[:2], b"MZ")
        manifest = self.read_fat("/md5sum.txt").decode()
        self.assertNotIn("./md5sum.txt", manifest)
        self.assertIn(hashlib.md5(config.encode()).hexdigest() + "  ./boot/rufus/grub.cfg", manifest)
        self.assertEqual(core.checksum(self.iso), source_hash)
        with self.output.open("rb") as disk:
            disk.seek(0x5c)
            self.assertEqual(struct.unpack("<Q", disk.read(8))[0], 1)
            disk.seek(512 + 500)
            self.assertEqual(struct.unpack("<Q", disk.read(8))[0], 2)

    def test_arm32_and_arm64_create_architecture_correct_uefi_images(self):
        for architecture in ("arm64", "arm"):
            with self.subTest(architecture=architecture):
                self.modules(architecture)
                self.fixture(architecture)
                self.output.unlink(missing_ok=True)
                result = linux_iso.create_extracted_image(self.iso, self.output, size=160 * linux_iso.MIB)
                self.assertEqual((result["architecture"], result["firmware"], result["table"]),
                                 (architecture, "uefi", "gpt"))
                loader = self.read_fat("/EFI/BOOT/" + grub.EFI[architecture][1])
                pe = struct.unpack_from("<I", loader, 60)[0]
                machine = struct.unpack_from("<H", loader, pe + 4)[0]
                self.assertEqual(machine, 0xaa64 if architecture == "arm64" else 0x1c2)

    def test_debian_persistence_and_legacy_casper_labels(self):
        self.modules("x86_64", "uefi")
        self.fixture(kind="live")
        result = linux_iso.create_extracted_image(self.iso, self.output, size=240 * linux_iso.MIB,
                                                  persistence_size=64 * linux_iso.MIB, firmware="uefi")
        self.assertEqual(result["persistence_label"], "persistence")
        self.assertIn(b'"persistence" "---"', self.read_fat("/boot/rufus/grub.cfg"))
        table = json.loads(subprocess.run(["sfdisk", "--json", str(self.output)], capture_output=True,
                                          text=True, check=True).stdout)["partitiontable"]
        part = table["partitions"][1]
        with self.output.open("rb") as disk:
            disk.seek(part["start"] * 512 + 1024 + 120)
            self.assertEqual(disk.read(16).rstrip(b"\0"), b"persistence")

    def test_debian_root_alias_and_readonly_configuration_are_supported(self):
        self.modules("x86_64", "uefi")
        (self.tree / "debian").symlink_to(".", target_is_directory=True)
        (self.tree / "pool").mkdir()
        (self.tree / "pool/firmware.deb").write_bytes(b"private firmware package fixture")
        (self.tree / "firmware").mkdir()
        (self.tree / "firmware/package.deb").symlink_to("../pool/firmware.deb")
        (self.tree / "dists/trixie").mkdir(parents=True)
        (self.tree / "dists/trixie/Release").write_bytes(b"Suite: stable\n")
        (self.tree / "dists/stable").symlink_to("trixie", target_is_directory=True)
        self.fixture(kind="live")
        # Rebuild with the same read-only directory/file modes as official ISOs.
        for path in self.tree.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)
        self.iso.unlink()
        core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(self.iso), str(self.tree)])
        before = core.checksum(self.iso)
        result = linux_iso.create_extracted_image(self.iso, self.output, size=240 * linux_iso.MIB,
                                                  persistence_size=64 * linux_iso.MIB, firmware="uefi")
        self.assertEqual(result["omitted_aliases"], ["debian", "dists/stable"])
        self.assertEqual(self.read_fat("/firmware/package.deb"), b"private firmware package fixture")
        self.assertIn(b'"persistence"', self.read_fat("/boot/rufus/grub.cfg"))
        self.assertEqual(core.checksum(self.iso), before)

    def test_architecture_mismatch_and_unsafe_boot_paths_fail_before_output(self):
        self.fixture("arm64")
        with self.assertRaisesRegex(core.RufusError, "does not match"):
            linux_iso.create_extracted_image(self.iso, self.output, architecture="x86_64")
        self.assertFalse(self.output.exists())
        with self.assertRaises(core.RufusError):
            linux_iso.tree_file(self.tree, "/../../outside")

    def test_checksum_failure_existing_output_and_cancel_are_protected(self):
        self.fixture()
        with self.assertRaisesRegex(core.RufusError, "does not match"):
            linux_iso.create_extracted_image(self.iso, self.output, expected="0" * 64)
        self.assertFalse(self.output.exists())
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(core.Cancelled):
            linux_iso.create_extracted_image(self.iso, self.output, cancel=cancel)
        self.assertFalse(self.output.exists())
        self.output.write_bytes(b"keep existing")
        with self.assertRaisesRegex(core.RufusError, "already exists"):
            linux_iso.create_extracted_image(self.iso, self.output)
        self.assertEqual(self.output.read_bytes(), b"keep existing")

    def test_cli_extract_selects_arm_automatically_without_drive_discovery(self):
        from rufus_linux.cli import main
        self.modules("arm64")
        self.fixture("arm64")
        with patch("rufus_linux.cli.discover_devices") as discover, \
             contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["linux", str(self.iso), "--mode", "extract", "--output", str(self.output)]), 0)
            self.assertEqual(json.loads(output.getvalue())["architecture"], "arm64")
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
