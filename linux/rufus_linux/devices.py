"""Linux drive discovery and UDisks2/Polkit storage authorization."""

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import time

from .core import (RufusError, check_cancel, checksum, copy_windows_tree, emit,
                   expected_sha256, prepare_image, stage_windows_iso, write_image)

BLOCK = "org.freedesktop.UDisks2.Block"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
TABLE = "org.freedesktop.UDisks2.PartitionTable"
FS_TOOLS = {"vfat": "mkfs.vfat", "exfat": "mkfs.exfat", "ntfs": "mkfs.ntfs",
            "ext2": "mkfs.ext2", "ext3": "mkfs.ext3", "ext4": "mkfs.ext4"}


@dataclass(frozen=True)
class Device:
    path: str
    size: int
    model: str
    serial: str
    number: str
    nodes: tuple
    mounts: tuple
    reason: str = ""

    @property
    def identity(self):
        return self.path, self.size, self.model, self.serial, self.number


def descendants(node):
    yield node
    for child in node.get("children", []):
        yield from descendants(child)


def parse_devices(data):
    devices = []
    for disk in data.get("blockdevices", []):
        if disk.get("type") != "disk" or not (disk.get("rm") or disk.get("tran") == "usb"):
            continue
        nodes = list(descendants(disk))
        mounts = tuple(m for node in nodes for m in node.get("mountpoints", []) if m)
        reason = ""
        if disk.get("ro"):
            reason = "Drive is read-only."
        elif not disk.get("size"):
            reason = "Drive has no media."
        elif any(node.get("type") not in ("disk", "part") for node in nodes):
            reason = "Drive contains active LVM, RAID, or encrypted devices."
        elif any(not m.startswith(("/media/", "/run/media/", "/mnt/")) for m in mounts):
            reason = "Drive contains a system, swap, or non-removable mount."
        devices.append(Device(disk["path"], int(disk["size"]), (disk.get("model") or "USB drive").strip(),
                              disk.get("serial") or "", disk["maj:min"],
                              tuple(node["maj:min"] for node in nodes), mounts, reason))
    return devices


def discover_devices():
    result = subprocess.run(["lsblk", "--json", "--tree", "--bytes", "--output",
                             "PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,MAJ:MIN"],
                            text=True, capture_output=True, check=False, timeout=15)
    if result.returncode:
        raise RufusError("Cannot enumerate block devices: " + result.stderr.strip())
    return parse_devices(json.loads(result.stdout))


def storage_numbers(number):
    """Include physical ancestors of device-mapper and other stacked storage."""
    found = {number}
    pending = [number]
    while pending:
        node = Path("/sys/dev/block") / pending.pop()
        try:
            resolved = node.resolve(strict=True)
            candidates = list((resolved / "slaves").glob("*/dev"))
            if (resolved / "partition").exists():
                candidates.append(resolved.parent / "dev")
            for candidate in candidates:
                value = candidate.read_text().strip()
                if value not in found:
                    found.add(value)
                    pending.append(value)
        except (OSError, RuntimeError):
            continue
    return found


def guard(device, source=None):
    current = next((d for d in discover_devices() if d.path == device.path), None)
    if current is None or current.identity != device.identity:
        raise RufusError("Drive was removed or changed. Refresh and select it again.")
    if current.reason:
        raise RufusError(current.reason)
    # Also protect ancestors of the live root, home, and boot filesystems, even
    # when lsblk cannot associate a mapper mountpoint with its physical drive.
    protected = ["/", "/home", "/boot", "/boot/efi", "/usr", "/var"]
    if source is not None:
        protected.append(str(source))
    for path in protected:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue
        number = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
        if set(current.nodes) & storage_numbers(number):
            raise RufusError(f"Refusing to erase the drive containing {path}.")
    return current


class UDisks:
    def __init__(self):
        try:
            import gi
            from gi.repository import Gio, GLib
            self.Gio, self.GLib = Gio, GLib
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except Exception as exc:
            raise RufusError("Cannot connect to UDisks2. Install python3-gi and udisks2, "
                             "and run from a local desktop or authorized terminal: " + str(exc)) from exc

    def call(self, path, interface, method, signature, arguments, fds=False):
        parameters = self.GLib.Variant(signature, arguments)
        # -1 uses D-Bus's usual ~25 second timeout, which is too short for
        # filesystem creation or a user responding to a Polkit prompt.
        timeout = 2**31 - 1
        try:
            if fds:
                result, descriptors = self.bus.call_with_unix_fd_list_sync(
                    "org.freedesktop.UDisks2", path, interface, method, parameters,
                    None, self.Gio.DBusCallFlags.NONE, timeout, None, None)
                return descriptors.get(result.unpack()[0])
            result = self.bus.call_sync("org.freedesktop.UDisks2", path, interface,
                                       method, parameters, None, self.Gio.DBusCallFlags.NONE,
                                       timeout, None)
            return result.unpack()
        except self.GLib.Error as exc:
            raise RufusError(f"UDisks2 {method}: {exc.message}") from exc

    def objects(self):
        return self.call("/org/freedesktop/UDisks2", "org.freedesktop.DBus.ObjectManager",
                         "GetManagedObjects", "()", ())[0]

    def object_for(self, path):
        number = os.stat(path).st_rdev
        for name, interfaces in self.objects().items():
            if interfaces.get(BLOCK, {}).get("DeviceNumber") == number:
                return name
        raise RufusError(f"UDisks2 cannot find {path}.")

    def check_system_hint(self, path):
        block = self.objects().get(path, {}).get(BLOCK, {})
        if not block or block.get("HintSystem") or block.get("HintIgnore"):
            raise RufusError("UDisks2 marks this drive as a system or ignored device.")

    def unmount(self, device):
        numbers = set(device.nodes)
        for path, interfaces in self.objects().items():
            block = interfaces.get(BLOCK, {})
            number = block.get("DeviceNumber", 0)
            key = f"{os.major(number)}:{os.minor(number)}"
            fs = interfaces.get(FILESYSTEM, {})
            if key in numbers and fs.get("MountPoints"):
                try:
                    self.call(path, FILESYSTEM, "Unmount", "(a{sv})", ({},))
                except RufusError as exc:
                    if ".NotMounted" not in str(exc):
                        raise

    def ensure_unmounted(self, device, source=None):
        # Kernel mountinfo and UDisks's cached properties can briefly disagree
        # when a desktop volume monitor has just auto-mounted a new filesystem.
        for _ in range(20):
            current = guard(device, source)
            self.unmount(current)
            current = guard(device, source)
            if not current.mounts:
                return current
            time.sleep(0.1)
        raise RufusError("A drive partition is still mounted. Close files on it and retry.")

    def open_disk(self, device):
        path = self.object_for(device.path)
        self.check_system_hint(path)
        return self.call(path, BLOCK, "OpenDevice", "(sa{sv})",
                         ("rw", {"flags": self.GLib.Variant("i", os.O_EXCL | os.O_CLOEXEC)}), fds=True)

    def mount(self, partition):
        def current_mount():
            mounts = self.objects().get(partition, {}).get(FILESYSTEM, {}).get("MountPoints", [])
            return os.fsdecode(bytes(mounts[0]).rstrip(b"\0")) if mounts else None

        # A desktop volume monitor may mount a freshly formatted filesystem.
        mounted = current_mount()
        if mounted:
            return mounted
        try:
            return self.call(partition, FILESYSTEM, "Mount", "(a{sv})", ({},))[0]
        except RufusError:
            mounted = current_mount()
            if mounted:
                return mounted
            raise

    def format(self, device, filesystem, label, table, efi=False, source=None):
        device = guard(device, source)
        path = self.object_for(device.path)
        self.check_system_hint(path)
        self.ensure_unmounted(device, source)
        self.call(path, BLOCK, "Format", "(sa{sv})", (table, {}))
        if guard(device, source).mounts:
            raise RufusError("The drive was mounted during formatting. Unmount it and retry.")
        kind = ("c12a7328-f81f-11d2-ba4b-00a0c93ec93b" if efi else
                "0fc63daf-8483-4772-8e79-3d69d8477de4" if filesystem.startswith("ext") else
                "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7") if table == "gpt" else (
                    "0x83" if filesystem.startswith("ext") else "0x0c" if filesystem == "vfat" else "0x07")
        options = {"label": self.GLib.Variant("s", label),
                   "take-ownership": self.GLib.Variant("b", True)}
        if filesystem == "vfat":
            options["mkfs-args"] = self.GLib.Variant("as", ["-F", "32"])
        return self.call(path, TABLE, "CreatePartitionAndFormat", "(ttssa{sv}sa{sv})",
                         (1024 * 1024, 0, kind, "Rufus" if table == "gpt" else "", {},
                          filesystem, options))[0]


def validate_format(device, filesystem, label, table):
    if filesystem not in FS_TOOLS or table not in ("gpt", "dos"):
        raise RufusError("Unsupported filesystem or partition table.")
    if not shutil.which(FS_TOOLS[filesystem]):
        raise RufusError(f"Install {FS_TOOLS[filesystem]} before formatting this filesystem.")
    if device.size < 64 * 1024 * 1024:
        raise RufusError("Formatting requires a drive of at least 64 MiB.")
    if table == "dos" and device.size > 2**41:
        raise RufusError("Use GPT for drives larger than 2 TiB.")
    limit = {"vfat": 11, "ntfs": 32, "exfat": 15}.get(filesystem, 16)
    if (not label or len(label.encode("utf-8")) > limit or
            any(ord(c) < 32 or c in '\\/:*?"<>|' for c in label)):
        raise RufusError(f"Volume label must contain 1–{limit} bytes without reserved characters.")
    if filesystem == "vfat" and (not label.isascii() or any(c in '+.,;=[]' for c in label)):
        raise RufusError("FAT32 labels require ASCII and cannot contain + . , ; = [ ].")


def write_to_device(source, device, expected="", verify=True, cancel=None, progress=None):
    with prepare_image(source, expected, cancel, progress) as image:
        device = guard(device, source)
        if image.size > device.size or (image.is_iso and not image.hybrid):
            # Run all image checks before unmounting or requesting authorization.
            if image.size > device.size:
                raise RufusError("The image is larger than the selected drive.")
            raise RufusError("This ISO cannot boot in DD mode. Use Windows installer (UEFI) "
                             "for Windows media, or select a hybrid Linux ISO.")
        check_cancel(cancel)
        service = UDisks()
        service.ensure_unmounted(device, source)
        guard(device, source)
        fd = service.open_disk(device)
        try:
            info = os.fstat(fd)
            actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
            capacity = struct.unpack("Q", fcntl.ioctl(fd, 0x80081272, b"\0" * 8))[0]
            if not stat.S_ISBLK(info.st_mode) or actual != device.number or capacity != device.size:
                raise RufusError("Opened drive does not match the selected drive.")
            guard(device, source)
            return write_image(image, fd, capacity, verify, cancel, progress, block=True)
        finally:
            os.close(fd)


def format_device(device, filesystem="vfat", label="RUFUS", table="gpt", cancel=None, progress=None):
    validate_format(device, filesystem, label, table)
    guard(device)
    check_cancel(cancel)
    emit(progress, "Formatting drive")
    UDisks().format(device, filesystem, label, table)
    check_cancel(cancel)
    emit(progress, "Complete", 1, 1)


def windows_installer(source, device, expected="", verify=True, cancel=None, progress=None):
    expected = expected_sha256(expected)
    validate_format(device, "vfat", "WINDOWS", "gpt")
    guard(device, source)
    before = os.stat(source)
    actual = checksum(source, cancel, progress)
    if expected and actual != expected:
        raise RufusError("Windows ISO SHA-256 does not match the expected checksum.")
    # Extraction and WIM splitting must succeed before any destructive operation.
    with tempfile.TemporaryDirectory(prefix="rufus-windows-") as staging:
        guard(device, staging)
        files = stage_windows_iso(source, Path(staging), cancel, progress)
        after = os.stat(source)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RufusError("Windows ISO changed during extraction. Select a finished download and retry.")
        required = sum(size + 32768 for _, _, size in files) + 64 * 1024 * 1024
        if required > device.size:
            raise RufusError("Windows installer files will not fit on this drive.")
        check_cancel(cancel)
        service = UDisks()
        emit(progress, "Creating GPT and FAT32 installer partition")
        partition = service.format(device, "vfat", "WINDOWS", "gpt", efi=True, source=source)
        check_cancel(cancel)
        guard(device, source)
        mounted = service.mount(partition)
        try:
            number = service.objects()[partition][BLOCK]["DeviceNumber"]
            if os.stat(mounted).st_dev != number:
                raise RufusError("Mounted destination does not match the new partition.")
            if required > shutil.disk_usage(mounted).free:
                raise RufusError("Not enough free space on the formatted installer partition.")
            copy_windows_tree(files, mounted, verify, cancel, progress)
        finally:
            emit(progress, "Unmounting installer")
            service.call(partition, FILESYSTEM, "Unmount", "(a{sv})", ({},))
    emit(progress, "Complete", 1, 1)
    return actual
