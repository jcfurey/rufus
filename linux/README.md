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
    dosfstools xorriso wimtools exfatprogs ntfs-3g e2fsprogs
make -C linux
./linux/dist/rufus-linux
```

For development, `./rufus-linux` runs directly from this checkout. The standalone
`linux/dist/rufus-linux` archive contains the Python implementation and uses the
system Python/GTK installation. Building requires no package downloads or pip
environment. Python 3.10+ and GTK 4.10+ are required for the desktop interface;
Ubuntu 24.04 or newer satisfies these requirements. A graphical session is not
required for the CLI's image-file operations.

To create an Ubuntu package:

```sh
make -C linux deb
sudo apt install ./linux/dist/rufus-linux_0.1.0_all.deb
```

Alternatively, install the application and desktop launcher for your user:

```sh
make -C linux install PREFIX="$HOME/.local"
```

Your desktop session must have `$HOME/.local/bin` on `PATH` for that launcher.

## Implemented

- Native GTK 4 interface with USB selection, image chooser, progress, cancellation,
  optional published SHA-256 verification, and read-back verification.
- Raw/hybrid disk-image writing, including gzip, xz, and bzip2 compression.
  ISO files must contain a disk partition table for DD mode to be bootable.
- UEFI Windows installation media: extract an ISO, create GPT and a FAT32 EFI
  partition, copy installer files, and verify their contents. `install.wim` files
  exceeding FAT32's limit are split with `wimlib-imagex`. This requires the
  `xorriso` and, for large WIMs, `wimtools` packages.
- USB formatting with GPT or MBR and FAT32, exFAT, NTFS, ext2, ext3, or ext4.
- Command-line access to the same storage operations and SHA-256 calculation.
- A standalone executable archive, a desktop launcher, and Debian packaging.

This is an initial port, **not full parity with Windows Rufus**. Windows To Go,
BIOS Windows installer creation, Rufus Windows setup customization, persistence
partition creation, FreeDOS setup, ISO downloads, Secure Boot revocation checks,
VHD/VHDX/FFU support, translations, and destructive bad-block testing are not
implemented. DD mode preserves whatever boot support is already in an image.
UEFI Windows media retains the bootloader supplied by the selected ISO; the
port does not validate that loader's signature, architecture, or bootability.

## Command-line examples

```sh
./rufus-linux devices
./rufus-linux checksum /path/to/ubuntu.iso

# TEST: write and verify a NEW regular file, without touching a USB drive.
./rufus-linux write /path/to/hybrid-linux.iso --output /tmp/rufus-test.img

# DESTRUCTIVE: /dev/sdX must be replaced with a drive from `devices`.
./rufus-linux write /path/to/hybrid-linux.iso --device /dev/sdX --confirm /dev/sdX
./rufus-linux windows /path/to/windows.iso --device /dev/sdX --confirm /dev/sdX
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

## Storage behavior

Only whole USB/removable drives are selectable. Internal disks, read-only
devices, active layered storage, and disks with system or swap mounts are
excluded or blocked. The backend checks the drive identity again before writing,
protects the source image's drive, and checks physical ancestors of system
filesystems. Mounted removable filesystems are unmounted through UDisks2.
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
if needed). A Windows ISO with an oversized `install.esd`, or other files larger
than FAT32 supports, is rejected before formatting.

## Validation

```sh
make -C linux check       # file-backed tests; never writes to a block device
make -C linux smoke-test  # briefly opens and closes the GTK application
make -C linux gui-test    # GTK interaction and background checksum test; no drives opened
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
test files, not a bootable Windows installer. Actual USB hardware and a real
Windows installation/firmware boot require separate validation.

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
