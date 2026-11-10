# Rufus for Linux

This branch adds an **experimental native Linux port** alongside the original
Windows implementation. It uses GTK 4, Python 3, and UDisks2/Polkit. It does not
require Wine or run its graphical interface as root. The Linux storage backend
is new; the original Windows C implementation is not compiled for Linux.

## Build and launch on Ubuntu 26.04

The main dependencies are already present on a standard Ubuntu desktop. Install
any missing packages, including the tools for Windows ISO support:

```sh
sudo apt install python3 python3-gi gir1.2-gtk-4.0 udisks2 util-linux \
    dosfstools xorriso wimtools exfatprogs ntfs-3g e2fsprogs fdisk mtools 7zip
make -C linux
./linux/dist/rufus-linux
```

For development, `./rufus-linux` runs directly from this checkout. The standalone
`linux/dist/rufus-linux` archive contains the Python implementation and uses the
system Python/GTK installation. Building requires no package downloads or pip
environment. Python 3.10+ and GTK 4.10+ are required for the desktop interface;
Ubuntu 24.04 or newer satisfies these requirements. A graphical session is not
required for the CLI's image-file operations. The source launcher automatically
uses development tools unpacked in `linux/build/tools/root` when present, so this
checkout can use its locally prepared tools without a system install. Set
`RUFUS_USE_LOCAL_TOOLS=0` to use only system tools. The standalone archive uses
system dependencies; locally unpacked binaries are not shipped inside it.

To create an Ubuntu package:

```sh
make -C linux deb
sudo apt install ./linux/dist/rufus-linux_0.8.0_all.deb
```

Alternatively, install the application and desktop launcher for your user:

```sh
make -C linux install PREFIX="$HOME/.local"
```

Your desktop session must have `$HOME/.local/bin` on `PATH` for that launcher.

## Implemented

- Native GTK 4 interface with USB selection, image chooser, progress, cancellation,
  optional published SHA-256 verification, and read-back verification.
- Raw/hybrid disk-image writing, including gzip, xz, bzip2, LZMA, Unix compress
  (.Z), Zstandard, and single-file ZIP archives. Archive members are streamed,
  never extracted by their filenames. Corrupt/ambiguous archives fail preflight.
  ISO files must contain a disk partition table for DD mode to be bootable.
- Native fixed/dynamic VHD restoration, including compressed VHD files. VHD
  metadata and sector bitmaps are validated; differencing disks are refused.
- Drive backup to new raw IMG or fixed VHD files, using a read-only source
  descriptor and read-back verification. Existing output files are protected.
- MD5, SHA-1, SHA-256, and SHA-512 in a single read, in the desktop and CLI.
- Windows installation media with UEFI/GPT or BIOS/MBR, using FAT32 or NTFS.
  ISO9660 and UDF extraction are supported, including Windows ISOs whose ISO9660
  view contains only a README. UDF extraction requires `7zip`.
  FAT32 splits oversized `install.wim` files. NTFS uses native wimlib extraction
  into an ordinary filesystem image and the bundled UEFI:NTFS driver partition
  when targeting UEFI. Windows BIOS boot code is reused from upstream Rufus.
- Bootable FreeDOS FAT16/FAT32 media using Rufus's bundled kernel, command shell,
  locale utilities, and MBR/boot sectors. No FreeDOS download is required.
- Persistent Linux live media for supported Casper (including DragonOS) and
  Debian hybrid ISOs. Adds an ext4 partition and live boot options while
  retaining the ISO's bootloaders, volume identity, and firmware support.
  Available in the desktop's Linux live USB mode and the `linux` CLI command.
- FAT32 extraction for conventional GRUB Casper/Debian live ISOs: x86 BIOS,
  x86-64 BIOS/UEFI, ARM64 UEFI, and 32-bit ARM UEFI, with optional persistence.
  Detects kernel architecture and installs matching GRUB loaders and modules.
- Syslinux/ISOLINUX Casper/Debian menu extraction with nested includes, default
  selection, multiple initrds, and persistence. Native Syslinux 6.x handles
  x86 BIOS; matching GRUB handles UEFI and ARM. Rebuilds a simple live menu
  without reusing the ISO's potentially incompatible COM32 modules.
- NVIDIA Jetson installer USB preparation, legacy developer-kit SD-image
  writing, model/storage instructions, recovery USB inspection, and direct
  NVIDIA BSP recovery jobs with command review, authorization, cancellation,
  and logs. An installed SDK Manager wizard remains an optional route.
- Windows setup options in the desktop and CLI: TPM/Secure Boot/RAM check bypass,
  offline/local account setup, privacy choices, explicit Windows locale/time zone,
  and automatic device-encryption control. WinPE options are injected into the
  actual bootable WIM image using `wimtools`. OOBE-only options are staged under
  `sources/$OEM$/$$/Panther`. Existing answer files are preserved by refusing
  conflicting customization. Installation-time bypasses and OOBE behavior still
  need full installation tests.
- USB formatting with GPT or MBR and FAT32, exFAT, NTFS, ext2, ext3, or ext4.
- Command-line access to the same storage operations and SHA-256 calculation.
- A standalone executable archive, a desktop launcher, and Debian packaging.
- Windows To Go edition selection, native NTFS deployment, and boot/policy
  configuration in an isolated WinPE VM before physical writing. The desktop
  and CLI provide this workflow; the CLI also prepares image files without
  administrator access.
  See [WINDOWS_TO_GO.md](WINDOWS_TO_GO.md) for dependencies and validation scope.

This is an initial port, **not full parity with Windows Rufus**. Remaining
advanced Windows setup options, dynamic/utility Syslinux menus,
ISO downloads, Secure Boot revocation checks,
VHDX/FFU/VTSI support, translations, and destructive bad-block testing are not
implemented. DD mode preserves whatever boot support is already in an image.
Windows installation media retains the ISO's loader; signature and revocation
validation remain pending. Windows To Go additionally checks that the EFI
loader and WinPE match the selected edition's CPU architecture.
The detailed implementation and validation checklist is in [PARITY.md](PARITY.md).

## Command-line examples

```sh
./rufus-linux devices
./rufus-linux checksum /path/to/ubuntu.iso
./rufus-linux checksum /path/to/ubuntu.iso --all

# Save and verify a USB drive. The source is opened read-only.
./rufus-linux backup --device /dev/sdX --output /path/to/new-backup.vhd --format vhd

# Test VHD creation/restoration using only regular files.
./rufus-linux backup --source /path/to/disk.img --output /tmp/test.vhd --format vhd
./rufus-linux write /tmp/test.vhd --output /tmp/restored.img

# Prepare and inspect customized Windows files without erasing a drive.
./rufus-linux prepare-windows /path/to/windows.iso --output /tmp/windows-prepared \
    --local-account alice --privacy --disable-device-encryption --locale en-US

# TEST: write and verify a NEW regular file, without touching a USB drive.
./rufus-linux write /path/to/hybrid-linux.iso --output /tmp/rufus-test.img

# Build bootable images without touching a drive or requesting authentication.
./rufus-linux freedos --output /tmp/freedos.img --fat 32 --size-mib 64
./rufus-linux windows /path/to/windows.iso --output /tmp/windows.img \
    --filesystem ntfs --firmware uefi --local-account alice --privacy
./rufus-linux linux /path/to/DragonOS_Resolute_R2.2.iso \
    --output /tmp/dragonos-persistent.img --persistence-mib 4096
./rufus-linux linux /path/to/arm64-live.iso --mode extract \
    --output /tmp/arm64-fat32.img --persistence-mib 1024
./rufus-linux linux /path/to/armhf-live.iso --mode extract \
    --architecture arm --output /tmp/arm32-fat32.img

# DESTRUCTIVE: /dev/sdX must be replaced with a drive from `devices`.
./rufus-linux write /path/to/hybrid-linux.iso --device /dev/sdX --confirm /dev/sdX
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX \
    --bypass-hardware-checks --local-account alice --privacy
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX \
    --filesystem ntfs --firmware bios
./rufus-linux freedos --device /dev/sdX --confirm /dev/sdX --fat 32
./rufus-linux linux /path/to/live.iso --device /dev/sdX --confirm /dev/sdX \
    --persistence-mib 4096
./rufus-linux format --device /dev/sdX --confirm /dev/sdX \
    --filesystem exfat --table gpt --label STORAGE
```

Use `--sha256 HEX` with `write`, `windows`, or `linux` to check a published checksum before
erasing anything. For compressed images, the expected SHA-256 is for the
**downloaded compressed file**; the completion message identifies the
decompressed image checksum. Verification is enabled by default. Press Ctrl+C
or Cancel to stop an operation; a partially written drive must be rewritten.
Partitioning/formatting calls finish their current UDisks operation before
cancellation takes effect.

`windows-config --output NEWFILE.xml` generates just an answer file using the
same Windows options. Local accounts use Rufus's blank setup password and a
first-login command requesting a password change. The options do not contain
installer disk-selection or automatic partition-erasure instructions. Offline
account and hardware-check workarounds follow this branch's upstream Rufus
implementation; their effectiveness depends on the Windows build being installed.

## Linux live persistence

Select **Linux live USB** in the desktop, choose a hybrid live ISO, and set the
persistence size in MiB (minimum 64, default 1024). The drive must fit both the
ISO and the persistence volume. Casper images use the `persistent` boot option
and the `writable` filesystem label. Select **Older Casper (casper-rw)** or pass
`--casper-label casper-rw` for images that require the older label. Debian's
`boot=live` configuration is detected automatically; it gets the `persistence`
boot option, a volume labeled `persistence`, and `/ union` in `persistence.conf`.
See the [Casper manual](https://manpages.ubuntu.com/manpages/noble/man7/casper.7.html)
and [Debian persistence documentation](https://live-team.pages.debian.net/live-manual/html/live-manual/customizing-run-time-behaviours.en.html).

This mode rebuilds the ISO with xorriso's boot-equipment replay and appends an
ext4 volume. Its live files remain on ISO9660; it does not extract them to a
writable FAT32 partition. It supports conventional external GRUB/Syslinux `.cfg`
files using `boot=casper` or `boot=live`. Rescue entries explicitly disabling
persistence and media-check entries are preserved. Standard root MD5/SHA-256
manifests are updated for changed configuration files and replay-patched boot
images. Unsupported layouts,
mixed live systems, custom persistence-store options, and existing persistence
volumes are refused. Retained bootloaders do not establish Secure Boot support.

The prepared image is verified before the USB writer requests access. On GPT
drives, the backup partition table is relocated to the drive's end; unused space
after the requested persistence volume remains unallocated. Creating a large
full-drive image still requires reading its complete logical size for checksum
verification. File-backed output uses the space needed by the rebuilt image.

## Extracted media, ARM boards, and NVIDIA Jetson

In **Linux live USB**, choose **FAT32 files** to extract supported GRUB or
Syslinux/ISOLINUX Casper/Debian live menus. Kernel headers select x86 BIOS, x86-64 BIOS/UEFI, ARM64 UEFI,
or ARM32 UEFI. Install `grub-common` and the target's GRUB data package:
`grub-pc-bin`, `grub-efi-amd64-bin`, `grub-efi-arm64-bin`, or `grub-efi-arm-bin`.
For Syslinux BIOS output, install a native `syslinux` 6.x mtools installer;
GRUB is needed only for the firmware paths that use it. BIOS loader selection
is available in the desktop and CLI `--bootloader auto|grub|syslinux`.
The generated UEFI loaders are unsigned and require Secure Boot to be disabled.
CLI extraction defaults to no persistence; add `--persistence-mib` to enable it.
The application uses native Python/GI on x86 or ARM Linux hosts.

Raspberry Pi and firmware-specific board images use **Disk image (DD)** mode,
which preserves the vendor's complete boot and partition layout. The new
**NVIDIA Jetson** mode provides model/storage instructions, unchanged NVIDIA
installer USB creation, legacy developer-kit SD-image writing, and direct
recovery flashing through a prepared NVIDIA BSP without SDK Manager. Direct
jobs review the USB target, command, and firmware scope before one authorization
request. Installer USB media and the Jetson's installation target are separate
selections. BSP host executables must match the host CPU, including on ARM.

See [PLATFORMS.md](PLATFORMS.md) for ARM dependencies, supported Jetson workflows,
commands, and hardware validation limits.
See [JETSON_RECOVERY.md](JETSON_RECOVERY.md) for BSP preparation and direct jobs.

## Storage behavior

Direct Jetson recovery jobs select a recovery USB transport and storage attached
to the Jetson. They run NVIDIA's privileged tooling under the review flow above.
The removable-drive protections below apply to host image writing/formatting.

Only whole USB/removable drives are selectable. Internal disks, active layered
storage, and disks with system or swap mounts are
excluded or blocked. The backend checks the drive identity again before writing,
protects the source image's drive, and checks physical ancestors of system
filesystems. Mounted removable filesystems are unmounted through UDisks2.
Read-only removable drives can be backed up but cannot be written or formatted.
Polkit handles any required authorization. Run the app from a local desktop
session; an SSH session may need a Polkit authentication agent.

Raw writes hold an exclusive read/write device descriptor, flush writes, and
read back the image to compare its SHA-256. Verification requests eviction of
the OS file cache; it is not a power-cycle or firmware-cache test. Formatting
erases partition metadata, not all existing data, and is not secure erasure.

Compressed images are completely decompressed and validated in a temporary file
before the target is opened, so a truncated archive or oversized image fails
before a drive is erased. Windows ISOs are also extracted and checked before
formatting. Ensure the temporary filesystem has enough free space (set `TMPDIR`
if needed). Windows and FreeDOS media are fully prepared as sparse image files
before entering the shared verified disk writer. They request a writable disk
descriptor once, rather than performing separately authenticated format/copy
steps. Preparing and verifying a full-drive image can take time on large drives;
NTFS preparation also stages an intermediate WIM. Native Windows/FreeDOS image
construction currently requires 512-byte logical sectors.
A Windows ISO with an oversized `install.esd`, or other files larger than FAT32
supports, is rejected for FAT32; select NTFS for those files.

## Validation

```sh
make -C linux check       # file-backed tests; never writes to a block device
make -C linux smoke-test  # briefly opens and closes the GTK application
make -C linux gui-test    # GTK interaction and background checksum test; no drives opened
make -C linux boot-test   # optional QEMU/SeaBIOS FreeDOS boot tests, no root
make -C linux uefi-test   # optional QEMU/OVMF FAT32 and UEFI:NTFS boot tests, no root
# Optional real Casper ISO: two boots per firmware, private VM overlays, no root.
# LIVE_OUTPUT must be a NEW directory. No ISO is downloaded by the test.
make -C linux live-test LIVE_ISO=/path/to/DragonOS_Resolute_R2.2.iso \
    LIVE_OUTPUT=build/dragonos-reboot-test
sudo make -C linux integration # optional: one authorization for a disposable-loop test matrix
```

The optional integration harness requires one elevated invocation for the entire
test matrix, avoiding a separate password prompt for each UDisks operation.
Unprivileged invocations exit before contacting UDisks and never open a password
dialog. The regular application, GUI tests, and file-backed tests run as your
normal user. The integration harness checks its loop device's kernel backing-file path before
writing, and cannot accept an existing device as an argument. It exercises real
UDisks2 file-descriptor passing, GPT/FAT32 formatting, mounting, copying,
read-back verification, unmounting, and cleanup. Only its disposable test volumes
may be forcibly unmounted during cleanup; the application refuses busy drives.

Tests include image and compression round trips, partial writes, corrupt archive
rejection, checksum failures, cancellation, read-back corruption, drive replacement,
system/source-drive protection, FAT name collisions and symlink rejection, and
extraction/copying of a generated Windows-shaped ISO fixture. That fixture contains
test files, not a bootable Windows installer. Real-ISO results are recorded below;
physical USB hardware and a complete Windows installation need separate validation.

### Initial validation on the target machine

On Ubuntu 26.04.1 with Python 3.14 and GTK 4.22:

- All 45 file-backed tests passed.
- The packaged application opened successfully on the desktop. GUI interaction,
  erase-dialog cancellation, and background checksum tests passed.
- Real UDisks2 operations on a disposable loop image passed: exclusive descriptor
  access, raw write/read-back, GPT/FAT32 EFI partition creation, mounting, installer
  fixture copying/read-back, and unmounting.
- The executable archive and Debian package built, and the desktop entry validated.

The complete filesystem matrix was interrupted by desktop automounting and
repeated authorization prompts; it has not passed in full. The harness now
requires a single explicitly elevated run and does not prompt through Polkit
when invoked as a normal user. Real boot testing and large-WIM splitting remain
unvalidated; `wimtools` was not installed on this machine during initial development.

Version 0.2 adds file-backed tests using real wimlib and QEMU tools unpacked into
the ignored build directory, without installing system packages or requesting
administrator authentication. They cover actual WIM answer-file injection,
splitting/reapplying WIM resources with constrained part sizes, VHD interoperability,
and archive corruption handling. GUI tests also exercise the new setup and backup
controls. The CI dependency batch installs `wimtools`, `qemu-utils`, and `zstd` so
these tests run there; local runs skip tool-specific checks when tools are absent.

### Version 0.3 validation

- All 89 automated tests passed with the local validation toolset. The desktop
  interaction test and all four file-backed firmware boot cases also passed.
- Generated FAT16 and FAT32 images booted through SeaBIOS, the upstream MBR and
  boot sectors, the actual FreeDOS kernel, and `COMMAND.COM`.
- Generated GPT/FAT32 and GPT/UEFI:NTFS images booted an EFI test application
  through OVMF. The latter exercised the bundled NTFS driver and its handoff to
  the bootloader on the main NTFS partition.
- `/home/jcfurey/Downloads/Windows11_Client_x64_en-us_26300_9457.iso` was extracted
  through its UDF filesystem and prepared as NTFS/UEFI media with hardware-check,
  local-account, privacy, locale, and device-encryption options. That image booted
  to the actual Windows 11 Setup language screen in a private VM overlay. No OS
  installation or physical-disk test was performed. OOBE and the effectiveness of
  installation-time bypasses still require further validation.
- Boot resources are checked byte-for-byte against their C initializers using
  the C compiler; partition tables are checked independently using `sfdisk`.

The generated Windows image, validation metadata, and screenshot are retained
under ignored `linux/build/real-media/`. Original ISOs in Downloads were read
without modification. DragonOS was identified as a Casper-based live image;
persistence implementation and boot testing remain on the parity checklist.

### Version 0.4 validation

- All 108 automated tests passed with the local validation toolset. New tests
  cover actual hybrid ISO rebuilding, ext4 integrity, Casper and Debian layouts,
  legacy Casper labels, updated configuration and boot-image checksums, ISO UUID preservation,
  GPT relocation, cancellation, source changes, protected output, and preparation
  failures before device access. The GTK interaction test also passed.
- `DragonOS_Resolute_R2.2.iso` (SHA-256
  `59898fa7f2faec1fa951fa401c462d921f886f2a21561bfecc4b759269d0d5ec`)
  was prepared with a 512 MiB `writable` ext4 partition. In private VM overlays,
  its actual Casper initramfs mounted that partition as `/cow` and the live root
  as an overlay filesystem. A marker written through the live root was read
  back on a second boot.
- This write/reboot/read test passed through both the retained BIOS/GRUB loader
  under SeaBIOS and the retained UEFI loader under OVMF. Test copies included an
  initramfs hook, serial output, and a zero-second GRUB timeout. A separate direct
  kernel boot also passed twice. Source ISOs and the prepared user image were
  used read-only; VM changes were confined to private overlays.
- All 591 entries in the prepared DragonOS image's MD5 manifest were verified
  independently after extraction, including the rewritten GRUB configuration
  and patched BIOS boot image. The source manifest's stale self entry is removed.
- The prepared image, build metadata, VM overlays, and boot logs are retained
  under ignored `linux/build/real-media/dragonos-*`. Debian has file-backed
  partition/configuration tests; a real Debian live reboot remains unvalidated.
  Secure Boot and physical USB hardware remain separate acceptance work.

### Version 0.5 validation

- All 130 automated tests passed with the local toolset. They include ARM32/ARM64
  kernel/EFI checks, FAT32 extraction, correct removable loader paths,
  persistence, regenerated manifests, native 32/64-bit capacity requests, Jetson
  method restrictions, embedded NVIDIA EFI validation, unchanged vendor/board
  image restoration, and failures before device access.
- GTK tests passed for extracted-media options, ARM controls, all Jetson model
  selections, recovery setup without host-drive selection, confirmation
  cancellation, and asynchronous checksums.
- ARM64 and ARM32 images booted real Ubuntu Linux 7.0 kernels and static BusyBox
  initramfs through QEMU, AAVMF/AAVMF32, USB, FAT32, and the generated GRUB loader.
  The guests reported `aarch64` and `armv7l` and powered off successfully. Full
  ARM live-distribution persistence remains an additional acceptance test.
- DragonOS extracted to FAT32 with 256 MiB persistence passed real Casper
  marker-file write/reboot/read tests through both BIOS and UEFI. Test copies
  used private initramfs instrumentation. Artifacts are retained under
  `linux/build/real-media/dragonos-fat32*`, `arm64-uefi-test-2`, and `arm32-uefi-test`.
- Native ARM64 CI was added but has not run remotely in this session. Real
  NVIDIA ISO and physical Jetson installation/QSPI validation remain outstanding;
  current Jetson tests use structural fixtures and mocked SDK Manager launches.

### Version 0.6 validation

- All 150 automated tests passed with the local toolset. New tests cover direct
  BSP planning, all listed family/storage selections using synthetic BSPs,
  internal/external command scope, host ELF compatibility, USB reconnection,
  changed-input rejection, privileged-helper revalidation, one authorization,
  protected logs, failures, and process-group cancellation.
- GTK tests passed for BSP inspection, offline plan review and cancellation,
  optional SDK Manager mode, existing drive confirmation, and async checksums.
- NVIDIA's actual R39.2.1 BSP was inspected and planned read-only. Its Intel
  host tools were recognized, and missing rootfs/dependencies/USB selection
  correctly blocked execution. No vendor script or physical flash was run.

### Version 0.7 validation

- All 167 automated tests passed with the local development toolset.
- Syslinux tests cover nested relative includes, include cycles/escapes, default
  selection, global/local APPEND handling, multiple initrds, persistence,
  protected outputs, native installer/loader consistency, complete manifests,
  explicit ARM/BIOS rejection, automatic ARM-to-GRUB selection, and CLI operation
  without drive discovery. Existing GRUB/Jetson functionality remains covered.
- A real x86-64 Linux kernel and static BusyBox booted from the same FAT32 USB
  image through SeaBIOS/Syslinux and OVMF/GRUB in QEMU. Each guest verified both
  initrd archives and powered off. Artifacts are retained under
  `linux/build/real-media/syslinux-boot-test`.
- Actual DragonOS GRUB menus were converted to native Syslinux BIOS media with
  256 MiB persistence. Real Casper marker-file write/reboot/read tests passed
  under both BIOS/Syslinux and UEFI/GRUB in private VM overlays. Artifacts are
  retained under `linux/build/real-media/dragonos-syslinux-persistence`.
- ARM64 and ARM32 UEFI images again booted real kernels with the updated
  extractor. Full ARM live-distribution persistence remains acceptance work.
- GTK checks cover the new loader selection. The amd64 CI job includes the
  reproducible two-firmware kernel test; remote execution remains pending.

### Version 0.8 validation

- All 184 automated tests passed with the local development toolset. Windows
  tests apply selected WIM and solid ESD editions to real unmounted NTFS,
  validate FAT/GPT/MBR layouts and CPU metadata, and cover cancellation,
  protected outputs/logs, VM isolation, and failure before physical writing.
- GTK checks cover Windows To Go mode, explicit edition selection, CPU/firmware
  compatibility, edition capacity, installer-only options, and existing workflows.
  The standalone application and version 0.8.0 Debian package build successfully.
- A real Windows 11 Pro x64 image (index 6, build 26300) was prepared through
  the full CLI into a 64 GiB BIOS+UEFI image. Native wimlib applied the installed
  OS; the ISO's WinPE ran Microsoft's registry and boot tools successfully.
  Preparation used only regular files and an isolated software-emulated VM.
- Private virtual USB copies booted Windows through BIOS and UEFI and reported
  SAN policy 4 and PortableOperatingSystem 1 from their active registries.
  Queries ran through the real OS's diagnostic console; full OOBE and the
  staged answer-file commands were not validated. Source media and the prepared
  output remain unchanged by guest writes. Genuine ARM64/32-bit x86 Windows,
  full OOBE, Secure Boot, and physical USB validation remain pending.
- Build metadata, preparation logs, and boot diagnostics are under ignored
  `linux/build/real-media/windows-to-go-0.8*` and `windows-togo-0.8*`.
