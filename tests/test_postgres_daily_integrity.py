"""Real concurrency and restore checks; require a verified disposable local cluster."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from unittest.mock import patch

import psycopg2
from psycopg2 import sql

from flask_migrate import upgrade

from app import create_app, db
from app.cash.services import expected_cash
from app.models import CashMovement, CashRegisterSession, InventoryMovement, Product, Sale, User
from app.team.services import ensure_owner_organization


@unittest.skipUnless(os.environ.get("PATIA_ISOLATED_PG_METADATA"), "Disposable PostgreSQL cluster not configured")
class PostgresDailyIntegrityTests(unittest.TestCase):
    def connect(self, database="postgres"):
        connection = psycopg2.connect(
            host="127.0.0.1", port=self.metadata["port"], user="patia_audit",
            dbname=database, connect_timeout=5,
        )
        # Validate the actual server before creating, restoring or dropping anything.
        with connection.cursor() as cursor:
            cursor.execute("SHOW data_directory")
            self.assertEqual(Path(cursor.fetchone()[0]).resolve(), self.data_directory)
        connection.rollback()
        return connection

    def new_database(self):
        name = "patia_daily_" + uuid.uuid4().hex + "_test"
        connection = self.connect()
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
            self.databases.append(name)
        finally:
            connection.close()
        return name

    def cleanup(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()
        connection = self.connect()
        try:
            connection.autocommit = True
            with connection.cursor() as cursor:
                for name in self.databases:
                    self.assertTrue(name.startswith("patia_daily_") and name.endswith("_test"))
                    cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        finally:
            connection.close()

    def setUp(self):
        self.metadata = json.loads(Path(os.environ["PATIA_ISOLATED_PG_METADATA"]).read_text())
        root = Path(self.metadata["root"]).resolve()
        self.assertTrue(root.name.startswith("patia-pg-isolated-"))
        self.data_directory = (root / "data").resolve()
        self.assertEqual(self.metadata["user"], "patia_audit")
        self.databases = []
        self.database = self.new_database()
        database_url = f"postgresql://patia_audit@127.0.0.1:{self.metadata['port']}/{self.database}"
        with patch.dict(os.environ, {
            "DATABASE_URL": database_url, "SECRET_KEY": "isolated-postgres-tests",
            "STRIPE_DISABLED": "1", "PUBLIC_BASE_URL": "https://patia.test",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
        self.addCleanup(self.cleanup)
        with self.app.app_context():
            # Use the production migration path; never weaken the create_all guard.
            upgrade(directory=str(Path(__file__).resolve().parents[1] / "migrations"))
            owner = User(email="pg-owner@patia.test", company_name="Isolated PG shop", email_verified=True)
            owner.set_password("FictitiousPassword123")
            db.session.add(owner)
            db.session.flush()
            member = ensure_owner_organization(owner)
            product = Product(
                organization_id=member.organization_id, user_id=owner.id,
                name="PG test product", sku="PG-1", cost_price=Decimal("4.00"),
                sale_price=Decimal("10.00"), stock=30, min_stock=1,
            )
            db.session.add(product)
            db.session.commit()
            self.owner_id, self.organization_id, self.product_id = owner.id, member.organization_id, product.id
        client = self.client()
        self.assertEqual(client.post("/cash-register/open", data={"opening_cash": "100.00"}).status_code, 302)
        response = client.post("/sell-cart", json={
            "request_id": str(uuid.uuid4()), "payment_method": "cash", "amount_received": "25.00",
            "items": [{"product_id": self.product_id, "quantity": 2}],
        })
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            self.sale_id = Sale.query.one().id

    def client(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.owner_id
            session["organization_id"] = self.organization_id
        return client

    def concurrent_reversal(self, first_action, second_action):
        import app.routes as routes

        entered, release = threading.Event(), threading.Event()
        original = routes.record_inventory_movement

        def hold_transaction(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2] in {"RETURN", "SALE_CANCELLATION"}:
                entered.set()
                if not release.wait(15):
                    raise AssertionError("Timed out holding the first reversal")
            return result

        def reverse(action):
            return self.client().post(f"/sales/{self.sale_id}/{action}").status_code

        with patch("app.routes.record_inventory_movement", side_effect=hold_transaction):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(reverse, first_action)
                try:
                    self.assertTrue(entered.wait(10), "First reversal did not reach the protected mutation")
                    second = executor.submit(reverse, second_action)
                    # Observe the database lock, rather than relying on thread timing.
                    deadline = time.monotonic() + 10
                    blocked = False
                    connection = self.connect()
                    try:
                        connection.autocommit = True
                        while time.monotonic() < deadline:
                            with connection.cursor() as cursor:
                                cursor.execute("""SELECT EXISTS (
                                    SELECT 1 FROM pg_stat_activity
                                    WHERE datname = %s AND wait_event_type = 'Lock'
                                    AND query ILIKE '%%FROM sale%%'
                                    AND query ILIKE '%%FOR UPDATE%%'
                                )""", (self.database,))
                                blocked = cursor.fetchone()[0]
                            if blocked:
                                break
                            time.sleep(0.05)
                    finally:
                        connection.close()
                    self.assertTrue(blocked, "Second reversal never waited on the sale lock")
                    self.assertFalse(second.done())
                finally:
                    release.set()
                self.assertEqual(sorted([first.result(timeout=10), second.result(timeout=10)]), [302, 404])
        with self.app.app_context():
            self.assertEqual(Sale.query.count(), 0)
            self.assertEqual(db.session.get(Product, self.product_id).stock, 30)
            self.assertEqual(CashMovement.query.filter_by(movement_type="SALE_CASH").count(), 1)
            refunds = CashMovement.query.filter_by(movement_type="REFUND").all()
            self.assertEqual(len(refunds), 1)
            self.assertEqual(refunds[0].amount, Decimal("20.00"))
            movements = InventoryMovement.query.filter(InventoryMovement.movement_type.in_(("RETURN", "SALE_CANCELLATION"))).all()
            self.assertEqual(len(movements), 1)
            self.assertEqual(movements[0].quantity_delta, 2)
            self.assertEqual(expected_cash(CashRegisterSession.query.one().id), Decimal("100.00"))

    def test_two_returns_reverse_stock_and_cash_once(self):
        self.concurrent_reversal("return", "return")

    def test_two_cancellations_reverse_stock_and_cash_once(self):
        self.concurrent_reversal("cancel", "cancel")

    def test_cancel_and_return_reverse_stock_and_cash_once(self):
        self.concurrent_reversal("cancel", "return")

    def fingerprint(self, database):
        connection = self.connect(database)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
                tables = [row[0] for row in cursor.fetchall()]
                result = {}
                for table in tables:
                    cursor.execute(sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(table)))
                    rows = sorted(json.dumps(row, default=str, sort_keys=True) for row in cursor.fetchall())
                    result[table] = (len(rows), hashlib.sha256(json.dumps(rows).encode()).hexdigest())
                cursor.execute("SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename")
                result["_sequences"] = cursor.fetchall()
                cursor.execute("SELECT count(*) FROM pg_constraint WHERE NOT convalidated")
                self.assertEqual(cursor.fetchone()[0], 0)
                return result
        finally:
            connection.close()

    def test_logical_backup_restores_all_rows_constraints_and_sequences(self):
        before = self.fingerprint(self.database)
        target = self.new_database()
        binaries = Path(self.metadata["bin"])
        with tempfile.TemporaryDirectory(prefix="patia-pg-backup-") as directory:
            backup = str(Path(directory) / "isolated.dump")
            common = ["--host=127.0.0.1", f"--port={self.metadata['port']}", "--username=patia_audit"]
            subprocess.run([str(binaries / "pg_dump.exe"), *common, "--format=custom", "--no-owner", "--no-acl", f"--file={backup}", self.database], check=True, capture_output=True, timeout=60)
            subprocess.run([str(binaries / "pg_restore.exe"), *common, "--exit-on-error", "--no-owner", "--no-acl", f"--dbname={target}", backup], check=True, capture_output=True, timeout=60)
        self.assertEqual(before, self.fingerprint(target))
        subprocess.run([str(binaries / "pg_amcheck.exe"), *common, f"--database={target}", "--install-missing", "--heapallindexed", "--parent-check"], check=True, capture_output=True, timeout=60)
