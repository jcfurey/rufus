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

from .core import (RufusError, check_cancel, emit, expected_sha256, prepare_image, write_image)
from .formatting import FS_TOOLS, plan_format, require_formatter

BLOCK = "org.freedesktop.UDisks2.Block"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
TABLE = "org.freedesktop.UDisks2.PartitionTable"


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
    logical_sector: int = 512

    @property
    def identity(self):
        return self.path, self.size, self.model, self.serial, self.number, self.logical_sector


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
                              tuple(node["maj:min"] for node in nodes), mounts, reason, int(disk.get("log-sec") or 512)))
    return devices


def discover_devices():
    result = subprocess.run(["lsblk", "--json", "--tree", "--bytes", "--output",
                             "PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,RM,RO,MOUNTPOINTS,MAJ:MIN,LOG-SEC"],
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


def guard(device, source=None, writable=True):
    current = next((d for d in discover_devices() if d.path == device.path), None)
    if current is None or current.identity != device.identity:
        raise RufusError("Drive was removed or changed. Refresh and select it again.")
    if current.reason and not (not writable and current.reason == "Drive is read-only."):
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

    def ensure_unmounted(self, device, source=None, writable=True):
        # Kernel mountinfo and UDisks's cached properties can briefly disagree
        # when a desktop volume monitor has just auto-mounted a new filesystem.
        for _ in range(20):
            current = guard(device, source) if writable else guard(device, source, writable=False)
            self.unmount(current)
            current = guard(device, source) if writable else guard(device, source, writable=False)
            if not current.mounts:
                return current
            time.sleep(0.1)
        raise RufusError("A drive partition is still mounted. Close files on it and retry.")

    def open_disk(self, device, mode="rw"):
        path = self.object_for(device.path)
        self.check_system_hint(path)
        return self.call(path, BLOCK, "OpenDevice", "(sa{sv})",
                         (mode, {"flags": self.GLib.Variant("i", os.O_EXCL | os.O_CLOEXEC)}), fds=True)

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
        plan = validate_format(device, filesystem, label, table)
        if efi and filesystem != "vfat":
            raise RufusError("EFI system partition formatting requires FAT32.")
        # The daemon has a separate PATH and plugin set from this process.
        # Check it before unmounting or erasing the existing partition table.
        try:
            available, missing = self.call("/org/freedesktop/UDisks2/Manager", "org.freedesktop.UDisks2.Manager",
                                           "CanFormat", "(s)", (plan.backend,))[0]
        except RufusError as exc:
            raise RufusError(f"UDisks cannot format {filesystem}; check its filesystem backend: {exc}") from exc
        if not available:
            raise RufusError(f"UDisks cannot format {filesystem}. Install {missing or FS_TOOLS[filesystem]} system-wide before retrying.")
        device = guard(device, source)
        path = self.object_for(device.path)
        self.check_system_hint(path)
        self.ensure_unmounted(device, source)
        self.call(path, BLOCK, "Format", "(sa{sv})", (table, {}))
        if guard(device, source).mounts:
            raise RufusError("The drive was mounted during formatting. Unmount it and retry.")
        kind = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" if efi and table == "gpt" else plan.kind
        options = {"label": self.GLib.Variant("s", plan.label),
                   "take-ownership": self.GLib.Variant("b", True)}
        if plan.args:
            options["mkfs-args"] = self.GLib.Variant("as", list(plan.args))
        return self.call(path, TABLE, "CreatePartitionAndFormat", "(ttssa{sv}sa{sv})",
                         (plan.partition.start * plan.sector, plan.partition.sectors * plan.sector,
                          kind, "Rufus" if table == "gpt" else "", {}, plan.backend, options))[0]


def validate_format(device, filesystem, label, table):
    plan = plan_format(device.size, filesystem, label, table, device.logical_sector)
    require_formatter(plan)
    return plan


def disk_capacity(fd):
    # Linux fs.h encodes sizeof(size_t) in BLKGETSIZE64, even though the
    # returned argument is always u64. ARM32 therefore needs a different
    # request number from ARM64/x86-64.
    request = 0x80001272 | (struct.calcsize("P") << 16)
    return struct.unpack("Q", fcntl.ioctl(fd, request, b"\0" * 8))[0]


def write_to_device(source, device, expected="", verify=True, cancel=None, progress=None):
    with prepare_image(source, expected, cancel, progress, max_size=device.size) as image:
        device = guard(device, source)
        if image.logical_sector is not None and image.logical_sector != device.logical_sector:
            raise RufusError(f"Image uses {image.logical_sector}-byte logical sectors; the selected drive uses "
                             f"{device.logical_sector}. Restore to a drive with matching logical sectors.")
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
            capacity = disk_capacity(fd)
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


def windows_installer(source, device, expected="", verify=True, cancel=None, progress=None, options=None,
                      filesystem="vfat", firmware="uefi"):
    from .media import create_windows_image, windows_partitions

    expected = expected_sha256(expected)
    guard(device, source)
    if device.logical_sector != 512:
        raise RufusError("Windows image creation currently requires a USB drive with 512-byte logical sectors.")
    windows_partitions(device.size, filesystem, firmware)
    if options is not None:
        options.validate()
    # Build and validate everything as files before requesting device access.
    with tempfile.TemporaryDirectory(prefix="rufus-windows-drive-") as directory:
        guard(device, directory)
        image = Path(directory) / "windows.img"
        result = create_windows_image(source, image, device.size, filesystem, firmware, options, expected, cancel, progress)
        guard(device, source)
        write_to_device(image, device, result["sha256"], verify, cancel, progress)
    return result["source_sha256"]


def windows_to_go_device(source, device, *, index, firmware="uefi", options=None, expected="", verify=True,
                         log=None, timeout=900, cancel=None, progress=None):
    from .windows_togo import create_windows_to_go, partitions
    guard(device, source)
    log = Path(log) if log is not None else Path.cwd() / f"rufus-windows-to-go-{time.time_ns()}.log"
    guard(device, log.parent)
    if device.logical_sector != 512:
        raise RufusError("Windows To Go currently requires a drive with 512-byte logical sectors.")
    partitions(device.size, firmware)
    with tempfile.TemporaryDirectory(prefix="rufus-windows-togo-drive-") as directory:
        guard(device, directory)
        image = Path(directory) / "windows-to-go.img"
        result = create_windows_to_go(source, image, index=index, size=device.size, firmware=firmware,
            options=options, expected=expected, log=log, timeout=timeout, cancel=cancel, progress=progress)
        guard(device, source)
        write_to_device(image, device, result["sha256"], verify, cancel, progress)
    return {**result, "device": device.path}


def live_device(source, device, persistence_size=1024 * 1024**2, casper_label="writable",
                expected="", verify=True, cancel=None, progress=None, *, mode="hybrid", architecture="auto", firmware=None,
                bootloader="auto"):
    from .live import create_live_image, validate_persistence

    if mode not in ("hybrid", "extract"):
        raise RufusError("Select hybrid or FAT32 extraction mode for Linux media.")
    if mode == "hybrid" or persistence_size:
        validate_persistence(persistence_size, casper_label)
    if mode == "hybrid" and (architecture != "auto" or firmware is not None or bootloader != "auto"):
        raise RufusError("Hybrid mode retains the ISO's architecture and firmware support.")
    guard(device, source)
    if device.logical_sector != 512:
        raise RufusError("Persistent live media currently requires 512-byte logical sectors.")
    with tempfile.TemporaryDirectory(prefix="rufus-live-drive-") as directory:
        guard(device, directory)
        image = Path(directory) / "live.img"
        if mode == "extract":
            from .linux_iso import create_extracted_image
            result = create_extracted_image(source, image, device.size, persistence_size, casper_label,
                                            architecture, firmware, expected, cancel, progress, bootloader=bootloader)
        else:
            result = create_live_image(source, image, persistence_size, casper_label, device.size,
                                       expected, cancel, progress)
        guard(device, source)
        write_to_device(image, device, result["sha256"], verify, cancel, progress)
    return result["source_sha256"]


def backup_device(device, output, image_format="raw", verify=True, cancel=None, progress=None):
    from .backup import save_image

    output = Path(output).absolute()
    parent = output.parent.resolve(strict=True)
    if os.path.lexists(output):
        raise RufusError("Backup destination already exists. Select a new image filename.")
    if image_format not in ("raw", "vhd", "vhdx"):
        raise RufusError("Backup format must be raw, vhd, or vhdx.")
    required = device.size + (512 if image_format == "vhd" else 0)
    if image_format == "vhd":
        from .images import validate_vhd_size
        validate_vhd_size(device.size)
        if device.logical_sector != 512:
            raise RufusError("VHD requires 512-byte logical sectors. Use VHDX for a 4 KiB-sector source.")
    elif image_format == "vhdx":
        from .vhdx import fixed_layout
        required = fixed_layout(device.size, device.logical_sector)["file_size"]
    guard(device, parent, writable=False)
    if shutil.disk_usage(parent).free < required:
        raise RufusError("The destination filesystem does not have enough space for this drive image.")
    check_cancel(cancel)
    service = UDisks()
    service.ensure_unmounted(device, parent, writable=False)
    guard(device, parent, writable=False)
    fd = service.open_disk(device, "r")
    try:
        info = os.fstat(fd)
        actual = f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}"
        if not stat.S_ISBLK(info.st_mode) or actual != device.number:
            raise RufusError("Opened drive does not match the selected backup source.")
        capacity = disk_capacity(fd)
        if capacity != device.size:
            raise RufusError("The selected drive size changed. Refresh and retry.")
        guard(device, parent, writable=False)
        return save_image(fd, capacity, output, image_format, verify, cancel, progress, logical_sector=device.logical_sector)
    finally:
        os.close(fd)


def freedos_device(device, fat_bits=32, label="FREEDOS", size=None, verify=True, cancel=None, progress=None):
    from .boot import MIB, create_freedos, validate_freedos

    guard(device)
    if device.logical_sector != 512:
        raise RufusError("FreeDOS boot media requires 512-byte logical sectors.")
    size = size if size is not None else device.size // MIB * MIB
    validate_freedos(size, fat_bits, label)
    if size > device.size:
        raise RufusError("FreeDOS image is larger than the selected drive.")
    with tempfile.TemporaryDirectory(prefix="rufus-freedos-drive-") as directory:
        guard(device, directory)
        image = Path(directory) / "freedos.img"
        create_freedos(image, size, fat_bits, label, cancel, progress)
        return write_to_device(image, device, verify=verify, cancel=cancel, progress=progress)
