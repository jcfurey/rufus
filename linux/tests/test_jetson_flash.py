"""Direct recovery tests use synthetic BSPs, sysfs, and child processes only."""

import contextlib
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rufus_linux import jetson_flash as flash
from rufus_linux.core import Cancelled, RufusError


def fixture(root, release="39.2.1", machine=62):
    """No NVIDIA executable code; suitable for offline GTK tests too."""
    def put(name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.encode() if isinstance(data, str) else data)
    put("flash.sh", "#!/bin/bash\n# --usb-instance --read-info\nprintf 'fixture only\\n'\n")
    put("tools/kernel_flash/l4t_initrd_flash.sh", "#!/bin/bash\n# --usb-instance\nprintf 'fixture initrd only\\n'\n")
    put("kernel/Image", b"fixture kernel, never booted")
    major, revision = release.split(".", 1)
    version = f"# R{major} (release), REVISION: {revision}, fixture\n"
    put("nv_tegra/nv_tegra_release", version)
    put("rootfs/etc/nv_tegra_release", version)
    put("rootfs/etc/os-release", "ID=ubuntu\n")
    for board in {*flash.REFERENCE_PROFILES.values(), "jetson-nano-devkit-emmc", "jetson-xavier-nx-devkit-emmc", "custom-carrier"}:
        put(board + ".conf", "# fixture board configuration\n")
    for layout in ("tools/kernel_flash/flash_l4t_t234_nvme.xml", "tools/kernel_flash/flash_l4t_t264_nvme.xml",
                   "tools/kernel_flash/flash_l4t_external.xml", "bootloader/generic/cfg/flash_t234_qspi.xml"):
        put(layout, "<partition_layout/>\n")
    elf = bytearray(64)
    elf[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", elf, 18, machine)
    put("bootloader/tegrarcm_v2", elf)
    return root


class FlashPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rufus-bsp-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = fixture(Path(self.temp.name) / "Linux_for_Tegra")
        self.target = {"usb_port": "1-2.4", "vendor_id": "0955", "product_id": "7523", "model": "orin-nano",
                       "module_candidates": "P3767-0003/0005", "serial": "fixture", "busnum": "1",
                       "devnum": "4", "sysfs_inode": 123}
        self.request = flash.FlashRequest(str(self.root), "orin-nano", "nvme", "jetson-orin-nano-devkit", "1-2.4")
        for target, name, value in ((flash, "connected_devices", [self.target]), (flash, "_host_arch", "x86_64"),
                                    (flash.shutil, "which", "/usr/bin/fixture")):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def plan(self, **kwargs):
        return flash.make_plan(replace(self.request, **kwargs))

    def test_plan_is_read_only_and_external_installs_use_initrd(self):
        with patch.object(flash.subprocess, "Popen") as execute:
            plan = self.plan()
            self.assertTrue(plan["can_execute"], plan["issues"])
            self.assertIn("l4t_initrd_flash.sh", plan["command"][1])
            self.assertIn("--external-device", plan["command"])
            self.assertEqual(plan["command"][-4:], ["--usb-instance", "1-2.4", self.request.board, "external"])
            self.assertIn("-c bootloader/generic/cfg/flash_t234_qspi.xml", plan["command"])
            self.assertIn("internal boot firmware", plan["scope"])
            self.assertNotIn("--direct", plan["command"])
            execute.assert_not_called()

    def test_external_only_and_explicit_jetson_slot_layout(self):
        plan = self.plan(update_firmware=False, external_device="nvme1n1p1", layout="tools/kernel_flash/flash_l4t_external.xml")
        self.assertTrue(plan["can_execute"])
        self.assertIn("--external-only", plan["command"])
        self.assertNotIn("-p", plan["command"])
        self.assertIn("nvme1n1p1", plan["command"])
        self.assertIn("skip internal firmware", plan["scope"])

    def test_model_storage_command_matrix(self):
        for model, (_, targets, _, _) in flash.MODELS.items():
            for storage in targets:
                with self.subTest(model=model, storage=storage):
                    self.target["model"] = model
                    board = flash.REFERENCE_PROFILES[model]
                    if storage == "emmc" and model in ("nano", "xavier-nx"):
                        board += "-emmc"
                    plan = self.plan(model=model, storage=storage, board=board)
                    self.assertTrue(plan["can_execute"], plan["issues"])
                    if storage in ("nvme", "usb") or (storage == "sd" and model in ("orin-nano", "agx-orin")):
                        self.assertIn("--external-device", plan["command"])
                    else:
                        self.assertNotIn("--external-device", plan["command"])

    def test_legacy_internal_root_device_and_profiles(self):
        fixture(self.root, "32.7.6")
        self.target["model"] = "nano"
        plan = self.plan(model="nano", storage="emmc", board="jetson-nano-devkit-emmc")
        self.assertEqual(Path(plan["command"][1]).name, "flash.sh")
        self.assertEqual(plan["command"][-1], "mmcblk0p1")
        with self.assertRaisesRegex(RufusError, "eMMC"):
            self.plan(model="nano", storage="sd", board="jetson-nano-devkit-emmc")

    def test_read_info_does_not_require_prepared_rootfs(self):
        (self.root / "rootfs/etc/nv_tegra_release").unlink()
        plan = self.plan(action="probe")
        self.assertTrue(plan["can_execute"])
        self.assertIn("--read-info", plan["command"])
        self.assertNotIn("--external-device", plan["command"])
        self.assertIn("EEPROM", plan["scope"])
        (self.root / "flash.sh").write_text("# --usb-instance\n")
        with self.assertRaisesRegex(RufusError, "read-info"):
            self.plan(action="probe")

    def test_unsafe_or_missing_inputs_never_execute(self):
        cases = ({"board": "../profile"}, {"board": "profile;touch_bad"}, {"board": "absent"},
                 {"usb_port": "1-2;bad"}, {"layout": "/tmp/host.xml"}, {"layout": "../host.xml"},
                 {"layout": "$(touch_bad).xml"}, {"external_device": "/dev/sdb"},
                 {"external_device": "sda1"}, {"model": "thor", "storage": "emmc"},
                 {"board": "jetson-agx-orin-devkit"})
        with patch.object(flash.subprocess, "Popen") as execute:
            for values in cases:
                with self.subTest(values=values), self.assertRaises(RufusError):
                    self.plan(**values)
            execute.assert_not_called()
        (self.root / "custom-carrier.conf").unlink()
        (self.root / "custom-carrier.conf").symlink_to("/etc/passwd")
        with self.assertRaises(RufusError):
            self.plan(board="custom-carrier")

    def test_readiness_and_architecture_checks(self):
        (self.root / "rootfs/etc/nv_tegra_release").write_text("# R35 (release), REVISION: 6.2\n")
        with patch.object(flash, "_host_arch", return_value="arm64"):
            plan = self.plan()
        self.assertFalse(plan["can_execute"])
        self.assertTrue(any("rootfs" in issue for issue in plan["issues"]))
        self.assertTrue(any("architecture" in issue for issue in plan["issues"]))
        fixture(self.root, machine=3)  # NVIDIA's actual R39 tools are static i386 ELF.
        self.assertTrue(self.plan()["can_execute"])
        fixture(self.root, machine=183)
        with patch.object(flash, "_host_arch", return_value="arm64"):
            self.assertTrue(self.plan()["can_execute"])

    def test_missing_dependencies_target_and_wrong_model_block_authorization(self):
        with patch.object(flash.shutil, "which", return_value=None):
            self.assertTrue(any("missing tools" in issue for issue in self.plan()["issues"]))
        for target in ([], [{**self.target, "model": "orin-nx"}]):
            with patch.object(flash, "connected_devices", return_value=target), patch.object(flash.subprocess, "Popen") as execute:
                plan = self.plan()
                with self.assertRaises(RufusError):
                    flash.execute_plan(plan, plan["confirmation"], self.root / "unused.log")
                execute.assert_not_called()
        self.assertFalse(self.plan(usb_port="")["can_execute"])

    def test_review_token_and_every_changed_input_reject_stale_plan(self):
        plan = self.plan()
        with self.assertRaisesRegex(RufusError, "exact confirmation"):
            flash.verify_plan(plan, "yes")
        with self.assertRaisesRegex(RufusError, "exact confirmation"):
            flash.verify_plan({**plan, "confirmation": "yes"}, "yes")
        self.target["devnum"] = "8"
        with self.assertRaisesRegex(RufusError, "changed"):
            flash.verify_plan(plan, plan["confirmation"])
        self.target["devnum"] = "4"
        changed = {**plan, "request": {**plan["request"], "update_firmware": False}}
        with self.assertRaisesRegex(RufusError, "changed"):
            flash.verify_plan(changed, plan["confirmation"])
        (self.root / "flash.sh").write_text("# changed --usb-instance --read-info\n")
        with self.assertRaisesRegex(RufusError, "changed"):
            flash.verify_plan(plan, plan["confirmation"])

    def test_json_command_is_rebuilt_instead_of_trusted(self):
        plan = self.plan()
        forged = {**plan, "command": ["/bin/sh", "-c", "bad"], "can_execute": False}
        verified = flash.verify_plan(forged, plan["confirmation"])
        self.assertEqual(verified["command"], plan["command"])

    def test_cli_plan_and_inspection_never_authorize_or_open_disks(self):
        from rufus_linux.cli import main
        args = ["jetson-flash", "--bsp", str(self.root), "--model", "orin-nano", "--storage", "nvme",
                "--board", self.request.board, "--usb-port", self.request.usb_port]
        with patch.object(flash, "execute_plan") as execute, patch("rufus_linux.cli.discover_devices") as disks:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(args), 0)
            self.assertTrue(json.loads(output.getvalue())["can_execute"])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["jetson-bsp", str(self.root)]), 0)
            self.assertEqual(json.loads(output.getvalue())["release"], "39.2.1")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["--execute"]), 1)
            execute.assert_not_called()
            disks.assert_not_called()

    def test_root_helper_revalidates_and_locks_without_running_vendor_code(self):
        plan = self.plan()
        saved = self.root / "plan.json"
        saved.write_text(json.dumps(plan))
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, "rb") as input_stream, io.TextIOWrapper(input_stream) as text_input, \
             patch.object(flash.sys, "stdin", text_input), \
             patch.object(flash.sys, "argv", ["helper", str(saved), plan["plan_id"], plan["confirmation"]]), \
             patch.object(flash.os, "geteuid", return_value=0), \
             patch.object(flash, "JOB_LOCK", self.root / "job.lock"), \
             patch.object(flash, "_stream_command") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(flash._privileged_main(), 0)
            self.assertEqual(run.call_args.args[:2], (plan["command"], str(self.root)))
            self.assertEqual([json.loads(row)["type"] for row in output.getvalue().splitlines()], ["started", "complete"])
            self.target["devnum"] = "5"
            run.reset_mock()
            self.assertEqual(flash._privileged_main(), 1)
            run.assert_not_called()

    def test_root_helper_refuses_concurrent_job_and_cancelled_authorization(self):
        plan = self.plan()
        saved, lock_path = self.root / "plan.json", self.root / "job.lock"
        saved.write_text(json.dumps(plan))
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # EOF simulates an authorization cancelled by the client.
        with os.fdopen(read_fd, "rb") as stream, io.TextIOWrapper(stream) as text_input, \
             patch.object(flash.sys, "stdin", text_input), \
             patch.object(flash.sys, "argv", ["helper", str(saved), plan["plan_id"], plan["confirmation"]]), \
             patch.object(flash.os, "geteuid", return_value=0), patch.object(flash, "JOB_LOCK", lock_path), \
             patch.object(flash, "_stream_command") as run, contextlib.redirect_stdout(io.StringIO()) as output:
            with lock_path.open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(flash._privileged_main(), 1)
            self.assertIn("already running", output.getvalue())
            self.assertEqual(flash._privileged_main(), 130)
            run.assert_not_called()

    def client(self, helper_source, cancel=None, authorization="pkexec"):
        plan = self.plan()
        script = self.root / "mock-helper.py"
        script.write_text(helper_source.replace('"type":"started"', '"type":"started","plan_id":' + json.dumps(plan["plan_id"]))
                          .replace('"type":"complete"', '"type":"complete","plan_id":' + json.dumps(plan["plan_id"])))
        real_popen = subprocess.Popen
        launches = []
        def mock_popen(command, **kwargs):
            launches.append((command, kwargs.copy()))
            return real_popen([sys.executable, str(script)], **kwargs)
        log = self.root / "job.jsonl"
        with patch.object(flash.os, "geteuid", return_value=1000), patch.object(flash.subprocess, "Popen", side_effect=mock_popen):
            result = flash.execute_plan(plan, plan["confirmation"], log, cancel=cancel, authorization=authorization)
        return result, launches, log

    def test_execution_client_one_authorization_and_persistent_log(self):
        result, launches, log = self.client('import json\nfor e in [{"type":"started"},{"type":"log","line":"writing fixture"},{"type":"complete"}]:\n print(json.dumps(e),flush=True)\n')
        self.assertEqual(len(launches), 1)
        self.assertIn("-I", launches[0][0])
        self.assertTrue(launches[0][1]["start_new_session"])
        self.assertEqual(result["status"], "NVIDIA BSP command completed")
        self.assertIn("writing fixture", log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        plan = self.plan()
        with patch.object(flash.subprocess, "Popen") as run, self.assertRaisesRegex(RufusError, "already exists"):
            flash.execute_plan(plan, plan["confirmation"], log)
        run.assert_not_called()

    def test_sudo_keeps_controlling_terminal_for_password_authentication(self):
        _, launches, _ = self.client('print(\'{"type":"started"}\',flush=True)\nprint(\'{"type":"complete"}\',flush=True)\n', authorization="sudo")
        self.assertFalse(launches[0][1]["start_new_session"])
        self.assertEqual(launches[0][0][1], "--")

    def test_execution_client_reports_failure_and_cancel(self):
        with self.assertRaisesRegex(RufusError, "fixture failure"):
            self.client('print(\'{"type":"error","message":"fixture failure"}\',flush=True)\nraise SystemExit(3)\n')
        (self.root / "job.jsonl").unlink()
        cancel = threading.Event()
        timer = threading.Timer(0.25, cancel.set)
        timer.start()
        try:
            with self.assertRaises(Cancelled):
                self.client('import sys\nprint(\'{"type":"started"}\',flush=True)\nsys.stdin.buffer.read()\nprint(\'{"type":"error","message":"cancelled","cancelled":true}\',flush=True)\n', cancel)
        finally:
            timer.cancel()


class RecoveryDeviceTests(unittest.TestCase):
    def test_recognized_pids_only_and_replug_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(flash, "USB_ROOT", Path(directory)):
            root = Path(directory)
            for i, (product, model) in enumerate(flash.RECOVERY_IDS.items(), 1):
                path = root / f"1-{i}"
                path.mkdir()
                for name, value in {"idVendor": "0955", "idProduct": product, "busnum": "1", "devnum": str(i)}.items():
                    (path / name).write_text(value)
            bad = root / "1-99"
            bad.mkdir()
            (bad / "idVendor").write_text("0955")
            (bad / "idProduct").write_text("ffff")
            devices = flash.connected_devices()
            self.assertEqual(len(devices), len(flash.RECOVERY_IDS))
            first = next(d for d in devices if d["usb_port"] == "1-1")
            (root / "1-1/devnum").write_text("80")
            second = next(d for d in flash.connected_devices() if d["usb_port"] == "1-1")
            self.assertNotEqual(first, second)


class StreamTests(unittest.TestCase):
    def test_cancel_kills_resistant_descendant_after_shell_exits(self):
        cancel = threading.Event()
        def ready(line):
            if line == "ready":
                cancel.set()
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); time.sleep(30)"
        with tempfile.TemporaryDirectory() as directory, patch.object(flash, "TERMINATE_GRACE", 0.2):
            started = time.monotonic()
            with self.assertRaises(Cancelled):
                flash._stream_command([sys.executable, "-c", parent, child], directory, ready, cancel.is_set)
            self.assertLess(time.monotonic() - started, 3)

    def test_stdout_stderr_bounded_lines_and_exit_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            lines = []
            command = [sys.executable, "-c", "import sys; print('begin'); print('err',file=sys.stderr); print('x'*40000)"]
            flash._stream_command(command, directory, lines.append, lambda: False)
            self.assertIn("begin", lines)
            self.assertIn("err", lines)
            self.assertTrue(all(len(row) <= 16384 for row in lines))
            with self.assertRaisesRegex(RufusError, r"failed \(7\).*fixture"):
                flash._stream_command(["/bin/sh", "-c", "echo fixture; exit 7"], directory, lines.append, lambda: False)

    def test_cancel_terminates_job_group_and_drops_environment_overrides(self):
        lines, cancel = [], threading.Event()
        def receive(line):
            lines.append(line)
            cancel.set()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BOARDID": "spoof", "SKIP_EEPROM_CHECK": "1"}):
            with self.assertRaises(Cancelled):
                flash._stream_command(["/bin/sh", "-c", 'echo "${BOARDID-unset} ${SKIP_EEPROM_CHECK-unset}"; sleep 30 & wait'],
                                      directory, receive, cancel.is_set)
        self.assertEqual(lines, ["unset unset"])


if __name__ == "__main__":
    unittest.main()
