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
| FAT16, UDF | Implemented in desktop/CLI and file-image formatting; sector-aware preflight; x86/ARM32/ARM64 mount/write/reboot/read matrix passes | Physical devices, additional UDisks/releases and Windows/macOS interoperability |
| ReFS | Pending | Linux formatter/backend selection; ReFS write support |
| Raw/hybrid disk-image writing | Implemented | BIOS/UEFI boot fixtures and hardware testing |
| gzip, xz, bzip2 images | Implemented | Large-image and interrupted I/O cases |
| ZIP, LZMA, compress (.Z), Zstandard images | Implemented, integrity and round-trip tests | Large-image boundaries |
| Fixed/dynamic VHD restoration | Implemented natively; QEMU interoperability tested | Large-image and additional producer coverage |
| Standalone fixed/dynamic VHDX restoration | Native read-only disk view, 512-byte/4-KiB geometry, complete preflight; BIOS/UEFI and ARM32/ARM64 restored-image boots pass | Additional producers and large-file boundaries; physical 4Kn devices |
| FFU, VTSI, differencing VHD/VHDX and VHDX log replay | Pending | Parent-chain validation and recovery/conversion tests |
| SHA-256 | Implemented | Hardware validation of write/read-back |
| MD5, SHA-1, SHA-512 | Implemented in one pass, desktop and CLI | Known-vector and cancellation tests pass |
| Drive backup to DD/fixed VHD/fixed VHDX | Implemented, read-only source, VHDX sector geometry and protected output; native/file/QEMU round trips pass | Physical drive capture; additional producers and large-file boundaries |
| Drive backup to FFU | Pending | Format-specific creation and interoperability |
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
| GRUB ISO extraction mode | Implemented FAT32 for conventional Casper/Debian menus; real Debian/DragonOS persistence reboots under x86 BIOS/UEFI; ARM64/ARM32 UEFI kernel boots | Additional distributions, full ARM live-root persistence, Secure Boot and physical devices |
| Syslinux ISO extraction mode | Native Syslinux BIOS and GRUB UEFI; Debian static submenu scopes; real Debian/DragonOS persistence reboots pass | Additional Syslinux-source distributions, dynamic/utility menus and Syslinux variants |
| Linux persistence | Casper/DragonOS and Debian, desktop/CLI; full terminal userspace and user-file write/shutdown/reboot/read under BIOS/UEFI across hybrid/GRUB/Syslinux layouts | Graphical desktop interaction, additional distros, full ARM live-root reboots, Secure Boot and physical USB validation |
| Windows ISO and UEFI Shell download | Pending | Official-source catalog, cancellation, integrity and caching |
| UEFI runtime media validation | Pending | Validation loader integration and firmware tests |
| Secure Boot DBX/SBAT checks | Pending | PE hashing, signature/catalog parsing, revocation data and fixtures |
| Bad-block and fake-capacity tests | Pending | Explicitly destructive diagnostics and simulated faulty-media tests |
| 38-language interface | Pending | Reuse translation catalog, extraction, fallback and layout checks |
| Portable distribution | Implemented executable archive and .deb | Dependency diagnostics and distribution testing |

## Current work plan

Updated **2026-11-12**, following version **0.11.0**, commit `2ebb3352`.
Continue with software implementation and validation without physical storage
or target boards. Hardware acceptance remains a separate stage; pending
hardware checks do not prevent the work below.

### Established baseline

- 230 automated tests pass. Standalone and Debian packages build. The preceding
  version's GTK checks pass; the userspace-harness changes do not alter the GUI.
- Debian Live 13.7.0 passes twelve positive BIOS/UEFI USB VM boots across hybrid,
  extracted GRUB and extracted Syslinux persistence layouts. A control with
  persistence disabled correctly fails the live-root check.
- The later Debian and DragonOS userspace checkpoint passes all three layouts:
  24 positive BIOS/UEFI boots, terminal/PAM sessions as the live user, normal
  shutdown/reboot, and independent ext4 checks of user-file contents and owner.
  All boots report systemd `running`; all six disabled-persistence controls
  pass. Source/prepared/backing hashes are unchanged. Fixture-specific warnings
  are documented in [PERSISTENCE.md](PERSISTENCE.md).
- ARM32 and ARM64 media boot real kernels through UEFI and USB in emulation.
  FAT16/UDF mount/write/reboot/read tests also pass on x86-64, ARM32 and ARM64.
- The earlier DragonOS stall was caused by the harness's `ip=frommedia` option
  triggering late initramfs networking in a VM without a NIC. With that option
  removed, the original vendor customization completes and systemd reaches
  `running`. The userspace harness also handles Casper's UID-890 `user-light`
  terminal session and generic GPT data types on hybrid persistence partitions.

Kernel handoff, live-root persistence and full userspace startup are distinct
acceptance milestones. The Debian/DragonOS userspace matrix now reaches real
live-user terminal sessions and verifies home-file persistence. Graphical desktop
interaction and full ARM live-root persistence remain additional acceptance
work. See [PERSISTENCE.md](PERSISTENCE.md).

### Hardware-free priorities

The tested x86 **terminal startup and user-file persistence** milestone is
validated. The next priority is **ARM32/ARM64 live-root persistence**.
Graphical desktop interaction remains additional Linux startup coverage.
Other milestones remain open unless their acceptance evidence is recorded.

| Priority | Milestone | Work without physical hardware | Required acceptance evidence |
| --- | --- | --- | --- |
| 1 | Full Linux startup and user-session persistence — terminal matrix validated | Expand the completed Debian/DragonOS terminal checks to graphical desktop interaction and additional distributions. Retain vendor startup/shutdown diagnostics. | Recorded: 24 BIOS/UEFI userspace boots across hybrid/GRUB/Syslinux, real live-user terminal sessions, independent ext4 content/owner checks and six disabled-persistence controls. Graphical session acceptance remains open. |
| 2 | ARM32/ARM64 live-root persistence | Build or obtain matching kernel, userspace, initramfs and UEFI fixtures. Extend the existing ARM kernel tests to mount the real live root and persistence store. Execute native ARM host CI when a runner is available. | ARM32 and ARM64 UEFI live-root write/reboot/read checks, disabled-persistence controls, and recorded fixture provenance. Record native ARM host results separately from emulated guest results. |
| 3 | Windows Setup, OOBE and To Go coverage | Complete installations in private VM disks; check hardware-check bypass, account/privacy/locale/encryption options and remaining setup choices. Expand release/layout coverage, including x86 and ARM64 Windows targets when suitable OS/WinPE media are available. | Actual Setup/OOBE completion and resulting settings, plus boot and portable-policy checks for each tested edition/architecture/layout. ARM32/Windows RT remains unsupported. |
| 4 | Storage formats and diagnostics | Implement FFU/VTSI, differencing VHD/VHDX and VHDX log replay. Expand producer interoperability and large sparse-image boundaries. Exercise read/write failures, interruptions, permissions and fake-capacity detection with fault injection. | Independent producer/consumer checks, malformed/parent-chain/recovery fixtures, bounded-resource checks, cancellation and protected-output evidence. Simulated media faults must be labeled as simulations. |
| 5 | Secure Boot and UEFI media validation | Implement PE hashing/signature handling, DBX/SBAT checks and runtime media-validation integration. Exercise applicable signed boot components and enrolled test keys in virtual firmware. | Known signature/revocation fixtures and recorded firmware accept/reject results for each supported test configuration. Separate emulated firmware results from physical firmware acceptance. |
| 6 | SDK-Manager-free Jetson workflow coverage | Expand offline BSP/release/carrier-profile checks, storage command plans, vendor-image inspection and simulated recovery failures. Cover identity changes, cancellation, logs and helper failures. | Reproducible plan fixtures and failure-handling tests for supported model/storage combinations. Offline checks do not certify BSP/carrier compatibility or actual recovery flashing. |
| 7 | Desktop and distribution parity | Add official-source downloads with integrity, cancellation and caching; translations; accessibility and keyboard navigation; dependency diagnostics; and broader Linux distribution testing. | GTK interaction/accessibility coverage, locale/fallback/layout checks, download integrity/failure fixtures, and package/startup results on the tested distributions and native host architectures. |

ReFS still needs Linux backend feasibility research. MS-DOS needs supported
source media and extraction/boot validation. Additional formatter, container
producer and Windows/macOS interoperability checks can also use disposable
images or VMs where suitable tools and media are available.

### Physical-hardware acceptance

Keep these items open after software and VM milestones pass:

- Real USB controller/enclosure behavior, hotplug and disconnect races,
  stacked-storage protection, physical 4Kn drives and sustained large-drive I/O.
- Actual faulty/fake-capacity media, device-cache behavior and interruption or
  power-loss recovery. Fault-injection results cannot certify these behaviors.
- Booting prepared media on physical BIOS/UEFI systems, including real Secure
  Boot implementations and firmware-specific behavior.
- Raspberry Pi and other vendor-board boot/firmware combinations.
- Jetson recovery USB operation, QSPI/boot-firmware compatibility and real
  SD/NVMe/eMMC/USB installations across release/carrier/model combinations.
- Native ARM GUI/storage behavior on physical hosts, including ARM32, beyond
  the coverage obtainable from native CI and emulated guests.

Record the exact media, architecture, firmware, layout and validation stage for
each result. Keep source and prepared images unchanged by using private VM
overlays, and retain logs and checksums. Update the feature/platform tables only
when the corresponding acceptance evidence exists.

### Validation execution

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
Debian Live 13.7.0 now passes the same two-firmware persistence checks in hybrid,
native GRUB and native Syslinux layouts. Read-only ISO preparation, embedded EFI
partition preservation and safe FAT32 alias handling are covered. The Debian
hook runs after the original init-bottom scripts; Casper retains its earlier
root-mount checkpoint. See [PERSISTENCE.md](PERSISTENCE.md) for scope and limits.

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
