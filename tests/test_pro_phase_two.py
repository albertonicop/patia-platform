import os
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from babel.support import Translations
from sqlalchemy import event as sqlalchemy_event

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "pro-phase-two-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_phase_two")
os.environ.setdefault("STRIPE_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_STARTER_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "price_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_phase_two")
os.environ.setdefault("PUBLIC_BASE_URL", "https://phase-two.test")

from app import create_app, db
from app.inventory.services import record_opening_balance
from app.models import (
    CashRegisterSession,
    Customer,
    CustomerCreditMovement,
    InventoryMovement,
    InventoryRestockEvent,
    MonthlyOwnerReport,
    OrganizationMember,
    Product,
    PurchaseOrder,
    PurchaseReceipt,
    Sale,
    Supplier,
    User,
)
from app.monthly_reports import (
    generate_monthly_report,
    report_snapshot,
    run_monthly_reports,
)
from app.pro.purchases import (
    confirm_purchase_order,
    create_purchase_draft,
    purchase_suggestions,
    receive_purchase_order,
)
from app.team.services import ensure_owner_organization


class ProPhaseTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="patia-pro-phase-two-"
        )
        path = Path(self.temp_dir.name, "phase-two.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.membership = self._owner(
            "owner-phase-two@example.com",
            pro=True,
        )
        self.organization = self.membership.organization
        self.organization.monthly_report_enabled = True
        self.organization.monthly_report_recipient = (
            "reports-phase-two@example.com"
        )
        self.supplier = Supplier(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            name="Distribuidora Uno",
        )
        db.session.add(self.supplier)
        self.product = Product(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            sku="PHASE-001",
            name="Agua de prueba",
            category="Bebidas",
            supplier=self.supplier.name,
            cost_price=Decimal("10.00"),
            sale_price=Decimal("18.00"),
            stock=1,
            min_stock=5,
        )
        db.session.add(self.product)
        db.session.flush()
        record_opening_balance(
            self.product,
            self.membership,
            reason="Test opening balance",
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def _owner(self, email, *, pro=False):
        user = User(
            email=email,
            company_name=f"Negocio {email}",
            email_verified=True,
            preferred_language="es",
            manual_pro_access=pro,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def _member(self, role):
        user = User(
            email=f"{role.lower()}-phase-two@example.com",
            company_name="Equipo Phase Two",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = OrganizationMember(
            organization_id=self.organization.id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
        return user, membership

    def _client(self, user, membership, *, language="es"):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["organization_id"] = membership.organization_id
            session["language"] = language
        return client

    def _sale(self, created_at, *, quantity=2, price="18.00"):
        sale = Sale(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            product_id=self.product.id,
            quantity=quantity,
            unit_price=Decimal(price),
            unit_cost=Decimal("10.00"),
            total=Decimal(price) * quantity,
            payment_method="cash",
            created_at=created_at,
        )
        db.session.add(sale)
        db.session.commit()
        return sale

    def test_hub_navigation_and_role_access(self):
        manager, manager_membership = self._member("MANAGER")
        cashier, cashier_membership = self._member("CASHIER")
        starter, starter_membership = self._owner(
            "starter-phase-two@example.com"
        )

        owner_response = self._client(
            self.owner, self.membership
        ).get("/pro/hub")
        self.assertEqual(owner_response.status_code, 200)
        html = owner_response.get_data(as_text=True)
        self.assertIn("Centro de decisiones", html)
        self.assertIn("/pro/monthly-reports", html)
        self.assertIn("/pro/purchases", html)
        self.assertIn("/pro/alerts", html)
        self.assertEqual(
            self._client(manager, manager_membership)
            .get("/pro/hub")
            .status_code,
            200,
        )
        self.assertEqual(
            self._client(cashier, cashier_membership)
            .get("/pro/hub")
            .status_code,
            403,
        )
        starter_response = self._client(
            starter, starter_membership
        ).get("/pro/hub")
        self.assertEqual(starter_response.status_code, 200)
        starter_html = starter_response.get_data(as_text=True)
        self.assertIn("Tu centro de decisiones", starter_html)
        self.assertIn("Actualizar a Pro", starter_html)

    def test_starter_can_preview_every_pro_entry_point(self):
        starter, membership = self._owner(
            "starter-previews@example.com"
        )
        client = self._client(starter, membership)
        for path, expected in (
            ("/pro", "Panel ejecutivo"),
            ("/pro/hub", "Tu centro de decisiones"),
            ("/pro/monthly-reports", "Reporte mensual"),
            ("/pro/purchases", "Compras inteligentes"),
            ("/pro/alerts", "Detecta lo importante"),
        ):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(expected, html)
                self.assertIn("Actualizar a Pro", html)

    def test_empty_pro_views_explain_the_next_step(self):
        owner, membership = self._owner(
            "empty-pro-phase-two@example.com", pro=True
        )
        client = self._client(owner, membership)
        cases = (
            (
                "/pro/monthly-reports",
                "Todavía no hay reportes mensuales",
            ),
            (
                "/pro/purchases",
                "No necesitas reponer mercancía ahora",
            ),
            (
                "/pro/alerts",
                "Tu negocio no presenta alertas importantes",
            ),
        )
        for path, expected in cases:
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    expected,
                    response.get_data(as_text=True),
                )

    def test_monthly_snapshot_is_immutable_and_pdf_is_valid(self):
        self._sale(datetime(2026, 6, 15, 18))
        client = self._client(self.owner, self.membership)
        response = client.post(
            "/pro/monthly-reports/generate",
            data={"period": "2026-06"},
        )
        self.assertEqual(response.status_code, 302)
        record = MonthlyOwnerReport.query.one()
        original_hash = record.snapshot_hash
        original = report_snapshot(record)
        self.assertEqual(original["language"], "es")
        self.assertTrue(record.manual_generation)
        self.assertEqual(
            record.generated_by_member_id, self.membership.id
        )

        self._sale(datetime(2026, 6, 20, 18), quantity=10)
        generate_monthly_report(
            self.organization.id,
            2026,
            6,
            send=False,
        )
        db.session.refresh(record)
        self.assertEqual(record.snapshot_hash, original_hash)
        self.assertEqual(report_snapshot(record), original)

        detail = client.get(
            f"/pro/monthly-reports/{record.id}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Snapshot protegido", detail.get_data(as_text=True))
        pdf = client.get(
            f"/pro/monthly-reports/{record.id}/pdf"
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b"%PDF"))

        record.snapshot_json = '{"changed":true}'
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        record = db.session.get(MonthlyOwnerReport, record.id)
        self.assertEqual(record.snapshot_hash, original_hash)

        record.snapshot_hash = "0" * 64
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

    def test_starter_preview_uses_data_without_persisting(self):
        starter, membership = self._owner(
            "starter-preview@example.com"
        )
        response = self._client(
            starter, membership, language="en"
        ).get(
            "/pro/monthly-reports/preview"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "PATIA Pro preview",
            response.get_data(as_text=True),
        )
        self.assertIn(
            "Document language: English",
            response.get_data(as_text=True),
        )
        self.assertEqual(MonthlyOwnerReport.query.count(), 0)

    def test_monthly_resend_is_owner_only_and_uses_snapshot(self):
        self._sale(datetime(2026, 6, 15, 18))
        record, _ = generate_monthly_report(
            self.organization.id, 2026, 6, send=False
        )
        manager, manager_membership = self._member("MANAGER")
        self.assertEqual(
            self._client(manager, manager_membership).post(
                f"/pro/monthly-reports/{record.id}/resend"
            ).status_code,
            403,
        )
        with patch("app.routes.send_email", return_value=True) as send:
            response = self._client(
                self.owner, self.membership
            ).post(f"/pro/monthly-reports/{record.id}/resend")
        self.assertEqual(response.status_code, 302)
        db.session.refresh(record)
        self.assertEqual(record.status, "sent")
        send.assert_called_once()

    def test_failed_monthly_resend_uses_error_feedback(self):
        self._sale(datetime(2026, 6, 15, 18))
        record, _ = generate_monthly_report(
            self.organization.id, 2026, 6, send=False
        )
        with patch("app.routes.send_email", return_value=False):
            response = self._client(
                self.owner, self.membership
            ).post(
                f"/pro/monthly-reports/{record.id}/resend",
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('class="flash error"', html)
        self.assertIn("No pudimos reenviar el reporte", html)
        stylesheet = Path("app/static/css/styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(".app-shell-v2 .flash.error", stylesheet)

    def test_monthly_cron_counts_existing_sent_report_as_skipped(self):
        self._sale(datetime(2026, 6, 15, 18))
        record, _ = generate_monthly_report(
            self.organization.id, 2026, 6, send=False
        )
        record.status = "sent"
        record.sent_at = datetime.utcnow()
        db.session.commit()

        with patch("app.routes.send_email") as send:
            summary = run_monthly_reports(2026, 6)

        self.assertEqual(
            summary,
            {"sent": 0, "skipped": 1, "failed": 0},
        )
        send.assert_not_called()

    def test_purchase_suggestions_group_without_n_plus_one(self):
        self._sale(datetime.utcnow() - timedelta(days=2), quantity=6)
        data = purchase_suggestions(self.organization.id)
        self.assertEqual(data["summary"]["products"], 1)
        self.assertGreater(data["summary"]["units"], 0)
        self.assertEqual(
            data["groups"][0]["supplier_name"], self.supplier.name
        )
        self.assertEqual(
            data["groups"][0]["items"][0]["product_id"],
            self.product.id,
        )
        response = self._client(
            self.owner, self.membership
        ).get("/pro/purchases")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Agua de prueba",
            response.get_data(as_text=True),
        )

        for index in range(25):
            db.session.add(
                Product(
                    organization_id=self.organization.id,
                    user_id=self.owner.id,
                    sku=f"BOUND-{index:02d}",
                    name=f"Producto {index:02d}",
                    category="General",
                    cost_price=Decimal("2.00"),
                    sale_price=Decimal("3.00"),
                    stock=0,
                    min_stock=2,
                )
            )
        db.session.commit()
        statements = []

        def track(conn, cursor, statement, parameters, context, many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        sqlalchemy_event.listen(
            db.engine, "before_cursor_execute", track
        )
        try:
            purchase_suggestions(self.organization.id)
        finally:
            sqlalchemy_event.remove(
                db.engine, "before_cursor_execute", track
            )
        self.assertLessEqual(len(statements), 4)

    def test_order_receipt_updates_stock_and_kardex_once(self):
        order = create_purchase_draft(
            self.membership,
            {self.product.id: 8},
            supplier_name=self.supplier.name,
        )
        self.assertEqual(order.number, "PED-000001")
        confirm_purchase_order(order)
        receipt, created = receive_purchase_order(
            order,
            self.membership,
            {order.items[0].id: 8},
            request_id="receipt-phase-two-1",
        )
        self.assertTrue(created)
        self.assertEqual(self.product.stock, 9)
        self.assertEqual(order.status, "RECEIVED")
        self.assertEqual(PurchaseReceipt.query.count(), 1)
        self.assertEqual(InventoryRestockEvent.query.count(), 1)
        movement = InventoryMovement.query.filter_by(
            movement_type="RESTOCK"
        ).order_by(InventoryMovement.id.desc()).first()
        self.assertEqual(movement.quantity_delta, 8)
        self.assertIn(order.number, movement.reason)

        duplicate, created = receive_purchase_order(
            order,
            self.membership,
            {order.items[0].id: 8},
            request_id="receipt-phase-two-1",
        )
        self.assertFalse(created)
        self.assertEqual(duplicate.id, receipt.id)
        self.assertEqual(self.product.stock, 9)

    def test_partial_receipt_completes_later_and_sequences_are_per_org(self):
        order = create_purchase_draft(
            self.membership, {self.product.id: 8}
        )
        confirm_purchase_order(order)
        receive_purchase_order(
            order,
            self.membership,
            {order.items[0].id: 3},
            request_id="partial-receipt-1",
        )
        self.assertEqual(order.status, "PARTIALLY_RECEIVED")
        self.assertEqual(order.items[0].pending_quantity, 5)
        receive_purchase_order(
            order,
            self.membership,
            {order.items[0].id: 5},
            request_id="partial-receipt-2",
        )
        self.assertEqual(order.status, "RECEIVED")
        self.assertEqual(order.items[0].pending_quantity, 0)

        other, other_membership = self._owner(
            "sequence-other@example.com", pro=True
        )
        other_product = Product(
            organization_id=other_membership.organization_id,
            user_id=other.id,
            sku="SEQUENCE-OTHER",
            name="Otro producto",
            category="General",
            cost_price=Decimal("1.00"),
            sale_price=Decimal("2.00"),
            stock=0,
            min_stock=1,
        )
        db.session.add(other_product)
        db.session.commit()
        other_order = create_purchase_draft(
            other_membership, {other_product.id: 2}
        )
        self.assertEqual(other_order.number, "PED-000001")

    def test_purchase_isolation_and_cashier_block(self):
        other, other_membership = self._owner(
            "other-phase-two@example.com",
            pro=True,
        )
        other_product = Product(
            organization_id=other_membership.organization_id,
            user_id=other.id,
            sku="OTHER-1",
            name="Producto ajeno",
            category="General",
            cost_price=Decimal("1.00"),
            sale_price=Decimal("2.00"),
            stock=0,
            min_stock=5,
        )
        db.session.add(other_product)
        db.session.commit()
        order = create_purchase_draft(
            other_membership, {other_product.id: 3}
        )
        owner_response = self._client(
            self.owner, self.membership
        ).get(f"/pro/purchases/{order.id}")
        self.assertEqual(owner_response.status_code, 404)
        cashier, cashier_membership = self._member("CASHIER")
        self.assertEqual(
            self._client(cashier, cashier_membership)
            .get("/pro/purchases")
            .status_code,
            403,
        )
        own_suggestions = purchase_suggestions(self.organization.id)
        names = {
            item["name"] for item in own_suggestions["suggestions"]
        }
        self.assertNotIn("Producto ajeno", names)

    def test_purchase_empty_copy_contrast_is_scoped(self):
        owner, membership = self._owner(
            "empty-purchases-contrast@example.com", pro=True
        )
        response = self._client(owner, membership).get("/pro/purchases")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Todavía no has creado pedidos.",
            response.get_data(as_text=True),
        )
        stylesheet = Path("app/static/css/styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ".app-shell-v2 .pro-purchases-v1__orders .reports-text",
            stylesheet,
        )
        self.assertIn(
            "color: var(--v2-muted, #62697c) !important;",
            stylesheet,
        )

    def test_purchase_receipt_count_uses_singular_and_plural(self):
        second_product = Product(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            sku="PHASE-002",
            name="Segundo producto",
            category="Bebidas",
            cost_price=Decimal("8.00"),
            sale_price=Decimal("14.00"),
            stock=0,
            min_stock=2,
        )
        db.session.add(second_product)
        db.session.commit()

        singular_order = create_purchase_draft(
            self.membership, {self.product.id: 1}
        )
        confirm_purchase_order(singular_order)
        receive_purchase_order(
            singular_order,
            self.membership,
            {singular_order.items[0].id: 1},
            request_id="singular-receipt",
        )
        singular_html = self._client(
            self.owner, self.membership
        ).get(
            f"/pro/purchases/{singular_order.id}"
        ).get_data(as_text=True)
        self.assertIn("1 producto recibido", singular_html)

        plural_order = create_purchase_draft(
            self.membership,
            {self.product.id: 1, second_product.id: 1},
        )
        confirm_purchase_order(plural_order)
        receive_purchase_order(
            plural_order,
            self.membership,
            {item.id: 1 for item in plural_order.items},
            request_id="plural-receipt",
        )
        plural_html = self._client(
            self.owner, self.membership
        ).get(
            f"/pro/purchases/{plural_order.id}"
        ).get_data(as_text=True)
        self.assertIn("2 productos recibidos", plural_html)

        translations = Translations.load(
            "app/translations", locales=["en"]
        )
        self.assertEqual(
            translations.ngettext(
                "%(count)s producto recibido",
                "%(count)s productos recibidos",
                1,
            )
            % {"count": 1},
            "1 product received",
        )
        self.assertEqual(
            translations.ngettext(
                "%(count)s producto recibido",
                "%(count)s productos recibidos",
                2,
            )
            % {"count": 2},
            "2 products received",
        )

    def test_alerts_have_evidence_actions_and_english_copy(self):
        self._sale(datetime.utcnow() - timedelta(days=2))
        client = self._client(
            self.owner, self.membership, language="en"
        )
        response = client.get("/pro/alerts?period=30d")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Smart alerts", html)
        self.assertIn("Evidence:", html)
        self.assertIn("/products", html)

    def test_cash_alert_does_not_require_multiple_team_members(self):
        now = datetime.utcnow()
        db.session.add(
            CashRegisterSession(
                organization_id=self.organization.id,
                register_key="MAIN",
                open_key=None,
                status="CLOSED",
                opening_cash=Decimal("500.00"),
                expected_cash_at_close=Decimal("650.00"),
                counted_cash=Decimal("630.00"),
                difference=Decimal("-20.00"),
                opened_at=now - timedelta(hours=8),
                closed_at=now - timedelta(hours=1),
            )
        )
        db.session.commit()
        response = self._client(
            self.owner, self.membership
        ).get("/pro/alerts?period=7d")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "cierre tuvo diferencia",
            response.get_data(as_text=True),
        )

    def test_credit_alert_is_scoped_to_current_organization(self):
        customer = Customer(
            organization_id=self.organization.id,
            name="Cliente con saldo",
            is_active=True,
            credit_enabled=True,
            credit_limit=Decimal("500.00"),
        )
        db.session.add(customer)
        db.session.flush()
        db.session.add(
            CustomerCreditMovement(
                organization_id=self.organization.id,
                customer_id=customer.id,
                movement_type="CHARGE",
                amount=Decimal("100.00"),
                balance_before=Decimal("0.00"),
                balance_after=Decimal("100.00"),
                performed_by_member_id=self.membership.id,
                request_id="alert-credit-charge",
            )
        )
        db.session.commit()
        response = self._client(
            self.owner, self.membership
        ).get("/pro/alerts")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "saldos pendientes por cobrar",
            response.get_data(as_text=True),
        )


if __name__ == "__main__":
    unittest.main()
