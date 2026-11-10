"""Parse conventional ISOLINUX menus and install a coherent x86 BIOS loader.

Only the installer's embedded ldlinux files execute. ISO COM32 modules and
dynamic menu programs are never reused in the generated configuration.
"""

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil

from .core import RufusError, check_cancel, run_tool
from .linux_iso import LiveEntry, boot_arguments, tree_file
from .windows import case_path

PREFIX = "/boot/rufus"
CONFIGS = ("boot/syslinux/syslinux.cfg", "syslinux/syslinux.cfg", "syslinux.cfg",
           "boot/isolinux/isolinux.cfg", "isolinux/isolinux.cfg", "isolinux.cfg")


@dataclass(frozen=True)
class Menu:
    entries: tuple
    configs: tuple
    source_version: str | None
    serial: tuple | None
    timeout: int
    omitted_labels: tuple


def version(data):
    matches = re.findall(rb"(?:SYS|ISO)LINUX (\d+\.\d+(?: [ -~]*?)?)(?:\x00|\r|\n)", data)
    # Prefer the complete banner over the short initial version string.
    return max((m.decode("ascii").strip() for m in matches), key=len, default=None)


def read_menu(root, primary, cancel=None):
    root, primary = root.resolve(), primary.resolve()
    if not primary.is_relative_to(root):
        raise RufusError("Syslinux configuration must remain inside the extracted ISO.")
    base = primary.parent.relative_to(root).as_posix()
    visited, stack, budget = [], set(), [0]

    def path_for(value):
        if not value or value.startswith("//") or any(c in value for c in ("$", "\\", ":")):
            raise RufusError(f"Unsupported Syslinux boot path: {value}")
        parts = [] if value.startswith("/") else list(PurePosixPath(base).parts)
        for part in PurePosixPath(value).parts:
            if part == "/":
                continue
            if part == "..":
                if not parts:
                    raise RufusError(f"Syslinux boot path escapes the extracted ISO: {value}")
                parts.pop()
            else:
                parts.append(part)
        relative = "/".join(parts)
        path = case_path(root, relative)
        tree_file(root, "/" + path.relative_to(root).as_posix())
        return path

    def expand(path):
        check_cancel(cancel)
        if path in stack or len(stack) >= 8 or len(visited) >= 64:
            raise RufusError("Syslinux configuration includes are recursive or too deeply nested.")
        amount = path.stat().st_size
        budget[0] += amount
        if budget[0] > 1024 * 1024:
            raise RufusError("Syslinux configuration exceeds the supported size.")
        stack.add(path)
        visited.append(path)
        help_text = False
        for line in re.sub(r"\\\r?\n", " ", path.read_text(encoding="utf-8-sig")).splitlines():
            if help_text:
                if line.strip().lower() == "endtext":
                    help_text = False
                continue
            if line.strip().lower() == "text help":
                help_text = True
                continue
            words = shlex.split(line, comments=True)
            if not words:
                continue
            if words[0].lower() == "include" or [w.lower() for w in words[:2]] == ["menu", "include"]:
                args = words[2:] if words[0].lower() == "menu" else words[1:]
                if len(args) != 1:
                    raise RufusError("Syslinux includes must name one local configuration file.")
                yield from expand(path_for(args[0]))
            else:
                yield words
        if help_text:
            raise RufusError("Syslinux help text is unterminated.")
        stack.remove(path)

    labels, current, global_append, default, serial, timeout = [], None, (), None, None, 50
    ui, menu_default = False, None
    scopes, scope_number = [], 0
    ignored = {"prompt", "display", "say", "font", "kbdmap", "console", "nohalt"}
    for words in expand(primary):
        command, args = words[0].lower(), words[1:]
        if command == "label":
            if len(args) != 1 or not re.fullmatch(r"[A-Za-z0-9_.+-]+", args[0]) or len(labels) >= 64:
                raise RufusError("Syslinux requires conventional, unique Linux labels (maximum 64).")
            if any(label["label"] == args[0] and label["scope"] == tuple(scopes) for label in labels):
                raise RufusError("Syslinux configuration contains duplicate labels.")
            current = {"label": args[0], "scope": tuple(scopes), "title": args[0], "append": global_append, "initrd": None, "kernel": None}
            labels.append(current)
        elif command in ("linux", "kernel", "com32", "comboot", "localboot", "config", "boot", "bss", "pxe", "fdimage"):
            if current is None or len(args) != 1:
                raise RufusError("Syslinux boot directives must belong to a label and name one image.")
            current["kernel"] = args[0] if command in ("linux", "kernel") and not args[0].lower().endswith((".c32", ".com", ".bin")) else None
        elif command == "append":
            value = tuple(args) if args != ["-"] else ()
            if current is None:
                global_append = value
            else:
                current["append"] = value
        elif command == "initrd":
            if current is None or len(args) != 1:
                raise RufusError("Syslinux INITRD must belong to a label and name comma-separated files.")
            current["initrd"] = args[0]
        elif command == "menu":
            if args and args[0].lower() == "begin":
                if len(args) > 2 or len(scopes) >= 8:
                    raise RufusError("Syslinux submenus are too deeply nested or malformed.")
                scope_number += 1
                scopes.append(scope_number)
                current = None
                continue
            if [a.lower() for a in args] == ["end"]:
                if not scopes:
                    raise RufusError("Syslinux MENU END has no matching MENU BEGIN.")
                scopes.pop()
                current = None
                continue
            if args and args[0].lower() in ("passwd", "master", "disable", "goto", "save"):
                raise RufusError("Syslinux menu access controls or dynamic selection require hybrid or DD mode.")
            if args and args[0].lower() == "label" and current is not None:
                current["title"] = " ".join(args[1:]).replace("^", "")
            elif [a.lower() for a in args] == ["default"]:
                if current is None:
                    raise RufusError("Syslinux submenu defaults require hybrid or DD mode.")
                if not scopes:
                    menu_default = current["label"]
        elif command == "default":
            if len(args) != 1 or scopes:
                raise RufusError("Syslinux DEFAULT must name one label or menu module.")
            default = args[0]
        elif command == "ui":
            if len(args) != 1 or PurePosixPath(args[0]).name.lower() not in ("menu.c32", "vesamenu.c32"):
                raise RufusError("Dynamic Syslinux menu programs require hybrid or DD mode.")
            ui = True
        elif command == "timeout":
            if len(args) != 1 or not args[0].isdigit() or not 0 <= int(args[0]) <= 65535:
                raise RufusError("Unsupported Syslinux timeout.")
            timeout = int(args[0])
        elif command == "serial":
            if len(args) not in (1, 2) or not args[0].isdigit() or int(args[0]) > 3 or (len(args) == 2 and
                    (not args[1].isdigit() or not 1200 <= int(args[1]) <= 115200)):
                raise RufusError("Syslinux serial configuration requires a conventional COM port and baud rate.")
            serial = (int(args[0]), int(args[1]) if len(args) == 2 else 9600)
        elif command not in ignored and not re.fullmatch(r"f(?:[1-9]|1[0-2])", command):
            raise RufusError(f"Unsupported Syslinux directive '{command}'; use hybrid or DD mode.")
    if scopes:
        raise RufusError("Syslinux MENU BEGIN has no matching MENU END.")
    entries, supported, omitted = [], [], []
    for label in labels:
        if label["kernel"] is None:
            omitted.append(label["label"])
            continue
        kernel = "/" + path_for(label["kernel"]).relative_to(root).as_posix()
        arguments = label["append"]
        kind = "debian" if "boot=live" in arguments else "casper" if "boot=casper" in arguments or kernel.startswith("/casper/") else None
        if kind is None:
            omitted.append(label["label"])
            continue
        if label["label"] in supported:
            raise RufusError("Syslinux live labels repeated across submenus are ambiguous; use hybrid or DD mode.")
        initrd_args = [arg.split("=", 1)[1] for arg in arguments if arg.startswith("initrd=")]
        initrd = label["initrd"] if label["initrd"] is not None else initrd_args[-1] if initrd_args else ""
        if not initrd:
            raise RufusError("Syslinux live entries require an explicit initrd.")
        initrds = tuple("/" + path_for(value).relative_to(root).as_posix() for value in initrd.split(","))
        arguments = tuple(arg for arg in arguments if not arg.startswith("initrd="))
        entries.append(LiveEntry(label["title"], kernel, arguments, initrds, kind))
        supported.append(label["label"])
    if not entries or len({entry.kind for entry in entries}) != 1:
        raise RufusError("Syslinux extraction requires conventional Casper or Debian live Linux entries.")
    if default and default.lower().endswith(".c32") and PurePosixPath(default).name.lower() not in ("menu.c32", "vesamenu.c32"):
        raise RufusError("Dynamic Syslinux default programs require hybrid or DD mode.")
    menu_module = bool(default and default.lower().endswith(".c32"))
    selected = None if menu_module else default
    if menu_default is not None and (ui or menu_module):
        selected = menu_default
    if selected is None and labels:
        selected = labels[0]["label"]
    if selected is not None:
        if selected not in supported:
            raise RufusError("Syslinux default is not a supported Linux live entry; use hybrid or DD mode.")
        index = supported.index(selected)
        entries.insert(0, entries.pop(index))
    source_version = None
    for path in (primary.parent / "isolinux.bin", root / "ldlinux.sys"):
        if path.is_file():
            tree_file(root, "/" + path.relative_to(root).as_posix())
            with path.open("rb") as stream:
                source_version = version(stream.read(2 * 1024 * 1024))
            if source_version:
                break
    return Menu(tuple(entries), tuple("/" + p.relative_to(root).as_posix() for p in visited),
                source_version, serial, timeout, tuple(omitted))


def boot_config(entries, persistence, serial=None, timeout=50):
    lines = ["# Generated by Rufus for Linux", "DEFAULT rufus0", "PROMPT 1", f"TIMEOUT {timeout}"]
    if serial:
        lines.append(f"SERIAL {serial[0]} {serial[1]}")
    lines.append("SAY Type a label below or wait for the default live system:")
    for index, entry in enumerate(entries):
        words = (entry.kernel, *entry.initrds, *boot_arguments(entry, persistence))
        if any(not word or any(c.isspace() or c in '\\"\'' or ord(c) < 32 for c in word) for word in words):
            raise RufusError("Syslinux extraction requires boot paths/arguments without quoting or whitespace.")
        if any(ord(c) < 32 for c in entry.title):
            raise RufusError("Unsupported control character in Syslinux menu title.")
        lines += [f"SAY   rufus{index}: {entry.title}"]
    for index, entry in enumerate(entries):
        lines += [f"LABEL rufus{index}", f"  LINUX {entry.kernel}", "  INITRD " + ",".join(entry.initrds),
                  "  APPEND " + " ".join(boot_arguments(entry, persistence)), ""]
    return "\n".join(lines)


def installer():
    executable = os.environ.get("RUFUS_SYSLINUX") or shutil.which("syslinux")
    if not executable or not Path(executable).is_file():
        raise RufusError("Syslinux BIOS extraction requires the native syslinux installer and mtools.")
    path = Path(executable).resolve()
    if path.stat().st_size > 16 * 1024 * 1024:
        raise RufusError("Unsupported Syslinux installer size.")
    data = path.read_bytes()
    found = version(data)
    if not found or not found.startswith("6."):
        raise RufusError("Syslinux BIOS extraction requires a Syslinux 6.x installer with embedded loader files.")
    return path, data, found


def install(volume, root, selected, cancel=None):
    executable, data, found = selected
    # This mtools installer operates on our NEW filesystem image, never /dev.
    run_tool([str(executable), "--install", "--directory", PREFIX, str(volume)], cancel)
    destination = case_path(root, PREFIX.lstrip("/"))
    for name in ("ldlinux.sys", "ldlinux.c32"):
        path = destination / name
        run_tool(["mcopy", "-i", str(volume), "::" + PREFIX + "/" + name, str(path)], cancel)
    core = (destination / "ldlinux.c32").read_bytes()
    if not core or core not in data or version((destination / "ldlinux.sys").read_bytes()) != found:
        raise RufusError("Installed Syslinux loader files do not match the selected installer.")
    return found
