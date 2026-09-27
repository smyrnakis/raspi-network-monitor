import tempfile
import unittest
from pathlib import Path

from home_internet_monitor.monitor.ops import backup_database, check_database
from home_internet_monitor.storage import connect_database, migrate


class OperationsTests(unittest.TestCase):
    def test_check_and_online_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "monitor.db"
            backup = Path(directory) / "backups" / "monitor.db"
            connection = connect_database(source)
            migrate(connection)
            connection.execute(
                """
                INSERT INTO sites(
                    site_id, display_name, timezone, created_at_ms, updated_at_ms
                ) VALUES ('home', 'Home', 'UTC', 0, 0)
                """
            )
            connection.commit()

            source_result = check_database(source)
            backup_result = backup_database(source, backup)
            connection.close()

            self.assertEqual("ok", source_result["quick_check"])
            self.assertEqual("ok", backup_result["quick_check"])
            self.assertTrue(backup.exists())

    def test_backup_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "monitor.db"
            backup = Path(directory) / "backup.db"
            connection = connect_database(source)
            migrate(connection)
            connection.close()
            backup.write_bytes(b"existing")

            with self.assertRaises(FileExistsError):
                backup_database(source, backup)


if __name__ == "__main__":
    unittest.main()
