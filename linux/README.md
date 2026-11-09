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
    dosfstools xorriso wimtools exfatprogs ntfs-3g e2fsprogs mtools 7zip
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
sudo apt install ./linux/dist/rufus-linux_0.3.0_all.deb
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

This is an initial port, **not full parity with Windows Rufus**. Windows To Go,
remaining advanced Windows setup options, persistence
partition creation, ISO downloads, Secure Boot revocation checks,
VHDX/FFU/VTSI support, translations, and destructive bad-block testing are not
implemented. DD mode preserves whatever boot support is already in an image.
UEFI Windows media retains the bootloader supplied by the selected ISO; the
port does not validate that loader's signature, architecture, or bootability.
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

# DESTRUCTIVE: /dev/sdX must be replaced with a drive from `devices`.
./rufus-linux write /path/to/hybrid-linux.iso --device /dev/sdX --confirm /dev/sdX
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX \
    --bypass-hardware-checks --local-account alice --privacy
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX \
    --filesystem ntfs --firmware bios
./rufus-linux freedos --device /dev/sdX --confirm /dev/sdX --fat 32
./rufus-linux format --device /dev/sdX --confirm /dev/sdX \
    --filesystem exfat --table gpt --label STORAGE
```

Use `--sha256 HEX` with `write` or `windows` to check a published checksum before
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

## Storage behavior

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
