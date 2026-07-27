import os
from decimal import Decimal
import tempfile
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "cash-register-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

from app import create_app, db
from app.cash.services import expected_cash
from app.models import (
    CashMovement,
    CashRegisterSession,
    OrganizationMember,
    Product,
    Sale,
    SalesTicket,
    User,
)
from app.team.services import ensure_owner_organization


class CashRegisterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-cash-")
        database_path = Path(self.temp_dir.name, "cash.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.owner_member = self.add_owner("owner@cash.test")
        self.product = Product(
            organization_id=self.owner_member.organization_id,
            user_id=self.owner.id,
            sku="CASH-1",
            name="Producto caja",
            category="General",
            cost_price=Decimal("4.00"),
            sale_price=Decimal("10.00"),
            stock=30,
            min_stock=1,
        )
        db.session.add(self.product)
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

    def add_owner(self, email):
        user = User(
            email=email,
            company_name=email.split("@")[0],
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def add_member(self, role, email):
        user = User(
            email=email,
            company_name="Empleado",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        member = OrganizationMember(
            organization_id=self.owner_member.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(member)
        db.session.commit()
        return user, member

    def client_for(self, user):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["organization_id"] = self.owner_member.organization_id
        return client

    def open_register(self, client, amount="100.00"):
        return client.post(
            "/cash-register/open",
            data={"opening_cash": amount},
            follow_redirects=False,
        )

    def sell(self, client, method="cash", quantity=1):
        return client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": method,
                "items": [
                    {"product_id": self.product.id, "quantity": quantity}
                ],
            },
        )

    def test_opening_is_unique_and_records_initial_fund(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.open_register(client).status_code, 302)
        cash_session = CashRegisterSession.query.one()
        self.assertEqual(cash_session.opening_cash, Decimal("100.00"))
        self.assertEqual(cash_session.open_key, "MAIN")
        self.assertEqual(expected_cash(cash_session.id), Decimal("100.00"))
        self.assertEqual(CashMovement.query.one().movement_type, "OPENING")

        duplicate = self.open_register(client, "25.00")
        self.assertEqual(duplicate.status_code, 302)
        self.assertEqual(CashRegisterSession.query.count(), 1)

    def test_cash_requires_open_register_but_non_cash_does_not(self):
        client = self.client_for(self.owner)
        blocked = self.sell(client, "cash")
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("cash_register_url", blocked.get_json())
        self.assertEqual(Sale.query.count(), 0)

        card = self.sell(client, "card")
        transfer = self.sell(client, "transfer")
        self.assertEqual(card.status_code, 200)
        self.assertEqual(transfer.status_code, 200)
        self.assertEqual(CashMovement.query.count(), 0)

    def test_shift_links_sales_and_only_cash_changes_expected_amount(self):
        client = self.client_for(self.owner)
        self.open_register(client, "50.00")
        cash = self.sell(client, "cash", 2)
        card = self.sell(client, "card")
        transfer = self.sell(client, "transfer")
        self.assertEqual(
            [cash.status_code, card.status_code, transfer.status_code],
            [200, 200, 200],
        )
        cash_session = CashRegisterSession.query.one()
        self.assertEqual(expected_cash(cash_session.id), Decimal("70.00"))
        self.assertEqual(
            SalesTicket.query.filter(
                SalesTicket.cash_register_session_id == cash_session.id
            ).count(),
            3,
        )
        self.assertEqual(
            CashMovement.query.filter_by(movement_type="SALE_CASH").count(),
            1,
        )

    def test_entries_withdrawals_expenses_refunds_and_close_difference(self):
        client = self.client_for(self.owner)
        self.open_register(client, "100.00")
        sale_response = self.sell(client, "cash", 2)
        line = Sale.query.filter_by(
            ticket_id=sale_response.get_json()["ticket_id"]
        ).one()
        for movement_type, amount in (
            ("CASH_IN", "10.00"),
            ("WITHDRAWAL", "5.00"),
            ("EXPENSE", "3.00"),
        ):
            response = client.post(
                "/cash-register/movement",
                data={
                    "movement_type": movement_type,
                    "amount": amount,
                    "note": f"Nota {movement_type}",
                },
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(client.post(f"/sales/{line.id}/cancel").status_code, 302)
        cash_session = CashRegisterSession.query.one()
        self.assertEqual(expected_cash(cash_session.id), Decimal("102.00"))

        close = client.post(
            "/cash-register/close",
            data={
                "counted_cash": "100.00",
                "closing_notes": "Conteo final",
            },
        )
        self.assertEqual(close.status_code, 302)
        db.session.refresh(cash_session)
        self.assertEqual(cash_session.status, "CLOSED")
        self.assertIsNone(cash_session.open_key)
        self.assertEqual(
            cash_session.expected_cash_at_close, Decimal("102.00")
        )
        self.assertEqual(cash_session.difference, Decimal("-2.00"))
        self.assertEqual(
            {movement.movement_type for movement in cash_session.movements},
            {"OPENING", "SALE_CASH", "CASH_IN", "WITHDRAWAL", "EXPENSE", "REFUND"},
        )

    def test_history_detail_print_and_dashboard_status(self):
        client = self.client_for(self.owner)
        self.open_register(client, "20.00")
        cash_session = CashRegisterSession.query.one()
        dashboard = client.get("/").get_data(as_text=True)
        self.assertIn("En caja debería haber", dashboard)
        index = client.get("/cash-register").get_data(as_text=True)
        self.assertIn("En caja debería haber", index)
        self.assertEqual(
            client.post(
                "/cash-register/close",
                data={"counted_cash": "20.00", "closing_notes": ""},
            ).status_code,
            302,
        )
        detail = client.get(f"/cash-register/{cash_session.id}")
        self.assertEqual(detail.status_code, 200)
        html = detail.get_data(as_text=True)
        self.assertIn("Imprimir corte", html)
        self.assertIn("PATIA · Corte de caja", html)

    def test_role_permissions_and_cross_organization_isolation(self):
        cashier, _ = self.add_member("CASHIER", "cashier@cash.test")
        cashier_client = self.client_for(cashier)
        self.assertEqual(self.open_register(cashier_client).status_code, 302)
        self.assertEqual(
            cashier_client.post(
                "/cash-register/movement",
                data={
                    "movement_type": "EXPENSE",
                    "amount": "1.00",
                    "note": "No permitido",
                },
            ).status_code,
            403,
        )
        cash_session = CashRegisterSession.query.one()
        self.assertEqual(
            cashier_client.get(f"/cash-register/{cash_session.id}").status_code,
            403,
        )

        other_owner, other_membership = self.add_owner("other@cash.test")
        other_client = self.app.test_client()
        with other_client.session_transaction() as session:
            session["user_id"] = other_owner.id
            session["organization_id"] = other_membership.organization_id
        self.assertEqual(
            other_client.get(f"/cash-register/{cash_session.id}").status_code,
            404,
        )

    def test_cashier_close_redirects_to_accessible_register_screen(self):
        cashier, _ = self.add_member("CASHIER", "close@cash.test")
        client = self.client_for(cashier)
        self.open_register(client, "25.00")

        response = client.post(
            "/cash-register/close",
            data={"counted_cash": "25.00", "closing_notes": ""},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/cash-register"))
        destination = client.get(response.location)
        self.assertEqual(destination.status_code, 200)
        self.assertNotIn("Acceso no permitido", destination.get_data(as_text=True))
        self.assertEqual(CashRegisterSession.query.one().status, "CLOSED")

    def test_english_cash_register_copy(self):
        client = self.client_for(self.owner)
        with client.session_transaction() as session:
            session["language"] = "en"
        html = client.get("/cash-register").get_data(as_text=True)
        self.assertIn("Daily cash", html)
        self.assertIn("Open register", html)

    def test_closed_register_only_presents_opening_task(self):
        body = self.client_for(self.owner).get("/cash-register").get_data(as_text=True)
        self.assertIn("Todav\u00eda no has abierto caja", body)
        self.assertIn("Abrir caja", body)
        self.assertNotIn("Ver cierres anteriores", body)
        self.assertNotIn("Registrar gasto", body)

    def test_open_register_presents_summary_and_difference_explanation(self):
        client = self.client_for(self.owner)
        self.open_register(client, "20.00")
        body = client.get("/cash-register").get_data(as_text=True)
        self.assertIn("En caja debería haber", body)
        self.assertIn("Efectivo inicial", body)
        self.assertNotIn("Ventas en efectivo", body)
        self.assertIn("Todo cuadra.", body)
        self.assertIn("Faltan", body)
        self.assertIn("Sobran", body)


if __name__ == "__main__":
    unittest.main()
