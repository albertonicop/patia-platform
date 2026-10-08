"""Verify PostgreSQL startup and migration CLI without database connections."""
import unittest
from unittest.mock import patch

from app import create_app, db


class DatabaseDriverTests(unittest.TestCase):
    def test_postgresql_startup_uses_installed_driver_and_registers_db_command(self):
        for scheme in ("postgres", "postgresql", "postgresql+psycopg2"):
            with self.subTest(scheme=scheme), patch.dict(
                "os.environ",
                {
                    "DATABASE_URL": scheme + "://test:test@127.0.0.1:1/patia_driver_test",
                    "STRIPE_DISABLED": "true",
                    "SECRET_KEY": "database-driver-test",
                    "PATIA_AI_ENABLED": "false",
                },
            ), patch("psycopg2.connect", side_effect=AssertionError("Database connections forbidden")) as connect:
                app = create_app()
                with app.app_context():
                    self.assertEqual(db.engine.dialect.driver, "psycopg2")
                    self.assertEqual(db.engine.url.drivername, "postgresql+psycopg2")
                    result = app.test_cli_runner().invoke(args=["db", "upgrade", "--help"])
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertIn("upgrade", result.output)
                    db.engine.dispose()
                connect.assert_not_called()

    def test_sqlite_test_database_is_preserved(self):
        with patch.dict("os.environ", {"DATABASE_URL": "sqlite:///:memory:", "STRIPE_DISABLED": "true"}):
            app = create_app()
            with app.app_context():
                self.assertEqual(app.config["SQLALCHEMY_DATABASE_URI"], "sqlite:///:memory:")
                self.assertEqual(db.engine.dialect.name, "sqlite")
                db.engine.dispose()


if __name__ == "__main__":
    unittest.main()
