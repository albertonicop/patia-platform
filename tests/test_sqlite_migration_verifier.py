import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_sqlite_migrations import validation_environment
from tests.migration_safety import LOCAL_DATABASE


class SqliteMigrationVerifierTests(unittest.TestCase):
    def test_verifier_always_rejects_the_local_database(self):
        with self.assertRaisesRegex(RuntimeError, "instance/tiendaia.db"):
            validation_environment(LOCAL_DATABASE)

    def test_verifier_replaces_an_inherited_database_url(self):
        with tempfile.TemporaryDirectory(
            prefix="patia-safe-migration-"
        ) as root:
            database_path = Path(root, "isolated.db")
            with patch.dict(
                os.environ,
                {"DATABASE_URL": "sqlite:///instance/tiendaia.db"},
                clear=False,
            ):
                env = validation_environment(database_path)

        self.assertEqual(
            env["DATABASE_URL"],
            f"sqlite:///{database_path.resolve().as_posix()}",
        )
        self.assertNotIn("instance/tiendaia.db", env["DATABASE_URL"])

    def test_verifier_rejects_an_unidentified_temp_path(self):
        database_path = Path(tempfile.gettempdir(), "unsafe.db")
        with self.assertRaisesRegex(RuntimeError, "patia-"):
            validation_environment(database_path)


if __name__ == "__main__":
    unittest.main()
