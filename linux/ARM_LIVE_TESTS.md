# ARM live-root validation

`tests/arm_live_fixture.py` builds an Ubuntu Base + Casper source ISO inside a
disposable ARM VM. `tests/live_smoke.py` then prepares it through the normal
Rufus FAT32 extraction backend and checks persistence across two UEFI USB boots.
The fixture uses Ubuntu's Casper scripts, kernel modules, and generated
initramfs. It is a locally assembled test image, not an official Ubuntu live ISO.

## Inputs and host tools

Supply these files locally, for one architecture at a time:

- An Ubuntu Base archive whose checksum was checked against Ubuntu's signed
  `SHA256SUMS`, using the Ubuntu archive keyring.
- An APT-resolved directory of `.deb` files for that base and architecture,
  including `casper`, `systemd-sysv`, `dbus`, `libpam-systemd`, `squashfs-tools`,
  and exactly one versioned `linux-image-*` with its matching `linux-modules-*`.
- That package's kernel and a static BusyBox for the guest architecture.
- AAVMF for ARM64 or AAVMF32 for ARM32, and matching GRUB module data.

Use `arm64` packages for `--architecture arm64`, and `armhf` packages for
`--architecture arm`. Resolve packages with APT's `--download-only` and
`--no-install-recommends` options. For cross-architecture resolution, use the
base archive's `var/lib/dpkg/status`, override `APT::Architecture` and
`APT::Architectures`, and give APT separate `Dir::Etc`, `Dir::State`,
`Dir::Cache`, and `Dir::Log` directories. Use Ubuntu's signed ports repository
indexes. Do not resolve dependencies against the host's installed-package state.
This input acquisition is separate from the offline builder.

The host needs Python, `dpkg-deb`, `qemu-system-arm`, `qemu-img`, `xorriso`,
`mkfs.ext4`, `debugfs`, `mkfs.vfat`, `mtools`, `grub-mkimage`, and
`unmkinitramfs`. Matching GRUB modules can be unpacked locally as described in
[PLATFORMS.md](PLATFORMS.md). Host package installation, root access, loop
mounts, and QEMU user emulation are unnecessary. Allow several GiB of workspace
per fixture and up to 30 minutes for package configuration under TCG.

## Build and test

```sh
python3 linux/tests/arm_live_fixture.py --architecture arm64 \
    --base /path/to/ubuntu-base-26.04.1-base-arm64.tar.gz \
    --packages /path/to/arm64/apt/archives \
    --kernel /path/to/arm64/boot/vmlinuz-7.0.0-14-generic \
    --busybox /path/to/arm64/usr/bin/busybox \
    --output-dir /tmp/new-arm64-fixture

python3 linux/tests/live_smoke.py /tmp/new-arm64-fixture/fixture.iso \
    --layout extract --bootloader grub --firmware uefi --negative-control \
    --timeout 600 --output-dir /tmp/new-arm64-persistence-test
```

For ARM32, substitute `--architecture arm` and matching `armhf` inputs. Both
commands accept `--firmware-code` and `--firmware-vars`; otherwise they search
the QEMU installation's share directory and `/usr/share` for AAVMF/AAVMF32.
All output directories must be new. The Make `live-test` target also accepts
these ISOs with `LIVE_LAYOUT=extract LIVE_ROOT_TIMEOUT=600`; its
`--firmware all` selects UEFI on ARM.

The builder boots a small assembly initramfs through UEFI, extracts the base
archive into a private ext4 disk, and runs package configuration inside the
guest. APT has no repository sources in this step and receives only the supplied
package files. The guest builds its initramfs with the distribution's hooks and
creates SquashFS after unmounting pseudo-filesystems. This preserves native
ownership and permissions without host `fakeroot`. No builder or test guest has
a network interface, physical storage passthrough, or host directory shares.

Acceptance boots use new QCOW2 overlays on the prepared image. The root
checkpoint requires Casper's real ext4 store and root overlay, writes a random
nonce through the live root, checks the ext4 upper directory, and reads the
same nonce after shutdown/reboot. The ARM disabled-persistence control uses a
separate copy with `nopersistent` and fresh UEFI variables. It must reach
Casper's live root and fail specifically because no ext4 persistence store is
mounted. Merely booting a kernel or failing to boot cannot pass the control.

Fixture manifests retain base, kernel, BusyBox, package, firmware, initramfs,
SquashFS and ISO hashes. Acceptance manifests retain source/prepared/backing
hashes, console logs, exact QEMU commands, architecture, firmware and nonce.
Input hashes identify files; source authenticity comes from the separately
verified signatures and signed package indexes. Fixture creation alone is not
persistence acceptance.

The root checkpoint stops before Casper user setup and systemd startup.
ARM terminal/graphical sessions, native ARM host CI, Secure Boot and physical
board/USB behavior are separate acceptance work. See
[PERSISTENCE.md](PERSISTENCE.md) for the x86 userspace checkpoint and results.

## Recorded inputs, 2026-11-12

The local fixtures use [Ubuntu Base 26.04.1](https://cdimage.ubuntu.com/ubuntu-base/releases/resolute/release/),
Casper `26.04.2`, initramfs-tools `0.151ubuntu1`, and kernel/modules
`7.0.0-14.14` (`7.0.0-14-generic`). The base archives match these published
SHA-256 values:

```text
5a1906794ced63a71a8119c3f211ef5f0bbe0a243001b4bbd41fdf80c5b219fd  ubuntu-base-26.04.1-base-arm64.tar.gz
a40848e9c4ed72c22bdcb6a7bc9832e1ca8a30e5a3e2a0543dc56ed14e2e38b7  ubuntu-base-26.04.1-base-armhf.tar.gz
```

`gpgv` verified the base manifest with Ubuntu's CD-image signing key,
`843938DF228D22F7B3742BC0D94AA3F0EFE21092`. Each downloaded package was also
independently compared to the signed `resolute`, `resolute-updates`, or
`resolute-security` package indexes: 86 ARM64 packages and 85 ARMHF packages.
Their InRelease signer was `F6ECB3762474EDA9D21B7022871920D1991BC93C`.
The audit, exact package URLs/hashes and signed metadata are retained under
ignored `linux/build/arm-live-inputs/`, with `source-verification.json` as the
audit index.

The local boot tools use QEMU `10.2.1`, ARM64 AAVMF from
`qemu-efi-aarch64 2025.11-3ubuntu7.3`, ARM32 AAVMF32 from
`qemu-efi-arm 2024.02-2`, and GRUB data `2.14-2ubuntu1` (ARM64) /
`2.14-2ubuntu2` (ARM32). The assembly BusyBox is `1:1.37.0-7ubuntu1`.
VMs use TCG, two vCPUs and 2 GiB RAM. Secure Boot is disabled.

## Acceptance results, 2026-11-12

| Guest | UEFI USB write/reboot/read | UEFI persistence-disabled control | Prepared layout |
| --- | --- | --- | --- |
| ARM32 / ARMHF, Cortex-A15 | Passed | Passed | GPT, FAT32, GRUB `arm-efi`, ext4 `writable` |
| ARM64 / AArch64, Cortex-A57 | Passed | Passed | GPT, FAT32, GRUB `arm64-efi`, ext4 `writable` |

All four positive boots mount the real SquashFS root with Casper's ext4 store
at `/cow` and writable upper directory `/cow/upper`. Each architecture writes
a fresh nonce through the mounted root, verifies it in the ext4 upper, powers
off, and reads it after reboot. The two controls boot through the same UEFI/USB
path with `nopersistent`; Casper uses tmpfs for `/cow` and the checker reports
`RUFUS_PERSISTENCE_FAIL_NO_EXT4_STORE`, as required. Source, prepared, positive
backing and control-backing hashes remain unchanged.

The completed source ISOs have these SHA-256 values:

```text
3a9d374fd41239447b0a79cc1aab7520a1d523c89046f91dd05710a0ab3cea64  ARM32 fixture.iso
751bcb75e21acac528b666a2e48865926fa062b81d51beb8d0fdd4b908a1068c  ARM64 fixture.iso
```

The fixtures retain package versions and native ownership, including root and
system-account groups. Casper logs warnings for absent optional home/snapshot
media; the minimal ARM32 root also lacks `/var/crash/crash.init`. ARM64 logs an
overlay UUID fallback, and the kernels report that EVM does not support the
overlay. These diagnostics remain in the logs. This is the root-mount
checkpoint, before Casper user customization or a complete systemd session.

Artifacts are under ignored `linux/build/real-media/`:

- `arm{32,64}-casper-fixture-20261112-v3/`: completed fixture builds, manifests,
  installed-package lists and original builder logs.
- `arm{32,64}-live-root-20261112/`: three acceptance boots per architecture,
  private overlays, exact QEMU commands, hashes and results.
- `arm-live-root-20261112-summary.json`: audited result index, linked source
  verification, implementation hashes and boot-log hashes.
- `debian-userspace-arm-harness-regression-20261112/`: x86 regression with four
  BIOS/UEFI userspace boots, systemd `running`, independent persisted-file and
  ownership checks, and one passing disabled-persistence control.

Earlier failed fixture attempts are retained separately and are excluded from
the result index. All 234 automated tests and the standalone/Debian builds pass.
The host was x86-64; these are emulated ARM guests, not native ARM host CI results.
