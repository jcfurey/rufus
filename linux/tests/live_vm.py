"""Architecture-specific VM setup for the distribution persistence harness."""

from pathlib import Path
import shutil

from rufus_linux import core
from boot_smoke import firmware_paths
from uefi_smoke import ovmf_paths


class LiveVM:
    def __init__(self, architecture, *, firmware_code=None, firmware_vars=None):
        targets = {"x86_64": ("qemu-system-x86_64", "max", "ttyS0"),
                   "arm64": ("qemu-system-aarch64", "cortex-a57", "ttyAMA0"),
                   "arm": ("qemu-system-arm", "cortex-a15", "ttyAMA0")}
        if architecture not in targets:
            raise RuntimeError(f"Live reboot harness does not support {architecture} guests.")
        self.architecture = architecture
        program, self.cpu, self.console = targets[architecture]
        self.qemu = shutil.which(program)
        if not self.qemu:
            raise RuntimeError(f"Install {program} for this live reboot test.")
        if bool(firmware_code) != bool(firmware_vars):
            raise RuntimeError("Supply both --firmware-code and --firmware-vars.")
        self.code, self.variables = firmware_code, firmware_vars
        self.firmware_evidence = {}

    @property
    def arm(self):
        return self.architecture in ("arm", "arm64")

    def modes(self, requested):
        if self.arm and requested in ("bios", "direct"):
            raise RuntimeError("ARM live tests require --firmware uefi (or all), including EFI zboot kernels.")
        if requested == "all":
            return ("uefi",) if self.arm else ("bios", "uefi")
        return (requested,)

    def uefi_paths(self):
        if self.code:
            return self.code.resolve(), self.variables.resolve()
        if not self.arm:
            return ovmf_paths(self.qemu)
        name = "AAVMF" if self.architecture == "arm64" else "AAVMF32"
        for share in (Path(self.qemu).resolve().parents[1] / "share", Path("/usr/share")):
            for directory in (share / name, share / "AAVMF"):
                code, variables = directory / (name + "_CODE.fd"), directory / (name + "_VARS.fd")
                if code.is_file() and variables.is_file():
                    return code, variables
        raise RuntimeError(f"Install {name} firmware or provide --firmware-code and --firmware-vars.")

    def command(self, mode, overlay, root, kernel, initrd, kind, *, persistent=True, nvram_name="uefi-vars.fd"):
        """Create a fresh NVRAM copy once; reuse this command for both boots."""
        self.modes(mode)
        share = Path(self.qemu).resolve().parents[1] / "share/qemu"
        machine = "virt,accel=tcg" if self.arm else ("q35,accel=tcg" if mode == "uefi" else "pc,accel=tcg")
        boot_args, devices = [], []
        if not self.arm:
            bios, share = firmware_paths(self.qemu)
            devices = ["-device", "VGA,romfile=" + str(bios.parent / "vgabios-stdvga.bin")]
            if mode != "uefi":
                boot_args = ["-bios", str(bios)]
                self.firmware_evidence[mode] = {"bios": str(bios), "sha256": core.checksum(bios)}
        if mode == "uefi":
            code, variables = self.uefi_paths()
            nvram = root / nvram_name
            if nvram.exists():
                raise RuntimeError(f"Refusing to replace existing test NVRAM: {nvram}")
            shutil.copyfile(variables, nvram)
            self.firmware_evidence[mode] = {"code": str(code), "code_sha256": core.checksum(code),
                                            "vars_template": str(variables), "vars_sha256": core.checksum(variables)}
            boot_args = ["-drive", "if=pflash,format=raw,readonly=on,file=" + str(code).replace(",", ",,"),
                         "-drive", "if=pflash,format=raw,file=" + str(nvram).replace(",", ",,")]
        elif mode == "direct":
            boot, option = ("casper", "persistent") if kind == "casper" else ("live", "persistence")
            flags = f"boot={boot} {'' if persistent else 'no'}{option} console={self.console},115200 fsck.mode=skip panic=-1"
            boot_args += ["-kernel", str(kernel), "-initrd", str(initrd), "-append", flags]
        return [self.qemu, "-machine", machine, "-cpu", self.cpu, "-m", "2048", "-smp", "2",
                "-nodefaults", "-nic", "none", "-display", "none", "-serial", "stdio", "-monitor", "none",
                "-no-reboot", "-L", str(share), *devices, *boot_args, "-device", "qemu-xhci",
                "-drive", "if=none,id=stick,format=qcow2,file=" + str(overlay).replace(",", ",,"),
                "-device", "usb-storage,drive=stick,bootindex=1"]
