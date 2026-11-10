# Jetson recovery without SDK Manager

The desktop's **NVIDIA Jetson → Direct BSP recovery** option and CLI
`jetson-flash` run NVIDIA's `flash.sh` or `l4t_initrd_flash.sh` from a local
Linux_for_Tegra BSP. SDK Manager is optional. Rufus supplies review, target
selection, authorization, cancellation, and logs; NVIDIA's tools perform board
identification, image generation, firmware handling, and installation.

## Prepare the BSP and host

Download the NVIDIA Jetson Linux BSP and matching sample root filesystem for
the exact module, carrier board, and JetPack release. Follow that release's
instructions to populate `rootfs` and run `apply_binaries.sh`. A third-party
carrier may require its manufacturer's BSP and board profile. Keep the complete
prepared directory on a Linux filesystem with sufficient free space. Paths
containing spaces or shell metacharacters are refused because vendor scripts
construct shell commands internally.

Use a host OS supported by that BSP release and install its prerequisites
following NVIDIA's instructions. Initrd flashing uses USB networking, SSH, NFS,
and IPv6. Rufus checks for several required commands but does not establish full
host compatibility or install packages/change host networking during preview.
The vendor command can configure host services during execution.

The Rufus application runs on x86 and ARM Linux hosts. The selected **BSP host
binaries must also support the host CPU**. For example, the inspected R39.2.1
AArch64-target BSP contains static Intel i386 recovery executables; these are
accepted on x86-64 and rejected on ARM hosts. An AArch64 target filename does
not imply ARM-host compatibility. No transparent cross-CPU emulation is enabled.

Read-only inspection:

```sh
./rufus-linux jetson-bsp /path/to/Linux_for_Tegra
./rufus-linux jetson-devices
```

`jetson-bsp` lists the release, available `.conf` profiles, rootfs preparation
markers, and host ELF architectures. Inspection never sources a configuration
or executes a BSP script. Rootfs markers must match the BSP release; this check
does not prove a complete rootfs installation.

## Review and execute

Connect the Jetson's recovery USB port and enter Force Recovery using its board
instructions. Select the recovery port explicitly, even if only one is present.
USB product IDs identify a family and candidate module; they cannot establish
the exact carrier, module SKU, or firmware compatibility. NVIDIA's EEPROM/SKU
checks remain enabled. Rufus does not pass guessed BOARDID/BOARDSKU values or
inherit environment flags that bypass those checks.

In the desktop, select the BSP folder, click **Inspect / scan**, select the
exact board profile and recovery device, and click **Review BSP plan**. Custom
carrier profiles can be entered by name if they exist in that BSP. The review
shows the command, USB identity, storage/firmware scope, and blocking conditions.
An offline plan is available with no recovery port selected, but cannot run.

The CLI also defaults to preview only:

```sh
./rufus-linux jetson-flash --bsp /path/to/Linux_for_Tegra \
    --model orin-nano --storage nvme --board jetson-orin-nano-devkit \
    --usb-port 1-2.4
```

Execution requires `can_execute: true`, the exact `confirmation` token from that
review, and a new log filename. Repeat the same options and add:

```sh
# DESTRUCTIVE: use the actual token printed by your reviewed plan.
./rufus-linux jetson-flash --bsp /path/to/Linux_for_Tegra \
    --model orin-nano --storage nvme --board jetson-orin-nano-devkit \
    --usb-port 1-2.4 --execute --confirm '1-2.4 ACTUAL_PLAN_TOKEN' \
    --log /tmp/new-jetson-flash.jsonl
```

The desktop uses one `pkexec` request per job. An interactive CLI uses `sudo`;
otherwise it uses `pkexec`. Run the desktop as your regular user. Logs and the
UI stay unprivileged. Authorizing the job allows the selected BSP's code to run
as root, so use a trusted vendor BSP. The privileged helper reconstructs the
command, rechecks the reviewed options/BSP fingerprint and recovery connection,
and acquires a host-wide job lock before starting NVIDIA's script. A disconnect,
replacement, or change to reviewed inputs requires a new review. The fingerprint
covers selected configuration, scripts, host tools, kernel, XML, and release
markers; it is not a snapshot or signature check of the entire rootfs/BSP.

`--action probe` plans a privileged `flash.sh --read-info` job when that BSP
provides it. It reads chip/board/EEPROM information without requesting persistent
flashing, and still requires review and execution confirmation. Preview alone
does not contact the recovery transport or run even a vendor `--no-flash` job.

## Storage and firmware

| Target | Command selection |
| --- | --- |
| Nano/Xavier NX module SD, legacy eMMC | Matching board profile with `flash.sh`; R32 uses `mmcblk0p1` |
| Modern internal eMMC | `flash.sh` or the release's initrd tool (R39 Orin uses initrd) |
| NVMe, USB, Orin Nano SD, AGX Orin SD | Initrd external-storage workflow, with a BSP XML layout |
| Thor | Initrd workflow and Thor partition layout |

Both the required script and layout must be present in the supplied BSP.
Unsupported legacy external-storage workflows fail with a prerequisite error;
Rufus does not substitute a command that only prepares external boot pointers.
In particular, `flash.sh ... nvme0n1p1` alone does not populate an NVMe rootfs.

External targets default to `nvme0n1p1`, `sda1`, or `mmcblk1p1` **on the Jetson**.
Use `--external-device` for a different Jetson slot. No host `/dev` path or
NVIDIA `--direct` host-disk option is accepted. Select `--layout` with a relative
BSP XML path when the default layout does not fit the target's type/capacity.
The XML governs partition sizes, so review it against the actual storage.

By default, external installation also updates internal boot firmware/partitions
as configured by the board BSP. Orin Nano/NX include the matching QSPI layout.
Other families retain their BSP's internal configuration. `--skip-firmware`
uses `--external-only` and requires already compatible boot firmware. It applies
only to external-storage installation. Stock Nano/Xavier NX SD and eMMC module
profiles are distinguished; selecting eMMC with an SD-module profile is refused.
Industrial, Super, alternate-carrier, and production-module profiles must be
selected explicitly from the compatible BSP.

## Logs, cancellation, and validation limits

The desktop displays a bounded live log and writes the complete JSON-lines log
under the user's state directory (`rufus-linux/jetson-*.jsonl`). CLI execution
requires a new `--log` file. Existing logs are protected. Logs include the plan,
vendor output, and completion/error events. Success means NVIDIA's command
returned successfully; it does not verify the subsequent Jetson boot.

Cancel/Control-C requests termination of the vendor process group, with forced
termination if necessary. A cancelled or failed flash can leave the Jetson
unbootable until a complete recovery flash; re-enter Force Recovery before
retrying. Background host services configured by the BSP follow NVIDIA's own
lifecycle and troubleshooting procedures.

This version covers ordinary recovery installation. Fuse programming, signing
key management for fused boards, encrypted/A/B layouts, massflash, OTA, and
post-install SDK packages require the appropriate vendor workflow. Selecting a
listed Jetson family does not certify every carrier, SKU, storage, or release.

Validation includes synthetic BSP plans across listed families, USB reconnect
and changed-input rejection, mocked authorization/helper jobs, streaming logs,
process-group cancellation, and GTK offline review/cancellation. The actual
R39.2.1 BSP was inspected and planned without executing vendor code. No physical
Jetson was flashed, and native ARM-host execution of NVIDIA tools is unvalidated.

See NVIDIA's [R39 flashing guide](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupport.html),
[Thor flashing guide](https://docs.nvidia.com/jetson/archives/r39.2.1/DeveloperGuide/SD/FlashingSupportJetsonThor.html),
and [legacy quick start](https://docs.nvidia.com/jetson/l4t/Tegra%20Linux%20Driver%20Package%20Development%20Guide/quick_start.html).
