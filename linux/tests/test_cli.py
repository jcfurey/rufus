import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rufus_linux.cli import main


class CliTests(unittest.TestCase):
    def test_no_usb_devices_is_success(self):
        with patch("rufus_linux.cli.discover_devices", return_value=[]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["devices", "--json"]), 0)
            self.assertEqual(out.getvalue().strip(), "[]")

    def test_no_device_can_be_erased_without_exact_confirmation(self):
        with patch("rufus_linux.cli.discover_devices") as discover, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["write", "image.img", "--device", "/dev/sdz"]), 1)
            discover.assert_not_called()

    def test_write_file_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.img", Path(directory) / "target.img"
            source.write_bytes(b"Rufus Linux fixture" * 1234)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["write", str(source), "--output", str(target)]), 0)
            self.assertEqual(source.read_bytes(), target.read_bytes())


if __name__ == "__main__":
    unittest.main()
