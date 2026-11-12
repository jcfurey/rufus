import copy
import unittest
import xml.etree.ElementTree as ET

from rufus_linux import windows
from windows_setup_smoke import instrument_answer, validate_control, validate_evidence

NS = {"u": windows.UNATTEND}


class WindowsSetupAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.options = windows.WindowsOptions(bypass_hardware=True, offline_account=True,
            local_account="rufus-test", privacy=True, disable_encryption=True,
            locale="en-US", timezone="Central Standard Time")

    def evidence(self):
        sid = "S-1-5-21-12345-23456-34567-1001"
        return {"nonce": "test-nonce", "errors": [], "username": "rufus-test", "sid": sid,
                "account": {"sid": sid, "source": "Local", "enabled": True},
                "interactive": True, "session": 1, "explorerPids": [1234], "explorerOwners": [sid],
                "setup": {"imageState": "IMAGE_STATE_COMPLETE", "systemSetupInProgress": 0, "oobeInProgress": 0},
                "userMarkerReadback": "test-nonce", "culture": "en-US", "systemLocale": "en-US",
                "uiLanguage": "en-US", "timezone": "Central Standard Time", "bypassNRO": 1,
                "preventDeviceEncryption": 1, "privacy": {"advertising": 0, "tailored": 0, "telemetry": 1}}

    def test_setup_console_system_identity_and_incomplete_oobe_cannot_pass(self):
        changes = ({"nonce": "old-run"}, {"errors": ["script failed"]}, {"username": "SYSTEM"},
                   {"sid": "S-1-5-18"}, {"session": 0}, {"interactive": False},
                   {"explorerPids": []}, {"explorerOwners": ["S-1-5-18"]},
                   {"setup": {"imageState": "IMAGE_STATE_SPECIALIZE_RESEAL_TO_OOBE", "systemSetupInProgress": 0, "oobeInProgress": 0}},
                   {"setup": {"imageState": "IMAGE_STATE_COMPLETE", "systemSetupInProgress": 1, "oobeInProgress": 1}},
                   {"userMarkerReadback": "different"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AssertionError):
                validate_evidence({**self.evidence(), **change}, "test-nonce", self.options)

    def test_actual_settings_and_portable_policy_are_required(self):
        good = self.evidence()
        validate_evidence(good, "test-nonce", self.options)
        for key, value in (("culture", "en-GB"), ("systemLocale", "en-GB"), ("uiLanguage", "en-GB"),
                           ("timezone", "Pacific Standard Time"), ("bypassNRO", None),
                           ("preventDeviceEncryption", 0), ("privacy", {"advertising": 1, "tailored": 1, "telemetry": 3})):
            with self.subTest(setting=key), self.assertRaises(AssertionError):
                validate_evidence({**good, key: value}, "test-nonce", self.options)
        with self.assertRaises(AssertionError):
            validate_evidence(good, "test-nonce", self.options, portable=True)
        validate_evidence({**good, "sanPolicy": 4, "portableOperatingSystem": 1}, "test-nonce", self.options, portable=True)

    def test_control_requires_actual_hardware_block_and_live_bypass_query(self):
        marker = "RUFUS_HARDWARE_CONTROL test-nonce\nERROR: The system was unable to find the specified registry key or value.\n"
        activity = "\n".join(f"CHWReqChecker: checked {name}, found HardBlock." for name in ("TpmVersion", "Memory", "UefiSecureBoot"))
        validate_control(marker, activity, "test-nonce")
        for bad_marker, bad_errors in ((marker, "timeout"), (marker, activity.replace("Memory", "Disk")),
                (marker.replace("test-nonce", "stale"), activity), (marker.splitlines()[0], activity),
                (marker + "BypassTPMCheck    REG_DWORD    0x00000001\n", activity)):
            with self.subTest(marker=bad_marker, errors=bad_errors), self.assertRaises(AssertionError):
                validate_control(bad_marker, bad_errors, "test-nonce")
        with self.assertRaisesRegex(RuntimeError, "cannot retain WinPE setup commands"):
            instrument_answer(windows.answer_file(self.options), "uefi", 6, control=True)

    def test_instrumentation_preserves_production_choices_and_adds_only_private_target(self):
        original = windows.answer_file(self.options)
        before = ET.fromstring(original)
        for firmware, partition in (("uefi", "3"), ("bios", "2")):
            with self.subTest(firmware=firmware):
                after = ET.fromstring(instrument_answer(original, firmware, 6))
                for pass_name in ("specialize", "oobeSystem"):
                    expected = before.find(f"u:settings[@pass='{pass_name}']", NS)
                    actual = copy.deepcopy(after.find(f"u:settings[@pass='{pass_name}']", NS))
                    for commands in actual.findall(".//u:FirstLogonCommands", NS):
                        commands.remove(list(commands)[-1])
                    # Ignore whitespace added by the serializer.
                    def values(node):
                        return node.tag, node.attrib, (node.text or "").strip(), [values(child) for child in node]
                    self.assertEqual(values(actual), values(expected))
                hardware_before = before.findall(".//u:RunSynchronousCommand/u:Path", NS)
                hardware_after = after.findall(".//u:RunSynchronousCommand/u:Path", NS)
                self.assertEqual([p.text for p in hardware_before], [p.text for p in hardware_after])
                self.assertEqual(after.findtext(".//u:InstallTo/u:DiskID", namespaces=NS), "0")
                self.assertEqual(after.findtext(".//u:InstallTo/u:PartitionID", namespaces=NS), partition)
                with self.assertRaisesRegex(RuntimeError, "existing installation target"):
                    instrument_answer(ET.tostring(after), firmware, 6)


if __name__ == "__main__":
    unittest.main()
