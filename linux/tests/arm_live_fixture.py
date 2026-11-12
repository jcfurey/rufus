#!/usr/bin/python3
"""Build an ARM Casper fixture offline inside a disposable, unprivileged VM.

Supply a verified Ubuntu Base archive and an APT-resolved directory of matching
packages. No packages are installed on the host, and the guest has no network.
The fixture uses Ubuntu's actual Casper and initramfs-tools, not a replacement
live-root implementation. Inputs and generated artifacts receive SHA-256 records.
"""

import argparse
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rufus_linux import core, grub, linux_iso
from live_smoke import archive
from live_vm import LiveVM


BUILD = '''#!/bin/sh
set -eu
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export HOME=/root LC_ALL=C DEBIAN_FRONTEND=noninteractive
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
apt-get -o Dir::Etc::sourcelist=/dev/null -o Dir::Etc::sourceparts=- --no-install-recommends --assume-yes install /fixture-packages/*.deb
rm /usr/sbin/policy-rc.d
rm -rf /fixture-packages /var/cache/apt/archives/*.deb
# A fresh machine identity is generated independently in each test overlay.
: >/etc/machine-id
rm -f /var/lib/dbus/machine-id
ln -s /etc/machine-id /var/lib/dbus/machine-id
printf 'COMPRESS=gzip\nMODULES=most\n' >/etc/initramfs-tools/conf.d/rufus-fixture
depmod @RELEASE@
update-initramfs -u -k @RELEASE@
test -s /boot/initrd.img-@RELEASE@
test -x /sbin/init
dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' >/fixture-packages.tsv
rm /fixture-build.sh
'''


BOOTSTRAP = '''#!/bin/busybox sh
/bin/busybox mount -t devtmpfs devtmpfs /dev
/bin/busybox mount -t proc proc /proc
/bin/busybox mount -t sysfs sysfs /sys
exec >/dev/ttyAMA0 2>&1
export PATH=/bin
/bin/busybox --install -s /bin
fail() { echo RUFUS_ARM_FIXTURE_FAILED; sync; poweroff -f; exit 1; }
for n in $(seq 1 30); do [ -b /dev/vda ] && break; sleep 1; done
mount -t ext4 /dev/vda /root || fail
tar xzpf /root/base.tar.gz -C /root || fail
rm /root/base.tar.gz
chown 0:0 /root
chmod 755 /root
mkdir -p /root/dev /root/proc /root/sys /root/run /root/dev/pts
mount --bind /dev /root/dev || fail
mkdir -p /root/dev/pts
mount -t devpts devpts /root/dev/pts || fail
mount --bind /proc /root/proc || fail
mount --bind /sys /root/sys || fail
mount -t tmpfs tmpfs /root/run || fail
chroot /root /bin/sh /fixture-build.sh || fail
sync
umount /root/run /root/sys /root/proc /root/dev/pts /root/dev || fail
# Build after unmounting pseudo-filesystems; the output itself is automatically
# excluded. Native root ownership and special files remain intact in SquashFS.
chroot /root /usr/bin/mksquashfs / /fixture-filesystem.squashfs -noappend -comp gzip -processors 2 -mem 256M || fail
sync
umount /root || fail
echo RUFUS_ARM_FIXTURE_READY
poweroff -f
'''


def package_metadata(path):
    values = subprocess.check_output(["dpkg-deb", "--show", "--showformat=${Package}\n${Version}\n${Architecture}\n", str(path)], text=True).splitlines()
    if len(values) != 3:
        raise RuntimeError(f"Invalid package metadata: {path}")
    return {"name": values[0], "version": values[1], "architecture": values[2],
            "file": path.name, "sha256": core.checksum(path)}


def debugfs_quote(path):
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--architecture", choices=("arm64", "arm"), required=True)
    parser.add_argument("--base", type=Path, required=True, help="verified Ubuntu Base tar.gz")
    parser.add_argument("--packages", type=Path, required=True, help="matching APT --download-only archive directory")
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--busybox", type=Path, required=True, help="static guest BusyBox for fixture assembly")
    parser.add_argument("--firmware-code", type=Path)
    parser.add_argument("--firmware-vars", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True, help="NEW output directory")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if grub.kernel_architecture(args.kernel) != args.architecture:
        parser.error("Kernel architecture does not match the requested fixture.")
    packages = sorted(args.packages.resolve().glob("*.deb"))
    metadata = [package_metadata(p) for p in packages]
    architecture = "arm64" if args.architecture == "arm64" else "armhf"
    if any(p["architecture"] not in (architecture, "all") for p in metadata):
        parser.error("Package directory contains another architecture.")
    names = {p["name"] for p in metadata}
    if not {"casper", "initramfs-tools", "systemd-sysv", "squashfs-tools"} <= names:
        parser.error("Supply an APT-resolved Casper/systemd/initramfs/squashfs-tools package set.")
    releases = [p["name"].removeprefix("linux-image-") for p in metadata if p["name"].startswith("linux-image-")]
    if len(releases) != 1 or not re.fullmatch(r"[A-Za-z0-9.+-]+", releases[0]):
        parser.error("Supply exactly one matching kernel image package.")
    release = releases[0]
    if "linux-modules-" + release not in names:
        parser.error("The matching kernel modules package is missing.")
    vm = LiveVM(args.architecture, firmware_code=args.firmware_code, firmware_vars=args.firmware_vars)
    root = args.output_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    tree = root / "staging"
    tree.mkdir()
    inputs = {"architecture": args.architecture, "kernel_release": release,
              "base": str(args.base.resolve()), "base_sha256": core.checksum(args.base),
              "kernel_sha256": core.checksum(args.kernel), "busybox_sha256": core.checksum(args.busybox),
              "packages": metadata, "physical_storage_used": False, "guest_network": "none"}
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    print(f"Staging {architecture} Ubuntu Base and {len(packages)} packages", flush=True)
    # Extract the base inside the VM so numeric ownership is independent of
    # host privileges, user namespaces, and fakeroot's syscall coverage.
    shutil.copyfile(args.base, tree / "base.tar.gz")
    staging = tree / "fixture-packages"
    staging.mkdir()
    for package in packages:
        shutil.copyfile(package, staging / package.name)
    (tree / "fixture-build.sh").write_text(BUILD.replace("@RELEASE@", release))
    disk = root / "build-root.ext4"
    with disk.open("xb") as stream:
        stream.truncate(4 * 1024**3)
    core.run_tool(["mkfs.ext4", "-q", "-F", "-d", str(tree), str(disk)])
    files = [(p, b"", stat.S_IFDIR | 0o755) for p in ("bin", "dev", "proc", "sys", "root")]
    files += [("bin/busybox", args.busybox.read_bytes(), stat.S_IFREG | 0o755),
              ("init", BOOTSTRAP.encode(), stat.S_IFREG | 0o755)]
    initrd = root / "bootstrap-initrd.gz"
    initrd.write_bytes(gzip.compress(archive(files)))
    bootstrap = root / "bootstrap-tree"
    (bootstrap / "casper").mkdir(parents=True)
    (bootstrap / "boot/grub").mkdir(parents=True)
    kernel = bootstrap / "casper/vmlinuz"
    shutil.copyfile(args.kernel, kernel)
    shutil.copyfile(initrd, bootstrap / "casper/initrd")
    (bootstrap / "boot/grub/grub.cfg").write_text("set timeout=0\nmenuentry 'Build ARM live fixture' {\n"
        "linux /casper/vmlinuz boot=casper console=ttyAMA0 rdinit=/init panic=-1\ninitrd /casper/initrd\n}\n")
    boot_iso, boot_image, boot_overlay = (root / name for name in ("bootstrap.iso", "bootstrap.img", "bootstrap.qcow2"))
    core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-o", str(boot_iso), str(bootstrap)])
    linux_iso.create_extracted_image(boot_iso, boot_image, architecture=args.architecture)
    core.run_tool(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(boot_image), str(boot_overlay)])
    command = vm.command("uefi", boot_overlay, root, kernel, initrd, "casper")
    command += ["-drive", "if=none,id=root,format=raw,file=" + str(disk).replace(",", ",,"),
                "-device", "virtio-blk-device,drive=root"]
    inputs["firmware"] = vm.firmware_evidence
    (root / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    (root / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    print(f"Running native {architecture} package configuration and mkinitramfs in a network-free VM", flush=True)
    log = root / "build.log"
    with log.open("xb") as output:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                timeout=args.timeout, env={**os.environ, "TMPDIR": str(root)})
    data = log.read_bytes()
    if result.returncode or b"RUFUS_ARM_FIXTURE_READY" not in data or b"RUFUS_ARM_FIXTURE_FAILED" in data:
        raise AssertionError(f"Fixture VM failed; inspect {log}: " + data[-4000:].decode(errors="replace"))
    output_tree = root / "iso-tree"
    (output_tree / "casper").mkdir(parents=True)
    (output_tree / "boot/grub").mkdir(parents=True)
    (output_tree / ".disk").mkdir()
    shutil.copyfile(args.kernel, output_tree / "casper/vmlinuz")
    for source, output in ((f"/boot/initrd.img-{release}", output_tree / "casper/initrd"),
                           ("/fixture-filesystem.squashfs", output_tree / "casper/filesystem.squashfs"),
                           ("/fixture-packages.tsv", root / "installed-packages.tsv")):
        core.run_tool(["debugfs", "-R", f"dump {debugfs_quote(source)} {debugfs_quote(output)}", str(disk)])
        if not output.is_file() or not output.stat().st_size:
            raise RuntimeError(f"Missing guest output {source}.")
    squashfs = output_tree / "casper/filesystem.squashfs"
    (output_tree / ".disk/info").write_text(f"Rufus Ubuntu Base Casper validation fixture {architecture}\n")
    (output_tree / "boot/grub/grub.cfg").write_text("set timeout=0\nmenuentry 'ARM Casper live-root validation' {\n"
        "linux /casper/vmlinuz boot=casper console=ttyAMA0,115200 panic=-1\n"
        "initrd /casper/initrd\n}\n")
    iso = root / "fixture.iso"
    core.run_tool(["xorriso", "-as", "mkisofs", "-R", "-J", "-V", "RUFUS_ARM_LIVE", "-o", str(iso), str(output_tree)])
    result = {**inputs, "iso": str(iso), "iso_sha256": core.checksum(iso),
              "initrd_sha256": core.checksum(output_tree / "casper/initrd"),
              "squashfs_sha256": core.checksum(squashfs), "fixture_build": "passed",
              "distribution": "Ubuntu Base with Ubuntu Casper", "vendor_live_iso": False,
              "acceptance": "fixture creation only; run live_smoke.py for persistence acceptance"}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(f"PASS: fixture ready at {iso}", flush=True)


if __name__ == "__main__":
    main()
