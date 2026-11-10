# Portable Windows / Windows To Go

The Windows desktop mode includes **Windows To Go**, and the CLI provides
`windows-images` and `windows-to-go`. This deploys an installed Windows edition
to NTFS and prepares boot files; it is separate from creating installation media.

Linux applies the selected WIM/ESD image directly to a temporary NTFS file with
native wimlib, preserving Windows metadata. A private, unprivileged QEMU VM
boots a customized copy of the ISO's WinPE and runs Microsoft BCDBoot, BCDEdit,
and registry tools. The VM sees only the new disk image and firmware. It has
no network, physical disks, USB passthrough, or host filesystem shares. The
original ISO is never changed. No Windows program runs natively on Linux.

BCDBoot creates BCD stores and copies matching boot components from the deployed
Windows image. The guest sets internal disks offline (SAN policy 4), marks the
installation portable, disables hibernation/fast startup, and disables boot
recovery. Both job markers must match before modifying the deployed image. The
temporary WinPE is removed after successful completion; a failed or cancelled
job cannot enter the physical USB writer.

## Requirements and scope

- A complete, uncompressed Windows installation ISO containing `boot.wim` and
  one `install.wim` or `install.esd`. Split SWM sets and standalone install WIMs
  are not currently accepted.
- Native Linux `wimtools`/libwim, `ntfs-3g`, `dosfstools`, and `mtools`. No
  privileged mounts or loop devices are used. ISO extraction requires `xorriso`
  or `7zip`; UDF Windows ISOs require `7zip`.
- QEMU for the target CPU and matching firmware: `qemu-system-x86`, `seabios`,
  and `ovmf` for x86-64; `qemu-system-arm` and `qemu-efi-aarch64` for ARM64.
- At least 32 GiB destination capacity, with the selected edition's expanded
  size plus 8 GiB available in the Windows partition. Modern Windows 11 images
  may need more; CLI output files default to 64 GiB.
- Ample temporary disk space for ISO extraction, the NTFS volume, and assembled
  image, plus space for the final output. Sparse files reduce allocation but
  do not eliminate this requirement. Set `TMPDIR` to a roomy Linux filesystem;
  a small RAM-backed `/tmp` is unsuitable for large Windows images.
- The private VM uses software CPU emulation and 4 GiB guest RAM for 64-bit
  Windows or 2 GiB for 32-bit Windows. Preparing 64-bit Windows requires a
  64-bit Linux host. Boot configuration may take several minutes.

| Windows target | Output layout | Preparation/validation scope |
| --- | --- | --- |
| x86-64 | UEFI/GPT, BIOS/MBR, or BIOS+UEFI/MBR | Native WIM application and x86 WinPE boot configuration |
| 32-bit x86 | BIOS/MBR or BIOS+UEFI/MBR | Architecture checked; genuine x86 Windows boot validation pending |
| ARM64 | UEFI/GPT | Native WIM application, ARM64 WinPE/QEMU and EFI checks implemented; genuine ARM64 Windows validation pending |
| ARM32/Windows RT | Unsupported | No generic Windows RT deployment/boot path |

Target architecture comes from WIM metadata and is checked against WinPE and
the ISO's EFI loader. It is independent of the host CPU. ARM64 is never routed
through an x86 Windows binary or BIOS. Separate ARM32/ARM64 Linux media workflows
remain available.

Microsoft removed official Windows To Go in Windows 10 version 2004. This is
a Rufus-style portable deployment workflow, including experimental use with
newer Windows, not Microsoft certification for every edition/device. Use Windows
media and licensing appropriate to your use. Older Windows may require
fixed-media USB devices. Physical USB, controllers/firmware, activation, and full
OOBE remain acceptance work. Secure Boot revocation checks and selection of 2023
bootloaders are separate parity items.

## Desktop workflow

Choose **Windows installer** in Boot selection, then **Windows To Go**. Select
the ISO, click **Inspect editions**, and choose its edition explicitly. Select
UEFI, BIOS, or both, then confirm the removable drive to erase. Windows To Go
uses an NTFS Windows partition and a FAT32 system partition. GPT output also
includes a Microsoft reserved partition.
MBR output uses an active FAT32 system partition (type `0x0C`), verified with
the native WinPE finalizer and real Windows BIOS/UEFI boot tests. UEFI/GPT
output uses an EFI System Partition.

Local account, privacy, offline-account, language/time zone, and encryption
choices can be staged under `Windows\Panther\unattend.xml`. Existing answer
files are protected. The installer hardware-check bypass is hidden in this mode.
Inspection and deployment run in background workers. Cancellation applies to
NTFS application, VM finalization, and the common verified USB writer.
CPU/firmware mismatches and insufficient capacity for the selected edition
disable Start with an explanation before preparation begins.

Physical writing begins after preparation/finalization succeed and the drive
identity is rechecked. New diagnostic logs are saved under the user's state
directory (`rufus-linux/windows-to-go-*.log`).

## CLI examples

Inspect before choosing an edition; indices vary between downloads:

```sh
./rufus-linux windows-images /path/to/windows.iso

# NEW image file; no administrator access or physical storage operations.
TMPDIR=/path/to/large-temp ./rufus-linux windows-to-go /path/to/windows.iso \
    --index 6 --firmware both --size-gib 64 \
    --output /path/to/new-portable-windows.img \
    --offline-account --privacy --disable-device-encryption
```

The index is illustrative; use the value from your ISO's inspection result.
`--architecture` asserts an expected CPU and rejects mismatches. `--boot-index`
overrides the WinPE boot index when needed. `--sha256` checks the downloaded ISO,
and `--vm-timeout` accepts 30–7200 seconds (default 900).

Image output uses a new adjacent `.log` by default; `--log` selects another new
filename. Existing images/logs are protected. Logs contain QEMU errors and guest
boot/policy output. The log is created when the VM starts and retained after
VM failure. Earlier preparation failures may have no log. Temporary disks are
discarded on failure, and existing log files are never overwritten.

```sh
# DESTRUCTIVE: prepares files, then erases this exact removable drive.
TMPDIR=/path/to/large-temp ./rufus-linux windows-to-go /path/to/windows.iso \
    --index 6 --firmware uefi --device /dev/sdX --confirm /dev/sdX \
    --log /path/to/new-portable-windows.log
```

Physical media uses its whole capacity and requires 512-byte logical sectors.
Do not specify `--size-gib` with a device. `--no-verify` disables USB read-back,
not VM finalization. Logs must live outside the drive being erased.

## Reproducing software validation

The full CLI prepared Windows 11 Pro x64, index 6, build 26300.9457 into a
64 GiB dual-firmware MBR image. Private copies booted through SeaBIOS and OVMF
using virtual USB storage. The running OS reported SAN policy `4` and
`PortableOperatingSystem = 1` through its diagnostic console. These checks
cover kernel/service startup and active policies; full OOBE remains unvalidated.

Default tests use synthetic WIMs, real unmounted NTFS/FAT images, mocked VM
completion, cancellation, and device guards. They do not boot proprietary
Windows or request drive authorization.

The opt-in test creates private copies of a freshly prepared image. It adds a
test-only specialize commands to record real Windows boot and query active
SAN/portable policies, then stops the disposable VM. The source stays read-only:

```sh
python3 linux/tests/windows_togo_smoke.py \
    --image /path/to/new-portable-windows.img \
    --output-dir /path/to/roomy/new-windows-boot-test --firmware both --usb
```

Use `--firmware uefi` for GPT images. The test uses a local QMP socket for
screenshots, event recording, and input through one shared connection. It
exposes no network or physical devices. Instrumentation validates
kernel/specialize boot and policy values, not OOBE completion, login, activation,
or physical-device compatibility. Each firmware test temporarily needs two
additional copies of the allocated Windows data; use a roomy output filesystem.
The default first-boot timeout is 2400 seconds; `--timeout` can extend it for
slow software emulation. Successful private disk copies are removed unless
`--keep-images` is supplied; screenshots, policy evidence, and results remain.

For x86/x64 Windows with a US keyboard layout, `--console-probe` additionally
opens Setup's Shift+F10 diagnostic console and queries the live registry. This
can verify kernel/service startup before lengthy app registration finishes.
`--console-delay` controls when to open the console (default 360 seconds), and
`--console-warmup` allows it to initialize before typing (default 120 seconds).
The probe changes no registry values; output stays inside the disposable copy.
Console evidence does not establish completion of the specialize answer-file
commands or OOBE. The default probe waits for the answer-file commands instead.

The workflow follows Microsoft's
[deployment instructions](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/deployment/windows-to-go/deploy-windows-to-go)
and [BCDBoot reference](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/bcdboot-command-line-options-techref-di).
