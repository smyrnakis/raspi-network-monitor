import tempfile
import unittest
import uuid
from pathlib import Path

from home_internet_monitor.monitor.runtime import new_process_id, read_boot_id


class RuntimeTests(unittest.TestCase):
    def test_reads_and_normalizes_linux_boot_id(self):
        expected = uuid.uuid4()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boot_id"
            path.write_text(f"{expected}\n", encoding="ascii")
            self.assertEqual(str(expected), read_boot_id(path))

    def test_invalid_boot_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boot_id"
            path.write_text("not-a-uuid", encoding="ascii")
            with self.assertRaisesRegex(RuntimeError, "boot ID is invalid"):
                read_boot_id(path)

    def test_process_identity_is_unique(self):
        self.assertNotEqual(new_process_id(), new_process_id())


if __name__ == "__main__":
    unittest.main()
