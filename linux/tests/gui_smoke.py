#!/usr/bin/python3
"""Opt-in GTK interaction test; discovery is empty and no disk is ever opened."""

import hashlib
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk
from rufus_linux import gui
from rufus_linux.devices import Device
from rufus_linux import jetson_flash
from test_jetson_flash import fixture


def main():
    app = Gtk.Application(application_id="ie.rufus.Linux.GuiTest", flags=Gio.ApplicationFlags.NON_UNIQUE)
    failed = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rufus-gui-test-") as directory:
        image = Path(directory) / "source.img"
        image.write_bytes(b"Rufus GTK checksum test" * 65536)
        expected = hashlib.sha256(image.read_bytes()).hexdigest()
        bsp = fixture(Path(directory) / "Linux_for_Tegra")
        state = {"phase": "initial"}

        def activate(app):
            try:
                window = gui.Window(app)
                window.present()
                assert not window.start.get_sensitive()
                window.mode.set_selected(2)
                assert window.format_box.get_visible()
                assert not window.image_row.get_visible()
                window.mode.set_selected(3)
                assert window.backup_format.get_visible()
                assert not window.hash_box.get_visible()
                assert window.start.get_label() == "Save image"
                window.mode.set_selected(4)
                assert window.freedos_box.get_visible()
                assert not window.image_row.get_visible()
                assert window.start.get_label() == "Start"
                window.mode.set_selected(5)
                assert window.live_box.get_visible()
                assert window.hash_box.get_visible()
                assert window.image_row.get_visible()
                assert not window.windows_box.get_visible()
                assert window.persistence.get_value_as_int() == 1024
                assert "persistence" in window.mode_note.get_text()
                window.persistence.set_value(512)
                window.casper_label.set_selected(1)
                window.live_layout.set_selected(1)
                assert window.live_target.get_visible()
                assert window.live_bootloader.get_visible()
                window.live_bootloader.set_selected(2)
                assert "Syslinux" in window.mode_note.get_text()
                window.live_bootloader.set_selected(0)
                window.live_arch.set_selected(2)
                window.live_persist.set_active(False)
                assert not window.persistence.get_sensitive()
                assert "ARM" in window.mode_note.get_text()
                window.mode.set_selected(6)
                assert window.jetson_box.get_visible()
                assert window.image_row.get_visible()
                assert "USB flash drive" in window.mode_note.get_text()
                window.jetson_method.set_selected(2)
                assert not window.device_row.get_visible()
                assert not window.image_row.get_visible()
                assert window.jetson_direct.get_visible()
                assert window.start.get_label() == "Review BSP plan"
                assert not window.start.get_sensitive()
                window.jetson_method.set_selected(3)
                assert not window.jetson_direct.get_visible()
                assert window.start.get_label() == "Open SDK Manager"
                assert window.start.get_sensitive()
                for index, (_, targets, _, _) in enumerate(gui.JETSON_MODELS.values()):
                    window.jetson_model.set_selected(index)
                    window.jetson_storage.set_selected(("nvme", "sd", "emmc", "usb").index(targets[0]))
                    assert window.start.get_sensitive()
                window.jetson_method.set_selected(0)
                assert not window.start.get_sensitive()  # TX1 has no modern installer ISO.
                window.mode.set_selected(1)
                assert window.device_row.get_visible()
                assert window.windows_box.get_visible()
                assert window.windows_media.get_visible()
                window.win_filesystem.set_selected(1)
                window.win_firmware.set_selected(1)
                assert "NTFS" in window.mode_note.get_text()
                assert "BIOS/MBR" in window.mode_note.get_text()
                window.win_filesystem.set_selected(0)
                window.win_firmware.set_selected(0)
                assert not window.backup_format.get_visible()
                window.win_user.set_text("gui-test-user")
                window.win_privacy.set_active(True)
                options = window.windows_options()
                options.validate()
                assert options.local_account == "gui-test-user" and options.privacy
                assert not window.format_box.get_visible()
                assert window.image_row.get_visible()
                window.refreshed([Device("/dev/TEST_ONLY", 1024**3, "UI TEST", "", "999:999", (), ())], None)
                window.drive.set_selected(1)
                window.image = str(image)
                window.update_controls()
                assert window.start.get_sensitive()
                window.start_clicked(None)
                assert window.confirmation is not None

                def cancel_confirmation():
                    window.confirmation.response(Gtk.ResponseType.CANCEL)
                    assert not window.busy
                    assert window.confirmation is None
                    window.mode.set_selected(6)
                    window.jetson_model.set_selected(0)
                    window.jetson_storage.set_selected(0)
                    window.jetson_method.set_selected(2)
                    window.jetson_bsp.set_text(str(bsp))
                    window.show_bsp((jetson_flash.inspect_bsp(bsp), []))
                    assert window.jetson_board.get_text() == "jetson-orin-nano-devkit"
                    assert window.start.get_sensitive()
                    state["phase"] = "review"
                    window.start_clicked()
                    return False
                # Let Wayland configure the transient window before simulating
                # a user closing it; responding during activate precedes mapping.
                GLib.timeout_add(350, cancel_confirmation)

                def completed():
                    try:
                        if state["phase"] == "review" and window.confirmation is not None:
                            assert not window.confirmation.get_widget_for_response(Gtk.ResponseType.ACCEPT).get_sensitive()
                            window.confirmation.response(Gtk.ResponseType.CANCEL)
                            assert not window.busy and window.confirmation is None
                            state["phase"] = "hash"
                            window.mode.set_selected(1)
                            window.calculate_hash()
                            return True
                        if window.busy or window.confirmation is not None:
                            assert time.monotonic() - started < 15, "GTK worker timed out"
                            return True
                        assert window.hash.get_text() == expected
                        assert window.progress.get_fraction() == 1
                        assert not window.stop.get_sensitive()
                        print("PASS: GTK modes, BSP inspection/offline review/cancellation, drive confirmation, async checksum")
                    except Exception as exc:
                        failed.append(exc)
                    app.quit()
                    return False
                GLib.timeout_add(100, completed)
            except Exception as exc:
                failed.append(exc)
                app.quit()

        app.connect("activate", activate)
        with patch.object(gui, "discover_devices", return_value=[]), patch.object(jetson_flash, "execute_plan") as execute:
            app.run([])
            execute.assert_not_called()
    if failed:
        raise AssertionError(str(failed[0]))


if __name__ == "__main__":
    main()
