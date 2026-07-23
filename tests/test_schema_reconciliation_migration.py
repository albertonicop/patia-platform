import os
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import sqlalchemy as sa


ROOT = Path(__file__).resolve().parents[1]


CURRENT_USER_COLUMNS = {
    "id",
    "first_name",
    "last_name",
    "email",
    "password",
    "phone",
    "company_name",
    "address",
    "city",
    "state",
    "business_type",
    "postal_code",
    "email_verified",
    "verification_code",
    "verification_code_expires",
    "reset_token",
    "reset_token_expires",
    "session_token",
    "created_at",
    "plan",
    "manual_pro_access",
    "stripe_customer_id",
    "stripe_subscription_id",
    "subscription_status",
    "current_period_end",
    "next_payment_attempt",
    "stripe_subscription_updated_at",
    "stripe_invoice_updated_at",
    "cancel_at_period_end",
    "trial_warning_sent",
    "rfc",
    "tax_regime",
    "preferred_language",
    "timezone",
    "next_ticket_number",
}


CURRENT_WEBHOOK_COLUMNS = {
    "id",
    "stripe_event_id",
    "event_type",
    "object_id",
    "stripe_created_at",
    "completed_at",
    "failed_at",
    "status",
    "error_message",
}


class SchemaReconciliationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-schema-")

    def tearDown(self):
        self.temp_dir.cleanup()

    def database_path(self, name):
        return Path(self.temp_dir.name, name)

    def environment(self, database_path):
        env = os.environ.copy()
        env.update(
            DATABASE_URL=f"sqlite:///{database_path.as_posix()}",
            SECRET_KEY="schema-reconciliation-tests-only",
            STRIPE_DISABLED="1",
            PUBLIC_BASE_URL="http://127.0.0.1:5000",
            FLASK_DEBUG="0",
        )
        return env

    def run_upgrade(self, database_path, revision="head", *, expect_success=True):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "flask",
                "--app",
                "run.py",
                "db",
                "upgrade",
                revision,
            ],
            cwd=ROOT,
            env=self.environment(database_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if expect_success:
            self.assertEqual(
                result.returncode,
                0,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def run_downgrade(self, database_path, revision):
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "downgrade", revision],
            cwd=ROOT,
            env=self.environment(database_path),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def create_legacy_database_marked_at_02(
        self,
        database_path,
        *,
        include_created_at=True,
    ):
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        metadata = sa.MetaData()
        user_columns = [
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("email", sa.String(120), nullable=False),
            sa.Column("password", sa.String(255), nullable=False),
            sa.Column("company_name", sa.String(120), nullable=False),
        ]
        if include_created_at:
            user_columns.append(sa.Column("created_at", sa.DateTime(), nullable=False))
        user = sa.Table("user", metadata, *user_columns)
        product = sa.Table(
            "product",
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("sku", sa.String(64), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
        )
        sale = sa.Table(
            "sale",
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Float(), nullable=False),
            sa.Column("total", sa.Float(), nullable=False),
        )
        supplier = sa.Table(
            "supplier",
            metadata,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("name", sa.String(120), nullable=False),
        )
        alembic_version = sa.Table(
            "alembic_version",
            metadata,
            sa.Column("version_num", sa.String(32), nullable=False),
        )
        metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(
                user.insert(),
                [
                    {
                        "id": 1,
                        "email": "first@example.test",
                        "password": "existing-hash-1",
                        "company_name": "First Store",
                        **(
                            {"created_at": datetime(2026, 7, 1, 10, 0)}
                            if include_created_at
                            else {}
                        ),
                    },
                    {
                        "id": 2,
                        "email": "second@example.test",
                        "password": "existing-hash-2",
                        "company_name": "Second Store",
                        **(
                            {"created_at": datetime(2026, 7, 2, 11, 0)}
                            if include_created_at
                            else {}
                        ),
                    },
                ],
            )
            connection.execute(
                product.insert(),
                [{"id": 10, "user_id": 1, "sku": "LEGACY-1", "name": "Legacy"}],
            )
            connection.execute(
                sale.insert(),
                [
                    {
                        "id": 20,
                        "user_id": 1,
                        "product_id": 10,
                        "quantity": 2,
                        "unit_price": 15,
                        "total": 30,
                    }
                ],
            )
            connection.execute(
                supplier.insert(),
                [{"id": 30, "user_id": 1, "name": "Legacy Supplier"}],
            )
            connection.execute(
                alembic_version.insert(),
                [{"version_num": "20260715_02"}],
            )
        engine.dispose()

    def test_missing_created_at_stops_before_any_schema_or_data_change(self):
        database_path = self.database_path("missing-created-at.db")
        self.create_legacy_database_marked_at_02(
            database_path,
            include_created_at=False,
        )
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        before_tables = set(sa.inspect(engine).get_table_names())
        with engine.connect() as connection:
            before_users = connection.execute(
                sa.text('SELECT id, email, company_name FROM "user" ORDER BY id')
            ).all()
        engine.dispose()

        result = self.run_upgrade(database_path, expect_success=False)

        self.assertIn(
            "missing essential columns: created_at",
            result.stderr,
        )
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        inspector = sa.inspect(engine)
        self.assertEqual(set(inspector.get_table_names()), before_tables)
        self.assertNotIn("manual_pro_access", self.columns(inspector, "user"))
        self.assertNotIn("stripe_webhook_event", inspector.get_table_names())
        with engine.connect() as connection:
            self.assertEqual(
                connection.execute(
                    sa.text('SELECT id, email, company_name FROM "user" ORDER BY id')
                ).all(),
                before_users,
            )
            self.assertEqual(
                connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one(),
                "20260715_02",
            )
        engine.dispose()

    @staticmethod
    def columns(inspector, table_name):
        return {column["name"] for column in inspector.get_columns(table_name)}

    def test_reconciles_schema_marked_at_02_and_preserves_all_business_data(self):
        database_path = self.database_path("legacy.db")
        self.create_legacy_database_marked_at_02(database_path)
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")

        before = {}
        with engine.connect() as connection:
            for table_name in ("user", "product", "sale", "supplier"):
                before[table_name] = connection.execute(
                    sa.text(f'SELECT COUNT(*) FROM "{table_name}"')
                ).scalar_one()
        inspector = sa.inspect(engine)
        self.assertNotIn("manual_pro_access", self.columns(inspector, "user"))
        self.assertNotIn("trial_warning_sent", self.columns(inspector, "user"))
        self.assertNotIn("ticket_id", self.columns(inspector, "sale"))
        self.assertNotIn("stripe_webhook_event", inspector.get_table_names())
        engine.dispose()

        self.run_upgrade(database_path)

        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        inspector = sa.inspect(engine)
        self.assertTrue(CURRENT_USER_COLUMNS.issubset(self.columns(inspector, "user")))
        self.assertIn("ticket_id", self.columns(inspector, "sale"))
        self.assertIn("payment_method", self.columns(inspector, "sale"))
        self.assertIn("sales_ticket_id", self.columns(inspector, "sale"))
        self.assertIn("unit_cost", self.columns(inspector, "sale"))
        self.assertIn("cost_is_estimated", self.columns(inspector, "sale"))
        self.assertEqual(
            {
                "id",
                "organization_id",
                "user_id",
                "number",
                "public_id",
                "payment_method",
                "created_at",
            },
            self.columns(inspector, "sales_ticket"),
        )
        self.assertIn("is_active", self.columns(inspector, "product"))
        self.assertIn("organization_id", self.columns(inspector, "product"))
        self.assertIn("organization_id", self.columns(inspector, "sale"))
        self.assertIn("organization_id", self.columns(inspector, "supplier"))
        self.assertIn("organization", inspector.get_table_names())
        self.assertIn("organization_member", inspector.get_table_names())
        self.assertIn("organization_invitation", inspector.get_table_names())
        self.assertEqual(
            self.columns(inspector, "stripe_webhook_event"),
            CURRENT_WEBHOOK_COLUMNS,
        )
        with engine.connect() as connection:
            self.assertEqual(
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one(),
                "20260722_12",
            )
            for table_name, expected_count in before.items():
                self.assertEqual(
                    connection.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table_name}"')
                    ).scalar_one(),
                    expected_count,
                )
            self.assertEqual(
                connection.execute(sa.text("SELECT COUNT(*) FROM organization")).scalar_one(),
                before["user"],
            )
            self.assertEqual(
                connection.execute(sa.text("SELECT COUNT(*) FROM organization_member")).scalar_one(),
                before["user"],
            )
            self.assertEqual(
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM product WHERE organization_id IS NULL")
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    sa.text("SELECT COUNT(*) FROM sale WHERE organization_id IS NULL")
                ).scalar_one(),
                0,
            )
            users = connection.execute(
                sa.text(
                    'SELECT id, email, company_name, timezone '
                    'FROM "user" ORDER BY id'
                )
            ).all()
            self.assertEqual(
                users,
                [
                    (
                        1,
                        "first@example.test",
                        "First Store",
                        "America/Mexico_City",
                    ),
                    (
                        2,
                        "second@example.test",
                        "Second Store",
                        "America/Mexico_City",
                    ),
                ],
            )
            migrated_sale = connection.execute(
                sa.text(
                    "SELECT sales_ticket_id, unit_cost, cost_is_estimated "
                    "FROM sale WHERE id = 20"
                )
            ).one()
            self.assertIsNotNone(migrated_sale.sales_ticket_id)
            self.assertIsNone(migrated_sale.unit_cost)
            self.assertEqual(migrated_sale.cost_is_estimated, 1)
            self.assertEqual(
                connection.execute(
                    sa.text(
                        "SELECT number FROM sales_ticket "
                        "WHERE user_id = 1"
                    )
                ).scalar_one(),
                1,
            )
        engine.dispose()

        # A normal second deployment runs `db upgrade` again. Alembic performs
        # no DDL because the database is already at head, and data remains intact.
        self.run_upgrade(database_path)
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        with engine.connect() as connection:
            for table_name, expected_count in before.items():
                self.assertEqual(
                    connection.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table_name}"')
                    ).scalar_one(),
                    expected_count,
                )
            self.assertEqual(
                connection.execute(sa.text("PRAGMA integrity_check")).scalar_one(),
                "ok",
            )
        engine.dispose()

    def test_empty_database_upgrades_from_zero_to_reconciliation_head(self):
        database_path = self.database_path("empty.db")

        self.run_upgrade(database_path)

        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        inspector = sa.inspect(engine)
        self.assertTrue(
            {
                "alembic_version",
                "user",
                "product",
                "sale",
                "sales_ticket",
                "supplier",
                "stripe_webhook_event",
                "inventory_restock_event",
            "organization",
            "organization_member",
            "organization_invitation",
            }.issubset(inspector.get_table_names())
        )
        self.assertTrue(CURRENT_USER_COLUMNS.issubset(self.columns(inspector, "user")))
        self.assertIn("ticket_id", self.columns(inspector, "sale"))
        self.assertIn("payment_method", self.columns(inspector, "sale"))
        self.assertIn("sales_ticket_id", self.columns(inspector, "sale"))
        self.assertIn("unit_cost", self.columns(inspector, "sale"))
        self.assertIn("cost_is_estimated", self.columns(inspector, "sale"))
        self.assertIn("is_active", self.columns(inspector, "product"))
        self.assertEqual(
            self.columns(inspector, "stripe_webhook_event"),
            CURRENT_WEBHOOK_COLUMNS,
        )
        with engine.connect() as connection:
            self.assertEqual(
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one(),
                "20260722_12",
            )
            self.assertEqual(
                connection.execute(sa.text("PRAGMA integrity_check")).scalar_one(),
                "ok",
            )
        engine.dispose()

    def test_payment_method_revision_downgrades_without_losing_sales(self):
        database_path = self.database_path("payment-downgrade.db")
        self.run_upgrade(database_path, "20260715_04")
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        with engine.begin() as connection:
            connection.execute(sa.text(
                'INSERT INTO "user" (id, email, password, company_name, created_at) '
                "VALUES (1, 'owner@example.test', 'hash', 'Store', '2026-07-15 00:00:00')"
            ))
            connection.execute(sa.text(
                'INSERT INTO product (id, user_id, sku, name, category, cost_price, sale_price, stock, min_stock, created_at) '
                "VALUES (1, 1, 'SKU', 'Product', 'General', 1, 2, 1, 1, '2026-07-15 00:00:00')"
            ))
            connection.execute(sa.text(
                "INSERT INTO sale (id, user_id, product_id, quantity, unit_price, total, created_at, payment_method) "
                "VALUES (1, 1, 1, 1, 2, 2, '2026-07-15 00:00:00', 'cash')"
            ))
        engine.dispose()

        self.run_downgrade(database_path, "20260715_03")

        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        inspector = sa.inspect(engine)
        self.assertNotIn("payment_method", self.columns(inspector, "sale"))
        with engine.connect() as connection:
            self.assertEqual(connection.execute(sa.text("SELECT COUNT(*) FROM sale")).scalar_one(), 1)
        engine.dispose()

    def test_decimal_revision_rounds_money_and_preserves_business_rows(self):
        database_path = self.database_path("decimal-existing.db")
        self.run_upgrade(database_path, "20260722_11")
        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        with engine.begin() as connection:
            connection.execute(sa.text(
                'INSERT INTO "user" (id, email, password, company_name, created_at) '
                "VALUES (1, 'decimal@example.test', 'hash', 'Decimal Store', "
                "'2026-07-22 00:00:00')"
            ))
            connection.execute(sa.text(
                "INSERT INTO organization "
                "(id, name, slug, owner_user_id, timezone, currency, is_active, "
                "created_at, updated_at) "
                "VALUES (1, 'Decimal Store', 'decimal-store', 1, "
                "'America/Mexico_City', 'MXN', 1, '2026-07-22 00:00:00', "
                "'2026-07-22 00:00:00')"
            ))
            connection.execute(sa.text(
                "INSERT INTO organization_member "
                "(id, organization_id, user_id, role, is_active, created_at, "
                "updated_at) VALUES (1, 1, 1, 'OWNER', 1, "
                "'2026-07-22 00:00:00', '2026-07-22 00:00:00')"
            ))
            connection.execute(sa.text(
                "INSERT INTO product "
                "(id, organization_id, user_id, sku, name, category, cost_price, "
                "sale_price, stock, min_stock, is_active, created_at) "
                "VALUES (1, 1, 1, 'DEC-1', 'Decimal Product', 'General', "
                "10.126, 20.125, 8, 1, 1, '2026-07-22 00:00:00')"
            ))
            connection.execute(sa.text(
                "INSERT INTO sale "
                "(id, organization_id, user_id, product_id, quantity, unit_price, "
                "total, unit_cost, cost_is_estimated, created_at) "
                "VALUES (1, 1, 1, 1, 3, 20.125, 60.375, 10.126, 0, "
                "'2026-07-22 00:00:00')"
            ))
        engine.dispose()

        self.run_upgrade(database_path)

        engine = sa.create_engine(f"sqlite:///{database_path.as_posix()}")
        inspector = sa.inspect(engine)
        for table_name, column_names in {
            "product": {"cost_price", "sale_price"},
            "sale": {"unit_price", "total", "unit_cost"},
        }.items():
            types = {
                column["name"]: column["type"]
                for column in inspector.get_columns(table_name)
            }
            for column_name in column_names:
                self.assertIsInstance(types[column_name], sa.Numeric)
                self.assertEqual(types[column_name].scale, 2)
        with engine.connect() as connection:
            self.assertEqual(
                f"{connection.execute(sa.text(
                    'SELECT cost_price FROM product WHERE id = 1'
                )).scalar_one():.2f}",
                "10.13",
            )
            self.assertEqual(
                f"{connection.execute(sa.text(
                    'SELECT total FROM sale WHERE id = 1'
                )).scalar_one():.2f}",
                "60.38",
            )
            for table_name in ("user", "product", "sale"):
                self.assertEqual(
                    connection.execute(
                        sa.text(f'SELECT COUNT(*) FROM "{table_name}"')
                    ).scalar_one(),
                    1,
                )
            self.assertEqual(
                connection.execute(sa.text("PRAGMA integrity_check")).scalar_one(),
                "ok",
            )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
