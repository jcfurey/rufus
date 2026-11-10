"""GTK 4 frontend; storage operations run outside the GTK main thread."""

from pathlib import Path
import json
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

from . import __version__
from .core import RufusError, checksums, expected_sha256, human_size
from .devices import (backup_device, discover_devices, format_device, freedos_device, live_device,
                      windows_installer, write_to_device)
from .windows import WindowsOptions
from .jetson import MODELS as JETSON_MODELS, installation_plan


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Rufus for Linux")
        self.set_default_size(620, 640)
        self.set_resizable(True)
        self.devices = []
        self.wtg_images = []
        self.wtg_source = None
        self.image = None
        self.output = None
        self.hash_window = None
        self.busy = False
        self.refreshing = False
        self.cancel = threading.Event()
        self.started = 0
        self.last_progress = 0
        self.last_stage = ""
        self.confirmation = None
        self.jetson_connections = []
        self.jetson_profiles = []
        self.jetson_log_window = None
        self.connect("close-request", self.close_requested)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for side in ("top", "bottom", "start", "end"):
            getattr(outer, f"set_margin_{side}")(24)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(outer)
        self.set_child(scroller)
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
        self.device_heading = self.label("Device")
        self.form.append(self.device_heading)
        device_row = Gtk.Box(spacing=8)
        self.device_row = device_row
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
            "Windows installer — ISO (BIOS / UEFI)",
            "Non-bootable — format drive",
            "Save drive to image — backup",
            "FreeDOS (BIOS) — bundled DOS boot media",
            "Linux live USB — ISO layout / FAT32 files / persistence",
            "NVIDIA Jetson — installer / SD image / recovery setup"])
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

        self.win_mode = Gtk.DropDown.new_from_strings(["Windows installation USB", "Windows To Go — portable Windows"])
        self.form.append(self.win_mode)
        self.wtg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        editions = Gtk.Box(spacing=8)
        self.wtg_index = Gtk.DropDown.new_from_strings(["Inspect the ISO, then choose a Windows edition"])
        self.wtg_index.set_hexpand(True)
        editions.append(self.wtg_index)
        self.wtg_inspect = Gtk.Button(label="Inspect editions")
        self.wtg_inspect.connect("clicked", self.inspect_windows_editions)
        editions.append(self.wtg_inspect)
        self.wtg_box.append(editions)
        self.wtg_firmware = Gtk.DropDown.new_from_strings(["UEFI (GPT)", "BIOS (MBR)", "BIOS + UEFI (MBR)"])
        self.wtg_box.append(self.wtg_firmware)
        self.form.append(self.wtg_box)
        self.windows_media = Gtk.Box(spacing=8)
        self.win_filesystem = Gtk.DropDown.new_from_strings(["FAT32 (split large WIM files)", "NTFS (UEFI:NTFS)"])
        self.win_firmware = Gtk.DropDown.new_from_strings(["UEFI (GPT)", "BIOS (MBR)"])
        self.win_filesystem.set_hexpand(True)
        self.windows_media.append(self.win_filesystem)
        self.windows_media.append(self.win_firmware)
        self.form.append(self.windows_media)

        self.windows_box = Gtk.Expander(label="Windows setup options (optional)")
        windows_options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        windows_options.set_margin_top(8)
        self.win_bypass = Gtk.CheckButton(label="Remove TPM, Secure Boot and RAM requirements")
        self.win_offline = Gtk.CheckButton(label="Request offline account setup")
        self.win_privacy = Gtk.CheckButton(label="Skip express data-collection choices")
        self.win_encrypt = Gtk.CheckButton(label="Prevent automatic device encryption")
        for widget in (self.win_bypass, self.win_offline, self.win_privacy, self.win_encrypt):
            windows_options.append(widget)
        self.win_user = Gtk.Entry(placeholder_text="Local administrator name (optional; password change at first login)")
        self.win_locale = Gtk.Entry(placeholder_text="Windows language and region, e.g. en-US (optional)")
        self.win_timezone = Gtk.Entry(placeholder_text="Windows time zone, e.g. Central Standard Time (optional)")
        self.win_arch = Gtk.DropDown.new_from_strings(["Detect architecture from ISO", "amd64", "x86", "arm64", "arm"])
        for widget in (self.win_user, self.win_locale, self.win_timezone, self.win_arch):
            windows_options.append(widget)
        self.windows_box.set_child(windows_options)
        self.form.append(self.windows_box)

        self.backup_format = Gtk.DropDown.new_from_strings(["Raw disk image (.img)", "Fixed virtual hard disk (.vhd)"])
        self.form.append(self.backup_format)

        self.freedos_box = Gtk.Box(spacing=8)
        self.freedos_fat = Gtk.DropDown.new_from_strings(["FAT16", "FAT32"])
        self.freedos_fat.set_selected(1)
        self.freedos_label = Gtk.Entry(text="FREEDOS", placeholder_text="Volume label")
        self.freedos_label.set_hexpand(True)
        self.freedos_box.append(self.freedos_fat)
        self.freedos_box.append(self.freedos_label)
        self.form.append(self.freedos_box)

        self.live_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.live_layout = Gtk.DropDown.new_from_strings(["ISO layout — preserve original bootloaders",
                                                         "FAT32 files — GRUB / Syslinux"])
        self.live_box.append(self.live_layout)
        self.live_target = Gtk.Box(spacing=8)
        self.live_arch = Gtk.DropDown.new_from_strings(["Detect kernel architecture", "x86-64", "ARM64 / AArch64",
                                                       "32-bit ARM", "32-bit x86"])
        self.live_firmware = Gtk.DropDown.new_from_strings(["Automatic firmware", "BIOS + UEFI", "UEFI", "BIOS"])
        self.live_target.append(self.live_arch)
        self.live_target.append(self.live_firmware)
        self.live_box.append(self.live_target)
        self.live_bootloader = Gtk.DropDown.new_from_strings(["BIOS loader: follow ISO menu (UEFI uses GRUB)",
                                                             "BIOS loader: GRUB", "BIOS loader: Syslinux 6.x"])
        self.live_box.append(self.live_bootloader)
        self.live_persist = Gtk.CheckButton(label="Keep changes between boots (persistence)", active=True)
        self.live_box.append(self.live_persist)
        self.live_box.append(self.label("Persistence size (MiB)"))
        self.persistence = Gtk.SpinButton.new_with_range(64, 16 * 1024 * 1024, 64)
        self.persistence.set_value(1024)
        self.persistence.set_numeric(True)
        self.live_box.append(self.persistence)
        self.casper_label = Gtk.DropDown.new_from_strings(["Modern Casper / DragonOS (writable)",
                                                         "Older Casper (casper-rw)"])
        self.live_box.append(self.casper_label)
        self.form.append(self.live_box)

        self.jetson_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.jetson_model = Gtk.DropDown.new_from_strings([value[0] for value in JETSON_MODELS.values()])
        self.jetson_storage = Gtk.DropDown.new_from_strings(["Install onto Jetson NVMe", "Install onto Jetson microSD",
                                                            "Install onto Jetson eMMC", "Install onto Jetson USB storage"])
        self.jetson_method = Gtk.DropDown.new_from_strings(["Create NVIDIA installer USB — current Orin / Thor",
                                                           "Write legacy NVIDIA SD-card image",
                                                           "Direct BSP recovery — no SDK Manager",
                                                           "Recovery flashing — open NVIDIA SDK Manager"])
        for widget in (self.jetson_model, self.jetson_storage, self.jetson_method):
            self.jetson_box.append(widget)
        self.jetson_guide = Gtk.LinkButton.new_with_label(
            "https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html",
            "NVIDIA installation instructions")
        self.jetson_box.append(self.jetson_guide)
        self.jetson_direct = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        bsp_row = Gtk.Box(spacing=8)
        self.jetson_bsp = Gtk.Entry(placeholder_text="Prepared Linux_for_Tegra directory", hexpand=True)
        bsp_row.append(self.jetson_bsp)
        choose_bsp = Gtk.Button(label="Select BSP…")
        choose_bsp.connect("clicked", self.choose_bsp)
        bsp_row.append(choose_bsp)
        inspect = Gtk.Button(label="Inspect / scan")
        inspect.connect("clicked", self.inspect_jetson)
        bsp_row.append(inspect)
        self.jetson_direct.append(bsp_row)
        self.jetson_profile = Gtk.DropDown.new_from_strings(["Inspect BSP to list board profiles"])
        self.jetson_profile.connect("notify::selected", self.jetson_profile_changed)
        self.jetson_direct.append(self.jetson_profile)
        self.jetson_board = Gtk.Entry(placeholder_text="Exact board profile, without .conf")
        self.jetson_direct.append(self.jetson_board)
        self.jetson_usb = Gtk.DropDown.new_from_strings(["No recovery port selected — offline review"])
        self.jetson_direct.append(self.jetson_usb)
        self.jetson_external = Gtk.Entry(placeholder_text="Jetson external device (optional, e.g. nvme1n1p1)")
        self.jetson_layout = Gtk.Entry(placeholder_text="External XML inside BSP (optional, e.g. tools/kernel_flash/flash_l4t_external.xml)")
        self.jetson_firmware = Gtk.CheckButton(label="Update internal boot firmware with external storage", active=True)
        self.jetson_action = Gtk.DropDown.new_from_strings(["Install OS / flash storage", "Read board / EEPROM information"])
        for widget in (self.jetson_external, self.jetson_layout, self.jetson_firmware, self.jetson_action):
            self.jetson_direct.append(widget)
        self.jetson_box.append(self.jetson_direct)
        self.form.append(self.jetson_box)

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
        self.hash_button = Gtk.Button(label="Calculate all")
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
        self.win_filesystem.connect("notify::selected", lambda *_: self.update_controls())
        self.win_firmware.connect("notify::selected", lambda *_: self.update_controls())
        for widget in (self.win_mode, self.wtg_index, self.wtg_firmware, self.win_arch):
            widget.connect("notify::selected", lambda *_: self.update_controls())
        self.live_layout.connect("notify::selected", lambda *_: self.update_controls())
        self.live_persist.connect("toggled", lambda *_: self.update_controls())
        for widget in (self.jetson_model, self.jetson_storage, self.jetson_method):
            widget.connect("notify::selected", lambda *_: self.update_controls())
        for widget in (self.jetson_bsp, self.jetson_board):
            widget.connect("changed", lambda *_: self.update_controls())
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
        recovery = mode == 6 and self.jetson_method.get_selected() >= 2
        direct = recovery and self.jetson_method.get_selected() == 2
        wtg = mode == 1 and self.win_mode.get_selected() == 1
        self.win_mode.set_visible(mode == 1)
        self.wtg_box.set_visible(wtg)
        self.win_bypass.set_visible(not wtg)
        self.jetson_direct.set_visible(direct)
        self.image_row.set_visible(mode not in (2, 4) and not recovery)
        self.hash_box.set_visible(mode in (0, 1, 5, 6) and not recovery)
        self.verify.set_visible(mode != 2 and not recovery)
        self.device_heading.set_visible(not recovery)
        self.device_row.set_visible(not recovery)
        self.format_box.set_visible(mode == 2)
        self.windows_box.set_visible(mode == 1)
        self.windows_media.set_visible(mode == 1 and not wtg)
        self.backup_format.set_visible(mode == 3)
        self.freedos_box.set_visible(mode == 4)
        self.live_box.set_visible(mode == 5)
        self.jetson_box.set_visible(mode == 6)
        extracted = self.live_layout.get_selected() == 1
        self.live_target.set_visible(extracted)
        self.live_bootloader.set_visible(extracted)
        self.live_persist.set_sensitive(extracted)
        if not extracted:
            self.live_persist.set_active(True)
        self.persistence.set_sensitive(self.live_persist.get_active())
        self.casper_label.set_sensitive(self.live_persist.get_active())
        self.choose.set_label("Save as…" if mode == 3 else "Select image…")
        selected_path = self.output if mode == 3 else self.image
        self.image_label.set_text(Path(selected_path).name if selected_path else
                                  "Choose a new image filename" if mode == 3 else "No image selected")
        self.image_label.set_tooltip_text(selected_path)
        self.start.set_label("Review BSP plan" if direct else "Open SDK Manager" if recovery else "Save image" if mode == 3 else "Start")
        self.mode_note.set_text((
            "Restores complete disk and board images, including ARM and Raspberry Pi firmware layouts. "
            "Supports compressed images, ZIP, and fixed/dynamic VHD.",
            "Creates GPT + FAT32 media for UEFI PCs. Large install.wim files are split automatically.",
            "Creates one data partition using the selected filesystem.",
            "Opens the USB drive read-only and saves all sectors to a new image file. Existing files are protected.",
            "Creates BIOS/MBR boot media using Rufus's bundled FreeDOS kernel and shell. Requires mtools.",
            "Adds an ext4 persistence partition to a supported hybrid live ISO. Retains its BIOS/UEFI bootloaders. "
            "Debian persistence is detected automatically. Requires xorriso and e2fsprogs.",
            "Prepares NVIDIA's model-specific installation media or opens its recovery setup.")[mode])
        if mode == 1:
            fs = "FAT32" if self.win_filesystem.get_selected() == 0 else "NTFS"
            target = "UEFI/GPT" if self.win_firmware.get_selected() == 0 else "BIOS/MBR"
            self.mode_note.set_text(f"Prepares {target} Windows media using {fs}, then writes and verifies the image.")
            if wtg:
                self.mode_note.set_text("Deploys the selected Windows edition to NTFS, then configures boot and portable "
                    "policies in a private VM before writing the drive. Requires QEMU, matching firmware, and ample "
                    "temporary space. Internal disks stay offline in portable Windows. Choose an edition explicitly.")
        if mode == 5 and extracted:
            self.mode_note.set_text("Creates FAT32 Casper/Debian live media from GRUB or Syslinux menus. "
                                    "BIOS uses the selected GRUB/Syslinux loader; ARM and UEFI use GRUB. "
                                    "The generated UEFI loader requires Secure Boot to be disabled. "
                                    "Use disk-image mode for board-specific Raspberry Pi images.")
        jetson_valid = True
        if mode == 6:
            try:
                plan = installation_plan(*self.jetson_selection())
                self.mode_note.set_text("\n".join(plan["steps"]))
                self.jetson_guide.set_uri(plan["documentation"])
            except RufusError as exc:
                self.mode_note.set_text(str(exc))
                jetson_valid = False
        device = self.selected_device()
        blocked = device is not None and device.reason and not (mode == 3 and device.reason == "Drive is read-only.")
        recovery_ready = recovery and (not direct or bool(self.jetson_bsp.get_text() and self.jetson_board.get_text()))
        wtg_problem = ""
        if wtg:
            if self.wtg_source != self.image or not 0 < self.wtg_index.get_selected() <= len(self.wtg_images):
                wtg_problem = "Inspect this ISO and choose a Windows edition."
            elif device:
                from .windows_togo import validate_selection
                try:
                    if device.logical_sector != 512:
                        raise RufusError("Windows To Go requires a drive with 512-byte logical sectors.")
                    validate_selection(self.wtg_images[self.wtg_index.get_selected() - 1], device.size,
                                       ("uefi", "bios", "both")[self.wtg_firmware.get_selected()],
                                       ("", "amd64", "x86", "arm64", "arm")[self.win_arch.get_selected()])
                except RufusError as exc:
                    wtg_problem = str(exc)
        wtg_ready = not wtg or (not wtg_problem and device is not None)
        self.start.set_sensitive(not self.busy and self.confirmation is None and jetson_valid and wtg_ready and (recovery_ready or (not recovery and
                                 device is not None and not blocked and (mode in (2, 4) or selected_path is not None))))
        self.wtg_inspect.set_sensitive(self.image is not None and not self.busy)
        self.hash_button.set_sensitive(self.image is not None and not self.busy)
        if direct:
            self.device_note.set_text("Select a connected recovery USB port under the BSP settings. Review shows the OS target and firmware scope.")
        elif recovery:
            self.device_note.set_text("Connect the Jetson's recovery USB port. Board detection and flashing occur in NVIDIA SDK Manager.")
        elif wtg_problem:
            self.device_note.set_text(wtg_problem)
        elif device:
            self.device_note.set_text(device.reason if blocked else
                f"{device.path} · {human_size(device.size)} · " +
                ("Read-only backup; the source drive will be unmounted." if mode == 3 else "All existing data will be erased."))
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
            f"{d.model} · {human_size(d.size)} ({d.path})" +
            (" · read-only" if d.reason == "Drive is read-only." else " · unavailable" if d.reason else "")
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
        if self.mode.get_selected() == 3:
            suffix = ".vhd" if self.backup_format.get_selected() else ".img"
            dialog = Gtk.FileDialog(title="Save USB drive to a new image file", initial_name="usb-backup" + suffix)
            dialog.save(self, None, self.output_selected)
            return
        dialog = Gtk.FileDialog(title="Select a bootable disk image or installation/live ISO")
        dialog.open(self, None, self.image_selected)

    def image_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            path = file.get_path()
            if not path:
                raise RufusError("Select a local image file.")
            self.image = path
            self.wtg_source, self.wtg_images = None, []
            self.wtg_index.set_model(Gtk.StringList.new(["Inspect the ISO, then choose a Windows edition"]))
            self.image_label.set_text(Path(path).name)
            self.image_label.set_tooltip_text(path)
            self.hash.set_text("")
            self.update_controls()
        except GLib.Error:
            pass  # File chooser cancellation.
        except RufusError as exc:
            self.status.set_text(str(exc))

    def output_selected(self, dialog, result):
        try:
            path = dialog.save_finish(result).get_path()
            if not path:
                raise RufusError("Select a local destination for the drive image.")
            self.output = path
            self.update_controls()
        except GLib.Error:
            pass
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
            self.status.set_text(f"Complete · {time.monotonic() - self.started:.1f} seconds")
            self.progress.set_fraction(1)
            self.progress.set_text("Complete")
            if on_success:
                on_success(result)
        self.update_controls()
        self.refresh()
        return False

    def calculate_hash(self, *_):
        self.work(lambda: checksums(self.image, cancel=self.cancel, progress=self.report), self.show_checksums)

    def show_checksums(self, results):
        self.hash.set_text(results["sha256"])
        if self.hash_window is not None:
            self.hash_window.destroy()
        self.hash_window = Gtk.Window(title="Image checksums", transient_for=self, default_width=760, default_height=260)
        text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text("\n\n".join(f"{name.upper()}\n{value}" for name, value in results.items()))
        text.set_top_margin(16)
        text.set_bottom_margin(16)
        text.set_left_margin(16)
        text.set_right_margin(16)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(text)
        self.hash_window.set_child(scroller)
        self.hash_window.present()

    def windows_options(self):
        wtg = self.mode.get_selected() == 1 and self.win_mode.get_selected() == 1
        return WindowsOptions(bypass_hardware=self.win_bypass.get_active() and not wtg, offline_account=self.win_offline.get_active(),
                              local_account=self.win_user.get_text(), privacy=self.win_privacy.get_active(),
                              disable_encryption=self.win_encrypt.get_active(), locale=self.win_locale.get_text(),
                              timezone=self.win_timezone.get_text(),
                              architecture=("", "amd64", "x86", "arm64", "arm")[self.win_arch.get_selected()])

    def inspect_windows_editions(self, *_):
        if not self.image:
            return
        from .windows_togo import inspect_iso
        source = self.image
        self.work(lambda: inspect_iso(source, self.cancel, self.report),
                  lambda result: self.show_windows_editions(source, result))

    def show_windows_editions(self, source, editions):
        if source != self.image:
            return
        self.wtg_source, self.wtg_images = source, editions
        self.wtg_index.set_model(Gtk.StringList.new(["Select a Windows edition…"] + [
            f"{entry['index']}: {entry['name']} · {entry['architecture']}" for entry in editions]))
        self.wtg_index.set_selected(0)
        self.status.set_text("Choose the edition to install as portable Windows.")

    def jetson_selection(self):
        return (tuple(JETSON_MODELS)[self.jetson_model.get_selected()],
                ("nvme", "sd", "emmc", "usb")[self.jetson_storage.get_selected()],
                ("installer", "sd", "direct", "recovery")[self.jetson_method.get_selected()])

    def choose_bsp(self, *_):
        Gtk.FileDialog(title="Select prepared Linux_for_Tegra BSP").select_folder(self, None, self.bsp_selected)

    def bsp_selected(self, dialog, result):
        try:
            path = dialog.select_folder_finish(result).get_path()
            if path:
                self.jetson_bsp.set_text(path)
                self.inspect_jetson()
        except GLib.Error:
            pass

    def inspect_jetson(self, *_):
        from .jetson_flash import connected_devices, inspect_bsp
        directory = self.jetson_bsp.get_text()
        self.work(lambda: (inspect_bsp(directory), connected_devices()), self.show_bsp)

    def show_bsp(self, result):
        from .jetson_flash import REFERENCE_PROFILES
        info, self.jetson_connections = result
        self.jetson_profiles = info["profiles"]
        previous = self.jetson_board.get_text()
        recommended = REFERENCE_PROFILES[self.jetson_selection()[0]]
        if self.jetson_selection()[1] == "emmc" and self.jetson_selection()[0] in ("nano", "xavier-nx"):
            recommended += "-emmc"
        chosen = previous if previous in self.jetson_profiles else recommended
        self.jetson_profile.set_model(Gtk.StringList.new(self.jetson_profiles))
        self.jetson_profile.set_selected(self.jetson_profiles.index(chosen) if chosen in self.jetson_profiles else 0)
        self.jetson_profile_changed()
        choices = ["No recovery port selected — offline review"] + [
            f"{d['usb_port']} · {JETSON_MODELS[d['model']][0]} · {d['module_candidates']}"
            for d in self.jetson_connections]
        self.jetson_usb.set_model(Gtk.StringList.new(choices))
        # Require selection even when only one board is connected.
        self.jetson_usb.set_selected(0)
        self.status.set_text(f"BSP R{info['release']} · {len(self.jetson_profiles)} profiles · "
                             f"rootfs {'prepared' if info['rootfs_ready'] else 'needs preparation'} · "
                             f"{len(self.jetson_connections)} recovery device(s)")

    def jetson_profile_changed(self, *_):
        index = self.jetson_profile.get_selected()
        if index < len(self.jetson_profiles):
            self.jetson_board.set_text(self.jetson_profiles[index])

    def review_jetson(self):
        from .jetson_flash import FlashRequest, make_plan
        model, storage, _ = self.jetson_selection()
        index = self.jetson_usb.get_selected() - 1
        port = self.jetson_connections[index]["usb_port"] if 0 <= index < len(self.jetson_connections) else ""
        request = FlashRequest(self.jetson_bsp.get_text(), model, storage, self.jetson_board.get_text(), port,
                               self.jetson_external.get_text(), self.jetson_layout.get_text(),
                               self.jetson_firmware.get_active(), ("flash", "probe")[self.jetson_action.get_selected()])
        self.work(lambda: make_plan(request), self.show_jetson_plan)

    def show_jetson_plan(self, plan):
        dialog = Gtk.Dialog(title="Review direct NVIDIA BSP job", transient_for=self, modal=True)
        dialog.set_default_size(760, 500)
        dialog.add_button("Close", Gtk.ResponseType.CANCEL)
        execute = dialog.add_button("Authorize and run", Gtk.ResponseType.ACCEPT)
        execute.add_css_class("destructive-action")
        execute.set_sensitive(False)
        content = dialog.get_content_area()
        content.set_spacing(12)
        for side in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{side}")(16)
        details = (plan["scope"] + "\n\n" + plan["identification"] + "\n\n" +
                   json.dumps(plan["target"], indent=2) + "\n\n" + plan["command_display"] + "\n\n" +
                   "\n".join(plan["issues"]))
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(details)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=260)
        scroll.set_child(view)
        content.append(scroll)
        content.append(Gtk.Label(label="Type the reviewed token to continue: " + plan["confirmation"], xalign=0, selectable=True))
        entry = Gtk.Entry(placeholder_text=plan["confirmation"])
        entry.connect("changed", lambda entry: execute.set_sensitive(plan["can_execute"] and entry.get_text() == plan["confirmation"]))
        content.append(entry)
        self.confirmation = dialog
        self.update_controls()

        def response(dialog, response_id):
            self.confirmation = None
            token = entry.get_text()
            accepted = response_id == Gtk.ResponseType.ACCEPT and plan["can_execute"] and token == plan["confirmation"]
            dialog.destroy()
            self.update_controls()
            if accepted:
                try:
                    self.run_jetson_plan(plan, token)
                except OSError as exc:
                    self.status.set_text(str(exc))
        dialog.connect("response", response)
        dialog.present()

    def run_jetson_plan(self, plan, token):
        from .jetson_flash import execute_plan
        directory = Path(GLib.get_user_state_dir()) / "rufus-linux"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = directory / f"jetson-{time.time_ns()}.jsonl"
        if self.jetson_log_window is not None:
            self.jetson_log_window.destroy()
        self.jetson_log_window = Gtk.Window(title=f"NVIDIA BSP log · {log}", transient_for=self,
                                          default_width=850, default_height=450)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        buffer = view.get_buffer()
        def append(line):
            buffer.insert(buffer.get_end_iter(), line + "\n")
            if buffer.get_char_count() > 200000:
                buffer.delete(buffer.get_start_iter(), buffer.get_iter_at_offset(buffer.get_char_count() - 150000))
            return False
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(view)
        self.jetson_log_window.set_child(scroll)
        self.jetson_log_window.present()
        self.work(lambda: execute_plan(plan, token, log, self.cancel, self.report,
                                      lambda line: GLib.idle_add(append, line), authorization="pkexec"),
                  lambda result: self.status.set_text(f"{result['status']} · log: {result['log']}"))

    def start_clicked(self, *_):
        if self.busy or self.confirmation is not None:
            return
        mode = self.mode.get_selected()
        if mode == 6 and self.jetson_method.get_selected() == 2:
            self.review_jetson()
            return
        if mode == 6 and self.jetson_method.get_selected() == 3:
            from .jetson import launch_sdkmanager
            self.work(launch_sdkmanager)
            return
        device = self.selected_device()
        if not device:
            return
        mode = self.mode.get_selected()
        if mode == 3:
            output, image_format, verify = self.output, ("raw", "vhd")[self.backup_format.get_selected()], self.verify.get_active()
            self.work(lambda: backup_device(device, output, image_format, verify, self.cancel, self.report))
            return
        try:
            if mode in (0, 1, 5, 6):
                expected_sha256(self.hash.get_text())
            if mode == 6:
                installation_plan(*self.jetson_selection())
            if mode == 5:
                from .live import validate_persistence
                if self.live_persist.get_active():
                    validate_persistence(self.persistence.get_value_as_int() * 1024**2)
            options = self.windows_options()
            if mode == 1:
                options.validate()
        except RufusError as exc:
            self.status.set_text(str(exc))
            return
        # Snapshot the confirmed selection; refresh cannot redirect the operation.
        mode, image = self.mode.get_selected(), self.image
        expected, verify = self.hash.get_text(), self.verify.get_active()
        filesystem = ("vfat", "exfat", "ntfs", "ext2", "ext3", "ext4")[self.filesystem.get_selected()]
        table, label = ("gpt", "dos")[self.table.get_selected()], self.volume.get_text()
        dos_fat, dos_label = (16, 32)[self.freedos_fat.get_selected()], self.freedos_label.get_text()
        win_fs, win_target = ("vfat", "ntfs")[self.win_filesystem.get_selected()], ("uefi", "bios")[self.win_firmware.get_selected()]
        wtg = mode == 1 and self.win_mode.get_selected() == 1
        wtg_index = None
        if wtg:
            selected = self.wtg_index.get_selected() - 1
            if self.wtg_source != image or not 0 <= selected < len(self.wtg_images):
                self.status.set_text("Inspect this ISO and select a Windows edition first.")
                return
            wtg_index = self.wtg_images[selected]["index"]
        wtg_firmware = ("uefi", "bios", "both")[self.wtg_firmware.get_selected()]
        persistence = self.persistence.get_value_as_int() * 1024**2 if self.live_persist.get_active() else 0
        casper_label = ("writable", "casper-rw")[self.casper_label.get_selected()]
        live_mode = ("hybrid", "extract")[self.live_layout.get_selected()]
        live_arch = ("auto", "x86_64", "arm64", "arm", "x86")[self.live_arch.get_selected()]
        live_firmware = (None, "both", "uefi", "bios")[self.live_firmware.get_selected()]
        live_bootloader = ("auto", "grub", "syslinux")[self.live_bootloader.get_selected()]
        jetson_model, jetson_storage, jetson_method = self.jetson_selection()
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
                if wtg:
                    from .devices import windows_to_go_device
                    def portable():
                        state = Path(GLib.get_user_state_dir()) / "rufus-linux"
                        state.mkdir(mode=0o700, parents=True, exist_ok=True)
                        log = state / f"windows-to-go-{time.time_ns()}.log"
                        return windows_to_go_device(image, device, index=wtg_index, firmware=wtg_firmware, options=options,
                            expected=expected, verify=verify, log=log, cancel=self.cancel, progress=self.report)
                    self.work(portable, lambda result: self.status.set_text("Portable Windows prepared · log: " + result["log"]))
                else:
                    self.work(lambda: windows_installer(image, device, expected, verify, self.cancel, self.report, options, win_fs, win_target))
            elif mode == 2:
                self.work(lambda: format_device(device, filesystem, label, table, self.cancel, self.report))
            elif mode == 5:
                self.work(lambda: live_device(image, device, persistence, casper_label, expected, verify,
                                             self.cancel, self.report, mode=live_mode,
                                             architecture=live_arch if live_mode == "extract" else "auto",
                                             firmware=live_firmware if live_mode == "extract" else None,
                                             bootloader=live_bootloader if live_mode == "extract" else "auto"))
            elif mode == 6:
                from .jetson import write_media
                self.work(lambda: write_media(image, model=jetson_model, storage=jetson_storage,
                                             method=jetson_method, device=device, expected=expected, verify=verify,
                                             cancel=self.cancel, progress=self.report))
            else:
                self.work(lambda: freedos_device(device, dos_fat, dos_label, verify=verify,
                                                cancel=self.cancel, progress=self.report))
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
