# Rufus Linux feature parity

Target: the Windows application at the `master` base of this branch, currently
Rufus 4.16. A feature counts as complete only when the Linux backend, desktop
workflow, command-line workflow, failure handling, and applicable validation
are present. File-backed tests alone do not establish bootability.

| Windows feature | Linux implementation | Remaining acceptance work |
| --- | --- | --- |
| GTK desktop, progress, cancellation | Implemented | Accessibility, keyboard navigation, long-operation coverage |
| USB enumeration and system-disk protection | Implemented | Hardware hotplug and stacked-storage cases |
| GPT/MBR formatting | Implemented | Complete disposable-disk filesystem matrix |
| FAT32, exFAT, NTFS, ext2/ext3/ext4 | Implemented | Filesystem matrix and large-device boundaries |
| FAT16, UDF, ReFS | Pending | Linux formatter/backend selection; ReFS write support |
| Raw/hybrid disk-image writing | Implemented | BIOS/UEFI boot fixtures and hardware testing |
| gzip, xz, bzip2 images | Implemented | Large-image and interrupted I/O cases |
| ZIP, LZMA, compress (.Z), Zstandard images | Implemented, integrity and round-trip tests | Large-image boundaries |
| Fixed/dynamic VHD restoration | Implemented natively; QEMU interoperability tested | Large-image and additional producer coverage |
| VHDX, FFU, VTSI and differencing VHD restoration | Pending | Container validation/conversion and format-specific tests |
| SHA-256 | Implemented | Hardware validation of write/read-back |
| MD5, SHA-1, SHA-512 | Implemented in one pass, desktop and CLI | Known-vector and cancellation tests pass |
| Drive backup to DD/fixed VHD | Implemented, read-only source and protected output | Physical drive capture; file round trips and QEMU interop pass |
| Drive backup to VHDX/FFU | Pending | Format-specific creation and interoperability |
| Windows UEFI/FAT32 install media | Implemented; OVMF firmware fixture boots | Real Windows FAT32 boot and hardware validation |
| Windows BIOS media | Implemented with upstream boot code, FAT32/NTFS, desktop and CLI | Actual Windows BIOS boot and hardware validation |
| UEFI:NTFS | Implemented with bundled driver; firmware fixture and actual Windows 11 Setup boot pass | Secure Boot and physical hardware validation |
| Windows UDF installation ISOs | Implemented with 7-Zip; actual Windows 11 UDF bridge tested | Additional ISO versions and malformed-UDF coverage |
| Windows hardware-check bypass | WinPE answer-file injection implemented and tested with wimlib | Actual Windows Setup validation |
| Local account, privacy, locale, encryption options | Implemented in desktop and CLI; safe answer-file staging tested | Actual OOBE validation; automatic Linux-to-Windows regional mapping |
| Remaining Windows setup/QoL options | Pending | Installer edition selection, 2023 bootloaders, policy changes, silent installation |
| Windows To Go | Native NTFS WIM/ESD deployment, edition/CPU selection, isolated Microsoft WinPE tools; desktop/CLI; real Windows 11 BIOS/UEFI USB startup and live portable-policy checks pass | Additional releases/layouts, ARM64/32-bit Windows, full OOBE and physical USB validation |
| FreeDOS | Implemented, desktop/CLI; SeaBIOS boots FAT16 and FAT32 through COMMAND.COM | Physical hardware and regional keyboard configuration |
| MS-DOS | Pending | Supported source extraction and boot tests |
| GRUB ISO extraction mode | Implemented FAT32 for conventional Casper/Debian menus; x86 BIOS/UEFI, ARM64 UEFI, ARM32 UEFI, optional persistence | Additional distributions, full ARM live-root persistence, Secure Boot and physical devices |
| Syslinux ISO extraction mode | Implemented for conventional Casper/Debian live entries; native Syslinux BIOS and GRUB UEFI; real kernel boots and DragonOS persistence reboots pass | Additional Syslinux-source distributions, dynamic/utility menus and Syslinux variants |
| Linux persistence | Implemented for Casper/DragonOS and Debian hybrid ISOs, desktop and CLI; real DragonOS write/reboot/read passes under BIOS and UEFI | Real Debian and additional distro reboots; Secure Boot and physical USB validation |
| Windows ISO and UEFI Shell download | Pending | Official-source catalog, cancellation, integrity and caching |
| UEFI runtime media validation | Pending | Validation loader integration and firmware tests |
| Secure Boot DBX/SBAT checks | Pending | PE hashing, signature/catalog parsing, revocation data and fixtures |
| Bad-block and fake-capacity tests | Pending | Explicitly destructive diagnostics and simulated faulty-media tests |
| 38-language interface | Pending | Reuse translation catalog, extraction, fallback and layout checks |
| Portable distribution | Implemented executable archive and .deb | Dependency diagnostics and distribution testing |

## Work sequence

1. Windows setup configuration, checksum parity, image containers, and drive backup.
2. Windows BIOS/UEFI:NTFS and FreeDOS boot media; boot-test infrastructure.
3. Linux ISO extraction and persistence.
4. Windows To Go and remaining container/deployment formats.
5. Downloads, media diagnostics, Secure Boot validation, translations, and final
   cross-feature/hardware validation.

Privileged integration tests are opt-in and use one explicitly elevated run.
Development checks must not produce repeated Polkit password dialogs. No
physical drive is erased by the default test suite.

The source checkout's ignored `build/tools` directory may contain unpacked Ubuntu
tools. The source launcher uses these without installing them into the OS;
`RUFUS_USE_LOCAL_TOOLS=0` disables this development convenience. They are not
shipped inside the portable archive.

Windows and FreeDOS now prepare disk images without elevated access and enter
the common disk writer only after successful construction. Actual Windows 11
Setup boot was validated with a private VM overlay, without altering the source
ISO or a physical drive. Full installation/OOBE validation remains outstanding.
Windows To Go has a separate NTFS deployment and isolated WinPE boot/policy
configuration path. See [WINDOWS_TO_GO.md](WINDOWS_TO_GO.md) for dependencies,
CPU/layout selection, transaction boundaries, and validation limits.

Linux persistence uses hybrid ISO remastering with the source boot equipment
retained. Casper/DragonOS uses ext4 labeled `writable` (or `casper-rw` for older
images); Debian uses `persistence` with `/ union` in `persistence.conf`. All
preparation completes before entering the common disk writer. DragonOS's actual
Casper root overlay passed marker-file write/reboot/read tests through BIOS and
UEFI in private VM overlays. Native FAT32 extraction now installs matching GRUB
loaders/modules and passes DragonOS persistence reboots under BIOS and UEFI.
ARM64 and ARM32 UEFI USB images also boot real Linux kernels in QEMU. Syslinux
extraction now rebuilds conventional Casper/Debian menus with a coherent native
Syslinux BIOS loader set. SeaBIOS/Syslinux and OVMF/GRUB both boot real Linux
kernels from one generated FAT32 USB image, including multiple initrds.
Actual DragonOS GRUB menus converted to native Syslinux BIOS media also passed
Casper marker-file write/reboot/read tests under both BIOS and UEFI. These VM
checks do not replace physical USB/firmware validation.

## Additional requested platforms

| Platform | Implementation | Remaining acceptance work |
| --- | --- | --- |
| ARM64/ARM32 UEFI media | Kernel-header detection, correct EFI loader names, matching GRUB data; real kernel boot tests pass | Full live-distribution persistence, Secure Boot and physical boards |
| Native ARM Linux host | Architecture-independent Python/GI, native capacity ioctl, ARM64 CI job added | Execute remote native ARM CI; native ARM32 GUI/storage hardware |
| Raspberry Pi/vendor board images | Complete compressed/raw/ZIP image restoration preserves boot layout; multi-partition round trips pass | Physical board and firmware combinations |
| Jetson Orin/Thor installer USB | Vendor ISO retained unchanged; NVIDIA overlay/repository and ARM64 loader checked, including embedded ESP | Real vendor ISO and physical target installations |
| Legacy Jetson SD media | Model/method restrictions, verified complete-image restoration | Actual developer-kit images, QSPI compatibility and physical boots |
| Jetson recovery/eMMC/NVMe/SD/USB | Direct local-BSP command review, USB identity checks, one privileged job, live/persistent logs and cancellation; optional SDK Manager handoff | Physical recovery flashing; real BSP/release/carrier/host compatibility matrix |

ARM media and model-specific Jetson workflows are documented in
[PLATFORMS.md](PLATFORMS.md). Generic ARM boot validation does not establish
Jetson firmware or Raspberry Pi boot compatibility.
Direct recovery operation and its validation limits are documented in
[JETSON_RECOVERY.md](JETSON_RECOVERY.md).
