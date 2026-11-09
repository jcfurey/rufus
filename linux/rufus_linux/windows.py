"""Windows answer-file support, ported from src/wue.c's setup workflow."""

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import struct
import tempfile
import xml.etree.ElementTree as ET

from .core import (RufusError, check_cancel, checksum, emit, expected_sha256,
                   run_tool, stage_windows_iso, validate_windows_tree)

UNATTEND = "urn:schemas-microsoft-com:unattend"
WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"
ET.register_namespace("", UNATTEND)
ET.register_namespace("wcm", WCM)
ARCH_BOOT = {"amd64": "bootx64.efi", "x86": "bootia32.efi",
             "arm64": "bootaa64.efi", "arm": "bootarm.efi"}


@dataclass(frozen=True)
class WindowsOptions:
    bypass_hardware: bool = False
    offline_account: bool = False
    local_account: str = ""
    privacy: bool = False
    disable_encryption: bool = False
    locale: str = ""
    timezone: str = ""
    architecture: str = ""
    boot_index: int = 0

    @property
    def enabled(self):
        return any((self.bypass_hardware, self.offline_account, self.local_account,
                    self.privacy, self.disable_encryption, self.locale, self.timezone))

    def validate(self):
        if self.architecture and self.architecture not in ARCH_BOOT:
            raise RufusError("Windows architecture must be amd64, x86, arm64, or arm.")
        if self.boot_index < 0:
            raise RufusError("WIM boot image index must be positive.")
        if self.local_account:
            name = self.local_account
            forbidden = {"administrator", "guest", "defaultaccount", "defaultuser0",
                         "wdagutilityaccount", "system", "local service", "network service",
                         "järjestelmänvalvoja", "administrateur", "rendszergazda", "administrador",
                         "администратор", "administratör", "helpassistant", "krbtgt", "local", "none"}
            if (not re.fullmatch(r"[\w .-]{1,20}", name) or name != name.strip() or
                    name.endswith(".") or not any(c.isalnum() for c in name) or
                    name.casefold() in forbidden or len(name.encode("utf-16-le")) > 40):
                raise RufusError("Local account must be a non-reserved Windows username of 1–20 "
                                 "characters, using letters, numbers, spaces, periods, underscores or hyphens.")
        if self.locale and not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})+", self.locale):
            raise RufusError("Use a Windows language/region name such as en-US for locale.")
        if self.timezone and (len(self.timezone) > 128 or any(
                ord(c) < 32 or ord(c) > 126 for c in self.timezone)):
            raise RufusError("Use a Windows time zone ID such as Central Standard Time.")


def element(parent, tag, text=None, **attributes):
    child = ET.SubElement(parent, f"{{{UNATTEND}}}{tag}", attributes)
    if text is not None:
        child.text = str(text)
    return child


def answer_file(options, architecture="amd64"):
    options.validate()
    architecture = options.architecture or architecture
    if architecture not in ARCH_BOOT:
        raise RufusError("Cannot determine the Windows installer architecture.")
    if not options.enabled:
        raise RufusError("Select at least one Windows setup option.")
    root = ET.Element(f"{{{UNATTEND}}}unattend")
    passes = {}

    def component(config_pass, name):
        if config_pass not in passes:
            passes[config_pass] = element(root, "settings", **{"pass": config_pass})
        return element(passes[config_pass], "component", name=name,
                       processorArchitecture=architecture, publicKeyToken="31bf3856ad364e35",
                       language="neutral", versionScope="nonSxS")

    if options.bypass_hardware:
        setup = component("windowsPE", "Microsoft-Windows-Setup")
        user = element(setup, "UserData")
        element(user, "AcceptEula", "true")
        element(element(user, "ProductKey"), "Key", "")
        commands = element(setup, "RunSynchronous")
        for order, key in enumerate(("BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck"), 1):
            command = element(commands, "RunSynchronousCommand", **{f"{{{WCM}}}action": "add"})
            element(command, "Order", order)
            element(command, "Path", f"reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {key} /t REG_DWORD /d 1 /f")
    if options.offline_account:
        deployment = component("specialize", "Microsoft-Windows-Deployment")
        command = element(element(deployment, "RunSynchronous"), "RunSynchronousCommand",
                          **{f"{{{WCM}}}action": "add"})
        element(command, "Order", 1)
        element(command, "Path", 'reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\OOBE" '
                '/v BypassNRO /t REG_DWORD /d 1 /f')
    if options.privacy or options.local_account or options.offline_account or options.timezone:
        shell = component("oobeSystem", "Microsoft-Windows-Shell-Setup")
        if options.privacy or options.offline_account:
            oobe = element(shell, "OOBE")
            if options.privacy:
                element(oobe, "ProtectYourPC", 3)
            if options.offline_account:
                element(oobe, "HideOnlineAccountScreens", "true")
                element(oobe, "HideWirelessSetupInOOBE", "true")
        if options.local_account:
            accounts = element(element(shell, "UserAccounts"), "LocalAccounts")
            account = element(accounts, "LocalAccount", **{f"{{{WCM}}}action": "add"})
            element(account, "Name", options.local_account)
            element(account, "DisplayName", options.local_account)
            element(account, "Group", "Administrators;Power Users")
            password = element(account, "Password")
            # Same blank setup password and first-logon password change as Rufus.
            element(password, "Value", "UABhAHMAcwB3AG8AcgBkAA==")
            element(password, "PlainText", "false")
            command = element(element(shell, "FirstLogonCommands"), "SynchronousCommand",
                              **{f"{{{WCM}}}action": "add"})
            element(command, "Order", 1)
            element(command, "CommandLine", f'net user "{options.local_account}" /logonpasswordchg:yes')
        if options.timezone:
            element(shell, "TimeZone", options.timezone)
    if options.locale:
        international = component("oobeSystem", "Microsoft-Windows-International-Core")
        for tag in ("InputLocale", "SystemLocale", "UserLocale", "UILanguage"):
            element(international, tag, options.locale)
    if options.disable_encryption:
        element(component("oobeSystem", "Microsoft-Windows-SecureStartup-FilterDriver"),
                "PreventDeviceEncryption", "true")
        element(component("oobeSystem", "Microsoft-Windows-EnhancedStorage-Adm"),
                "TCGSecurityActivationDisabled", 1)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def case_path(root, relative, create=False):
    path = Path(root)
    for part in relative.split("/"):
        matches = [p for p in path.iterdir() if p.name.casefold() == part.casefold()] if path.is_dir() else []
        if len(matches) > 1:
            raise RufusError(f"Conflicting Windows media paths: {relative}")
        if matches:
            path = matches[0]
        else:
            path /= part
            if create:
                path.mkdir()
    return path


def installer_architecture(root, selected=""):
    found = [arch for arch, filename in ARCH_BOOT.items()
             if case_path(root, f"efi/boot/{filename}").is_file()]
    if selected:
        if selected not in found:
            raise RufusError(f"This ISO does not contain a {selected} UEFI bootloader.")
        return selected
    if len(found) != 1:
        raise RufusError("Select the Windows architecture explicitly for a multi-architecture ISO.")
    return found[0]


def wim_boot_index(path, selected=0):
    with path.open("rb") as stream:
        header = stream.read(208)
    if len(header) != 208 or header[:8] != b"MSWIM\0\0\0":
        raise RufusError("Windows setup customization requires a valid sources/boot.wim.")
    count = struct.unpack_from("<I", header, 44)[0]
    boot = selected or struct.unpack_from("<I", header, 120)[0] or (1 if count == 1 else 0)
    if not 1 <= boot <= count:
        raise RufusError("Cannot identify the Setup image in boot.wim; specify --boot-index explicitly.")
    return boot


def quote_wim_path(path):
    path = str(path)
    if any(ord(c) < 32 for c in path):
        raise RufusError("Temporary path contains control characters; select another TMPDIR.")
    for quote in ('"', "'"):
        if quote not in path:
            return quote + path + quote
    raise RufusError("Temporary path contains both quote styles; select another TMPDIR.")


def apply_windows_options(root, options, cancel=None, progress=None, *, require_uefi=True):
    options.validate()
    if not options.enabled:
        return validate_windows_tree(root, require_uefi)
    files = validate_windows_tree(root, require_uefi)
    reserved = {"autounattend.xml", "unattend.xml", "sources/$oem$/$$/panther/unattend.xml"}
    if any(relative.casefold() in reserved for _, relative, _ in files):
        raise RufusError("ISO already contains a Windows answer file. Refusing to overwrite its setup configuration.")
    architecture = (options.architecture if not require_uefi and options.architecture else
                    installer_architecture(root, options.architecture))
    data = answer_file(options, architecture)
    check_cancel(cancel)
    emit(progress, "Applying Windows setup options")
    if options.bypass_hardware:
        if not shutil.which("wimlib-imagex"):
            raise RufusError("Hardware-check customization requires wimtools (wimlib-imagex).")
        boot = case_path(root, "sources/boot.wim")
        index = wim_boot_index(boot, options.boot_index)
        with tempfile.TemporaryDirectory(prefix="rufus-answer-") as directory:
            answer = Path(directory) / "Autounattend.xml"
            answer.write_bytes(data)
            # --no-replace also protects an answer file already embedded in WIM.
            command = f"add --no-replace {quote_wim_path(answer)} /Autounattend.xml"
            run_tool(["wimlib-imagex", "update", str(boot), str(index), "--check", "--command=" + command], cancel)
            run_tool(["wimlib-imagex", "verify", str(boot)], cancel)
    else:
        destination = case_path(root, "sources/$OEM$/$$/Panther", create=True) / "unattend.xml"
        with destination.open("xb") as output:
            output.write(data)
    return validate_windows_tree(root, require_uefi)


def prepare_windows(source, output, options=None, expected="", cancel=None, progress=None):
    """Prepare a NEW installer directory without opening or formatting any drive."""
    options = options or WindowsOptions()
    options.validate()
    expected = expected_sha256(expected)
    before = os.stat(source)
    actual = checksum(source, cancel, progress)
    if expected and actual != expected:
        raise RufusError("Windows ISO SHA-256 does not match the expected checksum.")
    output = Path(output)
    output.mkdir(mode=0o700)
    try:
        files = stage_windows_iso(source, output, cancel, progress)
        if options.enabled:
            files = apply_windows_options(output, options, cancel, progress)
        if any(size > 2**32 - 1 for _, _, size in files):
            raise RufusError("Customized installer exceeds FAT32's file size limit.")
        after = os.stat(source)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise RufusError("Windows ISO changed during extraction.")
        check_cancel(cancel)
    except BaseException:
        shutil.rmtree(output)
        raise
    emit(progress, "Complete", 1, 1)
    return {"sha256": actual, "files": len(files), "bytes": sum(size for _, _, size in files)}
