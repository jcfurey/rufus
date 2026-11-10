import contextlib
import io
import json
import lzma
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from rufus_linux import core, jetson, layout, linux_iso, live


def efi_fixture(machine=0xaa64):
    value = bytearray(4096)
    value[:2] = b"MZ"
    struct.pack_into("<I", value, 60, 128)
    value[128:132] = b"PE\0\0"
    struct.pack_into("<H", value, 132, machine)
    return value


class JetsonPlanTests(unittest.TestCase):
    def test_all_models_have_supported_recovery_workflow(self):
        for model, (_, targets, _, _) in jetson.MODELS.items():
            for storage in targets:
                plan = jetson.installation_plan(model, storage, "recovery")
                self.assertIsNone(plan["media_destination"])
                self.assertTrue(any("SDK Manager" in step for step in plan["steps"]))

    def test_unsupported_model_storage_and_installation_combinations_fail(self):
        for model, storage, method in (("orin-nano", "emmc", "installer"), ("nano", "sd", "installer"),
                                       ("agx-orin", "nvme", "sd"), ("orin-nx", "sd", "sd"),
                                       ("thor", "emmc", "recovery")):
            with self.subTest(model=model, storage=storage, method=method), self.assertRaises(core.RufusError):
                jetson.installation_plan(model, storage, method)

    def test_recovery_handoff_does_not_supply_flash_or_authentication_flags(self):
        with patch.object(jetson.shutil, "which", return_value="/opt/nvidia/sdkmanager"), \
             patch.object(jetson.subprocess, "Popen", return_value=MagicMock(pid=42)) as launch:
            self.assertEqual(jetson.launch_sdkmanager()["pid"], 42)
            launch.assert_called_once_with(["/opt/nvidia/sdkmanager"], start_new_session=True)

    def test_usb_enumeration_distinguishes_recovery_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for port, vendor, product in (("1-1", "0955", "APX"), ("1-2", "0955", "NVIDIA USB device"),
                                           ("1-3", "1234", "APX")):
                path = root / port
                path.mkdir()
                (path / "idVendor").write_text(vendor)
                (path / "idProduct").write_text("test")
                (path / "product").write_text(product)
            result = jetson.recovery_devices(root)
            self.assertEqual([d["usb_port"] for d in result], ["1-1", "1-2"])
            self.assertEqual([d["recovery_candidate"] for d in result], [True, False])

    def test_cli_plan_does_not_discover_or_open_host_disks(self):
        from rufus_linux.cli import main
        with patch("rufus_linux.cli.discover_devices") as discover, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["jetson", "--model", "agx-orin", "--storage", "emmc", "--plan"]), 0)
            self.assertEqual(json.loads(output.getvalue())["media_destination"], "USB installer drive")
            discover.assert_not_called()

    def test_legacy_jetson_and_pi_style_compressed_partitions_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "board.img"
            size = 8 * live.MIB
            with raw.open("w+b") as stream:
                stream.truncate(size)
                layout.write_layout(stream.fileno(), size, [layout.Partition(2048, 2048, "esp", 0x0c, "firmware"),
                                                            layout.Partition(4096, 4096, "linux", 0x83, "APP")], "dos")
                core.write_at(stream.fileno(), b"board-specific boot firmware and DTB", live.MIB)
                core.write_at(stream.fileno(), b"board root filesystem", 2 * live.MIB)
            source, target = root / "board.img.xz", root / "restored.img"
            source.write_bytes(lzma.compress(raw.read_bytes()))
            jetson.write_media(source, model="nano", storage="sd", method="sd", output=target)
            self.assertEqual(target.read_bytes(), raw.read_bytes())
            pi = root / "pi-restored.img"
            core.write_to_file(source, pi)
            self.assertEqual(pi.read_bytes(), raw.read_bytes())


@unittest.skipUnless(shutil.which("xorriso"), "xorriso is not installed")
class JetsonMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "source"
        for relative in ("casper", "boot/grub", "EFI/BOOT", "pool/main/n"):
            (self.tree / relative).mkdir(parents=True)
        (self.tree / "casper/installer.nvidia.squashfs").write_bytes(b"NVIDIA overlay fixture; not an installer")
        (self.tree / "boot/grub/grub.cfg").write_text("# NVIDIA installer fixture\n")
        (self.tree / "pool/main/n/nvidia.deb").write_bytes(b"repository fixture")
        self.loader = self.tree / "EFI/BOOT/BOOTAA64.EFI"
        self.loader.write_bytes(efi_fixture())
        self.iso, self.output = self.root / "jetson.iso", self.root / "usb.img"

    def fixture(self):
        core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-partition_offset", "16",
                       "-append_partition", "2", "0xef", str(self.loader), "-appended_part_as_gpt",
                       "-e", "--interval:appended_partition_2:all::", "-no-emul-boot", "-o", str(self.iso), str(self.tree)])

    def test_nvidia_installer_is_written_byte_for_byte_for_modern_targets(self):
        self.fixture()
        for model, storage in (("orin-nano", "nvme"), ("orin-nx", "nvme"), ("agx-orin", "emmc"), ("thor", "nvme")):
            self.output.unlink(missing_ok=True)
            result = jetson.write_media(self.iso, model=model, storage=storage, output=self.output)
            self.assertTrue(result["vendor_media_preserved"])
            self.assertEqual(self.output.read_bytes(), self.iso.read_bytes())

    def test_wrong_architecture_checksum_and_generic_isos_are_rejected(self):
        self.loader.write_bytes(efi_fixture(0x8664))
        self.fixture()
        with self.assertRaisesRegex(core.RufusError, "not ARM64"):
            jetson.write_media(self.iso, model="orin-nano", output=self.output)
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(core.RufusError, "mismatch"):
            jetson.write_media(self.iso, model="orin-nano", output=self.output, expected="0" * 64)
        self.assertFalse(self.output.exists())
        (self.tree / "casper/installer.nvidia.squashfs").unlink()
        self.fixture()
        with self.assertRaisesRegex(core.RufusError, "NVIDIA"):
            jetson.write_media(self.iso, model="orin-nano", output=self.output)
        self.assertFalse(self.output.exists())

    def test_generic_linux_modes_refuse_to_replace_vendor_installer_bootloaders(self):
        self.fixture()
        for function, kwargs in ((live.create_live_image, {"persistence_size": 64 * live.MIB}),
                                 (linux_iso.create_extracted_image, {})):
            with self.assertRaisesRegex(core.RufusError, "Jetson installer mode"):
                function(self.iso, self.output, **kwargs)
            self.assertFalse(self.output.exists())

    @unittest.skipUnless(all(shutil.which(t) for t in ("mcopy", "mmd", "mtype", "mkfs.vfat")), "mtools is missing")
    def test_embedded_arm64_efi_partition_is_validated(self):
        efi = self.tree / "boot/grub/efi.img"
        with efi.open("xb") as stream:
            stream.truncate(4 * live.MIB)
        core.run_tool(["mkfs.vfat", str(efi)])
        core.run_tool(["mmd", "-i", str(efi), "::/EFI", "::/EFI/BOOT"])
        core.run_tool(["mcopy", "-i", str(efi), str(self.loader), "::/EFI/BOOT/BOOTAA64.EFI"])
        self.loader.unlink()
        self.loader = efi
        self.fixture()
        result = jetson.write_media(self.iso, model="agx-orin", storage="nvme", output=self.output)
        self.assertTrue(result["vendor_media_preserved"])
        self.assertEqual(self.output.read_bytes(), self.iso.read_bytes())

    def test_invalid_installer_never_opens_a_physical_device(self):
        from test_devices import usb
        from rufus_linux import devices
        self.loader.write_bytes(efi_fixture(0x8664))
        self.fixture()
        with patch.object(devices, "guard", return_value=usb()), patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "not ARM64"):
                jetson.write_media(self.iso, model="orin-nano", device=usb())
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
