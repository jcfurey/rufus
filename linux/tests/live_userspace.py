"""Test-only userspace instrumentation and independent persistence inspection."""

import json
from pathlib import Path
import re
import stat
import tempfile

from rufus_linux import core, live


def initrd_files(kind, nonce, console="ttyS0"):
    fixtures = Path(__file__).parent / "fixtures"
    def content(name):
        return (fixtures / name).read_text().replace("@KIND@", kind).replace("@NONCE@", nonce).replace("@CONSOLE@", console).encode()
    files = [("rufus-userspace", b"", stat.S_IFDIR | 0o755)]
    files += [("rufus-userspace/" + name, content(name), stat.S_IFREG | 0o755)
              for name in ("live-userspace-check.sh", "live-session-check.sh")]
    return content("live-userspace-install.sh"), files


def verify_userspace(data, overlay, root, nonce, qemu_img):
    """Read the user's file independently from the stopped guest's ext4 store."""
    # The guest terminal and its nested PTY can both expand LF to CRLF.
    console = data.decode(errors="replace").replace("\r", "")
    console = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", console)
    console = re.sub(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|P.*?\x1b\\)", "", console, flags=re.S)

    def field(pattern):
        matches = set(re.findall(pattern, console, re.M))
        if len(matches) != 1:
            raise AssertionError(f"Missing or ambiguous userspace evidence: {pattern}")
        return matches.pop()

    user, uid, home, session = field(r"^RUFUS_USER_SESSION ([a-z_][a-z0-9_-]*) (\d+) (/home/[a-z_][a-z0-9_-]*) ([a-zA-Z0-9_-]+)$")
    upper = field(r"^RUFUS_USERSPACE_UPPER (/[a-zA-Z0-9_/-]+)$")
    state = field(r"^RUFUS_USERSPACE_STATE (running|degraded)$")
    session_class = field(r"^Class=(user|user-light)$")
    session_type = field(r"^Type=(tty)$")
    if uid == "0" or ".." in upper.split("/"):
        raise AssertionError("Invalid user or upper directory in guest evidence.")
    if b"reboot: Power down" not in data:
        raise AssertionError("Guest did not finish an orderly shutdown.")
    marker = f"{upper}{home}/rufus-persistence-{nonce}"
    with tempfile.TemporaryDirectory(prefix="verify-userspace-", dir=root) as directory:
        raw, volume = Path(directory) / "disk.img", Path(directory) / "store.ext4"
        core.run_tool([qemu_img, "convert", "-f", "qcow2", "-O", "raw", str(overlay), str(raw)])
        table = json.loads(live.tool_text(["sfdisk", "--json", str(raw)]))["partitiontable"]
        sector = table.get("sectorsize", 512)
        candidates = []
        with raw.open("rb") as source:
            for part in table["partitions"]:
                # Hybrid remastering can label both ISO9660 and ext4 with the
                # generic GPT data type. Inspect filesystems, not type codes.
                source.seek(part["start"] * sector + 1024 + 56)
                if source.read(2) == b"\x53\xef":
                    candidates.append(part)
        if len(candidates) != 1:
            raise AssertionError("Expected exactly one ext persistence partition.")
        part = candidates[0]
        remaining = part["size"] * sector
        with raw.open("rb") as source, volume.open("xb") as target:
            source.seek(part["start"] * sector)
            while remaining:
                chunk = source.read(min(remaining, 4 * live.MIB))
                if not chunk:
                    raise AssertionError("Truncated persistence partition.")
                target.write(chunk)
                remaining -= len(chunk)
        value = live.tool_text(["debugfs", "-R", f"cat {marker}", str(volume)])
        metadata = live.tool_text(["debugfs", "-R", f"stat {marker}", str(volume)])
        if value != nonce + "\n" or not re.search(r"User:\s+" + re.escape(uid) + r"\s", metadata):
            raise AssertionError("User's marker content/ownership did not persist in ext4.")
    return {"user": user, "uid": int(uid), "home": home, "pam_session": session,
            "session_class": session_class, "session_type": session_type,
            "system_state": state, "ext4_marker": marker, "offline_marker_verified": True,
            "orderly_shutdown": True}
