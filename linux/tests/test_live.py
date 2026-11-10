import hashlib
import contextlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from rufus_linux import core, live


class LiveConfigTests(unittest.TestCase):
    def test_casper_and_debian_options_precede_end_of_kernel_options(self):
        for boot, option, kind in (("casper", "persistent", "casper"), ("live", "persistence", "debian")):
            for command in ("linux /casper/vmlinuz", "linuxefi /live/vmlinuz", "  APPEND"):
                text = f"{command} boot={boot} quiet --- # live boot\n"
                result, kinds, count = live.rewrite_config(text)
                self.assertIn(f"boot={boot} {option} quiet ---", result)
                self.assertEqual((kinds, count), ({kind}, 1))
                self.assertEqual(live.rewrite_config(result)[0], result)

    def test_rescue_checks_comments_and_other_boot_commands_are_preserved(self):
        text = ("# linux /casper/vmlinuz boot=casper\n"
                "echo boot=casper\n"
                "linux /casper/vmlinuz boot=casper nopersistent\n"
                "linux /casper/vmlinuz boot=casper integrity-check\n"
                "append boot=live nopersistence\n")
        self.assertEqual(live.rewrite_config(text), (text, set(), 0))

    def test_continued_lines_and_existing_options(self):
        text = "linux /vmlinuz \\\n boot=live \\\n persistence quiet ---\n"
        result, kinds, count = live.rewrite_config(text)
        self.assertEqual((kinds, count), ({"debian"}, 1))
        self.assertEqual(result.count("persistence"), 1)

    def test_custom_persistence_store_is_refused(self):
        for option in ("persistent-path=/old", "persistence-label=old", "persistence-read-only=true"):
            with self.assertRaisesRegex(core.RufusError, "custom persistence"):
                live.rewrite_config(f"linux /vmlinuz boot=casper {option}\n")

    def test_only_modified_entries_in_manifests_are_updated(self):
        for algorithm in ("md5", "sha256"):
            old = "0" * (hashlib.new(algorithm).digest_size * 2)
            text = f"{old}  ./boot/grub/grub.cfg\n{old} *casper/filesystem.squashfs\n"
            changed = {"/boot/grub/grub.cfg": b"new config"}
            value = live.update_manifest(text, changed, algorithm)
            self.assertTrue(value.startswith(hashlib.new(algorithm, b"new config").hexdigest()))
            self.assertIn(f"{old} *casper/filesystem.squashfs\n", value)

    def test_stale_manifest_self_entry_is_removed(self):
        text = "0" * 32 + "  ./md5sum.txt\n" + "1" * 32 + "  ./casper/filesystem.squashfs\n"
        result = live.update_manifest(text, {}, "md5", exclude="/md5sum.txt")
        self.assertEqual(result, "1" * 32 + "  ./casper/filesystem.squashfs\n")


@unittest.skipUnless(all(shutil.which(t) for t in ("xorriso", "mkfs.ext4", "e2fsck", "sfdisk", "debugfs")),
                     "live image tools are not installed")
class LiveImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tree = self.root / "tree"
        (self.tree / "boot/grub").mkdir(parents=True)
        (self.tree / "casper").mkdir()
        (self.tree / "casper/filesystem.squashfs").write_bytes(b"live filesystem fixture; not bootable")
        self.efi = self.root / "efi.img"
        # A declared EFI image tests table/boot-catalog preservation. Firmware
        # execution is covered by the separate boot harness, not this stub.
        self.efi.write_bytes(b"EFI test stub".ljust(512 * 16, b"\0"))
        self.iso = self.root / "fixture.iso"
        self.output = self.root / "live.img"

    def fixture(self, kind="casper", extra="", bios=False):
        config = f"linux /casper/vmlinuz boot={kind} quiet ---\ninitrd /casper/initrd\n" + extra
        (self.tree / "boot/grub/grub.cfg").write_text(config)
        self.original_hash = hashlib.sha256(config.encode()).hexdigest()
        manifest = self.original_hash + "  ./boot/grub/grub.cfg\n"
        bios_args = []
        if bios:
            self.boot_file = self.tree / "boot/grub/eltorito.img"
            self.boot_file.write_bytes(b"GRUB boot image fixture".ljust(32768, b"\0"))
            manifest += hashlib.sha256(self.boot_file.read_bytes()).hexdigest() + "  ./boot/grub/eltorito.img\n"
            bios_args = ["-b", "boot/grub/eltorito.img", "-no-emul-boot", "-boot-load-size", "4",
                         "-boot-info-table", "-eltorito-alt-boot"]
        (self.tree / "SHA256SUMS").write_text(manifest)
        subprocess.run(["xorriso", "-as", "mkisofs", "-R", "-J", "-V", "RUFUS_LIVE_TEST", *bios_args,
                        "--modification-date=2026010100000000", "-partition_offset", "16",
                        "-append_partition", "2", "0xef", str(self.efi), "-appended_part_as_gpt",
                        "-e", "--interval:appended_partition_2:all::", "-no-emul-boot",
                        "-o", str(self.iso), str(self.tree)], check=True, capture_output=True)

    def extract(self, path):
        dest = self.root / "extracted"
        dest.unlink(missing_ok=True)
        core.run_tool(live.iso_command(self.output, "-osirrox", "on", "-extract", path, str(dest)))
        return dest.read_bytes()

    def persistence(self):
        table = json.loads(live.tool_text(["sfdisk", "--json", str(self.output)]))["partitiontable"]
        part = next(p for p in table["partitions"] if p["type"].lower() == live.GUID_TYPES["linux"])
        volume = self.root / "volume.ext4"
        with self.output.open("rb") as source, volume.open("wb") as target:
            source.seek(part["start"] * 512)
            target.write(source.read(part["size"] * 512))
        return volume

    def test_casper_partition_configs_manifest_and_boot_uuid(self):
        self.fixture()
        source_hash = core.checksum(self.iso)
        result = live.create_live_image(self.iso, self.output, 64 * live.MIB, expected=source_hash)
        self.assertEqual((result["kind"], result["persistence_label"]), ("casper", "writable"))
        self.assertEqual(result["firmware"], ["UEFI"])
        self.assertEqual(core.checksum(self.iso), source_hash)
        config = self.extract("/boot/grub/grub.cfg")
        self.assertIn(b"boot=casper persistent quiet ---", config)
        self.assertEqual(self.extract("/SHA256SUMS").split()[0].decode(), hashlib.sha256(config).hexdigest())
        self.assertEqual(self.extract("/casper/filesystem.squashfs"), b"live filesystem fixture; not bootable")
        with self.iso.open("rb") as source, self.output.open("rb") as output:
            source.seek(16 * 2048 + 813)
            output.seek(16 * 2048 + 813)
            self.assertEqual(source.read(17), output.read(17), "ISO UUID used by EFI GRUB must be preserved")
        volume = self.persistence()
        core.run_tool(["e2fsck", "-fn", str(volume)])
        with volume.open("rb") as source:
            source.seek(1024 + 120)
            self.assertEqual(source.read(16).rstrip(b"\0"), b"writable")

    def test_debian_union_configuration_and_gpt_at_full_capacity(self):
        self.fixture("live")
        capacity = 80 * live.MIB
        result = live.create_live_image(self.iso, self.output, 64 * live.MIB, capacity=capacity)
        self.assertEqual((result["kind"], result["persistence_label"]), ("debian", "persistence"))
        self.assertEqual(self.output.stat().st_size, capacity)
        with self.output.open("rb") as disk:
            disk.seek(512 + 32)
            self.assertEqual(struct.unpack("<Q", disk.read(8))[0], capacity // 512 - 1)
            disk.seek(-512, 2)
            self.assertEqual(disk.read(8), b"EFI PART")
        data = live.tool_text(["debugfs", "-R", "cat /persistence.conf", str(self.persistence())])
        self.assertEqual(data, "/ union\n")
        core.run_tool(["sfdisk", "--verify", str(self.output)])

    def test_cli_creates_legacy_casper_image_without_discovering_drives(self):
        from rufus_linux.cli import main
        self.fixture()
        with patch("rufus_linux.cli.discover_devices") as discover, \
             contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["linux", str(self.iso), "--output", str(self.output),
                                   "--persistence-mib", "64", "--casper-label", "casper-rw"]), 0)
            self.assertEqual(json.loads(output.getvalue())["persistence_label"], "casper-rw")
            discover.assert_not_called()
        with self.persistence().open("rb") as source:
            source.seek(1024 + 120)
            self.assertEqual(source.read(16).rstrip(b"\0"), b"casper-rw")

    def test_readback_failure_removes_partial_output(self):
        self.fixture()
        checksum = core.checksum
        def damaged(path, *args):
            if Path(path) == self.output:
                return "0" * 64
            return checksum(path, *args)
        with patch.object(live, "checksum", side_effect=damaged):
            with self.assertRaisesRegex(core.RufusError, "verification failed"):
                live.create_live_image(self.iso, self.output, 64 * live.MIB)
        self.assertFalse(self.output.exists())

    def test_duplicate_persistence_store_is_refused(self):
        self.fixture()
        live.create_live_image(self.iso, self.output, 64 * live.MIB)
        other = self.root / "second.img"
        with self.assertRaisesRegex(core.RufusError, "already contains a persistence volume"):
            live.create_live_image(self.output, other, 64 * live.MIB)
        self.assertFalse(other.exists())

    def test_boot_image_checksums_match_after_replay_patches_boot_info(self):
        self.fixture(bios=True)
        live.create_live_image(self.iso, self.output, 64 * live.MIB)
        boot_file = self.extract("/boot/grub/eltorito.img")
        manifest = self.extract("/SHA256SUMS").decode()
        self.assertIn(hashlib.sha256(boot_file).hexdigest() + "  ./boot/grub/eltorito.img\n", manifest)
        self.assertIn(hashlib.sha256(self.extract("/boot/grub/grub.cfg")).hexdigest() +
                      "  ./boot/grub/grub.cfg\n", manifest)

    def test_existing_output_and_symlink_are_protected(self):
        self.fixture()
        protected = self.root / "protected"
        protected.write_bytes(b"keep")
        self.output.symlink_to(protected)
        with self.assertRaisesRegex(core.RufusError, "already exists"):
            live.create_live_image(self.iso, self.output, 64 * live.MIB)
        self.assertEqual(protected.read_bytes(), b"keep")

    def test_checksum_capacity_size_and_mixed_distro_fail_without_output(self):
        self.fixture(extra="linux /live/vmlinuz boot=live\n")
        for kwargs in ({"expected": "0" * 64}, {"capacity": 64 * live.MIB},
                       {"persistence_size": 63 * live.MIB}, {}):
            with self.subTest(kwargs=kwargs), self.assertRaises(core.RufusError):
                live.create_live_image(self.iso, self.output, **{"persistence_size": 64 * live.MIB, **kwargs})
            self.assertFalse(self.output.exists())

    def test_cancellation_during_build_leaves_no_output(self):
        self.fixture()
        cancel = threading.Event()
        def progress(stage, *_):
            if stage == "Rebuilding hybrid live image":
                cancel.set()
        with self.assertRaises(core.Cancelled):
            live.create_live_image(self.iso, self.output, 64 * live.MIB, cancel=cancel, progress=progress)
        self.assertFalse(self.output.exists())

    def test_source_mutation_during_build_is_rejected(self):
        self.fixture()
        def progress(stage, *_):
            if stage == "Creating ext4 persistence volume":
                with self.iso.open("ab") as source:
                    source.write(b"changed")
        with self.assertRaisesRegex(core.RufusError, "changed during preparation"):
            live.create_live_image(self.iso, self.output, 64 * live.MIB, progress=progress)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
