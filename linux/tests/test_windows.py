import contextlib
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from rufus_linux import core, windows
from rufus_linux.cli import main

NS = {"u": windows.UNATTEND, "wcm": windows.WCM}


class WindowsConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tree = self.root / "installer"
        (self.tree / "EFI/BOOT").mkdir(parents=True)
        (self.tree / "Sources").mkdir()
        (self.tree / "EFI/BOOT/BOOTX64.EFI").write_bytes(b"EFI TEST FIXTURE")
        (self.tree / "Sources/boot.wim").write_bytes(b"WIM TEST FIXTURE")

    def tearDown(self):
        self.temp.cleanup()

    def test_answer_file_matches_upstream_setup_passes_and_architecture(self):
        options = windows.WindowsOptions(bypass_hardware=True, offline_account=True, local_account="Test User",
                                         privacy=True, disable_encryption=True, locale="en-US",
                                         timezone="Central Standard Time", architecture="arm64")
        root = ET.fromstring(windows.answer_file(options))
        self.assertEqual(root.tag, f"{{{windows.UNATTEND}}}unattend")
        self.assertEqual({node.get("pass") for node in root}, {"windowsPE", "specialize", "oobeSystem"})
        self.assertTrue(all(node.get("processorArchitecture") == "arm64" for node in root.findall(".//u:component", NS)))
        self.assertEqual(root.findtext(".//u:LocalAccount/u:Name", namespaces=NS), "Test User")
        self.assertEqual(root.findtext(".//u:ProtectYourPC", namespaces=NS), "3")
        self.assertEqual(root.findtext(".//u:PreventDeviceEncryption", namespaces=NS), "true")
        self.assertEqual(root.findtext(".//u:UILanguage", namespaces=NS), "en-US")
        paths = [node.text for node in root.findall(".//u:Path", NS)]
        for key in ("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck", "BypassNRO"):
            self.assertTrue(any(key in path for path in paths))

    def test_account_names_cannot_inject_commands(self):
        for name in ('bad" & calc', "%PATH%", "hello\nworld", "SYSTEM", "Administrator", "Administrateur",
                     "Администратор", "name.", " user", "u" * 21, ""):
            if not name:
                continue
            with self.subTest(name=name), self.assertRaises(core.RufusError):
                windows.WindowsOptions(local_account=name).validate()

    def test_xml_escaping_preserves_values_without_elements(self):
        root = ET.fromstring(windows.answer_file(windows.WindowsOptions(timezone="A & B < C")))
        self.assertEqual(root.findtext(".//u:TimeZone", namespaces=NS), "A & B < C")

    def test_oobe_only_config_is_in_oem_panther_and_keeps_case(self):
        options = windows.WindowsOptions(local_account="alice", privacy=True)
        files = windows.apply_windows_options(self.tree, options)
        destination = self.tree / "Sources/$OEM$/$$/Panther/unattend.xml"
        self.assertTrue(destination.is_file())
        self.assertIn(destination, [file for file, _, _ in files])
        self.assertFalse((self.tree / "sources").exists())
        root = ET.fromstring(destination.read_bytes())
        self.assertEqual(root.findtext(".//u:LocalAccount/u:Name", namespaces=NS), "alice")

    def test_existing_answer_files_are_preserved(self):
        original = self.tree / "AUTOunattend.XML"
        original.write_bytes(b"custom answer")
        with self.assertRaisesRegex(core.RufusError, "already contains"):
            windows.apply_windows_options(self.tree, windows.WindowsOptions(privacy=True))
        self.assertEqual(original.read_bytes(), b"custom answer")

    def test_architecture_mismatch_fails_before_customization(self):
        with self.assertRaisesRegex(core.RufusError, "does not contain"):
            windows.apply_windows_options(self.tree, windows.WindowsOptions(privacy=True, architecture="arm64"))
        self.assertFalse((self.tree / "Sources/$OEM$").exists())

    def test_boot_index_uses_wim_bootable_index_not_assumed_image_two(self):
        path = self.tree / "Sources/boot.wim"
        header = bytearray(208)
        header[:8] = b"MSWIM\0\0\0"
        struct.pack_into("<I", header, 44, 4)
        struct.pack_into("<I", header, 120, 3)
        path.write_bytes(header)
        self.assertEqual(windows.wim_boot_index(path), 3)
        self.assertEqual(windows.wim_boot_index(path, 2), 2)
        with self.assertRaises(core.RufusError):
            windows.wim_boot_index(path, 5)

    def test_answer_file_cli_creates_new_file_and_refuses_overwrite(self):
        output = self.root / "Autounattend.xml"
        args = ["windows-config", "--output", str(output), "--local-account", "alice", "--privacy"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)
            original = output.read_bytes()
            self.assertEqual(main(args), 1)
        self.assertEqual(output.read_bytes(), original)

    @unittest.skipUnless(shutil.which("xorriso"), "xorriso is not installed")
    def test_real_iso_customization_prepares_without_disk_or_authentication(self):
        iso = self.root / "windows.iso"
        subprocess.run(["xorriso", "-as", "mkisofs", "-J", "-R", "-o", str(iso), str(self.tree)],
                        check=True, capture_output=True)
        output = self.root / "prepared"
        with patch("rufus_linux.devices.UDisks") as service:
            result = windows.prepare_windows(iso, output, windows.WindowsOptions(local_account="alice", privacy=True))
            service.assert_not_called()
        self.assertGreater(result["files"], 2)
        self.assertTrue((output / "Sources/$OEM$/$$/Panther/unattend.xml").exists())

    @unittest.skipUnless(shutil.which("wimlib-imagex"), "wimtools is not installed")
    def test_actual_boot_wim_injection_and_extract(self):
        payload = self.root / "winpe"
        payload.mkdir()
        (payload / "fixture.txt").write_text("Windows PE fixture, not bootable")
        boot = self.tree / "Sources/boot.wim"
        boot.unlink()
        subprocess.run(["wimlib-imagex", "capture", str(payload), str(boot), "Windows Setup",
                        "--compress=none", "--boot", "--check"], check=True, capture_output=True)
        windows.apply_windows_options(self.tree, windows.WindowsOptions(bypass_hardware=True, local_account="alice"))
        extracted = self.root / "extracted"
        extracted.mkdir()
        subprocess.run(["wimlib-imagex", "extract", str(boot), "1", "/Autounattend.xml",
                        "--dest-dir=" + str(extracted), "--no-acls"], check=True, capture_output=True)
        root = ET.fromstring((extracted / "Autounattend.xml").read_bytes())
        self.assertIsNotNone(root.find("u:settings[@pass='windowsPE']", NS))
        self.assertEqual(root.findtext(".//u:LocalAccount/u:Name", namespaces=NS), "alice")

    @unittest.skipUnless(shutil.which("wimlib-imagex"), "wimtools is not installed")
    def test_real_wim_split_preserves_all_installer_files(self):
        payload = self.root / "windows-files"
        payload.mkdir()
        for index in range(5):
            (payload / f"file-{index}.bin").write_bytes(os.urandom(700000))
        install = self.tree / "Sources/install.wim"
        subprocess.run(["wimlib-imagex", "capture", str(payload), str(install), "Windows fixture",
                        "--compress=none", "--check"], check=True, capture_output=True)
        files = core.validate_windows_tree(self.tree)
        result = core.ensure_fat32_files(self.tree, files, file_limit=2 * 1024**2, part_size_mb=1)
        self.assertFalse(install.exists())
        self.assertGreater(len(list(install.parent.glob("install*.swm"))), 1)
        self.assertTrue(all(size <= 2 * 1024**2 for _, _, size in result))
        restored = self.root / "restored"
        restored.mkdir()
        subprocess.run(["wimlib-imagex", "apply", str(install.with_name("install.swm")), "1", str(restored),
                        "--ref=" + str(install.parent / "install*.swm"), "--no-acls"], check=True, capture_output=True)
        for path in payload.iterdir():
            self.assertEqual(path.read_bytes(), (restored / path.name).read_bytes())


if __name__ == "__main__":
    unittest.main()
