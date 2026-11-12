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

### Userspace and live-user sessions

`--check-stage userspace` continues through the original Casper customization
and Debian live-config scripts into the distribution's systemd startup. The
corresponding Make target runs both BIOS and UEFI, with a ten-minute limit per
boot (override `LIVE_USERSPACE_TIMEOUT` if needed):

```sh
make -C linux live-userspace-test LIVE_ISO=/path/to/debian-live.iso \
    LIVE_KIND=debian LIVE_OUTPUT=build/debian-userspace
make -C linux live-userspace-test LIVE_ISO=/path/to/DragonOS.iso \
    LIVE_KIND=casper LIVE_LAYOUT=extract LIVE_BOOTLOADER=syslinux \
    LIVE_OUTPUT=build/dragonos-userspace
```

This test installs its checker and systemd unit only in the guest's volatile
`/run`. It reserves the serial console for evidence, waits for normal startup,
and opens a separate terminal through `systemd-run` and the distribution's
`login` PAM stack. The selected account must already be a live user with a home
directory; the harness never creates a substitute account. The unprivileged
user writes a fresh nonce in that home directory, then reads it on the next
boot. Each boot requests an ordinary systemd poweroff. After QEMU exits, the
host independently reads the ext4 partition using `debugfs` and checks both
the file's bytes and its ownership. A successful early kernel exit, a root-only
write, or a missing user session cannot satisfy this checkpoint.

The logs retain the boot journal, session identity, system state and shutdown.
Results distinguish `running` from `degraded`; any failed distribution services
remain visible in the log. A terminal session does not establish that every
desktop application works. Test copies remove graphical splash flags and do
not add `ip=frommedia`: newer generic initramfs-tools interprets that Casper
option as a network request at its later checkpoint, stalling a NIC-free VM.
These harness settings are never added to ordinary prepared user images.

Guests receive only new private QEMU overlays attached as USB storage, plus
firmware and test kernel/initrd inputs where needed. They have no network,
physical drive passthrough, or host directory shares. Source ISO, prepared
image and test backing image hashes are checked before and after booting.

## Root-mount checkpoint matrix

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

The root-mount checks finish after mounting the real distribution root, before full
userspace/desktop startup. Debian also runs its original init-bottom scripts;
Casper's later vendor customization is outside its root-mount checkpoint.
These results establish live-root persistence. The userspace checkpoint above
adds a separate acceptance stage for normal startup and live-user files.
Physical USB reliability, Secure Boot compatibility and full ARM live-root
persistence remain separate from these x86 VM checks.

## Userspace results, 2026-11-12

Debian Live 13.7.0 amd64 standard and the cached DragonOS Resolute R2.2 media
pass the later checkpoint for the layouts below. Each cell covers two complete
startups, a real live-user terminal/PAM session, user-file write/read across
shutdown and reboot, and independent ext4 content/owner checks after both boots.
All positive boots report systemd state `running`, with no failed units at the checkpoint.

| Distribution and layout | BIOS USB | UEFI USB |
| --- | --- | --- |
| Debian hybrid | Passed | Passed |
| Debian FAT32 / GRUB | Passed | Passed |
| Debian FAT32 / Syslinux BIOS + GRUB UEFI | Passed | Passed |
| DragonOS hybrid | Passed | Passed |
| DragonOS FAT32 / GRUB | Passed | Passed |
| DragonOS FAT32 / Syslinux BIOS + GRUB UEFI | Passed | Passed |

The Debian matrix comprises twelve positive boots and three disabled-persistence
controls. Its live account is `user`, UID 1000. The controls fail with
`RUFUS_PERSISTENCE_FAIL_NO_EXT4_STORE`, as required. Source, prepared-image and
test-backing hashes remain unchanged. Results and complete boot journals are
under `linux/build/real-media/debian-userspace-{hybrid,grub,syslinux}-20261112-final/`.

The DragonOS matrix adds twelve positive boots and three passing
disabled-persistence controls. They use the existing
`live` account, UID 890, and its real `user-light` terminal/PAM session. The
kernel is `7.0.0-38-generic`. The cached input is the earlier Rufus-remastered
`dragonos-persistent-0.4.img`, SHA-256:

```text
0f04bce3d0f74e6b0b17c68ed41e2c02455bd50e2b01d38a923473eb2689977b
```

That input is distinct from the original vendor ISO checksum recorded in the
[version 0.4 notes](README.md#version-04-validation). The original customization
scripts run unchanged. They emit
`/dev/null` permission warnings and try offline APT refreshes; the cached
shutdown environment also reports missing libmount support and incomplete
unmount/loop cleanup before powering off. These fixture warnings are retained
in the logs. The acceptance check verifies a requested systemd shutdown,
completed kernel poweroff and persisted user files; it does not certify a
warning-free vendor shutdown or a graphical desktop session.

The earlier startup stall was a harness problem: `ip=frommedia` triggered
generic initramfs-tools networking after Casper finished. Removing it lets the
original scripts complete without a NIC. Other harness fixes reserve the
serial port to prevent getty from interrupting evidence output, accept
systemd's interactive `user-light` class for the live account, and identify
the persistence filesystem even when hybrid GPT entries use generic data types.

DragonOS artifacts are retained in
`linux/build/real-media/dragonos-userspace-{grub,syslinux}-20261112-final/`
and `dragonos-userspace-hybrid-{bios,uefi}-20261112-final/` beneath the same
directory. Hybrid BIOS and UEFI were run separately. Failed diagnostic runs
remain in adjacent directories; only the completed runs listed here constitute
the acceptance matrix. The combined matrix covers **24 positive boots and six
disabled-persistence controls**. A machine-readable index is retained at
`linux/build/real-media/userspace-20261112-summary.json`.

These runs use x86-64 QEMU 10.2.1 (`1:10.2.1+ds-1ubuntu3.3`), software TCG,
2 GiB RAM and two vCPUs, SeaBIOS 1.17.0 (`1.17.0-1ubuntu1`), and OVMF from
`ovmf-generic 2025.11-3ubuntu7.3`, with private writable firmware variables.
Only virtual USB image files are attached; no network or physical storage is
passed through. This acceptance covers terminal sessions. Graphical desktop
interaction, Secure Boot, full ARM live-root persistence and physical USB
acceptance remain separate work.

References: [Debian live images](https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/),
[Debian persistence documentation](https://live-team.pages.debian.net/live-manual/html/live-manual/customizing-run-time-behaviours.en.html),
and [live-boot parameters](https://manpages.debian.org/trixie/live-boot-doc/live-boot.7.en.html).
