import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from rufus_linux import assets, core, grub, linux_iso, syslinux
from test_linux_iso import kernel_fixture


def fixture(root, text=None, architecture="x86_64", kind="casper"):
    for directory in ("isolinux", "isolinux/menus", "casper"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "casper/vmlinuz").write_bytes(kernel_fixture(architecture))
    (root / "casper/initrd").write_bytes(b"test initramfs")
    (root / "casper/early").write_bytes(b"test early initramfs")
    (root / "casper/filesystem.squashfs").write_bytes(b"test live root, not bootable")
    (root / "isolinux/isolinux.bin").write_bytes(b"\0" * 100 + b"ISOLINUX 4.07 20130725\0")
    primary = root / "isolinux/isolinux.cfg"
    primary.write_text(text if text is not None else
                       f"DEFAULT live\nSERIAL 0 115200\nTIMEOUT 10\nLABEL live\nMENU LABEL ^Live system\n"
                       f"KERNEL /casper/vmlinuz\nAPPEND boot={kind} initrd=/casper/early,/casper/initrd quiet ---\n")
    return primary


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rufus-syslinux-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = fixture(self.root)

    def test_multiple_initrds_defaults_serial_and_source_version(self):
        menu = syslinux.read_menu(self.root, self.primary)
        entry = menu.entries[0]
        self.assertEqual(entry.title, "Live system")
        self.assertEqual(entry.initrds, ("/casper/early", "/casper/initrd"))
        self.assertEqual(menu.source_version, "4.07 20130725")
        self.assertEqual(menu.serial, (0, 115200))
        self.assertEqual(menu.timeout, 10)
        config = syslinux.boot_config(menu.entries, False, menu.serial, menu.timeout)
        self.assertIn("SERIAL 0 115200", config)
        self.assertIn("INITRD /casper/early,/casper/initrd", config)
        self.assertIn("quiet nopersistent ---", config)
        self.assertNotIn("menu.c32", config)
        self.assertIn("persistent ---", syslinux.boot_config(menu.entries, True))

    def test_non_linux_labels_are_reported_and_cannot_be_default(self):
        original = self.primary.read_text()
        self.primary.write_text(original + "LABEL local\nLOCALBOOT 0\n")
        menu = syslinux.read_menu(self.root, self.primary)
        self.assertEqual(menu.omitted_labels, ("local",))
        self.primary.write_text("UI menu.c32\nLABEL local\nLOCALBOOT 0\n" + original.replace("DEFAULT live\n", ""))
        with self.assertRaisesRegex(core.RufusError, "default"):
            syslinux.read_menu(self.root, self.primary)

    def test_nested_includes_use_original_working_directory_and_override_append(self):
        self.primary.write_text("UI vesamenu.c32\nAPPEND boot=live initrd=/casper/initrd inherited\nINCLUDE menus/main.cfg\n")
        (self.root / "isolinux/menus/main.cfg").write_text(
            "LABEL first\nKERNEL /casper/vmlinuz\nTEXT HELP\nDescription with unmatched ' quote\nENDTEXT\n"
            "MENU INCLUDE extra.cfg\n")
        (self.root / "isolinux/extra.cfg").write_text(
            "LABEL second\nMENU DEFAULT\nMENU LABEL Second entry\nLINUX /casper/vmlinuz\n"
            "INITRD /casper/initrd\nAPPEND boot=live quiet \\\n---\n")
        menu = syslinux.read_menu(self.root, self.primary)
        self.assertEqual(len(menu.entries), 2)
        self.assertEqual(menu.entries[0].title, "Second entry")
        self.assertNotIn("inherited", menu.entries[0].arguments)
        self.assertIn("inherited", menu.entries[1].arguments)
        self.assertEqual(len(menu.configs), 3)

    def test_append_reset_and_initrd_directive_override(self):
        self.primary.write_text("APPEND unsafe_global=1\nDEFAULT live\nLABEL live\nKERNEL /casper/vmlinuz\n"
                                "APPEND -\nINITRD /casper/initrd\n")
        menu = syslinux.read_menu(self.root, self.primary)
        self.assertEqual(menu.entries[0].arguments, ())
        self.primary.write_text("LABEL live\nKERNEL /casper/vmlinuz\n"
                                "APPEND initrd=/casper/missing boot=casper\nINITRD /casper/initrd\n")
        self.assertEqual(syslinux.read_menu(self.root, self.primary).entries[0].initrds, ("/casper/initrd",))

    def test_repeated_utility_labels_in_submenus_keep_the_root_live_default(self):
        self.primary.write_text("DEFAULT vesamenu.c32\nLABEL live\nMENU LABEL Debian Live\nMENU DEFAULT\n"
            "LINUX /casper/vmlinuz\nINITRD /casper/initrd\nAPPEND boot=live\n"
            "MENU BEGIN install\nMENU LABEL Installer submenu\nLABEL mainmenu\nMENU EXIT\nMENU END\n"
            "MENU BEGIN utilities\nMENU LABEL Utilities submenu\nLABEL mainmenu\nMENU EXIT\nMENU END\n")
        menu = syslinux.read_menu(self.root, self.primary)
        self.assertEqual(menu.entries[0].title, "Debian Live")
        self.assertEqual(menu.entries[0].kind, "debian")
        self.assertEqual(menu.omitted_labels, ("mainmenu", "mainmenu"))

    def test_ambiguous_live_labels_and_malformed_submenus_are_refused(self):
        original = self.primary.read_text()
        for suffix in ("MENU END\n", "MENU BEGIN missing_end\n", "MENU BEGIN submenu\nMENU DEFAULT\nMENU END\n",
                       "MENU BEGIN submenu\nLABEL live\nLINUX /casper/vmlinuz\nINITRD /casper/initrd\nAPPEND boot=live\nMENU END\n"):
            self.primary.write_text(original + suffix)
            with self.subTest(suffix=suffix), self.assertRaises(core.RufusError):
                syslinux.read_menu(self.root, self.primary)

    def test_relative_parent_paths_stay_inside_iso_and_ui_respects_global_default(self):
        self.primary.write_text("UI menu.c32\nDEFAULT second\nLABEL first\nKERNEL ../casper/vmlinuz\n"
                                "INITRD ../casper/initrd\nLABEL second\nKERNEL ../casper/vmlinuz\nINITRD ../casper/early\n")
        menu = syslinux.read_menu(self.root, self.primary)
        self.assertEqual(menu.entries[0].title, "second")
        self.assertEqual(menu.entries[0].kernel, "/casper/vmlinuz")
        self.assertEqual(menu.entries[1].initrds, ("/casper/initrd",))

    @unittest.skipUnless(all(shutil.which(name) for name in ("xorriso", "mcopy", "grub-mkimage")), "GRUB extraction tools missing")
    def test_arm_translation_needs_no_native_syslinux_installer(self):
        try:
            grub.module_directory("arm64-efi")
        except core.RufusError as exc:
            self.skipTest(str(exc))
        tree = self.root / "arm-iso-tree"
        fixture(tree, architecture="arm64")
        iso, output = self.root / "arm.iso", self.root / "arm.img"
        core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(iso), str(tree)])
        with patch.object(syslinux, "installer", side_effect=AssertionError("ARM does not use Syslinux")):
            result = linux_iso.create_extracted_image(iso, output)
        self.assertEqual((result["architecture"], result["firmware"]), ("arm64", "uefi"))

    def test_includes_cycles_escapes_size_and_missing_paths_fail(self):
        for text in ("INCLUDE isolinux.cfg\n", "INCLUDE ../../outside\n", "INCLUDE /missing\n",
                     "INCLUDE http://example.test/config\n", "INCLUDE $config\n", "x" * (1024**2 + 1)):
            with self.subTest(text=text[:60]):
                self.primary.write_text(text)
                with self.assertRaises(core.RufusError):
                    syslinux.read_menu(self.root, self.primary)
        self.primary.write_text("INCLUDE link.cfg\n")
        (self.root / "isolinux/link.cfg").symlink_to("/etc/passwd")
        with self.assertRaises(core.RufusError):
            syslinux.read_menu(self.root, self.primary)

    def test_dynamic_defaults_access_controls_and_duplicates_are_not_silently_translated(self):
        for prefix in ("UI gfxboot.c32\n", "DEFAULT ifcpu64.c32\n", "MENU PASSWD secret\n",
                       "DEFAULT missing\n", "ONTIMEOUT live\n", "LABEL live\nLABEL live\n"):
            self.primary = fixture(self.root, prefix + "LABEL live\nKERNEL /casper/vmlinuz\nINITRD /casper/initrd\n")
            with self.subTest(prefix=prefix), self.assertRaises(core.RufusError):
                syslinux.read_menu(self.root, self.primary)

    def test_persistence_arguments_match_grub_and_reject_unsafe_values(self):
        self.primary = fixture(self.root, kind="live")
        entry = syslinux.read_menu(self.root, self.primary).entries[0]
        self.assertIn("persistence ---", syslinux.boot_config((entry,), True))
        self.assertIn('"persistence" "---"', linux_iso.boot_config((entry,), True))
        for arg in ("$dynamic", ";reboot", "persistence-path=/custom", 'value with spaces'):
            changed = linux_iso.LiveEntry(entry.title, entry.kernel, (*entry.arguments, arg), entry.initrds, entry.kind)
            with self.subTest(arg=arg), self.assertRaises(core.RufusError):
                syslinux.boot_config((changed,), True)

    def test_cancel_stops_config_processing(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(core.Cancelled):
            syslinux.read_menu(self.root, self.primary, cancel)


TOOLS = all(shutil.which(name) for name in ("syslinux", "xorriso", "mkfs.vfat", "mcopy", "mtype", "fsck.fat"))


@unittest.skipUnless(TOOLS, "Syslinux extraction tools are not installed")
class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rufus-syslinux-image-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree"
        self.primary = fixture(self.tree)
        (self.tree / "md5sum.txt").write_text("old ISO manifest\n")
        self.iso, self.output = self.root / "source.iso", self.root / "output.img"

    def iso_fixture(self):
        core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(self.iso), str(self.tree)])

    def read(self, path):
        return subprocess.check_output(["mtype", "-i", f"{self.output}@@{linux_iso.MIB}", "::" + path])

    def test_bios_installs_its_own_coherent_loader_and_complete_manifest_without_grub(self):
        self.iso_fixture()
        with patch.object(grub, "module_directory", side_effect=AssertionError("BIOS Syslinux needs no GRUB")), \
             patch("rufus_linux.devices.UDisks") as service:
            result = linux_iso.create_extracted_image(self.iso, self.output, firmware="bios")
            service.assert_not_called()
        self.assertEqual(result["bios_bootloader"], "syslinux")
        self.assertEqual(result["source_syslinux_version"], "4.07 20130725")
        self.assertTrue(result["syslinux_version"].startswith("6."))
        self.assertEqual(result["grub_versions"], {})
        _, installer, installed_version = syslinux.installer()
        self.assertIn(self.read("/boot/rufus/ldlinux.c32"), installer)
        self.assertEqual(syslinux.version(self.read("/boot/rufus/ldlinux.sys")), installed_version)
        with self.output.open("rb") as disk:
            self.assertEqual(disk.read(440), assets.asset("mbr-syslinux")[:440])
        manifest = self.read("/md5sum.txt").decode()
        for path in ("/boot/rufus/ldlinux.sys", "/boot/rufus/ldlinux.c32", "/boot/rufus/syslinux.cfg"):
            self.assertIn(hashlib.md5(self.read(path)).hexdigest() + "  ." + path, manifest)
        self.assertIn("nopersistent", self.read("/boot/rufus/syslinux.cfg").decode())
        self.assertEqual(self.read("/isolinux/isolinux.cfg"), self.primary.read_bytes())

    def test_auto_falls_back_from_unsupported_grub_menu_and_supports_32_bit_bios(self):
        fixture(self.tree, architecture="x86")
        (self.tree / "boot/grub").mkdir(parents=True)
        (self.tree / "boot/grub/grub.cfg").write_text("configfile /another/menu.cfg\n")
        self.iso_fixture()
        result = linux_iso.create_extracted_image(self.iso, self.output)
        self.assertEqual((result["architecture"], result["firmware"], result["bios_bootloader"]), ("x86", "bios", "syslinux"))
        self.output.unlink()
        with self.assertRaises(core.RufusError):
            linux_iso.create_extracted_image(self.iso, self.output, bootloader="grub")
        self.assertFalse(self.output.exists())

    def test_explicit_syslinux_can_render_a_grub_only_source(self):
        self.primary.unlink()
        (self.tree / "boot/grub").mkdir(parents=True)
        (self.tree / "boot/grub/grub.cfg").write_text("menuentry 'Live system' {\n"
            "linux /casper/vmlinuz boot=casper quiet ---\ninitrd /casper/initrd\n}\n")
        self.iso_fixture()
        result = linux_iso.create_extracted_image(self.iso, self.output, bootloader="syslinux", firmware="bios")
        self.assertEqual((result["source_bootloader"], result["bios_bootloader"]), ("grub", "syslinux"))
        self.assertIn("LINUX /casper/vmlinuz", self.read("/boot/rufus/syslinux.cfg").decode())

    def test_both_firmware_modes_use_syslinux_bios_and_matching_grub_uefi(self):
        try:
            grub.module_directory("x86_64-efi")
        except core.RufusError as exc:
            self.skipTest(str(exc))
        self.iso_fixture()
        result = linux_iso.create_extracted_image(self.iso, self.output, persistence_size=64 * linux_iso.MIB)
        self.assertEqual((result["firmware"], result["table"], result["persistence_label"]), ("both", "dos", "writable"))
        self.assertEqual(self.read("/EFI/BOOT/BOOTX64.EFI")[:2], b"MZ")
        self.assertIn('"persistent" "---"', self.read("/boot/rufus/grub.cfg").decode())
        self.assertIn("persistent ---", self.read("/boot/rufus/syslinux.cfg").decode())

    def test_arm_and_uefi_only_can_translate_syslinux_source_to_grub(self):
        try:
            grub.module_directory("arm64-efi")
        except core.RufusError as exc:
            self.skipTest(str(exc))
        fixture(self.tree, architecture="arm64")
        self.iso_fixture()
        with self.assertRaisesRegex(core.RufusError, "x86"):
            linux_iso.create_extracted_image(self.iso, self.output, bootloader="syslinux")
        self.assertFalse(self.output.exists())
        result = linux_iso.create_extracted_image(self.iso, self.output, bootloader="grub")
        self.assertEqual((result["architecture"], result["firmware"], result["source_bootloader"]), ("arm64", "uefi", "syslinux"))
        self.assertIsNone(result["syslinux_version"])

    def test_missing_installer_and_bad_configs_fail_before_output_or_device_access(self):
        self.iso_fixture()
        with patch.dict("os.environ", {"RUFUS_SYSLINUX": "/absent/syslinux"}), patch("rufus_linux.devices.UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "installer"):
                linux_iso.create_extracted_image(self.iso, self.output, firmware="bios")
            service.assert_not_called()
            self.assertFalse(self.output.exists())
        self.primary.write_text("INCLUDE ../escape\n")
        self.iso_fixture()
        with self.assertRaises(core.RufusError):
            linux_iso.create_extracted_image(self.iso, self.output, firmware="bios")
        self.assertFalse(self.output.exists())

    def test_cli_auto_detection_does_not_discover_host_disks(self):
        from rufus_linux.cli import main
        self.iso_fixture()
        with patch("rufus_linux.cli.discover_devices") as disks, contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["linux", str(self.iso), "--mode", "extract", "--firmware", "bios",
                                   "--output", str(self.output)]), 0)
        self.assertEqual(json.loads(output.getvalue())["bios_bootloader"], "syslinux")
        disks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
