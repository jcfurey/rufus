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
from rufus_linux import core, live
from boot_smoke import firmware_paths
from uefi_smoke import ovmf_paths


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
console=/dev/ttyS0
[ -e "${rootmnt}/dev/ttyS0" ] && console="${rootmnt}/dev/ttyS0"
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


def instrument_initrd(initrd, root, kind, nonce, check_stage="root"):
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
        hook, extra = initrd_files(kind, nonce)
    else:
        hook = HOOK.replace("@KIND@", kind).replace("@NONCE@", nonce).encode()
    call = f'/{hook_path} "$@"\n'.encode()
    # Casper's existing checks stop before vendor user/desktop customization.
    # Debian's later checkpoint runs all original init-bottom scripts first.
    order = call + order if stage == "casper-bottom" else order.rstrip(b"\n") + b"\n" + call
    initrd.chmod(0o600)
    with initrd.open("ab") as stream:
        stream.write(gzip.compress(archive([(hook_path, hook, stat.S_IFREG | 0o755),
            (order_path, order, stat.S_IFREG | 0o644), *extra])))
    shutil.rmtree(expanded)


def instrument_config(text, kind):
    if kind == "casper":
        # Plymouth's graphical handoff can wait indefinitely in a headless VM.
        # Keep the distribution's scripts, using its ordinary text boot path.
        text = "".join(re.sub(r'(?<!\S)(?:"(?:quiet|splash)"|quiet|splash)(?=\s|$)', "", line)
                       if re.match(r"^\s*(?:linux(?:efi|16)?|append)\s", line, re.I) else line
                       for line in text.splitlines(keepends=True))
    boot = "boot=casper" if kind == "casper" else "boot=live"
    # ip=frommedia is understood by Casper, but generic initramfs-tools later
    # treats it as a network request and waits forever in our NIC-free guest.
    flags = "console=ttyS0,115200 panic=-1"
    # Generated GRUB entries quote every word; source GRUB/Syslinux use bare words.
    quoted = '"' + boot + '"'
    count = text.count(quoted)
    if count:
        text = text.replace(quoted, quoted + ' "' + flags.replace(" ", '" "') + '"')
    text, bare = re.subn(r"(?<!\S)" + re.escape(boot) + r"(?=\s|$)", boot + " " + flags, text)
    text = re.sub(r"(?mi)^\s*set\s+timeout=.*$", "set timeout=0", text)
    text = re.sub(r"(?mi)^\s*timeout\s+\d+.*$", "TIMEOUT 1", text)
    return text, count + bare


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
    parser.add_argument("--layout", choices=("hybrid", "extract"), default="hybrid")
    parser.add_argument("--bootloader", choices=("auto", "grub", "syslinux"), default="auto")
    args = parser.parse_args()
    if args.timeout is None:
        args.timeout = 600 if args.check_stage == "userspace" else 240
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.layout != "extract" and args.bootloader != "auto":
        parser.error("--bootloader requires --layout extract")
    qemu, qemu_img = shutil.which("qemu-system-x86_64"), shutil.which("qemu-img")
    if not qemu or not qemu_img:
        raise RuntimeError("Install qemu-system-x86 and qemu-utils for the live reboot test.")
    bios, firmware = firmware_paths(qemu)
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
    prepared = args.prepared.resolve() if args.prepared else root / "live.img"
    if not args.prepared:
        if args.layout == "extract":
            from rufus_linux.linux_iso import create_extracted_image
            create_extracted_image(args.iso, prepared, persistence_size=256 * live.MIB, bootloader=args.bootloader)
        else:
            live.create_live_image(args.iso, prepared, 256 * live.MIB)
    prepared_hash = core.checksum(prepared)
    kernel, initrd = root / "vmlinuz", root / "test-initrd"
    core.run_tool(live.iso_command(args.iso.resolve(), "-osirrox", "on", "-extract", args.kernel_path, str(kernel),
                                   "-extract", args.initrd_path, str(initrd)))
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
    instrument_initrd(initrd, root, kind, nonce, args.check_stage)
    modes = ("bios", "uefi") if args.firmware == "all" else (args.firmware,)
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
            text, number = instrument_config(config.read_text(), kind)
            count += number
            if path.startswith(("/isolinux/", "/syslinux/")) or path.endswith("syslinux.cfg"):
                text = "SERIAL 0 115200\n" + text
            else:
                text = ("serial --unit=0 --speed=115200\nterminal_input serial\nterminal_output serial\n"
                        "set timeout=0\nset timeout_style=hidden\n" + text)
            config.chmod(0o600)
            config.write_text(text)
            mappings.append((config, path))
        if not count:
            raise RuntimeError("Firmware harness found no live kernel entries.")
        mappings.extend((initrd, path) for path in initrd_paths)
        backing = root / "firmware-test.img"
        if args.layout == "extract":
            shutil.copyfile(prepared, backing)
            for local, path in mappings:
                core.run_tool(["mcopy", "-o", "-i", f"{backing}@@{live.MIB}", str(local), "::" + path])
        else:
            mapping_args = [word for local, path in mappings for word in ("-map", str(local), path)]
            core.run_tool(live.iso_command(prepared, "-outdev", str(backing), *mapping_args,
                                          "-boot_image", "any", "replay", "-end"))
    backing_hash = core.checksum(backing)
    # Keep provenance even if a guest hangs or fails before result.json exists.
    inputs = {"iso": str(args.iso), "prepared": str(prepared), "backing": str(backing), "kind": kind,
              "source_sha256": source_hash, "prepared_sha256": prepared_hash, "backing_sha256": backing_hash,
              "kernel_sha256": core.checksum(kernel), "test_initrd_sha256": core.checksum(initrd),
              "layout": args.layout, "bootloader": args.bootloader, "marker_nonce": nonce,
              "initrd_paths": initrd_paths, "check_stage": args.check_stage,
              "vm_architecture": "x86_64", "acceleration": "tcg", "network": "none"}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    userspace_results = {}
    for mode in modes:
        overlay = root / f"{mode}-persistence.qcow2"
        core.run_tool([qemu_img, "create", "-f", "qcow2", "-F", "raw", "-b", str(backing), str(overlay)])
        if mode == "uefi":
            code, variables = ovmf_paths(qemu)
            nvram = root / "uefi-vars.fd"
            shutil.copyfile(variables, nvram)
            machine = "q35,accel=tcg"
            boot_args = ["-drive", "if=pflash,format=raw,readonly=on,file=" + str(code).replace(",", ",,"),
                         "-drive", "if=pflash,format=raw,file=" + str(nvram).replace(",", ",,")]
        else:
            machine, boot_args = "pc,accel=tcg", ["-bios", str(bios)]
            if mode == "direct":
                boot = "boot=casper persistent" if kind == "casper" else "boot=live persistence"
                boot_args += ["-kernel", str(kernel), "-initrd", str(initrd), "-append",
                              boot + " console=ttyS0,115200 fsck.mode=skip panic=-1"]
        marker_prefix = "RUFUS_USERSPACE" if args.check_stage == "userspace" else "RUFUS_PERSISTENCE"
        for number, label in ((1, "FIRST"), (2, "SECOND")):
            expected = f"{marker_prefix}_{label}_BOOT_OK".encode()
            print(f"Booting real {kind} live root via USB/{mode}, pass {number}", flush=True)
            log = root / f"{mode}-boot-{number}.log"
            command = [qemu, "-machine", machine, "-cpu", "max", "-m", "2048", "-smp", "2", "-nodefaults", "-nic", "none",
                       "-display", "none", "-serial", "stdio", "-monitor", "none", "-no-reboot",
                       "-L", str(firmware), "-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin"),
                       *boot_args, "-device", "qemu-xhci", "-drive", "if=none,id=stick,format=qcow2,file=" + str(overlay).replace(",", ",,"),
                       "-device", "usb-storage,drive=stick,bootindex=1"]
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
        overlay = root / "without-persistence.qcow2"
        core.run_tool([qemu_img, "create", "-f", "qcow2", "-F", "raw", "-b", str(backing), str(overlay)])
        boot = "boot=casper nopersistent" if kind == "casper" else "boot=live nopersistence"
        log = root / "without-persistence.log"
        command = [qemu, "-machine", "pc,accel=tcg", "-cpu", "max", "-m", "2048", "-smp", "2",
                   "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio", "-monitor", "none",
                   "-no-reboot", "-L", str(firmware), "-bios", str(bios), "-kernel", str(kernel), "-initrd", str(initrd),
                   "-append", boot + " console=ttyS0,115200 panic=-1",
                   "-device", "qemu-xhci", "-drive", "if=none,id=stick,format=qcow2,file=" + str(overlay).replace(",", ",,"),
                   "-device", "usb-storage,drive=stick"]
        with log.open("xb") as stream:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
        data = log.read_bytes()
        if result.returncode or b"RUFUS_PERSISTENCE_FAIL_NO_EXT4_STORE" not in data or b"_BOOT_OK" in data:
            raise AssertionError(f"Disabled-persistence control did not fail as expected; inspect {log}")
        print("PASS: disabled persistence cannot satisfy the live-root check", flush=True)
    if core.checksum(args.iso) != source_hash or core.checksum(prepared) != prepared_hash or core.checksum(backing) != backing_hash:
        raise AssertionError("A read-only source/prepared/backing image changed during the test.")
    result = {**inputs,
              "check_stage": "userspace" if args.check_stage == "userspace" else
                  ("before-casper-bottom" if kind == "casper" else "after-init-bottom"),
              "userspace": userspace_results, "boots": 2 * len(modes) + int(args.negative_control),
              "negative_control": "passed" if args.negative_control else "not run",
              "persistence": "passed", "boot_method": modes,
              "physical_storage_used": False, "source_images_modified": False}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
