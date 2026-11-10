import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from rufus_linux import core, devices, layout, windows, windows_togo as togo
from rufus_linux.windows import WindowsOptions
from test_jetson import efi_fixture
from windows_togo_smoke import QMP, resident_root_file, test_answer, valid_evidence


class MetadataTests(unittest.TestCase):
    def test_vm_control_reuses_connection_and_handles_events_between_replies(self):
        class Stream:
            replies = iter([b'{"QMP": {}}\n', b'{"return": {}, "id": 0}\n',
                            b'{"event": "RESET", "data": {"guest": true}}\n',
                            b'{"return": {}, "id": 1}\n',
                            b'{"error": {"desc": "fixture rejection"}, "id": 2}\n'])
            def __init__(self):
                self.commands = []
            def write(self, data):
                self.commands.append(json.loads(data))
            def readline(self, _limit):
                return next(self.replies)
            def close(self):
                pass
        stream = Stream()
        with tempfile.TemporaryDirectory() as directory, patch("windows_togo_smoke.socket.socket") as socket:
            socket.return_value.makefile.return_value = stream
            events = Path(directory) / "events.jsonl"
            client = QMP(Path(directory) / "test.sock", events)
            try:
                self.assertEqual(client.command("send-key")["return"], {})
                self.assertIn("fixture rejection", client.command("query-status")["error"]["desc"])
                socket.return_value.connect.assert_called_once()
                self.assertEqual([x["execute"] for x in stream.commands], ["qmp_capabilities", "send-key", "query-status"])
                self.assertEqual(json.loads(events.read_text())["event"], "RESET")
            finally:
                client.close()

    def test_boot_probe_preserves_options_and_obeys_windows_answer_schema(self):
        original = windows.answer_file(WindowsOptions(offline_account=True, privacy=True), "amd64")
        root = ET.fromstring(test_answer(ET.fromstring(original), "amd64"))
        ns = {"u": windows.UNATTEND}
        commands = root.findall(".//u:RunSynchronousCommand", ns)
        self.assertEqual([node.findtext("u:Order", namespaces=ns) for node in commands], ["1", "2", "3", "4"])
        paths = [node.findtext("u:Path", namespaces=ns) for node in commands]
        self.assertTrue(all(0 < len(text) <= 259 for text in paths))
        self.assertIn("BypassNRO", paths[0])
        self.assertIn("SanPolicy", paths[-2])
        self.assertIn("PortableOperatingSystem", paths[-1])
        self.assertNotIn("shutdown", " ".join(paths).lower())

    def test_edition_metadata_is_bounded_typed_and_architecture_aware(self):
        xml = ('<WIM><IMAGE INDEX="2"><NAME>Windows fixture</NAME><TOTALBYTES>12345</TOTALBYTES>'
               '<WINDOWS><ARCH>12</ARCH><VERSION><MAJOR>10</MAJOR><MINOR>0</MINOR><BUILD>26100</BUILD>'
               '</VERSION><EDITIONID>Professional</EDITIONID></WINDOWS></IMAGE></WIM>')
        def respond(args, cancel, stdout):
            stdout.write(xml.encode("utf-16"))
        with patch.object(togo, "run_tool", side_effect=respond):
            result = togo.images("fixture.wim")
        self.assertEqual(result[0]["architecture"], "arm64")
        self.assertEqual((result[0]["index"], result[0]["expanded_size"]), (2, 12345))
        for xml in ('<!DOCTYPE WIM [<!ENTITY x "bad">]><WIM/>', '<WIM/>', '<WIM><IMAGE INDEX="-1"/></WIM>'):
            with patch.object(togo, "run_tool", side_effect=respond), self.assertRaises(core.RufusError):
                togo.images("fixture.wim")

    def test_pe_headers_verify_target_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "loader.efi"
            for machine, arch in ((0xaa64, "arm64"), (0x8664, "amd64"), (0x14c, "x86")):
                path.write_bytes(efi_fixture(machine))
                self.assertEqual(togo.pe_architecture(path), arch)
            path.write_bytes(b"not an EFI loader")
            with self.assertRaises(core.RufusError):
                togo.pe_architecture(path)

    def test_partitions_and_minimum_capacity(self):
        for firmware in ("uefi", "bios", "both"):
            table, parts = togo.partitions(64 * togo.GIB + 512, firmware)
            self.assertEqual(table, "gpt" if firmware == "uefi" else "dos")
            self.assertEqual(parts[-1].kind, "basic")
            self.assertEqual(parts[0].bootable, firmware != "uefi")
            self.assertTrue(all(a.end < b.start for a, b in zip(parts, parts[1:])))
            self.assertEqual([p.kind for p in parts], ["esp", "msr", "basic"] if firmware == "uefi" else ["basic", "basic"])
            if table == "dos":
                with tempfile.TemporaryFile() as stream:
                    layout.write_layout(stream.fileno(), 64 * togo.GIB + 512, parts, table)
                    stream.seek(446)
                    record = stream.read(16)
                self.assertEqual(record[0], 0x80)
                self.assertEqual(record[4], 0x0c)
        for size in (0, 16 * togo.GIB, 64 * togo.GIB + 1):
            with self.assertRaises(core.RufusError):
                togo.partitions(size, "uefi")

    def test_edition_cpu_firmware_and_expanded_capacity_gate_deployment(self):
        selected = {"architecture": "arm64", "major": 10, "minor": 0, "expanded_size": 12 * togo.GIB}
        self.assertEqual(togo.validate_selection(selected, 64 * togo.GIB, "uefi")[0], "gpt")
        for firmware in ("bios", "both"):
            with self.assertRaisesRegex(core.RufusError, "ARM64.*UEFI"):
                togo.validate_selection(selected, 64 * togo.GIB, firmware)
        with self.assertRaisesRegex(core.RufusError, "does not match"):
            togo.validate_selection(selected, 64 * togo.GIB, "uefi", "amd64")
        selected["architecture"] = "x86"
        with self.assertRaisesRegex(core.RufusError, "32-bit.*BIOS"):
            togo.validate_selection(selected, 64 * togo.GIB, "uefi")
        self.assertEqual(togo.validate_selection(selected, 64 * togo.GIB, "both")[0], "dos")
        selected.update(architecture="amd64", expanded_size=27 * togo.GIB)
        with self.assertRaisesRegex(core.RufusError, "more space"):
            togo.validate_selection(selected, 32 * togo.GIB, "uefi")
        selected.update(major=6, minor=1)
        with self.assertRaisesRegex(core.RufusError, "Windows 8"):
            togo.validate_selection(selected, 64 * togo.GIB, "uefi")

    def test_guest_script_binds_job_and_checks_every_portability_step(self):
        token = "ab" * 16
        script = togo.provisioning_script(token, "both", WindowsOptions(privacy=True)).decode()
        self.assertIn("system.marker", script)
        self.assertIn(f".rufus-wtg-{token}.marker", script)
        self.assertIn("SanPolicy /t REG_DWORD /d 4", script)
        self.assertIn("PortableOperatingSystem /t REG_DWORD /d 1", script)
        self.assertIn("HiberbootEnabled /t REG_DWORD /d 0", script)
        self.assertIn("Current Default LastKnownGood", script)
        self.assertIn("/f ALL /c /v", script)
        self.assertIn("recoveryenabled no", script)
        self.assertIn("if exist W:\\Windows\\Panther\\unattend.xml exit /b 1", script)
        self.assertNotIn("findstr", script)
        self.assertNotIn("reg delete", script)
        with self.assertRaises(core.RufusError):
            togo.provisioning_script("bad & command", "uefi", WindowsOptions())

    def test_vm_exposes_only_private_image_and_firmware(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disk, code, variables = (root / name for name in ("disk.img", "code.fd", "vars.fd"))
            for path in (disk, code, variables, root / "seabios/vgabios-stdvga.bin"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            with patch.object(togo.shutil, "which", return_value="/usr/bin/qemu-fixture"), \
                 patch.object(togo, "firmware_files", return_value=(code, variables, root / "qemu")):
                command = togo.vm_command(disk, "amd64", "uefi", root)
            self.assertIn("-nodefaults", command)
            self.assertEqual(command[command.index("-nic") + 1], "none")
            self.assertEqual(sum("if=ide,format=raw,file=" in value for value in command), 1)
            self.assertNotIn("-virtfs", command)
            self.assertNotIn("-netdev", command)
            self.assertFalse(any("/dev/" in value for value in command))
            with self.assertRaises(core.RufusError):
                togo.vm_command(Path("/dev/null"), "amd64", "uefi", root)
            link = root / "link.img"
            link.symlink_to(disk)
            with self.assertRaises(core.RufusError):
                togo.vm_command(link, "amd64", "uefi", root)

    def test_existing_outputs_and_invalid_options_do_not_launch_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "keep.img"
            image.write_bytes(b"keep")
            with patch.object(togo, "run_vm") as vm:
                with self.assertRaises(core.RufusError):
                    togo.create_windows_to_go("missing.iso", image, index=1)
                self.assertEqual(image.read_bytes(), b"keep")
                with self.assertRaises(core.RufusError):
                    togo.create_windows_to_go("missing.iso", image, index=1, options=WindowsOptions(bypass_hardware=True))
                with self.assertRaises(core.RufusError):
                    togo.create_windows_to_go("missing.iso", image, index=0)
                vm.assert_not_called()

    def test_arm64_vm_uses_arm_firmware_and_virtual_usb_without_x86_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disk, code, variables = (root / name for name in ("disk.img", "code.fd", "vars.fd"))
            for path in (disk, code, variables):
                path.write_bytes(b"fixture")
            with patch.object(togo.shutil, "which", return_value="/usr/bin/qemu-system-aarch64") as lookup, \
                 patch.object(togo, "firmware_files", return_value=(code, variables, root)):
                command = togo.vm_command(disk, "arm64", "uefi", root)
            lookup.assert_called_once_with("qemu-system-aarch64")
            self.assertIn("virt,accel=tcg,gic-version=3", command)
            self.assertIn("usb-storage,drive=target", command)
            self.assertNotIn("-bios", command)
            with self.assertRaises(core.RufusError):
                togo.vm_command(disk, "arm64", "bios", root)

    def test_device_access_waits_for_successful_build(self):
        device = devices.Device("/dev/TEST_ONLY", 64 * togo.GIB, "test", "", "999:999", (), ())
        with patch.object(devices, "guard"), patch.object(togo, "create_windows_to_go", side_effect=core.RufusError("build failed")), \
             patch.object(devices, "write_to_device") as writer, patch.object(devices, "UDisks") as service:
            with self.assertRaisesRegex(core.RufusError, "build failed"):
                devices.windows_to_go_device("source.iso", device, index=2)
            writer.assert_not_called()
            service.assert_not_called()

    def test_cli_inspection_and_file_output_never_discover_drives(self):
        from rufus_linux.cli import main
        with patch.object(togo, "inspect_iso", return_value=[{"index": 2}]), \
             patch.object(togo, "create_windows_to_go", return_value={"mode": "windows-to-go"}) as build, \
             patch("rufus_linux.cli.discover_devices") as discover, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["windows-images", "source.iso"]), 0)
            self.assertEqual(main(["windows-to-go", "source.iso", "--output", "new.img", "--index", "2"]), 0)
            self.assertEqual(build.call_args.kwargs["index"], 2)
            discover.assert_not_called()

    def test_cancellation_terminates_the_private_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "vm.log"
            cancel = threading.Event()
            timer = threading.Timer(0.15, cancel.set)
            timer.start()
            try:
                with log.open("xb") as output, self.assertRaises(core.Cancelled):
                    togo.run_vm(["/bin/sleep", "30"], output, 60, cancel)
            finally:
                timer.cancel()


TOOLS = all(shutil.which(name) for name in ("wimlib-imagex", "mkfs.ntfs", "ntfscp", "ntfscat", "mkfs.vfat",
                                           "mcopy", "mtype", "mdel", "mdeltree", "fsck.fat"))


@unittest.skipUnless(TOOLS, "Windows To Go image tools are missing")
class NativeDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rufus-wtg-native-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "sources").mkdir(parents=True)
        (self.source / "efi/boot").mkdir(parents=True)
        (self.source / "boot").mkdir()
        (self.source / "efi/boot/bootx64.efi").write_bytes(efi_fixture(0x8664))
        (self.source / "boot/BCD").write_bytes(b"fixture BCD")
        (self.source / "bootmgr").write_bytes(b"fixture bootmgr")
        tree = self.root / "os"
        (tree / "Windows/System32/config").mkdir(parents=True)
        (tree / "Windows/System32/config/SYSTEM").write_bytes(b"fixture SYSTEM, not a registry")
        edition = tree / "Windows/edition.txt"
        archive = self.source / "sources/install.wim"
        for number, value in ((1, b"first edition"), (2, b"second edition")):
            edition.write_bytes(value)
            core.run_tool(["wimlib-imagex", "capture" if number == 1 else "append", str(tree), str(archive), f"Edition {number}"])
            self.metadata(archive, number)
        pe = self.root / "pe"
        (pe / "Windows/System32").mkdir(parents=True)
        (pe / "Windows/System32/winpeshl.ini").write_text("fixture original shell")
        boot = self.source / "sources/boot.wim"
        core.run_tool(["wimlib-imagex", "capture", str(pe), str(boot), "WinPE fixture", "--boot"])
        self.metadata(boot, 1)
        self.work = self.root / "work"
        self.work.mkdir()

    def metadata(self, archive, index):
        core.run_tool(["wimlib-imagex", "info", str(archive), str(index),
                       "--image-property=WINDOWS/ARCH=9", "--image-property=WINDOWS/VERSION/MAJOR=10",
                       "--image-property=WINDOWS/VERSION/MINOR=0", "--image-property=WINDOWS/VERSION/BUILD=26100"])

    def fake_vm(self, success=True):
        def execute(command, log, timeout, cancel, progress):
            job = next((self.work / "system-files").glob("rufus-wtg-*"))
            token = (job / "system.marker").read_text()
            fat = f"{self.work / 'provision.img'}@@{togo.MIB}"
            status = self.work / "status.txt"
            status.write_text(token if success else "FAILED")
            guest_log = self.work / "guest.log"
            guest_log.write_text("fixture VM completion; not a Windows boot")
            for local, target in ((status, "status.txt"), (guest_log, "provision.log")):
                core.run_tool(["mcopy", "-o", "-i", fat, str(local), "::/" + job.name + "/" + target])
        return execute

    def test_selected_wim_applies_native_ntfs_and_only_verified_success_releases_image(self):
        with patch.object(togo, "vm_command", return_value=["fixture-vm"]), \
             patch.object(togo, "run_vm", side_effect=self.fake_vm()), patch.object(devices, "UDisks") as service:
            image, selected, parts, table = togo._build(self.source, self.work, 2, 32 * togo.GIB, "uefi",
                WindowsOptions(), self.root / "build.log", 60)
        service.assert_not_called()
        self.assertEqual(selected["index"], 2)
        self.assertEqual(table, "gpt")
        value = subprocess.check_output(["ntfscat", str(self.work / "windows.ntfs"), "/Windows/edition.txt"])
        self.assertEqual(value, b"second edition")
        self.assertEqual([part.kind for part in parts], ["esp", "msr", "basic"])
        missing = subprocess.run(["mtype", "-i", f"{image}@@{togo.MIB}", "::/sources/boot.wim"], capture_output=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("fixture VM completion", (self.root / "build.log").read_text())
        self.assertEqual((self.root / "build.log").stat().st_mode & 0o777, 0o600)
        marker = self.root / "marker.txt"
        evidence = b"RUFUS_WINDOWS_TOGO_BOOT_OK\r\nSanPolicy REG_DWORD 0x4\r\nPortableOperatingSystem REG_DWORD 0x1\r\n"
        marker.write_bytes(evidence)
        core.run_tool(["ntfscp", str(self.work / "windows.ntfs"), str(marker), "/rufus-wtg-test.txt"])
        self.assertEqual(resident_root_file(self.work / "windows.ntfs", 0, "rufus-wtg-test.txt"), evidence)
        self.assertIsNone(resident_root_file(self.work / "windows.ntfs", 0, "edition.txt"))  # Not a root file.
        self.assertTrue(valid_evidence(evidence))
        self.assertFalse(valid_evidence(evidence.replace(b"SanPolicy REG_DWORD 0x4", b"SanPolicy REG_DWORD 0x1")))

    def test_solid_esd_applies_selected_windows_image(self):
        archive = self.source / "sources/install.wim"
        esd = archive.with_suffix(".esd")
        core.run_tool(["wimlib-imagex", "export", str(archive), "2", str(esd), "--compress=LZMS", "--solid", "--check"])
        archive.unlink()
        with patch.object(togo, "vm_command", return_value=["fixture-vm"]), \
             patch.object(togo, "run_vm", side_effect=self.fake_vm()):
            _, selected, _, _ = togo._build(self.source, self.work, 1, 32 * togo.GIB, "uefi",
                WindowsOptions(), self.root / "esd.log", 60)
        self.assertEqual(selected["index"], 1)
        value = subprocess.check_output(["ntfscat", str(self.work / "windows.ntfs"), "/Windows/edition.txt"])
        self.assertEqual(value, b"second edition")

    def test_failed_guest_status_does_not_release_a_bootable_result(self):
        with patch.object(togo, "vm_command", return_value=["fixture-vm"]), \
             patch.object(togo, "run_vm", side_effect=self.fake_vm(False)):
            with self.assertRaisesRegex(core.RufusError, "boot/policy configuration failed"):
                togo._build(self.source, self.work, 2, 32 * togo.GIB, "uefi",
                    WindowsOptions(), self.root / "failed.log", 60)
        self.assertTrue((self.root / "failed.log").is_file())

    def test_architecture_mismatch_fails_before_vm_or_ntfs_application(self):
        with patch.object(togo, "run_vm") as vm, patch.object(togo, "apply_ntfs_image") as apply:
            with self.assertRaisesRegex(core.RufusError, "does not match"):
                togo._build(self.source, self.work, 2, 32 * togo.GIB, "uefi", WindowsOptions(architecture="arm64"),
                            self.root / "unused.log", 60)
            vm.assert_not_called()
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
