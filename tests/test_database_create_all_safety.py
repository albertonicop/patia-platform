import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import inspect

from app import create_app, db
from app.database_safety import (
    UnsafeDatabaseTarget,
    assert_safe_ephemeral_database,
)


class CreateAllSafetyTestCase(unittest.TestCase):
    def setUp(self):
        self.project_root = Path(__file__).resolve().parents[1]
        self.persistent_database = (
            self.project_root / "instance" / "tiendaia.db"
        ).resolve()
        self.persistent_existed = self.persistent_database.exists()
        self.persistent_hash = (
            self._sha256(self.persistent_database)
            if self.persistent_existed
            else None
        )
        self.base_environment = {
            "SECRET_KEY": "create-all-safety-test",
            "STRIPE_DISABLED": "true",
            "PUBLIC_BASE_URL": "http://localhost",
        }

    @staticmethod
    def _sha256(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _app_with_database_url(self, database_url):
        environment = dict(self.base_environment)
        if database_url is not None:
            environment["DATABASE_URL"] = database_url
        with patch.dict(os.environ, environment, clear=True):
            app = create_app()
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        return app

    def tearDown(self):
        if self.persistent_existed:
            self.assertEqual(
                self.persistent_hash,
                self._sha256(self.persistent_database),
                "La prueba modificó instance/tiendaia.db.",
            )
        else:
            self.assertFalse(
                self.persistent_database.exists(),
                "La prueba creó instance/tiendaia.db.",
            )

    def test_explicit_temporary_database_allows_create_all(self):
        with tempfile.TemporaryDirectory(
            prefix="patia-create-all-safe-"
        ) as directory:
            database_path = (Path(directory) / "safe.db").resolve()
            database_url = f"sqlite:///{database_path.as_posix()}"
            with patch.dict(
                os.environ,
                self.base_environment | {"DATABASE_URL": database_url},
                clear=True,
            ):
                app = create_app()
                app.config.update(TESTING=True)
                with app.app_context():
                    self.assertEqual(
                        database_path,
                        assert_safe_ephemeral_database(app),
                    )
                    db.create_all()
                    self.assertIn("user", inspect(db.engine).get_table_names())
                    db.session.remove()
                    db.engine.dispose()

    def test_persistent_instance_database_is_rejected(self):
        app = self._app_with_database_url("sqlite:///tiendaia.db")
        with patch.dict(
            os.environ,
            self.base_environment | {"DATABASE_URL": "sqlite:///tiendaia.db"},
            clear=True,
        ):
            with app.app_context():
                with self.assertRaisesRegex(
                    UnsafeDatabaseTarget,
                    "rutas relativas son ambiguas",
                ):
                    db.create_all()

    def test_absolute_persistent_instance_database_is_rejected(self):
        database_url = f"sqlite:///{self.persistent_database.as_posix()}"
        app = self._app_with_database_url(database_url)
        with patch.dict(
            os.environ,
            self.base_environment | {"DATABASE_URL": database_url},
            clear=True,
        ):
            with app.app_context():
                with self.assertRaisesRegex(
                    UnsafeDatabaseTarget,
                    "instance/tiendaia.db",
                ):
                    db.create_all()

    def test_missing_database_url_is_rejected(self):
        app = self._app_with_database_url(None)
        with patch.dict(os.environ, self.base_environment, clear=True):
            with app.app_context():
                with self.assertRaisesRegex(
                    UnsafeDatabaseTarget,
                    "debe definirse explícitamente",
                ):
                    db.create_all()

    def test_empty_database_url_is_rejected_before_create_all(self):
        app = self._app_with_database_url(None)
        with patch.dict(
            os.environ,
            self.base_environment | {"DATABASE_URL": "   "},
            clear=True,
        ):
            with app.app_context():
                with self.assertRaisesRegex(
                    UnsafeDatabaseTarget,
                    "debe definirse explícitamente",
                ):
                    db.create_all()


if __name__ == "__main__":
    unittest.main()
