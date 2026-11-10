# Data-drive formatting

The desktop and `format` command create one data partition with GPT or MBR.
FAT16, FAT32, UDF 2.01, exFAT, NTFS, ext2, ext3 and ext4 are available. This
operation prepares storage; use the Windows, Linux or FreeDOS modes for boot
media.

```sh
# Erases the selected removable drive after an exact path confirmation.
./rufus-linux format --device /dev/sdX --confirm /dev/sdX \
    --filesystem fat16 --table dos --label STORAGE
./rufus-linux format --device /dev/sdX --confirm /dev/sdX \
    --filesystem udf --table gpt --label STORAGE

# Creates NEW regular files. No device discovery, root or authorization.
./rufus-linux format --output /tmp/fat16.img --size-mib 64 \
    --filesystem fat16 --table dos
./rufus-linux format --output /tmp/udf-4kn.img --size-mib 64 \
    --filesystem udf --sector-size 4096
```

`vfat` retains its existing CLI meaning of FAT32. `fat16` explicitly selects
FAT16; it is passed to UDisks as `vfat` with `mkfs.vfat -F 16`. MBR FAT16 uses
type `0x0e` (LBA), FAT32 uses `0x0c`, and UDF uses `0x07`. GPT uses Microsoft
Basic Data for these formats and the Linux filesystem type for ext filesystems.
The partition starts at 1 MiB and ends before the final MiB, leaving room for
the backup GPT. Partition-table fields use explicit little-endian encoding.

## Geometry and labels

- The minimum drive/image size is 64 MiB. Physical drives always use their
  reported capacity and logical sector size; file-only geometry overrides are
  refused for drive operations. File images accept 512, 1024, 2048 or 4096-byte
  sectors. A raw image must be restored to matching logical-sector geometry.
- FAT16 requires a drive/image smaller than 4 GiB. Cluster size is selected
  explicitly so small 4-KiB-sector volumes remain FAT16. Above 2 GiB, FAT16 uses
  64 KiB clusters, which older operating systems/readers may not support.
- FAT32 needs at least 65,525 clusters. Small images that cannot meet this
  requirement at the chosen sector size are rejected; use at least 512 MiB
  for a 4-KiB-sector FAT32 image. FAT32 and UDF also enforce their 32-bit
  sector/block-count limits. MBR's capacity limit follows logical sectors:
  2 TiB with 512-byte sectors, 16 TiB with 4096-byte sectors.
- FAT labels allow 11 ASCII characters and are uppercased. Control characters
  and reserved punctuation are rejected. UDF logical labels allow up to 126
  Latin-1 or 63 BMP characters; supplementary Unicode characters are refused.
  The ancillary UDF Volume Identifier uses a shortened label where needed;
  the logical volume label is retained in full.
- exFAT file images currently use 512-byte sectors. Drive formatting delegates
  sector detection to the system's exfatprogs through UDisks.

UDF uses hard-disk media type, revision 2.01 and a block size matching logical
sectors. It formats a partition, as do the other format choices. macOS may need
manual mounting for partitioned UDF media. A whole-disk UDF/superfloppy layout
is not currently offered.

## Authorization and failure handling

Install `dosfstools` for FAT and `udftools` for UDF. Other formats need their
corresponding system formatter. Before changing a drive, Rufus validates
geometry and labels, checks its formatter, then asks UDisks `CanFormat` to check
the daemon's own backend and dependencies. UDisks does not use the source
launcher's private development-tool PATH. Unpacking a formatter locally
therefore enables file-image tests, but not privileged drive formatting.

Missing daemon support fails before unmounting or changing the partition table.
Drive identity and system-storage checks remain in effect immediately before
destructive calls. UDisks/Polkit handles authorization. Cancellation waits for
the current UDisks operation to finish; it cannot roll back an erased drive.

File-image creation runs without UDisks. A private regular volume file is
formatted first, then assembled into a new disk image and SHA-256 hashed.
Existing destinations, including symlinks, are refused. Failed or cancelled
output creation removes only the incomplete file created by that operation.

## Software validation

The unit/integration suite checks independent filesystem probing, 512/4-KiB
geometry, MBR/GPT layouts, FAT16 cluster sizing, Unicode UDF labels, daemon
preflight, protected outputs and cancellation. Its regular-file matrix also
covers the previously supported filesystems. Default tests never open drives.

An opt-in VM test mounts eight new FAT16/UDF data disks, writes markers, shuts
down, then boots again to read them. The eight disks cover both filesystems,
MBR/GPT and 512/4096-byte logical sectors. Guests receive only newly created
regular test files; no networking, physical devices or host directory shares.

```sh
make -C linux filesystem-test FS_KERNEL=/path/to/readable/vmlinuz \
    FS_MODULES=/lib/modules/MATCHING_KERNEL FS_OUTPUT=build/new-filesystem-test
```

Use a static guest BusyBox and modules matching the supplied kernel. The test
also supports ARM32/ARM64 kernels through UEFI, with explicit `--busybox`,
`--firmware-code` and `--firmware-vars` arguments; see
`tests/filesystem_smoke.py --help`. CI includes amd64 and native ARM64 runs.
Local x86-64, ARM64 and ARM32 guests passed all eight combinations, including
write/shutdown/read-back across separate boots (24 volumes, six boots total).
Remote CI execution and physical hardware validation remain separate work.

The development host's real UDisks capability query also confirmed that a
private formatter does not make system UDF formatting available: the daemon
reported missing `mkudffs`. No system packages were installed. Actual UDisks
FAT16/UDF loop formatting, additional releases and Windows/macOS mounting are
not established by the file-image/VM checks.

The optional `sudo make -C linux integration` harness exercises the actual
UDisks formatting API on a newly allocated loop device only. It requires system
formatters, including udftools, and must be explicitly elevated once for the
whole run. It never accepts an existing block device.

Backend references: [UDisks formatting options](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.Block.html),
[UDisks formatter capability checks](https://storaged.org/doc/udisks2-api/latest/gdbus-org.freedesktop.UDisks2.Manager.html),
and [udftools formatting/compatibility documentation](https://github.com/pali/udftools/blob/master/doc/mkudffs.8).
