#!/usr/bin/python3
"""Opt-in Casper/Debian persistence reboot tests in private USB VM overlays.

The source ISO and prepared image remain read-only. A test-only initramfs hook
runs after the distribution mounts the real live root, writes a marker through that root,
and checks it on a second boot. Firmware tests remaster a private image with
that hook, serial console output, and a zero-second GRUB menu timeout.
--check-stage userspace instead runs the distribution's full startup and checks
a live user's terminal/PAM session, normal shutdown, and persisted home file.
"""

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, grub, live
from live_vm import LiveVM


def archive(files):
    """Small newc archive appended to a COPY of the distribution's initramfs."""
    data = bytearray()
    for index, (path, value, mode) in enumerate([*files, ("TRAILER!!!", b"", 0)]):
        name = path.encode() + b"\0"
        fields = (index + 1, mode, 0, 0, 1, 0, len(value), 0, 0, 0, 0, len(name), 0)
        data.extend(b"070701" + "".join(f"{n:08x}" for n in fields).encode() + name)
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(value)
        data.extend(b"\0" * (-len(data) % 4))
    data.extend(b"\0" * (-len(data) % 512))
    return bytes(data)


HOOK = '''#!/bin/sh
if [ "$1" = prereqs ]; then exit 0; fi
console=/dev/@CONSOLE@
[ -e "${rootmnt}/dev/@CONSOLE@" ] && console="${rootmnt}/dev/@CONSOLE@"
exec >"$console" 2>&1
fail() {
    echo "RUFUS_PERSISTENCE_FAIL_$1"
    cat /proc/mounts
    sync
    poweroff -f
    exit 1
}
if [ '@KIND@' = casper ]; then
    store=$(awk '$2 == "/cow" && $3 == "ext4" {print $2; exit}' /proc/mounts)
else
    store=$(awk '$3 == "ext4" && $2 ~ /\\/live\\/persistence\\// {print $2; exit}' /proc/mounts)
fi
[ -n "$store" ] || fail NO_EXT4_STORE
options=$(awk -v root="$rootmnt" '$2 == root && $3 == "overlay" {options=$4} END {print options}' /proc/mounts)
[ -n "$options" ] || fail NO_ROOT_OVERLAY
upper=${options##*upperdir=}
upper=${upper%%,*}
case "$upper" in "$store"/*) ;; *) fail WRONG_UPPER_STORE ;; esac
if [ '@KIND@' = debian ]; then
    [ "$(cat "$store/persistence.conf")" = '/ union' ] || fail WRONG_UNION_CONFIG
fi
marker=root/rufus-persistence-@NONCE@
if [ -e "${rootmnt}/$marker" ]; then
    [ "$(cat "${rootmnt}/$marker")" = '@NONCE@' ] || fail WRONG_MARKER
    result=SECOND
else
    echo '@NONCE@' >"${rootmnt}/$marker" || fail WRITE
    result=FIRST
fi
sync
[ "$(cat "$upper/$marker")" = '@NONCE@' ] || fail MARKER_NOT_IN_EXT4_UPPER
echo "RUFUS_PERSISTENCE_STORE @KIND@ $store $upper"
echo "RUFUS_PERSISTENCE_${result}_BOOT_OK"
poweroff -f
'''


def iso_paths(iso):
    return shlex.split(live.tool_text(live.iso_command(iso, "-find", "/", "-type", "f", "-exec", "echo", "--")))


def select_path(paths, explicit, preferred, prefix):
    if explicit:
        if explicit not in paths:
            raise RuntimeError(f"ISO does not contain {explicit}.")
        return explicit
    for path in preferred:
        if path in paths:
            return path
    candidates = [p for p in paths if p.startswith(prefix)]
    if len(candidates) != 1:
        raise RuntimeError(f"Specify an explicit kernel/initrd path; ambiguous {prefix} files.")
    return candidates[0]


def instrument_initrd(initrd, root, kind, nonce, check_stage="root", console="ttyS0"):
    """Preserve vendor hooks; choose a root checkpoint or install a runtime check."""
    expanded = root / "initrd-tree"
    core.run_tool(["unmkinitramfs", str(initrd), str(expanded)])
    stage = "casper-bottom" if kind == "casper" and check_stage == "root" else "init-bottom"
    order_path = f"scripts/{stage}/ORDER"
    candidates = [p for p in expanded.rglob("ORDER") if p.as_posix().endswith("/" + order_path)]
    if len(candidates) != 1 or candidates[0].is_symlink():
        raise RuntimeError(f"Harness needs one initramfs-tools {stage} ORDER file.")
    order = candidates[0].read_bytes()
    hook_path = f"scripts/{stage}/rufus-persistence-test"
    extra = []
    if check_stage == "userspace":
        from live_userspace import initrd_files
        hook, extra = initrd_files(kind, nonce, console)
    else:
        hook = HOOK.replace("@KIND@", kind).replace("@NONCE@", nonce).replace("@CONSOLE@", console).encode()
    call = f'/{hook_path} "$@"\n'.encode()
    # Casper's existing checks stop before vendor user/desktop customization.
    # Debian's later checkpoint runs all original init-bottom scripts first.
    order = call + order if stage == "casper-bottom" else order.rstrip(b"\n") + b"\n" + call
    initrd.chmod(0o600)
    with initrd.open("ab") as stream:
        stream.write(gzip.compress(archive([(hook_path, hook, stat.S_IFREG | 0o755),
            (order_path, order, stat.S_IFREG | 0o644), *extra])))
    shutil.rmtree(expanded)


def instrument_config(text, kind, console="ttyS0"):
    if kind == "casper":
        # Plymouth's graphical handoff can wait indefinitely in a headless VM.
        # Keep the distribution's scripts, using its ordinary text boot path.
        text = "".join(re.sub(r'(?<!\S)(?:"(?:quiet|splash)"|quiet|splash)(?=\s|$)', "", line)
                       if re.match(r"^\s*(?:linux(?:efi|16)?|append)\s", line, re.I) else line
                       for line in text.splitlines(keepends=True))
    boot = "boot=casper" if kind == "casper" else "boot=live"
    # ip=frommedia is understood by Casper, but generic initramfs-tools later
    # treats it as a network request and waits forever in our NIC-free guest.
    flags = f"console={console},115200 panic=-1"
    # Generated GRUB entries quote every word; source GRUB/Syslinux use bare words.
    quoted = '"' + boot + '"'
    count = text.count(quoted)
    if count:
        text = text.replace(quoted, quoted + ' "' + flags.replace(" ", '" "') + '"')
    text, bare = re.subn(r"(?<!\S)" + re.escape(boot) + r"(?=\s|$)", boot + " " + flags, text)
    text = re.sub(r"(?mi)^\s*set\s+timeout=.*$", "set timeout=0", text)
    text = re.sub(r"(?mi)^\s*timeout\s+\d+.*$", "TIMEOUT 1", text)
    return text, count + bare


def disable_persistence(text, kind):
    """Change only standalone live-kernel persistence options in a test copy."""
    option = "persistent" if kind == "casper" else "persistence"
    return "".join(re.sub(r'(?<![^\s"])' + option + r'(?=["\s]|$)', "no" + option, line)
                   if re.match(r"^\s*(?:linux(?:efi|16)?|append)\s", line, re.I) else line
                   for line in text.splitlines(keepends=True))


def remap_image(source, destination, mappings, layout):
    if layout == "extract":
        shutil.copyfile(source, destination)
        for local, path in mappings:
            core.run_tool(["mcopy", "-o", "-i", f"{destination}@@{live.MIB}", str(local), "::" + path])
    else:
        mapping_args = [word for local, path in mappings for word in ("-map", str(local), path)]
        core.run_tool(live.iso_command(source, "-outdev", str(destination), *mapping_args,
                                      "-boot_image", "any", "replay", "-end"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iso", type=Path)
    parser.add_argument("--prepared", type=Path, help="existing Rufus live image, used as a read-only backing file")
    parser.add_argument("--output-dir", required=True, type=Path, help="NEW directory for image, overlay, and logs")
    parser.add_argument("--kernel-path", help="auto-detected from Casper/Debian live paths by default")
    parser.add_argument("--initrd-path", help="auto-detected; identical initrd aliases are instrumented together")
    parser.add_argument("--kind", choices=("auto", "casper", "debian"), default="auto")
    parser.add_argument("--negative-control", action="store_true", help="also prove disabled persistence cannot pass")
    parser.add_argument("--check-stage", choices=("root", "userspace"), default="root",
                        help="root-mount checkpoint, or full systemd startup and a live user's PAM session")
    parser.add_argument("--timeout", type=int, help="seconds per boot (root: 240, userspace: 600)")
    parser.add_argument("--firmware", choices=("direct", "bios", "uefi", "all"), default="direct")
    parser.add_argument("--firmware-code", type=Path, help="optional matching UEFI firmware code")
    parser.add_argument("--firmware-vars", type=Path, help="optional matching UEFI variable template")
    parser.add_argument("--layout", choices=("hybrid", "extract"), default="hybrid")
    parser.add_argument("--bootloader", choices=("auto", "grub", "syslinux"), default="auto")
    args = parser.parse_args()
    if args.timeout is None:
        args.timeout = 600 if args.check_stage == "userspace" else 240
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.layout != "extract" and args.bootloader != "auto":
        parser.error("--bootloader requires --layout extract")
    qemu_img = shutil.which("qemu-img")
    if not qemu_img:
        raise RuntimeError("Install qemu-utils for the live reboot test.")
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    args.iso = args.iso.resolve()
    source_hash = core.checksum(args.iso)
    with tempfile.TemporaryDirectory(prefix="rufus-live-detect-") as directory:
        kind = live.stage_configs(args.iso, Path(directory))[0]
    if args.kind != "auto" and args.kind != kind:
        raise RuntimeError("The ISO's live family does not match --kind.")
    paths = iso_paths(args.iso)
    prefix = "/casper/" if kind == "casper" else "/live/"
    args.kernel_path = select_path(paths, args.kernel_path, [prefix + "vmlinuz"], prefix + "vmlinuz")
    args.initrd_path = select_path(paths, args.initrd_path, [prefix + "initrd.gz", prefix + "initrd.img", prefix + "initrd"], prefix + "initrd")
    kernel, initrd = root / "vmlinuz", root / "test-initrd"
    core.run_tool(live.iso_command(args.iso, "-osirrox", "on", "-extract", args.kernel_path, str(kernel),
                                   "-extract", args.initrd_path, str(initrd)))
    vm = LiveVM(grub.kernel_architecture(kernel), firmware_code=args.firmware_code, firmware_vars=args.firmware_vars)
    modes = vm.modes(args.firmware)
    prepared = args.prepared.resolve() if args.prepared else root / "live.img"
    if not args.prepared:
        if args.layout == "extract":
            from rufus_linux.linux_iso import create_extracted_image
            create_extracted_image(args.iso, prepared, persistence_size=256 * live.MIB, bootloader=args.bootloader)
        else:
            live.create_live_image(args.iso, prepared, 256 * live.MIB)
    prepared_hash = core.checksum(prepared)
    original_initrd_hash = core.checksum(initrd)
    initrd_paths = [args.initrd_path]
    for path in paths:
        if path.startswith(prefix + "initrd") and path != args.initrd_path:
            alias = root / "initrd-alias"
            core.run_tool(live.iso_command(args.iso, "-osirrox", "on", "-extract", path, str(alias)))
            if core.checksum(alias) == original_initrd_hash:
                initrd_paths.append(path)
            alias.unlink()
    nonce = secrets.token_hex(16)
    instrument_initrd(initrd, root, kind, nonce, args.check_stage, vm.console)
    backing = prepared
    if args.firmware != "direct":
        mappings = []
        if args.layout == "extract":
            configs = ["/boot/rufus/grub.cfg"]
            # Read the generated loader rather than trusting the caller's auto setting.
            probe = subprocess.run(["mcopy", "-i", f"{prepared}@@{live.MIB}", "::/boot/rufus/syslinux.cfg", str(root / "syslinux-probe")],
                                   stdin=subprocess.DEVNULL, capture_output=True)
            if probe.returncode == 0:
                configs.append("/boot/rufus/syslinux.cfg")
        else:
            configs = [p for p in paths if p.startswith(live.BOOT_DIRS) and p.lower().endswith(".cfg")]
        count = 0
        for index, path in enumerate(configs):
            config = root / f"config-{index}.cfg"
            if args.layout == "extract":
                core.run_tool(["mcopy", "-i", f"{prepared}@@{live.MIB}", "::" + path, str(config)])
            else:
                core.run_tool(live.iso_command(prepared, "-osirrox", "on", "-extract", path, str(config)))
            text, number = instrument_config(config.read_text(), kind, vm.console)
            count += number
            if path.startswith(("/isolinux/", "/syslinux/")) or path.endswith("syslinux.cfg"):
                text = "SERIAL 0 115200\n" + text
            else:
                # ARM firmware exposes its PL011 console to GRUB; x86 uses UART0.
                serial = "" if vm.arm else "serial --unit=0 --speed=115200\nterminal_input serial\nterminal_output serial\n"
                text = serial + "set timeout=0\nset timeout_style=hidden\n" + text
            config.chmod(0o600)
            config.write_text(text)
            mappings.append((config, path))
        if not count:
            raise RuntimeError("Firmware harness found no live kernel entries.")
        mappings.extend((initrd, path) for path in initrd_paths)
        backing = root / "firmware-test.img"
        remap_image(prepared, backing, mappings, args.layout)
    backing_hash = core.checksum(backing)
    # Keep provenance even if a guest hangs or fails before result.json exists.
    inputs = {"iso": str(args.iso), "prepared": str(prepared), "backing": str(backing), "kind": kind,
              "source_sha256": source_hash, "prepared_sha256": prepared_hash, "backing_sha256": backing_hash,
              "kernel_sha256": core.checksum(kernel), "test_initrd_sha256": core.checksum(initrd),
              "layout": args.layout, "bootloader": args.bootloader, "marker_nonce": nonce,
              "initrd_paths": initrd_paths, "check_stage": args.check_stage,
              "vm_architecture": vm.architecture, "serial_console": vm.console,
              "acceleration": "tcg", "network": "none", "firmware": vm.firmware_evidence}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    userspace_results = {}
    for mode in modes:
        overlay = root / f"{mode}-persistence.qcow2"
        core.run_tool([qemu_img, "create", "-f", "qcow2", "-F", "raw", "-b", str(backing), str(overlay)])
        command = vm.command(mode, overlay, root, kernel, initrd, kind)
        (root / f"{mode}-command.json").write_text(json.dumps(command, indent=2) + "\n")
        (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
        marker_prefix = "RUFUS_USERSPACE" if args.check_stage == "userspace" else "RUFUS_PERSISTENCE"
        for number, label in ((1, "FIRST"), (2, "SECOND")):
            expected = f"{marker_prefix}_{label}_BOOT_OK".encode()
            print(f"Booting real {kind} live root via USB/{mode}, pass {number}", flush=True)
            log = root / f"{mode}-boot-{number}.log"
            with log.open("xb") as stream:
                try:
                    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                            timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
                except subprocess.TimeoutExpired as exc:
                    raise AssertionError(f"{kind} {mode} boot {number} timed out; inspect {log}") from exc
            data = log.read_bytes()
            if result.returncode or expected not in data or b"RUFUS_PERSISTENCE_FAIL" in data or b"RUFUS_USERSPACE_FAIL" in data:
                raise AssertionError(f"{kind} {mode} boot {number} failed; inspect {log}: " +
                                     data[-3000:].decode(errors="replace"))
            if args.check_stage == "userspace":
                from live_userspace import verify_userspace
                userspace_results[f"{mode}-{number}"] = verify_userspace(data, overlay, root, nonce, qemu_img)
                (root / "userspace-boots.json").write_text(json.dumps(userspace_results, indent=2) + "\n")
            print(f"PASS: {mode}: {expected.decode()}", flush=True)
    if args.negative_control:
        control_backing = backing
        control_mode = "uefi" if vm.arm else "direct"
        if vm.arm:
            negative_mappings = []
            for index, (local, path) in enumerate(mappings):
                if path not in configs:
                    continue
                config = root / f"disabled-{index}.cfg"
                config.write_text(disable_persistence(local.read_text(), kind))
                negative_mappings.append((config, path))
            control_backing = root / "without-persistence.img"
            remap_image(backing, control_backing, negative_mappings, args.layout)
        control_hash = core.checksum(control_backing)
        overlay = root / "without-persistence.qcow2"
        core.run_tool([qemu_img, "create", "-f", "qcow2", "-F", "raw", "-b", str(control_backing), str(overlay)])
        log = root / "without-persistence.log"
        command = vm.command(control_mode, overlay, root, kernel, initrd, kind, persistent=False,
                             nvram_name="without-persistence-vars.fd")
        inputs["negative_control_backing"] = str(control_backing)
        inputs["negative_control_backing_sha256"] = control_hash
        (root / "without-persistence-command.json").write_text(json.dumps(command, indent=2) + "\n")
        (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
        with log.open("xb") as stream:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
        data = log.read_bytes()
        if result.returncode or b"RUFUS_PERSISTENCE_FAIL_NO_EXT4_STORE" not in data or b"_BOOT_OK" in data:
            raise AssertionError(f"Disabled-persistence control did not fail as expected; inspect {log}")
        if core.checksum(control_backing) != control_hash:
            raise AssertionError("The disabled-persistence backing image changed during the test.")
        print("PASS: disabled persistence cannot satisfy the live-root check", flush=True)
    if core.checksum(args.iso) != source_hash or core.checksum(prepared) != prepared_hash or core.checksum(backing) != backing_hash:
        raise AssertionError("A read-only source/prepared/backing image changed during the test.")
    result = {**inputs,
              "check_stage": "userspace" if args.check_stage == "userspace" else
                  ("before-casper-bottom" if kind == "casper" else "after-init-bottom"),
              "userspace": userspace_results, "boots": 2 * len(modes) + int(args.negative_control),
              "negative_control": "passed" if args.negative_control else "not run",
              "negative_control_boot_method": control_mode if args.negative_control else None,
              "persistence": "passed", "boot_method": modes,
              "physical_storage_used": False, "source_images_modified": False}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
