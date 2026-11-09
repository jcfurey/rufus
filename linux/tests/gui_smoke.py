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


def main():
    app = Gtk.Application(application_id="ie.rufus.Linux.GuiTest", flags=Gio.ApplicationFlags.NON_UNIQUE)
    failed = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rufus-gui-test-") as directory:
        image = Path(directory) / "source.img"
        image.write_bytes(b"Rufus GTK checksum test" * 65536)
        expected = hashlib.sha256(image.read_bytes()).hexdigest()

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
                window.mode.set_selected(1)
                assert window.windows_box.get_visible()
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
                    window.calculate_hash()
                    return False
                # Let Wayland configure the transient window before simulating
                # a user closing it; responding during activate precedes mapping.
                GLib.timeout_add(350, cancel_confirmation)

                def completed():
                    try:
                        if window.busy or window.confirmation is not None:
                            assert time.monotonic() - started < 15, "GTK worker timed out"
                            return True
                        assert window.hash.get_text() == expected
                        assert window.progress.get_fraction() == 1
                        assert not window.stop.get_sensitive()
                        print("PASS: GTK modes, drive selection, confirmation cancellation, async checksum")
                    except Exception as exc:
                        failed.append(exc)
                    app.quit()
                    return False
                GLib.timeout_add(100, completed)
            except Exception as exc:
                failed.append(exc)
                app.quit()

        app.connect("activate", activate)
        with patch.object(gui, "discover_devices", return_value=[]):
            app.run([])
    if failed:
        raise AssertionError(str(failed[0]))


if __name__ == "__main__":
    main()
