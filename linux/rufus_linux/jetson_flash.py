"""Reviewed direct NVIDIA BSP recovery jobs; no SDK Manager dependency.

Planning only reads local files/sysfs. Vendor scripts run only after explicit
execution, target/plan confirmation, and one privileged helper invocation.
"""

from collections import deque
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time

from .core import Cancelled, RufusError, check_cancel, emit
from .jetson import MODELS

SYSTEM_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
USB_ROOT = Path("/sys/bus/usb/devices")
JOB_LOCK = Path("/run/lock/rufus-jetson-flash.lock")
TERMINATE_GRACE = 5.0
GUIDE = "https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html"
# NVIDIA's Quick Start recovery IDs. A PID identifies a family/candidate SKU,
# not a carrier board. NVIDIA's scripts still read and validate EEPROM data.
RECOVERY_IDS = {
    "7023": ("agx-orin", "P3701"), "7223": ("agx-orin", "P3701-0004"),
    "7323": ("orin-nx", "P3767-0000"), "7423": ("orin-nx", "P3767-0001"),
    "7523": ("orin-nano", "P3767-0003/0005"), "7623": ("orin-nano", "P3767-0004"),
    "7026": ("thor", "P3834-0008"), "7226": ("thor", "P3834-0000"),
    "7e19": ("xavier-nx", "P3668"), "7019": ("agx-xavier", "P2888"),
    "7f21": ("nano", "P3448"), "7018": ("tx2", "P3489 (TX2i)"),
    "7c18": ("tx2", "P3310/P3636"), "7418": ("tx2", "P3489 (TX2 4GB)"), "7721": ("tx1", "P2180"),
}
REFERENCE_PROFILES = {
    "orin-nano": "jetson-orin-nano-devkit", "orin-nx": "jetson-orin-nano-devkit",
    "agx-orin": "jetson-agx-orin-devkit", "thor": "jetson-agx-thor-devkit",
    "xavier-nx": "jetson-xavier-nx-devkit", "agx-xavier": "jetson-agx-xavier-devkit",
    "nano": "jetson-nano-devkit", "tx2": "jetson-tx2-devkit", "tx1": "jetson-tx1-devkit",
}


@dataclass(frozen=True)
class FlashRequest:
    bsp: str
    model: str
    storage: str
    board: str
    usb_port: str = ""
    external_device: str = ""
    layout: str = ""
    update_firmware: bool = True
    action: str = "flash"


def _text(path, limit=1024 * 1024):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RufusError(f"BSP metadata is too large: {path.name}")
    return data.decode("utf-8", errors="replace")


def _member(root, relative):
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RufusError(f"BSP file is missing: {relative}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise RufusError(f"BSP file must stay inside Linux_for_Tegra: {relative}")
    return path


def _file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def connected_devices():
    devices = []
    for path in sorted(USB_ROOT.iterdir()):
        try:
            if not re.fullmatch(r"\d+-\d+(?:\.\d+)*", path.name):
                continue
            vendor = _text(path / "idVendor", 128).strip().lower()
            product = _text(path / "idProduct", 128).strip().lower()
            if vendor != "0955" or product not in RECOVERY_IDS:
                continue
            family, module = RECOVERY_IDS[product]
            # Bus/devnum plus the sysfs inode detect unplug/replug, including
            # replacement by another unit of the same model on the same port.
            devices.append({"usb_port": path.name, "vendor_id": vendor, "product_id": product,
                            "model": family, "module_candidates": module,
                            "serial": _text(path / "serial", 1024).strip() if (path / "serial").is_file() else "",
                            "busnum": _text(path / "busnum", 128).strip(),
                            "devnum": _text(path / "devnum", 128).strip(),
                            "sysfs_inode": path.stat().st_ino})
        except (OSError, ValueError, RufusError):
            continue
    return devices


def _elf_arch(path):
    with path.open("rb") as stream:
        data = stream.read(64)
    if len(data) < 20 or data[:4] != b"\x7fELF" or data[5] not in (1, 2):
        return None
    machine = struct.unpack_from("<H" if data[5] == 1 else ">H", data, 18)[0]
    return {3: "x86", 62: "x86_64", 40: "arm", 183: "arm64"}.get(machine, f"ELF machine {machine}")


def _host_arch():
    value = platform.machine().lower()
    return {"amd64": "x86_64", "aarch64": "arm64", "armv8l": "arm", "armv7l": "arm", "armv6l": "arm",
            "i386": "x86", "i686": "x86"}.get(value, value)


def inspect_bsp(directory):
    root = Path(directory).expanduser().resolve()
    # NVIDIA's shell scripts build/eval their own commands. Limit their input
    # paths to ordinary characters instead of assuming every vendor release
    # correctly shell-quotes whitespace or metacharacters internally.
    if not re.fullmatch(r"/[A-Za-z0-9_./+\-]+", str(root)) or not root.is_dir():
        raise RufusError("Select an existing Linux_for_Tegra directory without spaces or shell metacharacters.")
    flash = _member(root, "flash.sh")
    _member(root, "kernel/Image")
    profiles = sorted(p.stem for p in root.glob("*.conf") if p.is_file() and
                      re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*", p.stem) and p.resolve().is_relative_to(root))
    if not profiles:
        raise RufusError("BSP has no board configuration profiles.")
    version = None
    for relative in ("nv_tegra/nv_tegra_release", "rootfs/etc/nv_tegra_release"):
        path = root / relative
        if path.is_file():
            with path.open("rb") as stream:
                line = stream.read(4096).decode(errors="replace")
            found = re.search(r"#\s*R(\d+)\s*\(release\).*?REVISION:\s*([\d.]+)", line)
            if found:
                version = f"{found[1]}.{found[2]}"
                break
    if version is None:
        raise RufusError("Cannot identify the Jetson Linux BSP release from nv_tegra_release.")
    tool_arches = {}
    for name in ("tegrarcm_v2", "tegrarcm", "tegradevflash_v2", "tegrahost_v2", "tegraparser_v2"):
        path = root / "bootloader" / name
        if path.is_file():
            _member(root, "bootloader/" + name)
            tool_arches[name] = _elf_arch(path)
    if not tool_arches:
        raise RufusError("BSP is missing NVIDIA's host recovery binaries.")
    host = _host_arch()
    runnable = {host, "x86"} if host == "x86_64" else {host}
    incompatible = [name for name, arch in tool_arches.items() if arch is not None and arch not in runnable]
    rootfs_release = root / "rootfs/etc/nv_tegra_release"
    rootfs_ready = rootfs_release.is_file() and (root / "rootfs/etc/os-release").is_file()
    if rootfs_ready:
        with rootfs_release.open("rb") as stream:
            match = re.search(rb"#\s*R(\d+)\s*\(release\).*?REVISION:\s*([\d.]+)", stream.read(4096))
        rootfs_ready = bool(match and f"{match[1].decode()}.{match[2].decode()}" == version)
    return {"directory": str(root), "release": version, "profiles": profiles, "host_architecture": host,
            "tool_architectures": tool_arches, "incompatible_tools": incompatible,
            "rootfs_ready": rootfs_ready, "read_info": "--read-info" in _text(flash)}


def _first_layout(root, choices):
    for relative in choices:
        if (root / relative).is_file():
            _member(root, relative)
            return relative
    raise RufusError("BSP has no matching external-storage XML. Select its vendor-provided partition layout.")


def _fingerprint(root, extras):
    paths = set(extras)
    for folder, patterns in ((root, ("*.conf*", "*.sh")),
                             (root / "tools/kernel_flash", ("*.sh", "*.func", "*.xml")),
                             (root / "bootloader", ("*.py", "tegra*"))):
        for pattern in patterns:
            paths.update(p for p in folder.glob(pattern) if p.is_file())
    fingerprint = hashlib.sha256()
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        _member(root, relative)
        info = path.stat()
        fingerprint.update(json.dumps([relative, str(path.resolve().relative_to(root)), info.st_size,
                                       info.st_mtime_ns, info.st_ino, _file_digest(path)], separators=(",", ":")).encode())
    return fingerprint.hexdigest()


def make_plan(request):
    if not isinstance(request, FlashRequest):
        raise RufusError("Invalid Jetson flash request.")
    if request.model not in MODELS or request.storage not in MODELS[request.model][1]:
        raise RufusError("Select a supported Jetson model/storage combination.")
    if request.action not in ("flash", "probe") or type(request.update_firmware) is not bool:
        raise RufusError("Select flash or probe and an explicit firmware-update choice.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*", request.board):
        raise RufusError("Select a board profile name from the BSP, without .conf or path separators.")
    if request.usb_port and not re.fullmatch(r"\d+-\d+(?:\.\d+)*", request.usb_port):
        raise RufusError("Use the recovery USB port path, for example 1-2.4.")
    info = inspect_bsp(request.bsp)
    root = Path(info["directory"])
    if request.board not in info["profiles"]:
        raise RufusError("Selected board profile is not present inside this BSP.")
    # Reject obvious reference-profile mixups without pretending to infer a
    # third-party carrier's arbitrary configuration from its filename.
    reference_families = {
        "jetson-orin-nano-": ("orin-nano", "orin-nx"), "jetson-agx-orin-": ("agx-orin",),
        "jetson-agx-thor-": ("thor",), "jetson-xavier-nx-": ("xavier-nx",),
        "jetson-agx-xavier-": ("agx-xavier",), "jetson-nano-": ("nano",),
        "jetson-tx2-": ("tx2",), "jetson-tx1-": ("tx1",),
    }
    if "tx2-nx" not in request.board:
        for prefix, families in reference_families.items():
            if request.board.startswith(prefix) and request.model not in families:
                raise RufusError("Selected NVIDIA reference board profile belongs to a different Jetson family.")
    if request.action == "flash" and request.storage == "emmc" and request.board in (
            "jetson-nano-devkit", "jetson-nano-2gb-devkit", "jetson-nano-qspi-sd", "jetson-xavier-nx-devkit"):
        raise RufusError("Select the eMMC module's BSP profile; this developer-kit profile targets an SD-card module.")
    major = int(info["release"].split(".")[0])
    for value in (request.layout, request.external_device):
        if value and not re.fullmatch(r"[A-Za-z0-9_./+\-]+", value):
            raise RufusError("BSP layout/device names cannot contain spaces or shell metacharacters.")
    if request.layout and (Path(request.layout).is_absolute() or ".." in Path(request.layout).parts):
        raise RufusError("Select an XML layout inside the BSP using its relative path.")
    paths = [_member(root, request.board + ".conf"), _member(root, "flash.sh"), _member(root, "kernel/Image")]
    paths.extend(root / relative for relative in ("nv_tegra/nv_tegra_release", "rootfs/etc/nv_tegra_release",
                                                 "rootfs/etc/os-release") if (root / relative).is_file())
    external = request.storage in ("nvme", "usb") or request.storage == "sd" and request.model in ("orin-nano", "agx-orin")
    if request.action == "probe":
        if not info["read_info"]:
            raise RufusError("This BSP does not provide flash.sh --read-info. Its flash script performs EEPROM detection.")
        script, args, rootdev = "flash.sh", ["--read-info"], "internal"
    elif external or request.model == "thor" or request.model in ("orin-nano", "orin-nx", "agx-orin") and major >= 39:
        script = "l4t_initrd_flash.sh" if (root / "l4t_initrd_flash.sh").is_file() else "tools/kernel_flash/l4t_initrd_flash.sh"
        args, rootdev = ["--network", "usb0"], "internal"
        if external:
            device = request.external_device or {"nvme": "nvme0n1p1", "usb": "sda1", "sd": "mmcblk1p1"}[request.storage]
            patterns = {"nvme": r"nvme\d+n[1-9]\d*(?:p[1-9]\d*)?", "usb": r"sd[a-z](?:[1-9]\d*)?",
                        "sd": r"mmcblk\d+(?:p[1-9]\d*)?"}
            if not re.fullmatch(patterns[request.storage], device):
                raise RufusError("External device must name the selected storage on the Jetson, without /dev/.")
            chip = "264" if request.model == "thor" else "234"
            layout = request.layout or _first_layout(root, (f"tools/kernel_flash/flash_l4t_t{chip}_nvme.xml",
                                                           "tools/kernel_flash/flash_l4t_external.xml"))
            if not layout.endswith(".xml"):
                raise RufusError("Select a vendor XML partition layout.")
            paths.append(_member(root, layout))
            args += ["--external-device", device, "-c", layout]
            rootdev = "internal" if request.model == "thor" else "external"
            if not request.update_firmware:
                args.append("--external-only")
            elif request.model in ("orin-nano", "orin-nx"):
                qspi = _first_layout(root, ("bootloader/generic/cfg/flash_t234_qspi.xml",
                                            "bootloader/t186ref/cfg/flash_t234_qspi.xml"))
                paths.append(_member(root, qspi))
                args += ["-p", shlex.join(["-c", qspi])]
    else:
        script, args, rootdev = "flash.sh", [], "mmcblk0p1" if major <= 32 else "internal"
    if request.action == "flash" and not external:
        if request.external_device or request.layout or not request.update_firmware:
            raise RufusError("External-device/layout and firmware-skip options apply only to external storage.")
        if request.storage == "sd" and "emmc" in request.board.casefold():
            raise RufusError("An eMMC board profile cannot be used to flash an SD-card module.")
    entry = _member(root, script)
    paths.append(entry)
    option_text = _text(entry)
    if script != "flash.sh" and (root / "tools/kernel_flash/l4t_initrd_flash.func").is_file():
        option_text += _text(_member(root, "tools/kernel_flash/l4t_initrd_flash.func"))
    issues = []
    if "usb-instance" not in option_text:
        issues.append("This BSP cannot pin the flash operation to a USB port (--usb-instance missing).")
    if request.action == "flash" and not info["rootfs_ready"]:
        issues.append("Prepare the matching sample rootfs and run NVIDIA apply_binaries.sh before flashing.")
    if info["incompatible_tools"]:
        issues.append("BSP host binaries cannot run on this host architecture: " + ", ".join(info["incompatible_tools"]))
    required = ("bash", "python3") + (("xmlstarlet", "sshpass", "abootimg", "zstd", "dtc", "rpcbind", "exportfs")
                                      if script != "flash.sh" else ())
    missing = [name for name in required if not shutil.which(name, path=SYSTEM_PATH)]
    if missing:
        issues.append("Install NVIDIA's BSP prerequisites; missing tools: " + ", ".join(missing))
    devices = connected_devices()
    target = next((d for d in devices if d["usb_port"] == request.usb_port), None)
    if not request.usb_port:
        issues.append("Select a connected NVIDIA recovery USB port to enable execution.")
    elif target is None:
        issues.append("The selected USB port does not contain a recognized NVIDIA recovery device.")
    elif target["model"] != request.model:
        issues.append("Detected recovery model does not match the selected Jetson model.")
    normalized = {**asdict(request), "bsp": str(root)}
    command = ["/bin/bash", str(entry), *args, "--usb-instance", request.usb_port or "<recovery-port>", request.board, rootdev]
    payload = {"schema": 1, "request": normalized, "release": info["release"], "target": target,
               "command": command, "bsp_fingerprint": _fingerprint(root, paths),
               "host_architecture": info["host_architecture"], "issues": issues}
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**payload, "plan_id": identity, "can_execute": not issues,
            "confirmation": f"{request.usb_port or 'offline'} {identity[:12]}",
            "command_display": shlex.join(command), "documentation": GUIDE,
            "scope": "Read board/chip/EEPROM information" if request.action == "probe" else (
                f"Erase/reinstall Jetson {request.storage} and update internal boot firmware as configured by the BSP."
                if request.update_firmware else f"Erase/reinstall external Jetson {request.storage}; skip internal firmware."),
            "identification": "USB family/candidate module; NVIDIA's script validates actual EEPROM/SKU/carrier data."}


def verify_plan(plan, confirmation):
    if not isinstance(plan, dict) or plan.get("schema") != 1 or confirmation != plan.get("confirmation"):
        raise RufusError("Direct flashing requires the exact confirmation token from the reviewed plan.")
    try:
        current = make_plan(FlashRequest(**plan["request"]))
    except (KeyError, TypeError) as exc:
        raise RufusError("Invalid saved BSP flash plan.") from exc
    if current["plan_id"] != plan.get("plan_id"):
        raise RufusError("BSP, options, or recovery device changed. Review a new flash plan.")
    if current["confirmation"] != confirmation:
        raise RufusError("Direct flashing requires the exact confirmation token from the reviewed plan.")
    if not current["can_execute"]:
        raise RufusError("Cannot execute this plan: " + " ".join(current["issues"]))
    return current


def _vendor_environment():
    value = {name: os.environ[name] for name in ("HOME", "USER", "LOGNAME") if name in os.environ}
    value.update(PATH=SYSTEM_PATH, LANG="C.UTF-8", LC_ALL="C.UTF-8")
    return value


def _stream_command(command, directory, on_line, cancelled):
    """Drain output while checking cancellation; terminate the entire job group."""
    process = subprocess.Popen(command, cwd=directory, env=_vendor_environment(), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    tail = deque(maxlen=30)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    pending = b""
    stopping = None
    def signal_group(value):
        try:
            os.killpg(process.pid, value)
        except ProcessLookupError:
            pass
    try:
        while selector.get_map() or process.poll() is None:
            if cancelled() and stopping is None:
                stopping = time.monotonic()
                signal_group(signal.SIGTERM)
            if stopping is not None and time.monotonic() - stopping > TERMINATE_GRACE:
                # The shell may already have exited while a child still owns
                # stdout. Kill the group even when its original leader exited.
                signal_group(signal.SIGKILL)
            for key, _ in selector.select(timeout=0.1):
                data = os.read(key.fileobj.fileno(), 16384)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                pending += data
                while b"\n" in pending or len(pending) > 16384:
                    if b"\n" in pending[:16385]:
                        line, pending = pending.split(b"\n", 1)
                    else:
                        line, pending = pending[:16384], pending[16384:]
                    value = line.decode("utf-8", errors="replace").rstrip("\r")
                    tail.append(value)
                    on_line(value)
        if pending:
            value = pending.decode("utf-8", errors="replace")
            tail.append(value)
            on_line(value)
        status = process.wait()
        if stopping is not None:
            raise Cancelled("Jetson job cancelled. Re-enter Force Recovery and reflash before using the target.")
        if status:
            raise RufusError(f"NVIDIA BSP command failed ({status}): " + "\n".join(tail)[-4096:])
        return status
    finally:
        selector.close()
        signal_group(signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGKILL)
                process.wait()
        process.stdout.close()


def _event(kind, **fields):
    print(json.dumps({"type": kind, **fields}), flush=True)


def _privileged_main():
    """Internal entry point. Rebuild all commands from reviewed request fields."""
    handlers = {}
    interrupted = [False]
    try:
        if os.geteuid() != 0 or len(sys.argv) != 4:
            raise RufusError("The Jetson execution helper requires authorized root access.")
        for value in (signal.SIGINT, signal.SIGTERM):
            handlers[value] = signal.signal(value, lambda *_: interrupted.__setitem__(0, True))
        plan_path, identity, confirmation = sys.argv[1:]
        with open(plan_path, "rb") as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise RufusError("Flash plan exceeds the supported size.")
        plan = json.loads(data)
        if plan.get("plan_id") != identity:
            raise RufusError("Reviewed plan identity does not match the execution request.")
        current = verify_plan(plan, confirmation)
        lock = os.open(JOB_LOCK, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise RufusError("Invalid Jetson job lock.")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RufusError("Another direct Jetson job is already running on this host.") from exc
            # Recheck after acquiring the operation lock, before any BSP code.
            current = verify_plan(plan, confirmation)
            cancel_input = selectors.DefaultSelector()
            cancel_input.register(sys.stdin.buffer, selectors.EVENT_READ)
            def cancelled():
                return interrupted[0] or bool(cancel_input.select(timeout=0))
            try:
                if cancelled():
                    raise Cancelled("Jetson authorization was cancelled before starting the BSP.")
                _event("started", plan_id=current["plan_id"])
                _stream_command(current["command"], current["request"]["bsp"],
                                lambda line: _event("log", line=line), cancelled)
            finally:
                cancel_input.close()
        finally:
            os.close(lock)
        _event("complete", plan_id=current["plan_id"])
        return 0
    except Cancelled as exc:
        _event("error", message=str(exc), cancelled=True)
        return 130
    except Exception as exc:
        _event("error", message=str(exc), cancelled=False)
        return 1
    finally:
        for value, handler in handlers.items():
            signal.signal(value, handler)


BOOTSTRAP = ("import sys; sys.path.insert(0,sys.argv.pop(1)); "
             "from rufus_linux.jetson_flash import _privileged_main; sys.exit(_privileged_main())")


def execute_plan(plan, confirmation, log_path, cancel=None, progress=None, on_line=None, authorization="auto"):
    """One auth request. The GUI and log writer remain unprivileged."""
    current = verify_plan(plan, confirmation)
    check_cancel(cancel)
    if authorization not in ("auto", "pkexec", "sudo"):
        raise RufusError("Invalid Jetson authorization method.")
    if os.path.lexists(log_path):
        raise RufusError("Jetson log destination already exists. Select a new log filename.")
    helper = ["/usr/bin/python3", "-I", "-c", BOOTSTRAP, str(Path(__file__).resolve().parents[1])]
    new_session = True
    if os.geteuid() != 0:
        method = authorization
        if method == "auto":
            method = "sudo" if sys.stdin.isatty() and sys.stderr.isatty() else "pkexec"
        executable = shutil.which(method, path=SYSTEM_PATH)
        if not executable:
            raise RufusError(f"Direct Jetson jobs require {method} for one authorization request.")
        helper = [executable, "--", *helper] if method == "sudo" else [executable, *helper]
        # sudo authenticates through /dev/tty. Keep the CLI's controlling
        # terminal; the root helper handles SIGINT and isolates the vendor job.
        new_session = method != "sudo"
    with tempfile.TemporaryDirectory(prefix="rufus-jetson-plan-") as directory:
        plan_file = Path(directory) / "plan.json"
        plan_file.write_text(json.dumps(current), encoding="utf-8")
        plan_file.chmod(0o600)
        command = [*helper, str(plan_file), current["plan_id"], confirmation]
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as log:
            log.write(json.dumps({"plan": current}, sort_keys=True) + "\n")
            log.flush()
            emit(progress, "Authorizing direct NVIDIA BSP job")
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=new_session)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            pending, started, complete, error, stopping = b"", False, False, None, False
            last_progress = 0.0
            def record(value):
                nonlocal started, complete, error, last_progress
                log.write(value + "\n")
                log.flush()
                try:
                    event = json.loads(value)
                except (ValueError, TypeError):
                    event = {"type": "log", "line": value}
                if not isinstance(event, dict):
                    event = {"type": "log", "line": value}
                if event.get("type") == "started":
                    if event.get("plan_id") != current["plan_id"]:
                        raise RufusError("Execution helper reported a different flash plan.")
                    started = True
                    emit(progress, "Running NVIDIA BSP recovery tools")
                elif event.get("type") == "complete":
                    if event.get("plan_id") != current["plan_id"]:
                        raise RufusError("Execution helper completed a different flash plan.")
                    complete = True
                elif event.get("type") == "error":
                    error = event
                elif event.get("type") == "log":
                    line = event.get("line", "")
                    if on_line:
                        on_line(line)
                    now = time.monotonic()
                    if now - last_progress >= 0.1:
                        emit(progress, "NVIDIA: " + line[-200:])
                        last_progress = now
            try:
                while selector.get_map() or process.poll() is None:
                    if cancel is not None and cancel.is_set() and not stopping:
                        stopping = True
                        process.stdin.close()  # EOF is a cancellation request to the root helper.
                        if not started:
                            # Dismiss an outstanding authorization process. If
                            # privilege has already changed, the helper sees EOF.
                            try:
                                process.terminate()
                            except (PermissionError, ProcessLookupError):
                                pass
                    for key, _ in selector.select(timeout=0.1):
                        data = os.read(key.fileobj.fileno(), 16384)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        pending += data
                        # A 16 KiB vendor line may expand sixfold in JSON.
                        while b"\n" in pending or len(pending) > 131072:
                            if b"\n" in pending[:131073]:
                                row, pending = pending.split(b"\n", 1)
                            else:
                                row, pending = pending[:131072], pending[131072:]
                            record(row.decode("utf-8", errors="replace"))
                if pending:
                    record(pending.decode("utf-8", errors="replace"))
                status = process.wait()
            finally:
                selector.close()
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
                # EOF instructs even a privileged helper to terminate its job.
                if process.poll() is None:
                    if not started:
                        try:
                            process.terminate()
                        except (PermissionError, ProcessLookupError):
                            pass
                    process.wait(timeout=15)
            os.fsync(log.fileno())
            if (error and error.get("cancelled")) or stopping:
                raise Cancelled(f"Jetson job cancelled; log: {log_path}. Reflash the target before use.")
            if status or error or not started or not complete:
                detail = error.get("message") if error else "Authorization failed or helper ended unexpectedly."
                raise RufusError(f"{detail} Log: {log_path}" + (" BSP execution did not start." if not started else ""))
    emit(progress, "Complete", 1, 1)
    return {"plan_id": current["plan_id"], "action": current["request"]["action"],
            "status": "NVIDIA BSP command completed", "log": str(log_path), "target": current["target"]}
