# Live-USB persistence

Rufus prepares Casper/DragonOS and Debian live media before requesting drive
access. Hybrid mode remasters conventional live boot configuration and appends
an ext4 persistence store. FAT32 extraction installs matching GRUB/Syslinux
loaders and adds a separate ext4 partition. Normal image creation never injects
test hooks into the distribution's initramfs.

```sh
./rufus-linux linux /path/to/debian-live.iso --output /tmp/debian-hybrid.img \
    --persistence-mib 1024
./rufus-linux linux /path/to/debian-live.iso --mode extract --bootloader grub \
    --output /tmp/debian-grub.img --persistence-mib 1024
./rufus-linux linux /path/to/debian-live.iso --mode extract --bootloader syslinux \
    --output /tmp/debian-syslinux.img --persistence-mib 1024
```

The desktop's Linux live USB mode uses the same backend. Select a hybrid or
FAT32 layout, enable persistence, and choose its size. Physical writing still
requires the existing drive confirmation and storage guards.

## Debian compatibility

`boot=live` selects Debian's `persistence` option, an ext4 volume labeled
`persistence`, and `/ union` in its root `persistence.conf`. Media-check entries
using `verify-checksums` do not gain persistence. Explicitly disabled/rescue
entries remain disabled. Custom persistence stores and an existing store in a
hybrid source are refused.

Official Debian images preserve read-only Rock Ridge permissions. Rufus makes
only its private configuration/tree copies writable. Hybrid partition selection
reserves both embedded and appended source partition slots, preserving an
embedded EFI volume when adding persistence.

For FAT32 extraction, internal symlinks resolving to regular files inside the
private tree are copied as regular files. External links, broken links, cycles
and general directory links are rejected. Debian's CD-layout aliases
`debian -> .` and `dists/{stable,testing,unstable}` are omitted, retaining the
real suite directory and reporting `omitted_aliases` in CLI JSON. This mode
generates live menus; the ISO's full installer/utility menu is not reproduced.

Syslinux static submenus may repeat utility labels such as `mainmenu` in
separate scopes. They remain omitted utility entries. Duplicate labels in one
scope, ambiguous repeated live labels, unbalanced menus and unsupported submenu
defaults are refused. Root live defaults are retained.

## Reproducing the software checks

The opt-in harness needs QEMU, qemu-img, xorriso, e2fsprogs, fdisk, mtools,
matching GRUB/Syslinux tools, SeaBIOS, OVMF, and `unmkinitramfs` from
`initramfs-tools-core`. Supply an ISO locally; the harness downloads nothing.
The output directory must be new.

```sh
make -C linux live-test LIVE_ISO=/path/to/debian-live.iso \
    LIVE_KIND=debian LIVE_OUTPUT=build/debian-hybrid-test
make -C linux live-test LIVE_ISO=/path/to/debian-live.iso \
    LIVE_KIND=debian LIVE_LAYOUT=extract LIVE_BOOTLOADER=grub \
    LIVE_OUTPUT=build/debian-grub-test
make -C linux live-test LIVE_ISO=/path/to/debian-live.iso \
    LIVE_KIND=debian LIVE_LAYOUT=extract LIVE_BOOTLOADER=syslinux \
    LIVE_OUTPUT=build/debian-syslinux-test
```

`tests/live_smoke.py --prepared /path/to/image.img` reuses an existing prepared
image as read-only input. `--kind auto` detects Casper/Debian. Kernel/initrd
paths are auto-detected; explicit overrides remain available. Identical initrd
aliases are instrumented together so BIOS and UEFI test the same checks.

For Debian, the harness appends a test-only hook after the original
initramfs-tools init-bottom scripts, preserving their ORDER. Casper retains the
earlier checkpoint used by the existing DragonOS tests: after root mounting,
before casper-bottom user/desktop customization. Its original ORDER contents
are retained after the test hook, but the test shuts down before running them.
The hook checks that the live root
is an overlay whose writable upper directory belongs to the mounted ext4
persistence store. Debian's union configuration is checked too. It writes a
fresh nonce through the mounted live root, verifies the same bytes in the ext4
upper directory, shuts down, and verifies them after a second boot. An optional
`--negative-control` (enabled by the Make target) confirms that disabling
persistence cannot satisfy these checks.

Guests receive only new private QEMU overlays attached as USB storage, plus
firmware and test kernel/initrd inputs where needed. They have no network,
physical drive passthrough, or host directory shares. Source ISO, prepared
image and test backing image hashes are checked before and after booting.

## Validated matrix and limits

Debian Live **13.7.0 amd64 standard**, kernel **6.12.107+deb13-amd64**, was
downloaded from Debian's official live-image directory and matched its published
SHA-256:

```text
040a44f35186321eb6cdaab52a8a2d06224fe6b77f6fbb9f1861ad89c14e17ec
```

| Prepared layout | BIOS USB write/reboot/read | UEFI USB write/reboot/read |
| --- | --- | --- |
| Hybrid, retained distribution bootloaders | Passed | Passed |
| FAT32, native GRUB | Passed | Passed |
| FAT32, native Syslinux BIOS / GRUB UEFI | Passed | Passed |

The three layouts cover twelve positive firmware boots. Disabled-persistence
controls also pass. Results, source hashes, marker nonces and console logs are
retained under ignored `linux/build/real-media/debian-*` directories.

These checks finish after mounting the real distribution root, before full
userspace/desktop startup. Debian also runs its original init-bottom scripts;
Casper's later vendor customization is outside its root-mount checkpoint.
A later Casper checkpoint stalled during user/init customization in the cached
DragonOS image and remains unvalidated. The passing checks establish live-root
persistence, not a complete desktop session, physical USB reliability or Secure
Boot compatibility. ARM32/ARM64 firmware-to-kernel regression tests remain
separate from full ARM live-root persistence acceptance.

References: [Debian live images](https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/),
[Debian persistence documentation](https://live-team.pages.debian.net/live-manual/html/live-manual/customizing-run-time-behaviours.en.html),
and [live-boot parameters](https://manpages.debian.org/trixie/live-boot-doc/live-boot.7.en.html).
