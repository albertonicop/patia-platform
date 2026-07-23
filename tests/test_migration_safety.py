from pathlib import Path
import tempfile
import unittest

from tests.migration_safety import (
    LOCAL_DATABASE,
    safe_temporary_database_url,
)


class MigrationSafetyTests(unittest.TestCase):
    def test_local_instance_database_is_always_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "instance/tiendaia.db"):
            safe_temporary_database_url(LOCAL_DATABASE)

    def test_non_temporary_database_is_rejected(self):
        unsafe = Path(__file__).resolve().parents[1] / "migration-check.db"
        with self.assertRaisesRegex(RuntimeError, "system temp"):
            safe_temporary_database_url(unsafe)

    def test_unidentified_temp_path_is_rejected(self):
        unsafe = Path(tempfile.gettempdir(), "migration-check.db")
        with self.assertRaisesRegex(RuntimeError, "patia-"):
            safe_temporary_database_url(unsafe)

    def test_identified_temporary_database_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="patia-safe-migration-") as root:
            path = Path(root, "isolated.db")
            self.assertEqual(
                safe_temporary_database_url(path),
                f"sqlite:///{path.resolve().as_posix()}",
            )


if __name__ == "__main__":
    unittest.main()
