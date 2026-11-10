"""NVIDIA Jetson installer media, legacy SD images, and SDK Manager handoff.

NVIDIA's installer and recovery tools own target-device installation and QSPI
updates. Rufus writes their media unchanged; it never substitutes generic GRUB.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import shutil
import struct
import subprocess
import tempfile

from .backup import new_output
from .core import RufusError, check_cancel, prepare_image, run_tool, write_image, write_to_file
from .live import iso_command, tool_text

GUIDE = "https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html"
SDK_GUIDE = "https://docs.nvidia.com/sdk-manager/install-with-sdkm-jetson/index.html"
MODELS = {
    "orin-nano": ("Jetson Orin Nano developer kit", ("nvme", "sd", "usb"), True, True),
    "orin-nx": ("Jetson Orin NX", ("nvme", "usb"), True, False),
    "agx-orin": ("Jetson AGX Orin", ("emmc", "nvme", "sd", "usb"), True, False),
    "thor": ("Jetson AGX Thor", ("nvme", "usb"), True, False),
    "xavier-nx": ("Jetson Xavier NX", ("sd", "emmc", "nvme", "usb"), False, True),
    "agx-xavier": ("Jetson AGX Xavier", ("emmc", "nvme", "usb"), False, False),
    "nano": ("Jetson Nano developer kit", ("sd", "emmc", "usb"), False, True),
    "tx2": ("Jetson TX2", ("emmc", "usb"), False, False),
    "tx1": ("Jetson TX1", ("emmc", "usb"), False, False),
}
SD_GUIDES = {
    "nano": "https://developer.nvidia.com/embedded/learn/get-started-jetson-nano-devkit",
    "xavier-nx": "https://developer.nvidia.com/embedded/learn/get-started-jetson-xavier-nx-devkit",
    "orin-nano": "https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/latest/update_firmware.html",
}


def installation_plan(model, storage="nvme", method="installer"):
    if model not in MODELS or method not in ("installer", "sd", "recovery"):
        raise RufusError("Select a supported Jetson model and installation method.")
    name, targets, installer, sd = MODELS[model]
    if storage not in targets:
        raise RufusError(f"{name}: select supported storage or use NVIDIA's carrier-specific BSP instructions.")
    if method == "installer" and not installer:
        raise RufusError(f"{name} needs its matching legacy SD image or NVIDIA SDK Manager recovery flashing.")
    if method == "sd" and (not sd or storage != "sd"):
        raise RufusError("Legacy SD-image mode requires a supported SD-card developer kit and an SD target.")
    steps = ["Match the NVIDIA release and carrier-board support to the selected Jetson model."]
    if method == "installer":
        steps += ["Write the NVIDIA Jetson installer ISO to a USB flash drive, not to the installation target.",
                  "Confirm compatible Jetson QSPI/UEFI firmware using NVIDIA's release instructions.",
                  f"Boot that USB drive on the Jetson and select its attached {storage} storage in NVIDIA's installer."]
    elif method == "sd":
        steps += ["Use the legacy NVIDIA SD-card image for this exact developer kit and JetPack release.",
                  "Write the image to the removable SD card and boot it in the Jetson.",
                  "A card image does not update QSPI firmware; use NVIDIA's matching firmware update procedure."]
    else:
        steps += ["Connect the Jetson's recovery USB port and put it in Force Recovery mode.",
                  "Use NVIDIA SDK Manager to detect the board and select the matching JetPack/BSP and target storage.",
                  "SDK Manager performs authentication, QSPI/eMMC/NVMe flashing, and its own final confirmation.",
                  "Third-party carrier boards require their vendor's BSP/configuration rather than a generic devkit profile."]
        if not installer:
            steps.append("Legacy modules may need archived JetPack/SDK Manager versions and their supported host OS.")
    return {"model": model, "name": name, "target_storage": storage, "method": method,
            "media_destination": "USB installer drive" if method == "installer" else "SD card" if method == "sd" else None,
            "steps": steps, "documentation": SDK_GUIDE if method == "recovery" else SD_GUIDES[model] if method == "sd" else GUIDE,
            "board_compatibility": "Selected by user; confirm exact module, carrier, and release with NVIDIA."}


def pe_arm64(data):
    if len(data) < 64 or data[:2] != b"MZ":
        return False
    position = struct.unpack_from("<I", data, 60)[0]
    return (position + 6 <= len(data) and data[position:position + 4] == b"PE\0\0" and
            struct.unpack_from("<H", data, position + 4)[0] == 0xaa64)


def validate_installer(image, work, cancel=None):
    if not shutil.which("xorriso"):
        raise RufusError("Jetson ISO validation requires xorriso.")
    paths = shlex.split(tool_text(iso_command(image, "-find", "/", "-type", "f", "-exec", "echo", "--"), cancel))
    names = {path.casefold(): path for path in paths}
    if not any(p.startswith("/casper/") and p.endswith(".nvidia.squashfs") for p in names):
        raise RufusError("ISO is missing NVIDIA's Jetson installer overlay (*.nvidia.squashfs).")
    if not any(p.startswith("/pool/") for p in names) or "/boot/grub/grub.cfg" not in names:
        raise RufusError("ISO is missing the Jetson installer repository or boot configuration.")
    loader = work / "bootaa64.efi"
    if "/efi/boot/bootaa64.efi" in names:
        run_tool(iso_command(image, "-osirrox", "on", "-extract", names["/efi/boot/bootaa64.efi"], str(loader)), cancel)
    elif "/boot/grub/efi.img" in names:
        if not shutil.which("mtype"):
            raise RufusError("Jetson's embedded EFI partition requires mtools for validation.")
        efi = work / "efi.img"
        run_tool(iso_command(image, "-osirrox", "on", "-extract", names["/boot/grub/efi.img"], str(efi)), cancel)
        with loader.open("xb") as stream:
            run_tool(["mtype", "-i", str(efi), "::/EFI/BOOT/BOOTAA64.EFI"], cancel, stdout=stream)
    else:
        raise RufusError("Jetson ISO has no ARM64 removable-media EFI loader.")
    with loader.open("rb") as stream:
        if not pe_arm64(stream.read(1024 * 1024)):
            raise RufusError("Jetson installer EFI loader is not ARM64.")


@contextmanager
def prepared_media(source, model, storage, method, expected="", cancel=None, progress=None, device=None):
    plan = installation_plan(model, storage, method)
    if method == "recovery":
        raise RufusError("Recovery flashing belongs to NVIDIA SDK Manager; it does not use a disk image writer.")
    with tempfile.TemporaryDirectory(prefix="rufus-jetson-") as directory:
        work = Path(directory)
        if device is not None:
            from .devices import guard
            guard(device, work)
        snapshot = work / "jetson.img"
        with prepare_image(source, expected, cancel, progress) as image:
            if device is not None and image.size > device.size:
                raise RufusError("Jetson image is larger than the selected removable drive.")
            if not image.hybrid or (method == "installer" and not image.is_iso) or (method == "sd" and image.is_iso):
                raise RufusError("Select a hybrid NVIDIA installer ISO." if method == "installer" else
                                 "Select a whole NVIDIA SD-card disk image, not an ISO or BSP/rootfs archive.")
            with new_output(snapshot) as target:
                write_image(image, target, image.size, True, cancel, progress)
        if method == "installer":
            validate_installer(snapshot, work, cancel)
        check_cancel(cancel)
        yield snapshot, image.sha256, plan


def write_media(source, *, model, storage="nvme", method="installer", output=None, device=None,
                expected="", verify=True, cancel=None, progress=None):
    if (output is None) == (device is None):
        raise RufusError("Select one Jetson media destination: a new output file or removable drive.")
    if output is not None and os.path.lexists(output):
        raise RufusError("Image destination already exists. Select a new filename.")
    if device is not None:
        from .devices import guard
        guard(device, source)
    with prepared_media(source, model, storage, method, expected, cancel, progress, device) as (image, digest, plan):
        if output is not None:
            result = write_to_file(image, output, digest, verify, cancel, progress)
        else:
            from .devices import guard, write_to_device
            guard(device, image)
            guard(device, source)
            result = write_to_device(image, device, digest, verify, cancel, progress)
    return {**plan, "sha256": result, "vendor_media_preserved": True,
            "target_installation": "Performed by NVIDIA on the Jetson; Rufus prepared boot media only."}


def recovery_devices(root=Path("/sys/bus/usb/devices")):
    found = []
    for path in sorted(root.iterdir()):
        try:
            if (path / "idVendor").read_text().strip().lower() != "0955":
                continue
            product = (path / "product").read_text().strip() if (path / "product").exists() else "NVIDIA USB device"
            found.append({"usb_port": path.name, "vendor_id": "0955",
                          "product_id": (path / "idProduct").read_text().strip(), "product": product,
                          "recovery_candidate": product.upper() == "APX"})
        except (OSError, ValueError):
            continue
    return found


def launch_sdkmanager():
    executable = shutil.which("sdkmanager")
    if not executable:
        raise RufusError("Install NVIDIA SDK Manager from https://developer.nvidia.com/sdk-manager "
                         "on a host supported by your JetPack release, then launch recovery setup again.")
    # Launch the vendor wizard. No passwords, EULA acceptance, flashing flags,
    # or board guesses are passed by Rufus.
    process = subprocess.Popen([executable], start_new_session=True)
    return {"status": "NVIDIA SDK Manager launched", "pid": process.pid, "documentation": SDK_GUIDE}
