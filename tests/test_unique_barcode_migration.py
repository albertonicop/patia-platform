import importlib.util
from pathlib import Path
import unittest

import sqlalchemy as sa


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260719_10_unique_company_barcode.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location(
        "unique_company_barcode_migration",
        MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UniqueCompanyBarcodeMigrationTests(unittest.TestCase):
    def make_engine(self):
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(sa.text(
                """
                CREATE TABLE product (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER,
                    sku VARCHAR(64) NOT NULL,
                    barcode VARCHAR(64)
                )
                """
            ))
            connection.execute(sa.text(
                "CREATE INDEX ix_product_user_barcode "
                "ON product (user_id, barcode)"
            ))
        return engine

    def run_upgrade(self, engine):
        migration = load_migration()
        from unittest.mock import patch

        with engine.begin() as connection, patch.object(
            migration.op,
            "get_bind",
            return_value=connection,
        ), patch.object(
            migration.op,
            "create_index",
            side_effect=lambda name, table, columns, unique=False: connection.execute(
                sa.text(
                    f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} "
                    f"ON {table} ({', '.join(columns)})"
                )
            ),
        ), patch.object(
            migration.op,
            "drop_index",
            side_effect=lambda name, table_name=None: connection.execute(
                sa.text(f"DROP INDEX {name}")
            ),
        ):
            migration.upgrade()

    def test_clean_data_gets_unique_company_barcode_index(self):
        engine = self.make_engine()
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO product (id, user_id, sku, barcode) "
                    "VALUES (1, 1, 'A', '001'), (2, 2, 'B', '001'), "
                    "(3, 1, 'C', NULL), (4, 1, 'D', NULL)"
                )
            )

        self.run_upgrade(engine)

        indexes = {
            index["name"]: index
            for index in sa.inspect(engine).get_indexes("product")
        }
        self.assertTrue(indexes["uq_product_user_barcode"]["unique"])
        self.assertNotIn("ix_product_user_barcode", indexes)
        with engine.begin() as connection:
            rows = connection.execute(
                sa.text("SELECT COUNT(*) FROM product")
            ).scalar_one()
        self.assertEqual(4, rows)

    def test_duplicates_stop_migration_without_changing_data_or_indexes(self):
        engine = self.make_engine()
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO product (id, user_id, sku, barcode) "
                    "VALUES (1, 1, 'A', 'DUP'), (2, 1, 'B', 'DUP')"
                )
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "user_id=1, barcode='DUP', rows=2",
        ):
            self.run_upgrade(engine)

        indexes = {
            index["name"] for index in sa.inspect(engine).get_indexes("product")
        }
        self.assertIn("ix_product_user_barcode", indexes)
        self.assertNotIn("uq_product_user_barcode", indexes)
        with engine.begin() as connection:
            rows = connection.execute(
                sa.text("SELECT COUNT(*) FROM product")
            ).scalar_one()
        self.assertEqual(2, rows)


if __name__ == "__main__":
    unittest.main()
