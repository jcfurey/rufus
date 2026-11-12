# Windows installation and first-login acceptance

`tests/windows_setup_smoke.py` exercises real Windows installation or a fresh
Windows To Go image in disposable QEMU disks. These opt-in tests require local
Windows media; the ordinary automated suite uses synthetic fixtures.

## Run the tests

Use a complete extracted x64 Windows installation tree, including the Setup
executables and supporting files as well as `boot.wim` and `install.wim`.
Select an edition index from that media. Every output directory must be new.

```sh
make -C linux windows-setup-test \
    WINDOWS_TREE=/path/to/complete/windows-tree WINDOWS_INDEX=6 \
    WINDOWS_SETUP_OUTPUT=/path/to/new/uefi-test \
    WINDOWS_SETUP_FIRMWARE=uefi

make -C linux windows-setup-test \
    WINDOWS_TREE=/path/to/complete/windows-tree WINDOWS_INDEX=6 \
    WINDOWS_SETUP_OUTPUT=/path/to/new/bios-test \
    WINDOWS_SETUP_FIRMWARE=bios
```

The installer tests request hardware-check bypass, an offline local account
named `rufus-test`, privacy settings, automatic device-encryption prevention,
`en-US`, and `Central Standard Time`. They use the production NTFS media builder.
Test-only answer-file additions select the edition, partition a newly created
64-GiB virtual disk, and launch an observer at first login. The production
account, settings and password-change commands are retained.

For Windows To Go, first create a fresh image with those same options, omitting
the installer hardware bypass. See [WINDOWS_TO_GO.md](WINDOWS_TO_GO.md) for
deployment requirements.

```sh
linux/dist/rufus-linux windows-to-go /path/to/windows.iso \
    --index 6 --firmware both --size-gib 64 \
    --output /path/to/fresh-portable-windows.img \
    --offline-account --local-account rufus-test --privacy \
    --disable-device-encryption --locale en-US --timezone 'Central Standard Time'

make -C linux windows-togo-oobe-test \
    WTG_IMAGE=/path/to/fresh-portable-windows.img \
    WTG_OOBE_OUTPUT=/path/to/new/togo-uefi-test WTG_FIRMWARE=uefi
```

Use a separate output directory and `WTG_FIRMWARE=bios` for the BIOS case.
The To Go observer is added to a private copy; the prepared source remains
unchanged. An image already booted through OOBE cannot establish fresh setup.

QEMU requires local Unix sockets for QMP. Install the native media-building
dependencies plus QEMU, SeaBIOS and OVMF. Give `TMPDIR` and the output directory
ample disk space: installation retains several copies of the source media and
an expanding Windows disk; To Go instrumentation temporarily duplicates its
allocated NTFS data. Software emulation can take well over an hour. The timeout
defaults to 14400 seconds and is configurable with `WINDOWS_SETUP_TIMEOUT`.

Run the matching unsupported-hardware control with the same source and edition:

```sh
python3 linux/tests/windows_setup_smoke.py \
    --source-tree /path/to/complete/windows-tree --index 6 --firmware uefi \
    --control --output-dir /path/to/new/hardware-control
```

The control omits hardware bypass. Its WinPE observer waits for Windows' explicit
`HardBlock` records for TPM, RAM and Secure Boot, captures Setup logs and the
live `LabConfig` query, then shuts down. The host requires all three blocks and
the run nonce, rejects enabled bypass values, and verifies input hashes.
It records `hardware_control: blocked_as_expected`; this is a successful
negative control, not a completed installation. A missing failure or timeout
cannot count as a pass.

## Evidence and limits

The guest has no NIC, host shares, physical disks or USB passthrough. UEFI
installation and hardware controls use 3 GiB of RAM; BIOS installation uses
4 GiB. None has a TPM or Secure Boot. A writable private
overlay models the installer USB, preserving the prepared image underneath.
To Go boots its private disk as USB storage with 4 GiB of RAM.

The x64 acceptance VM uses TCG with an Intel family-6 identity and disabled
performance monitoring. The initial `-cpu max` fixture exposed an AMD family-15
identity. Its To Go crash dump identifies `RDPMC` at
`ntoskrnl.exe+0x4c7f61`, with bugcheck `0x3B` and exception `0xC0000096`.
QEMU 10.2.1's
[`helper_rdpmc`](https://github.com/qemu/qemu/blob/v10.2.1/target/i386/tcg/misc_helper.c)
is explicitly unimplemented. PMU was already disabled in that initial fixture;
the Intel identity tests Windows' architectural PMU enumeration path. The dump,
decoded instructions and module list remain under
`build/real-media/windows-togo-oobe-uefi-20261112-v1/post-eula-diagnostics`.

SeaBIOS tries only the first hard disk, so a blank target cannot be placed ahead
of the USB installer for the initial boot. The BIOS fixture starts with USB
first, then updates QEMU's firmware boot list for subsequent resets. This
changes neither the media nor the current BIOS drive mapping. The switch is
recorded in `bios-boot-switch.json`; see the
[QEMU boot-order limitation](https://www.qemu.org/docs/master/system/bootindex.html).

The observer runs through `FirstLogonCommands` in the actual user's session.
It waits for completed Setup/OOBE and Explorer, then records:

- The local account SID, enabled state, interactive session and Explorer owner.
- `IMAGE_STATE_COMPLETE` and cleared setup/OOBE-in-progress flags.
- A nonce written and read back in that user's profile.
- Actual language, regional and time-zone values.
- Advertising, tailored-experience and diagnostic-data settings.
- Offline-account and device-encryption-prevention registry values.
- Portable-system and SAN policies for To Go.

The checker requests shutdown after sending its report over the emulated
serial port. The host rejects missing, duplicate, stale or incomplete evidence,
and verifies the prepared/source input hashes before writing `result.json`.
The checker runs with the first local administrator's privileges; the profile
write is not an unprivileged-access test. Encryption prevention is a policy
check on hardware without a TPM, not proof of behavior on encryption-capable
physical machines. Privacy acceptance does not mean all telemetry is disabled.

`inputs.json`, `command.json`, answer files, serial output, QMP events and the
latest screenshot remain in the output directory. A screenshot of Setup, a
SYSTEM diagnostic console, or kernel startup cannot pass first-login validation.
Timeouts and guest errors are failures. Source recovery/provenance and any
Windows warnings must accompany reported acceptance results.

The optional `--resume --output-dir DIR` preserves an interrupted private VM's
disks. It refuses a directory that already contains a successful result. Manual
diagnostics sent through `keyboard.json` are retained in `keyboard-history.jsonl`;
they must be disclosed when interpreting a run.

## Fixture provenance for the November 2026 investigation

The cached fixture contains Windows 11 Pro x64 index 6, build 26300.9457, and
WinPE/Setup 26100.9457. The originally downloaded ISO is no longer present, so
this work does not establish a fresh vendor-download integrity check.

The complete 977-file installer tree was recovered from the existing prepared
image, whose SHA-256 still matches its earlier recorded value:
`9dc89629a98407faddab6442e01d9d5b6a78aa102705a20add1a7498c64a0752`.
Its customized WIMs were replaced with the cached, uncustomized WIMs:

| File | SHA-256 |
| --- | --- |
| `sources/boot.wim` | `208f81574e275185286d3dbd7c9c2e0d05e7ca74ea560c8b6cd56570b6eacfce` |
| `sources/install.wim` | `b534f0053accb9b919e7a54dd8af953e6b80bfbd85b79118ec362145890ba004` |

Recovery provenance is in ignored
`build/windows-installer-recovered-20261112/provenance.json`.
The earlier 69-file cache is sufficient for native To Go deployment, but lacks
the supporting Setup files needed for standard installer acceptance.

The investigation reproduced and fixed two installer defects: copied Linux
directory attributes cleared NTFS root metadata, and the UEFI:NTFS helper was
incorrectly declared as an ESP. The [README](README.md) records the independent
CHKDSK and partition-selection evidence. Full first-login results are recorded
only when the complete evidence checks pass.

The first hardware control, `build/real-media/windows-setup-hardware-control-20261112-v1`,
reports all three Windows `HardBlock` records and an absent live `LabConfig`
key. Source, prepared and instrumented-input hashes are unchanged. Its records
were collected through the diagnostic console after selecting Windows' built-in
no-product-key link; `diagnostic-method.json` and the keyboard history disclose
those steps. The positive UEFI and BIOS installers pass the same hardware check
with bypass enabled and proceed to installation. The control establishes the
hardware-check difference, independently of first-login acceptance.

The automatic control in
`build/real-media/windows-setup-hardware-control-20261112-v2` also passes: it
captures all three blocks, the absent live bypass key and unchanged input
hashes, then shuts down without keyboard input. Its first observer attempt
used tools absent from this minimal WinPE. The corrected observer uses
`find.exe` and Windows Script Host, launched through the `windowsPE`
`RunAsynchronous` setting. The retry preserves the production prepared image;
its previous instrumentation and hashes are retained for audit. Large private
duplicate images were discarded after verification, as listed in each run's
`discarded-private-images.json`.

The final control, `build/real-media/windows-setup-hardware-control-20261112-v3`,
passes with the Intel CPU identity used by the corrected acceptance fixture.
It automatically captures the same three blocks, absent live `LabConfig`, and
unchanged input hashes, without keyboard input. Its `result.json` records the
hardware configuration and evidence; the verified private image copies were
discarded afterward with a checksum manifest.

The UEFI To Go investigation logged package preregistration warnings with
`0x80080008`, which Microsoft identifies as
[`CO_E_SERVER_STOPPING`](https://learn.microsoft.com/en-us/windows/win32/com/com-error-codes-2).
Later packages registered successfully. The AppX operational log records
validation of 4668 Web Experience files between guest timestamps 19:11:21 and
19:25:28. Guest time-zone changes make these unsuitable as host UTC timestamps.
Those warnings and the long software-emulated setup phase are retained in
`build/real-media/windows-togo-oobe-uefi-20261112-v1/setup-appx-diagnostic.txt`
and the serial log. No app packages or security services were removed to speed
up the test. Setup progress alone does not establish OOBE completion.

The initial UEFI To Go user setup displayed `OOBEEULA`. Comparing the generated
answer file with upstream `src/wue.c` found that the Linux privacy option omitted
`HideEULAPage=true`. The generator now includes it alongside `ProtectYourPC=3`,
matching upstream. A new image was built by the production native deployment
and WinPE finalizer directly from the verified cached tree; this rebuild did
not repeat the public CLI's ISO-extraction wrapper. Its SHA-256 is
`76d212257591b1dd64bc45cbec69e49361790d76a75f3968642e8de975d2ff2c`.
Fresh first-login validation of that image is still pending. The earlier error
screen and subsequent kernel crash are retained as distinct observations;
neither is a successful OOBE result.

The standard UEFI case, `build/real-media/windows-setup-uefi-20261112-v4`,
completed WinPE installation with the original AMD CPU identity and automatically
booted the new target's Windows Boot Manager. It was stopped and resumed with
the Intel identity before first login; `inputs.json` records that change and
the earlier logs are archived in `winpe-amd-cpu-attempt`. Its installed disk and
production settings were retained. This is an interrupted installation test.

The standard BIOS case, `build/real-media/windows-setup-bios-20261112-v1`,
initially reset during image application with 3 GiB of RAM. The partial disk
and logs remain under `three-gib-reset-attempt`; the reset's cause is unresolved.
The retry uses a fresh blank target and 4 GiB. Both standard installers were
prepared before the `HideEULAPage` correction, while the fresh To Go `v2` cases
use the corrected answer file and Intel identity from their first boot.
