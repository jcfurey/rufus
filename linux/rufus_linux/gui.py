"""GTK 4 frontend; storage operations run outside the GTK main thread."""

from pathlib import Path
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from . import __version__
from .core import RufusError, checksum, expected_sha256, human_size
from .devices import discover_devices, format_device, windows_installer, write_to_device


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rufus for Linux")
        self.set_default_size(620, 640)
        self.set_resizable(True)
        self.devices = []
        self.image = None
        self.busy = False
        self.refreshing = False
        self.cancel = threading.Event()
        self.started = 0
        self.last_progress = 0
        self.last_stage = ""
        self.confirmation = None
        self.connect("close-request", self.close_requested)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(24)
        self.set_child(outer)
        hero = Gtk.Box(spacing=16)
        icon = Gtk.Image.new_from_icon_name("drive-removable-media")
        icon.set_pixel_size(56)
        hero.append(icon)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label="Rufus for Linux", xalign=0)
        title.add_css_class("title-1")
        titles.append(title)
        subtitle = Gtk.Label(label="Create bootable USB drives · Native Linux preview", xalign=0)
        subtitle.add_css_class("dim-label")
        titles.append(subtitle)
        hero.append(titles)
        outer.append(hero)

        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.append(self.form)
        self.form.append(self.label("Device"))
        device_row = Gtk.Box(spacing=8)
        self.drive = Gtk.DropDown.new_from_strings(["Select a USB drive…"])
        self.drive.set_hexpand(True)
        self.drive.connect("notify::selected", lambda *_: self.update_controls())
        device_row.append(self.drive)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh USB drives")
        refresh.connect("clicked", lambda *_: self.refresh())
        device_row.append(refresh)
        self.form.append(device_row)
        self.device_note = Gtk.Label(label="Connect a USB drive to get started.", xalign=0, wrap=True)
        self.device_note.add_css_class("dim-label")
        self.form.append(self.device_note)

        self.form.append(self.label("Boot selection"))
        self.mode = Gtk.DropDown.new_from_strings([
            "Disk image (DD) — Linux / IMG / compressed image",
            "Windows installer (UEFI) — Windows ISO",
            "Non-bootable — format drive"])
        self.mode.connect("notify::selected", lambda *_: self.update_controls())
        self.form.append(self.mode)
        self.image_row = Gtk.Box(spacing=8)
        self.image_label = Gtk.Label(label="No image selected", xalign=0, ellipsize=3)
        self.image_label.set_hexpand(True)
        self.image_row.append(self.image_label)
        self.choose = Gtk.Button(label="Select image…")
        self.choose.connect("clicked", self.choose_image)
        self.image_row.append(self.choose)
        self.form.append(self.image_row)

        self.format_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.format_box.append(self.label("Partition scheme and filesystem"))
        row = Gtk.Box(spacing=8)
        self.table = Gtk.DropDown.new_from_strings(["GPT", "MBR"])
        self.filesystem = Gtk.DropDown.new_from_strings(["FAT32", "exFAT", "NTFS", "ext2", "ext3", "ext4"])
        self.volume = Gtk.Entry(text="RUFUS", placeholder_text="Volume label")
        self.volume.set_hexpand(True)
        for widget in (self.table, self.filesystem, self.volume):
            row.append(widget)
        self.format_box.append(row)
        self.form.append(self.format_box)

        self.hash_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.hash_box.append(self.label("Expected SHA-256 (optional)"))
        hash_row = Gtk.Box(spacing=8)
        self.hash = Gtk.Entry(placeholder_text="Paste the checksum published by the image provider")
        self.hash.set_hexpand(True)
        self.hash.set_max_length(64)
        hash_row.append(self.hash)
        self.hash_button = Gtk.Button(label="Calculate")
        self.hash_button.connect("clicked", self.calculate_hash)
        hash_row.append(self.hash_button)
        self.hash_box.append(hash_row)
        self.form.append(self.hash_box)

        self.verify = Gtk.CheckButton(label="Verify written files by reading them back", active=True)
        self.form.append(self.verify)
        self.mode_note = Gtk.Label(xalign=0, wrap=True)
        self.mode_note.add_css_class("dim-label")
        self.form.append(self.mode_note)
        outer.append(Gtk.Separator())
        self.status = Gtk.Label(label="Ready", xalign=0, wrap=True, selectable=True)
        outer.append(self.status)
        self.progress = Gtk.ProgressBar(show_text=True, text="Ready")
        outer.append(self.progress)
        buttons = Gtk.Box(spacing=8)
        version = Gtk.Label(label=f"Linux preview {__version__}", xalign=0)
        version.add_css_class("dim-label")
        version.set_hexpand(True)
        buttons.append(version)
        self.stop = Gtk.Button(label="Cancel")
        self.stop.set_sensitive(False)
        self.stop.connect("clicked", lambda *_: self.cancel.set())
        buttons.append(self.stop)
        self.start = Gtk.Button(label="Start")
        self.start.add_css_class("suggested-action")
        self.start.connect("clicked", self.start_clicked)
        buttons.append(self.start)
        outer.append(buttons)
        self.update_controls()
        self.refresh()
        GLib.timeout_add_seconds(5, self.periodic_refresh)

    @staticmethod
    def label(text):
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("heading")
        return label

    def selected_device(self):
        index = self.drive.get_selected() - 1
        return self.devices[index] if 0 <= index < len(self.devices) else None

    def update_controls(self):
        mode = self.mode.get_selected()
        self.image_row.set_visible(mode != 2)
        self.hash_box.set_visible(mode != 2)
        self.verify.set_visible(mode != 2)
        self.format_box.set_visible(mode == 2)
        self.mode_note.set_text((
            "Writes the image's partition layout. Supports hybrid ISOs, IMG, gzip, xz, and bzip2.",
            "Creates GPT + FAT32 media for UEFI PCs. Large install.wim files are split automatically.",
            "Creates one data partition using the selected filesystem.")[mode])
        device = self.selected_device()
        self.start.set_sensitive(not self.busy and device is not None and not device.reason
                                 and (mode == 2 or self.image is not None))
        self.hash_button.set_sensitive(self.image is not None and not self.busy)
        if device:
            self.device_note.set_text(device.reason or
                f"{device.path} · {human_size(device.size)} · All existing data will be erased.")
        else:
            self.device_note.set_text("No USB drives connected." if not self.devices else "Select a USB drive.")

    def periodic_refresh(self):
        if not self.busy and self.confirmation is None:
            self.refresh()
        return True

    def refresh(self):
        if self.busy or self.refreshing or self.confirmation is not None:
            return
        self.refreshing = True

        def worker():
            try:
                devices, error = discover_devices(), None
            except Exception as exc:
                devices, error = [], str(exc)
            GLib.idle_add(self.refreshed, devices, error)
        threading.Thread(target=worker, daemon=True).start()

    def refreshed(self, devices, error):
        self.refreshing = False
        if self.busy or self.confirmation is not None:
            return False
        previous = self.selected_device()
        self.devices = devices
        choices = ["Select a USB drive…"] + [
            f"{d.model} · {human_size(d.size)} ({d.path})" + (" · unavailable" if d.reason else "")
            for d in devices]
        self.drive.set_model(Gtk.StringList.new(choices))
        selected = next((i + 1 for i, d in enumerate(devices)
                         if previous and d.identity == previous.identity), 0)
        self.drive.set_selected(selected)
        if error:
            self.status.set_text(error)
        self.update_controls()
        return False

    def choose_image(self, _button):
        dialog = Gtk.FileDialog(title="Select a bootable disk image or Windows ISO")
        dialog.open(self, None, self.image_selected)

    def image_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            path = file.get_path()
            if not path:
                raise RufusError("Select a local image file.")
            self.image = path
            self.image_label.set_text(Path(path).name)
            self.image_label.set_tooltip_text(path)
            self.hash.set_text("")
            self.update_controls()
        except GLib.Error:
            pass  # File chooser cancellation.
        except RufusError as exc:
            self.status.set_text(str(exc))

    def close_requested(self, *_):
        if self.busy:
            self.status.set_text("Cancel the current operation and wait for it to finish before closing.")
            return True
        return False

    def report(self, stage, done, total):
        now = time.monotonic()
        if stage != self.last_stage or now - self.last_progress > 0.1 or (total and done == total):
            self.last_progress, self.last_stage = now, stage
            GLib.idle_add(self.show_progress, stage, done, total)

    def show_progress(self, stage, done, total):
        if total:
            self.progress.set_fraction(min(1.0, done / total))
            self.progress.set_text(f"{stage} · {100 * done / total:.0f}%")
        else:
            self.progress.pulse()
            self.progress.set_text(stage)
        self.status.set_text(stage)
        return False

    def work(self, action, on_success=None):
        self.busy = True
        self.cancel = threading.Event()
        self.form.set_sensitive(False)
        self.stop.set_sensitive(True)
        self.started = time.monotonic()
        self.progress.set_fraction(0)
        self.update_controls()

        def worker():
            try:
                result, error = action(), None
            except Exception as exc:
                result, error = None, str(exc)
            GLib.idle_add(self.finished, result, error, on_success)
        threading.Thread(target=worker, daemon=True).start()

    def finished(self, result, error, on_success):
        self.busy = False
        self.form.set_sensitive(True)
        self.stop.set_sensitive(False)
        if error:
            self.status.set_text(error)
            self.progress.set_text("Operation did not complete")
        else:
            if on_success:
                on_success(result)
            self.status.set_text(f"Complete · {time.monotonic() - self.started:.1f} seconds")
            self.progress.set_fraction(1)
            self.progress.set_text("Complete")
        self.update_controls()
        self.refresh()
        return False

    def calculate_hash(self, *_):
        self.work(lambda: checksum(self.image, self.cancel, self.report), self.hash.set_text)

    def start_clicked(self, *_):
        device = self.selected_device()
        if not device:
            return
        try:
            expected_sha256(self.hash.get_text())
        except RufusError as exc:
            self.status.set_text(str(exc))
            return
        # Snapshot the confirmed selection; refresh cannot redirect the operation.
        mode, image = self.mode.get_selected(), self.image
        expected, verify = self.hash.get_text(), self.verify.get_active()
        filesystem = ("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4")[self.filesystem.get_selected()]
        table, label = ("gpt", "dos")[self.table.get_selected()], self.volume.get_text()
        dialog = Gtk.Dialog(title="Erase selected USB drive?", transient_for=self, modal=True)
        dialog.set_default_size(470, 220)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        erase = dialog.add_button("Erase and start", Gtk.ResponseType.ACCEPT)
        erase.add_css_class("destructive-action")
        erase.set_sensitive(False)
        content = dialog.get_content_area()
        content.set_spacing(12)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(20)
        content.append(Gtk.Label(label=f"Erase all data on {device.model} ({human_size(device.size)})?",
                                 wrap=True, xalign=0))
        content.append(Gtk.Label(label=f"Type {device.path} to confirm this drive.", xalign=0))
        entry = Gtk.Entry(placeholder_text=device.path)
        entry.connect("changed", lambda entry: erase.set_sensitive(entry.get_text() == device.path))
        content.append(entry)
        self.confirmation = dialog

        def response(dialog, response_id):
            self.confirmation = None
            confirmed = response_id == Gtk.ResponseType.ACCEPT and entry.get_text() == device.path
            dialog.destroy()
            if not confirmed:
                return
            if mode == 0:
                self.work(lambda: write_to_device(image, device, expected, verify, self.cancel, self.report))
            elif mode == 1:
                self.work(lambda: windows_installer(image, device, expected, verify, self.cancel, self.report))
            else:
                self.work(lambda: format_device(device, filesystem, label, table, self.cancel, self.report))
        dialog.connect("response", response)
        dialog.present()


def run(smoke_test=False):
    if not Gtk.init_check():
        raise RufusError("Cannot connect to the desktop display. Run from your Ubuntu desktop, "
                         "or use the command-line subcommands.")
    app = Gtk.Application(application_id="ie.rufus.Linux", flags=Gio.ApplicationFlags.NON_UNIQUE)
    errors = []

    def activate(app):
        try:
            window = Window(app)
            window.present()
            if smoke_test:
                GLib.timeout_add(1800, lambda: (app.quit(), False)[1])
        except Exception as exc:
            errors.append(exc)
            app.quit()
    app.connect("activate", activate)
    status = app.run([])
    if errors:
        raise RufusError(f"Cannot open the desktop interface: {errors[0]}")
    return status
