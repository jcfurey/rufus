import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from live_smoke import disable_persistence, instrument_config
from live_userspace import initrd_files
from live_vm import LiveVM


class LiveVMTests(unittest.TestCase):
    def test_arm_firmware_boot_has_no_direct_kernel_or_x86_devices(self):
        for architecture, program, cpu in (("arm64", "qemu-system-aarch64", "cortex-a57"),
                                            ("arm", "qemu-system-arm", "cortex-a15")):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                code, variables = root / "code.fd", root / "template.fd"
                code.write_bytes(b"firmware")
                variables.write_bytes(b"fresh variables")
                with patch("live_vm.shutil.which", return_value="/tools/bin/" + program), \
                     patch("live_vm.firmware_paths", side_effect=AssertionError("x86 firmware requested")):
                    vm = LiveVM(architecture, firmware_code=code, firmware_vars=variables)
                    self.assertEqual(vm.modes("all"), ("uefi",))
                    for mode in ("bios", "direct"):
                        with self.assertRaisesRegex(RuntimeError, "require --firmware uefi"):
                            vm.modes(mode)
                    command = vm.command("uefi", root / "disk,1.qcow2", root, Path("kernel"), Path("initrd"), "casper")
                    self.assertEqual(command[0], "/tools/bin/" + program)
                    self.assertEqual(command[command.index("-cpu") + 1], cpu)
                    self.assertEqual(command[command.index("-machine") + 1], "virt,accel=tcg")
                    self.assertEqual(command[command.index("-nic") + 1], "none")
                    for option in ("-kernel", "-initrd", "-append", "-bios"):
                        self.assertNotIn(option, command)
                    self.assertNotIn("VGA", " ".join(command))
                    self.assertIn("disk,,1.qcow2", " ".join(command))
                    self.assertIn("readonly=on,file=" + str(code), " ".join(command))
                    self.assertEqual((root / "uefi-vars.fd").read_bytes(), b"fresh variables")
                    (root / "uefi-vars.fd").write_bytes(b"guest modification")
                    control = vm.command("uefi", root / "control.qcow2", root, Path("kernel"), Path("initrd"),
                                         "casper", nvram_name="control-vars.fd")
                    self.assertIn(str(root / "control-vars.fd"), " ".join(control))
                    self.assertEqual((root / "control-vars.fd").read_bytes(), b"fresh variables")
                    self.assertEqual(variables.read_bytes(), b"fresh variables")
                    with self.assertRaisesRegex(RuntimeError, "existing test NVRAM"):
                        vm.command("uefi", root / "disk.qcow2", root, Path("kernel"), Path("initrd"), "casper")

    def test_x86_direct_control_keeps_correct_disabled_option_and_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bios = root / "bios.bin"
            bios.write_bytes(b"bios")
            with patch("live_vm.shutil.which", return_value="/tools/bin/qemu-system-x86_64"), \
                 patch("live_vm.firmware_paths", return_value=(bios, root)):
                vm = LiveVM("x86_64")
                self.assertEqual(vm.modes("all"), ("bios", "uefi"))
                for kind, flags in (("casper", "boot=casper nopersistent"), ("debian", "boot=live nopersistence")):
                    command = vm.command("direct", root / "disk.qcow2", root, Path("kernel"), Path("initrd"),
                                         kind, persistent=False)
                    append = command[command.index("-append") + 1]
                    self.assertTrue(append.startswith(flags + " "))
                    self.assertIn("console=ttyS0,115200", append)
                    self.assertNotIn("ip=", append)
                    self.assertIn("-bios", command)

    def test_arm_console_reaches_all_userspace_instrumentation(self):
        hook, files = initrd_files("casper", "abc123", "ttyAMA0")
        combined = b"\n".join([hook, *(data for _, data, _ in files)])
        self.assertIn(b"serial-getty@ttyAMA0.service", combined)
        self.assertIn(b"TTYPath=/dev/ttyAMA0", combined)
        self.assertNotIn(b"ttyS0", combined)
        self.assertNotIn(b"@CONSOLE@", combined)
        config, count = instrument_config('linux "/casper/vmlinuz" "boot=casper" "persistent"\n', "casper", "ttyAMA0")
        self.assertEqual(count, 1)
        self.assertIn('"console=ttyAMA0,115200"', config)
        self.assertNotIn("ttyS0", config)

    def test_firmware_control_disables_only_kernel_persistence_tokens(self):
        for kind, option, boot in (("casper", "persistent", "casper"), ("debian", "persistence", "live")):
            text = (f'# {option}\nmenuentry "{option}" {{\n'
                    f'linux "/casper/vmlinuz" "boot={boot}" "{option}" "{option}-path=/test"\n'
                    f'APPEND boot={boot} {option}\n}}\n')
            disabled = disable_persistence(text, kind)
            self.assertIn(f'"no{option}"', disabled)
            self.assertIn(f'APPEND boot={boot} no{option}\n', disabled)
            self.assertIn(f'"{option}-path=/test"', disabled)
            self.assertIn(f'# {option}\nmenuentry "{option}"', disabled)
            self.assertEqual(disable_persistence(disabled, kind), disabled)


if __name__ == "__main__":
    unittest.main()
