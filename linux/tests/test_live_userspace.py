import gzip
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rufus_linux import core, layout, live
from live_smoke import archive, instrument_initrd
from live_userspace import verify_userspace


@unittest.skipUnless(shutil.which("unmkinitramfs"), "initramfs-tools-core is not installed")
class UserspaceInstrumentationTests(unittest.TestCase):
    def test_original_startup_hooks_remain_before_userspace_instrumentation(self):
        for kind in ("casper", "debian"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                initrd = root / "initrd"
                original = b'/scripts/init-bottom/vendor-hook "$@"\n[ -e /conf/param.conf ] && . /conf/param.conf\n'
                files = [(p, b"", stat.S_IFDIR | 0o755) for p in ("scripts", "scripts/init-bottom", "scripts/casper-bottom")]
                files += [("scripts/init-bottom/ORDER", original, stat.S_IFREG | 0o644),
                          ("scripts/casper-bottom/ORDER", b"vendor casper hooks\n", stat.S_IFREG | 0o644)]
                initrd.write_bytes(gzip.compress(archive(files)))
                original_size = initrd.stat().st_size
                instrument_initrd(initrd, root, kind, "abc123", "userspace")
                expanded = root / "expanded"
                expanded.mkdir()
                # The kernel unpacks every concatenated archive. cpio (and
                # some unmkinitramfs releases) stops at the first trailer.
                for data in (gzip.decompress(initrd.read_bytes()[:original_size]),
                             gzip.decompress(initrd.read_bytes()[original_size:])):
                    subprocess.run(["cpio", "-idmu", "--quiet", "--no-absolute-filenames"],
                                   input=data, cwd=expanded, check=True, capture_output=True)
                order = next(expanded.rglob("init-bottom/ORDER")).read_bytes()
                self.assertTrue(order.startswith(original))
                self.assertEqual(next(expanded.rglob("casper-bottom/ORDER")).read_bytes(), b"vendor casper hooks\n")
                session = next(expanded.rglob("live-session-check.sh")).read_text()
                self.assertIn("abc123", session)
                self.assertNotIn("@NONCE@", session)
                for path in expanded.rglob("*.sh"):
                    subprocess.run(["sh", "-n", str(path)], check=True)


class UserspaceEvidenceTests(unittest.TestCase):
    def evidence(self, *, uid=1000, upper="/rw", shutdown=True, session_class="user", session_type="tty"):
        value = (f"RUFUS_USER_SESSION user {uid} /home/user c2\n"
                 f"RUFUS_USERSPACE_UPPER {upper}\nRUFUS_USERSPACE_STATE running\n"
                 f"Class={session_class}\nType={session_type}\n")
        return (value + ("[ 50.0] reboot: Power down\n" if shutdown else "")).encode()

    def test_missing_session_root_user_and_forced_exit_cannot_pass(self):
        cases = [b"RUFUS_USERSPACE_FIRST_BOOT_OK\n", self.evidence(uid=0),
                 self.evidence(upper="/rw/../other"), self.evidence(shutdown=False),
                 self.evidence(session_class="background"), self.evidence(session_type="unspecified"),
                 self.evidence() + b"RUFUS_USER_SESSION other 1001 /home/other c3\n"]
        for evidence in cases:
            with self.subTest(evidence=evidence), patch("live_userspace.core.run_tool") as run:
                with self.assertRaises(AssertionError):
                    verify_userspace(evidence, Path("unused"), Path("unused"), "abc123", "qemu-img")
                run.assert_not_called()

    @unittest.skipUnless(all(shutil.which(t) for t in ("qemu-img", "mkfs.ext4", "debugfs", "sfdisk")),
                         "offline filesystem tools are not installed")
    def test_offline_ext4_verification_requires_exact_content_and_user_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tree = root / "tree/rw/home/user"
            tree.mkdir(parents=True)
            marker = tree / "rufus-persistence-abc123"
            marker.write_text("abc123\n")
            volume = root / "store.ext4"
            with volume.open("xb") as target:
                target.truncate(16 * live.MIB)
            core.run_tool(["mkfs.ext4", "-F", "-q", "-d", str(root / "tree"), str(volume)])
            marker_path = "/rw/home/user/rufus-persistence-abc123"
            for uid, expected in ((1000, True), (0, False)):
                core.run_tool(["debugfs", "-w", "-R", f"set_inode_field {marker_path} uid {uid}", str(volume)])
                disk = root / f"disk-{uid}.img"
                with disk.open("xb") as target:
                    target.truncate(20 * live.MIB)
                    # Hybrid remastering can mark both ISO and persistence as
                    # generic data; only the latter has an ext superblock.
                    layout.write_layout(target.fileno(), 20 * live.MIB,
                                        [layout.Partition(2048, 2048),
                                         layout.Partition(6144, 16 * live.MIB // 512)])
                    target.seek(3 * live.MIB)
                    with volume.open("rb") as source:
                        shutil.copyfileobj(source, target)
                overlay = root / f"overlay-{uid}.qcow2"
                core.run_tool(["qemu-img", "create", "-f", "qcow2", "-F", "raw", "-b", str(disk), str(overlay)])
                if expected:
                    console = self.evidence().replace(b"\n", b"\r\r\n").replace(b"RUFUS", b"\x1b[0m\x1b]104\x07RUFUS")
                    result = verify_userspace(console, overlay, root, "abc123", "qemu-img")
                    self.assertTrue(result["offline_marker_verified"])
                    with self.assertRaisesRegex(AssertionError, "content/ownership"):
                        verify_userspace(self.evidence(), overlay, root, "wrongnonce", "qemu-img")
                else:
                    with self.assertRaisesRegex(AssertionError, "content/ownership"):
                        verify_userspace(self.evidence(), overlay, root, "abc123", "qemu-img")
