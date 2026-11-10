"""Persistent hybrid live media, rebuilt without mounts or elevated access."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import tempfile
import uuid

from .backup import new_output
from .boot import MIB, copy_sparse
from .core import (RufusError, check_cancel, checksum, emit, expected_sha256,
                   run_tool, source_file)
from .layout import GUID_TYPES

MANIFESTS = {"/md5sum.txt": "md5", "/MD5SUMS": "md5",
             "/sha256sum.txt": "sha256", "/SHA256SUMS": "sha256"}
BOOT_DIRS = ("/boot/grub/", "/isolinux/", "/syslinux/", "/EFI/BOOT/")


def tool_text(args, cancel=None, limit=4 * MIB):
    with tempfile.TemporaryFile() as output:
        run_tool(args, cancel, stdout=output)
        output.seek(0)
        value = output.read(limit + 1)
    if len(value) > limit:
        raise RufusError("Live ISO metadata exceeds the supported size.")
    return value.decode("utf-8", errors="strict")


def iso_command(source, *args):
    return ["xorriso", "-no_rc", "-abort_on", "FAILURE", "-indev", str(source), *args]


def boot_report(source, cancel=None):
    return tool_text(iso_command(source, "-report_el_torito", "plain", "-report_system_area", "plain"), cancel)


def firmware_modes(report):
    return {match.group(1) for match in re.finditer(r"^El Torito boot img\s*:\s*\d+\s+(BIOS|UEFI)\s+y\b",
                                                   report, re.MULTILINE)}


def rewrite_config(text):
    """Modify live kernel arguments only; keep rescue and media-check entries."""
    kinds = set()
    count = 0
    lines = []
    # GRUB and Syslinux both accept continued command lines.
    logical = re.sub(r"\\\r?\n", " ", text)
    for line in logical.splitlines(keepends=True):
        if re.match(r"^\s*(?:linux(?:efi|16)?|append)\s", line, re.IGNORECASE):
            command, separator, comment = line.partition("#")
            found = re.search(r"(?<!\S)boot=(casper|live)(?=\s|$)", command)
            if found and not re.search(r"(?<!\S)(?:nopersistent|nopersistence|integrity-check)(?=\s|$)", command):
                kind = "casper" if found[1] == "casper" else "debian"
                # Custom persistence stores need a separate workflow; do not
                # silently create a volume that these options would ignore.
                if re.search(r"(?<!\S)(?:persistent-path|persistence-[\w-]+)=", command):
                    raise RufusError("ISO uses custom persistence options that are not supported.")
                option = "persistent" if kind == "casper" else "persistence"
                if not re.search(rf"(?<!\S){option}(?=\s|$)", command):
                    end = found.end()
                    command = command[:end] + " " + option + command[end:]
                line = command + separator + comment
                kinds.add(kind)
                count += 1
        lines.append(line)
    return "".join(lines) if count else text, kinds, count


def update_manifest(text, changed, algorithm, exclude=None):
    lines = []
    length = hashlib.new(algorithm).digest_size * 2
    for line in text.splitlines(keepends=True):
        match = re.match(rf"^([a-fA-F0-9]{{{length}}})( [ *])(.+?)(\r?\n)?$", line)
        if match:
            name = match[3]
            path = "/" + name.removeprefix("./").lstrip("/")
            # A manifest cannot contain its own final digest. Some generated
            # ISOs include a stale entry; remove it before assigning extents.
            if path == exclude:
                continue
            if path in changed:
                line = hashlib.new(algorithm, changed[path]).hexdigest() + match[2] + name + (match[4] or "")
        lines.append(line)
    return "".join(lines)


def stage_configs(source, directory, cancel=None):
    if tool_text(iso_command(source, "-find", "/", "-type", "f", "-name", "*.nvidia.squashfs",
                             "-exec", "echo", "--"), cancel).strip():
        raise RufusError("Use Jetson installer mode to preserve NVIDIA's installer and firmware support unchanged.")
    listing = tool_text(iso_command(source, "-find", "/", "-type", "f", "-size", "-8388609c",
                                    "-exec", "echo", "--"), cancel)
    paths = shlex.split(listing)
    configs = [p for p in paths if p.startswith(BOOT_DIRS) and p.lower().endswith(".cfg")]
    if not configs or len(configs) > 128:
        raise RufusError("ISO needs supported GRUB/Syslinux live boot configuration files.")
    changed, mapped, manifests, kinds, count = {}, [], [], set(), 0
    for index, path in enumerate(configs + [p for p in MANIFESTS if p in paths]):
        if (not path.startswith("/") or ".." in PurePosixPath(path).parts or
                any(ord(c) < 32 for c in path)):
            raise RufusError("Unsupported live ISO configuration path.")
        local = directory / f"config-{index}"
        run_tool(iso_command(source, "-osirrox", "on", "-extract", path, str(local)), cancel)
        data = local.read_bytes()
        if b"\0" in data or (path not in MANIFESTS and len(data) > MIB):
            raise RufusError("Unsupported live ISO boot configuration.")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RufusError("Live boot configuration must use UTF-8 text.") from exc
        if path in MANIFESTS:
            modified = update_manifest(text, changed, MANIFESTS[path], exclude=path)
            manifests.append((path, local))
        else:
            modified, detected, number = rewrite_config(text)
            kinds.update(detected)
            count += number
        if modified != text:
            local.write_bytes(modified.encode("utf-8"))
            changed[path] = local.read_bytes()
            mapped.extend(["-map", str(local), path])
    if len(kinds) != 1 or not count:
        raise RufusError("ISO must contain one supported Casper or Debian live boot configuration.")
    return kinds.pop(), mapped, manifests


def refresh_boot_manifests(image, report, manifests, directory, cancel=None):
    """Account for boot-info-table bytes patched by xorriso's replay.

    Digest replacements have identical lengths. Patch the newly built manifest
    extents in place so another remaster cannot move and repatch the boot files.
    xorriso's internal session MD5 tags are disabled when creating this image.
    """
    if not manifests:
        return
    boot_files = {}
    paths = re.findall(r"^El Torito img path\s*:\s*\d+\s+(.+)$", report, re.MULTILINE)
    for index, path in enumerate(paths):
        local = directory / f"boot-image-{index}"
        run_tool(iso_command(image, "-osirrox", "on", "-extract", path, str(local)), cancel)
        if local.stat().st_size > 64 * MIB:
            raise RufusError("Boot image is too large for media-check manifest updating.")
        boot_files[path] = local.read_bytes()
    for path, local in manifests:
        original = local.read_bytes()
        updated = update_manifest(original.decode("utf-8"), boot_files, MANIFESTS[path]).encode("utf-8")
        if original == updated:
            continue
        layout = tool_text(iso_command(image, "-find", path, "-exec", "report_lba", "--"), cancel)
        extents = re.findall(r"^File data lba:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,",
                             layout, re.MULTILINE)
        if len(extents) != 1 or len(updated) != len(original):
            raise RufusError("Unsupported media-check manifest layout.")
        extent, start, blocks, size = map(int, extents[0])
        offset = start * 2048
        if extent or size != len(original) or size > blocks * 2048 or offset + size > image.stat().st_size:
            raise RufusError("Invalid media-check manifest extent.")
        check_cancel(cancel)
        with image.open("r+b") as disk:
            disk.seek(offset)
            if disk.read(size) != original:
                raise RufusError("Media-check manifest changed during image construction.")
            disk.seek(offset)
            disk.write(updated)
            disk.flush()
            os.fsync(disk.fileno())


def validate_persistence(size, casper_label="writable"):
    if not isinstance(size, int) or size < 64 * MIB or size % MIB:
        raise RufusError("Persistence size must be a whole number of MiB, at least 64 MiB.")
    if casper_label not in ("writable", "casper-rw"):
        raise RufusError("Casper persistence label must be writable or casper-rw.")


def reject_existing_store(source, cancel=None):
    table = json.loads(tool_text(["sfdisk", "--json", str(source)], cancel))["partitiontable"]
    with source.open("rb") as disk:
        for partition in table.get("partitions", []):
            disk.seek(partition["start"] * 512 + 1024)
            block = disk.read(136)
            if (block[56:58] == b"\x53\xef" and
                    block[120:136].rstrip(b"\0") in (b"writable", b"casper-rw", b"persistence")):
                raise RufusError("Source already contains a persistence volume. Select the original live ISO.")


def validate_volume(image, volume_uuid, size, cancel=None):
    table = json.loads(tool_text(["sfdisk", "--json", str(image)], cancel))["partitiontable"]
    if table.get("sectorsize") != 512 or table.get("label") not in ("gpt", "dos"):
        raise RufusError("Rebuilt live image has an unsupported partition table.")
    candidates = [p for p in table["partitions"] if p["type"].lower() in ("83", GUID_TYPES["linux"])]
    matches = []
    with image.open("rb") as disk:
        for partition in candidates:
            offset, amount = partition["start"] * 512, partition["size"] * 512
            disk.seek(offset + 1024)
            superblock = disk.read(1024)
            if (len(superblock) == 1024 and superblock[56:58] == b"\x53\xef" and
                    superblock[104:120] == uuid.UUID(volume_uuid).bytes and amount >= size and
                    offset + amount <= image.stat().st_size):
                matches.append(partition)
    if len(matches) != 1:
        raise RufusError("Rebuilt ISO did not retain the new ext4 persistence partition.")
    store = matches[0]
    if any(p is not store and p["start"] < store["start"] + store["size"] and
           store["start"] < p["start"] + p["size"] for p in table["partitions"]):
        raise RufusError("Rebuilt persistence partition overlaps another image partition.")
    return table


def create_live_image(source, output, persistence_size=1024 * MIB, casper_label="writable",
                      capacity=None, expected="", cancel=None, progress=None):
    validate_persistence(persistence_size, casper_label)
    expected = expected_sha256(expected)
    if os.path.lexists(output):
        raise RufusError("Image destination already exists. Select a new filename.")
    for tool in ("xorriso", "mkfs.ext4", "e2fsck", "sfdisk"):
        if not shutil.which(tool):
            raise RufusError(f"Linux persistence requires {tool} (xorriso, e2fsprogs, fdisk).")
    source = Path(source).resolve()
    with source_file(source) as stream:
        before = os.fstat(stream.fileno())
        header = stream.read(512)
        if header[510:512] != b"\x55\xaa" or not any(header[446:510]):
            raise RufusError("Persistence requires a hybrid Linux live ISO with a disk partition table.")
    if capacity is not None and (capacity % 512 or before.st_size + persistence_size > capacity):
        raise RufusError("The ISO and persistence partition will not fit on the selected drive.")
    source_hash = checksum(source, cancel, progress)
    if expected and source_hash != expected:
        raise RufusError("Linux ISO SHA-256 does not match the expected checksum.")
    report = boot_report(source, cancel)
    modes = firmware_modes(report)
    if not modes or not any(word in report for word in ("grub2-mbr", "isohybrid", "GPT")):
        raise RufusError("ISO boot layout is not supported for persistent hybrid media.")
    reject_existing_store(source, cancel)
    replay = tool_text(iso_command(source, "-report_el_torito", "cmd"), cancel)
    used = [int(n) for n in re.findall(r"^-append_partition (\d+) ", replay, re.MULTILINE)]
    number = max([1, *used]) + 1
    if number > 4:
        raise RufusError("ISO has no available appended partition slot for persistence.")
    with tempfile.TemporaryDirectory(prefix="rufus-live-image-") as directory:
        directory = Path(directory)
        emit(progress, "Preparing live boot options")
        kind, mapped, manifests = stage_configs(source, directory, cancel)
        label = casper_label if kind == "casper" else "persistence"
        root = directory / "persistence-root"
        root.mkdir()
        if kind == "debian":
            (root / "persistence.conf").write_text("/ union\n", encoding="utf-8")
        volume = directory / "persistence.ext4"
        with volume.open("xb") as disk:
            disk.truncate(persistence_size)
        volume_uuid = str(uuid.uuid4())
        emit(progress, "Creating ext4 persistence volume")
        run_tool(["mkfs.ext4", "-q", "-F", "-L", label, "-U", volume_uuid, "-m", "0",
                  "-O", "^orphan_file", "-E", "lazy_itable_init=0,lazy_journal_init=0",
                  "-d", str(root), str(volume)], cancel)
        run_tool(["e2fsck", "-fn", str(volume)], cancel)
        prepared = directory / "live.img"
        emit(progress, "Rebuilding hybrid live image")
        # Replay also retains the ISO UUID used by embedded GRUB EFI loaders.
        # Append AFTER replay so the new store cannot be overwritten by replay.
        run_tool(iso_command(source, "-outdev", str(prepared), *mapped, "-md5", "off", "-boot_image", "any", "replay",
                             "-append_partition", str(number), GUID_TYPES["linux"], str(volume),
                             "-commit", "-end"), cancel)
        rebuilt_report = boot_report(prepared, cancel)
        if not modes <= firmware_modes(rebuilt_report):
            raise RufusError("Rebuilt live image lost an original firmware boot entry.")
        refresh_boot_manifests(prepared, rebuilt_report, manifests, directory, cancel)
        table = validate_volume(prepared, volume_uuid, persistence_size, cancel)
        size = prepared.stat().st_size
        if capacity is not None:
            if size > capacity:
                raise RufusError("The rebuilt live image is larger than the selected drive.")
            if table["label"] == "gpt" and size < capacity:
                # Keep the backup GPT at the physical drive's end while leaving
                # the user-selected persistence size unchanged.
                with prepared.open("r+b") as disk:
                    disk.truncate(capacity)
                run_tool(["sfdisk", "--relocate", "gpt-bak-std", str(prepared)], cancel)
                validate_volume(prepared, volume_uuid, persistence_size, cancel)
                size = capacity
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RufusError("Linux ISO changed during preparation.")
        prepared_hash = checksum(prepared, cancel, progress)
        with new_output(output) as target, prepared.open("rb") as payload:
            os.ftruncate(target, size)
            copy_sparse(payload.fileno(), target, 0, size, cancel, progress)
            os.fsync(target)
            if checksum(output, cancel, progress) != prepared_hash:
                raise RufusError("Prepared live image verification failed.")
            check_cancel(cancel)
            emit(progress, "Complete", size, size)
    return {"sha256": prepared_hash, "source_sha256": source_hash, "size": size,
            "persistence_size": persistence_size, "persistence_label": label, "kind": kind,
            "firmware": sorted(modes), "table": table["label"]}
