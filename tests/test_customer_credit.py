import os
from decimal import Decimal
import tempfile
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "customer-credit-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_credit")
os.environ.setdefault("STRIPE_PRICE_ID", "price_credit")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_credit")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

from app import create_app, db
from app.cash.services import expected_cash
from app.credit.services import customer_balance
from app.models import (
    CashMovement,
    CashRegisterSession,
    Customer,
    CustomerCreditMovement,
    OrganizationMember,
    Product,
    Sale,
    SalesTicket,
    User,
)
from app.team.services import ensure_owner_organization


class CustomerCreditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-credit-")
        database_path = Path(self.temp_dir.name, "credit.db")
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
        self.owner, self.owner_member = self.add_owner("owner@credit.test")
        self.customer = Customer(
            organization_id=self.owner_member.organization_id,
            created_by_member_id=self.owner_member.id,
            name="Abarrotes Lupita",
            phone="2381234567",
            phone_normalized="2381234567",
            credit_enabled=True,
            credit_limit=Decimal("100.00"),
        )
        self.product = Product(
            organization_id=self.owner_member.organization_id,
            user_id=self.owner.id,
            sku="CREDIT-1",
            name="Canasta básica",
            category="General",
            cost_price=Decimal("20.00"),
            sale_price=Decimal("40.00"),
            stock=20,
            min_stock=2,
        )
        db.session.add_all((self.customer, self.product))
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
        user = User(email=email, company_name=email.split("@")[0], email_verified=True)
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def add_member(self, role, email, pin=None):
        user = User(email=email, company_name="Empleado", email_verified=True)
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        member = OrganizationMember(
            organization_id=self.owner_member.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        if pin:
            member.set_pin(pin)
        db.session.add(member)
        db.session.commit()
        return user, member

    def client_for(self, user, organization_id=None, language="es"):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["organization_id"] = (
                organization_id or self.owner_member.organization_id
            )
            session["language"] = language
        return client

    def credit_sale(self, client, quantity=1, **extra):
        payload = {
            "request_id": str(uuid.uuid4()),
            "payment_method": "credit",
            "customer_id": self.customer.id,
            "items": [{"product_id": self.product.id, "quantity": quantity}],
        }
        payload.update(extra)
        return client.post("/sell-cart", json=payload)

    def open_register(self, client, amount="50.00"):
        return client.post(
            "/cash-register/open",
            data={"opening_cash": amount},
        )

    def test_credit_sale_creates_immutable_charge_and_links_ticket(self):
        self.assertFalse(hasattr(Customer, "balance"))
        response = self.credit_sale(self.client_for(self.owner), quantity=2)
        self.assertEqual(response.status_code, 200)
        movement = CustomerCreditMovement.query.one()
        ticket = SalesTicket.query.one()
        self.assertEqual(movement.movement_type, "CHARGE")
        self.assertEqual(movement.amount, Decimal("80.00"))
        self.assertEqual(movement.balance_before, Decimal("0.00"))
        self.assertEqual(movement.balance_after, Decimal("80.00"))
        self.assertEqual(movement.sales_ticket_id, ticket.id)
        self.assertEqual(ticket.customer_id, self.customer.id)
        self.assertEqual(ticket.payment_method, "credit")
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("80.00"))

        movement.note = "alterado"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        with self.assertRaises(ValueError):
            db.session.delete(movement)
            db.session.commit()
        db.session.rollback()

    def test_duplicate_credit_request_does_not_duplicate_charge(self):
        client = self.client_for(self.owner)
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "payment_method": "credit",
            "customer_id": self.customer.id,
            "items": [{"product_id": self.product.id, "quantity": 1}],
        }
        first = client.post("/sell-cart", json=payload)
        second = client.post("/sell-cart", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])
        self.assertEqual(SalesTicket.query.count(), 1)
        self.assertEqual(Sale.query.count(), 1)
        self.assertEqual(CustomerCreditMovement.query.count(), 1)
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("40.00"))

    def test_partial_and_full_payments_keep_exact_balance(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client, quantity=2).status_code, 200)
        partial = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={"amount": "30.25", "payment_method": "card", "note": "Primer abono"},
        )
        self.assertEqual(partial.status_code, 302)
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("49.75"))
        full = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={"amount": "49.75", "payment_method": "transfer"},
        )
        self.assertEqual(full.status_code, 302)
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("0.00"))
        self.assertEqual(
            [row.movement_type for row in CustomerCreditMovement.query.order_by(CustomerCreditMovement.id)],
            ["CHARGE", "PAYMENT", "PAYMENT"],
        )

    def test_payment_request_id_is_persistently_idempotent(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client, quantity=2).status_code, 200)
        request_id = str(uuid.uuid4())
        payload = {
            "amount": "30.25",
            "payment_method": "card",
            "request_id": request_id,
        }

        first = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data=payload,
        )
        second = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data=payload,
            follow_redirects=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 200)
        self.assertIn("ya había sido registrado", second.get_data(as_text=True))
        payments = CustomerCreditMovement.query.filter_by(
            movement_type="PAYMENT"
        ).all()
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0].request_id, request_id)
        self.assertEqual(
            customer_balance(
                self.customer.id,
                self.owner_member.organization_id,
            ),
            Decimal("49.75"),
        )

    def test_duplicate_cash_payment_does_not_duplicate_cash_movement(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client).status_code, 200)
        self.open_register(client)
        request_id = str(uuid.uuid4())
        payload = {
            "amount": "10.00",
            "payment_method": "cash",
            "request_id": request_id,
        }

        client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data=payload,
        )
        client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data=payload,
        )

        cash_session = CashRegisterSession.query.one()
        self.assertEqual(expected_cash(cash_session.id), Decimal("60.00"))
        self.assertEqual(
            CashMovement.query.filter_by(
                movement_type="CREDIT_PAYMENT"
            ).count(),
            1,
        )

    def test_payment_form_contains_persistent_request_id(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client).status_code, 200)

        html = client.get(
            f"/credit/customers/{self.customer.id}"
        ).get_data(as_text=True)

        self.assertIn('name="request_id"', html)

    def test_cash_payment_requires_open_register_and_updates_expected_cash(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client).status_code, 200)
        closed = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={"amount": "10.00", "payment_method": "cash"},
            follow_redirects=True,
        )
        self.assertIn("Abre la caja", closed.get_data(as_text=True))
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("40.00"))

        self.open_register(client, "50.00")
        paid = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={"amount": "10.00", "payment_method": "cash"},
        )
        self.assertEqual(paid.status_code, 302)
        cash_session = CashRegisterSession.query.one()
        self.assertEqual(expected_cash(cash_session.id), Decimal("60.00"))
        movement = CashMovement.query.filter_by(movement_type="CREDIT_PAYMENT").one()
        self.assertEqual(movement.amount, Decimal("10.00"))
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("30.00"))

    def test_non_cash_payments_do_not_change_expected_cash(self):
        client = self.client_for(self.owner)
        self.open_register(client, "50.00")
        self.assertEqual(self.credit_sale(client, quantity=2).status_code, 200)
        cash_session = CashRegisterSession.query.one()
        for payment_method in ("card", "transfer", "other"):
            response = client.post(
                f"/credit/customers/{self.customer.id}/payments",
                data={"amount": "10.00", "payment_method": payment_method},
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(expected_cash(cash_session.id), Decimal("50.00"))
        self.assertEqual(
            CashMovement.query.filter_by(movement_type="CREDIT_PAYMENT").count(),
            0,
        )

    def test_limit_blocks_atomic_sale_and_owner_can_authorize_excess(self):
        client = self.client_for(self.owner)
        self.customer.credit_limit = Decimal("30.00")
        db.session.commit()
        blocked = self.credit_sale(client)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.get_json()["error_code"], "credit_limit_exceeded")
        self.assertEqual(Sale.query.count(), 0)
        self.assertEqual(SalesTicket.query.count(), 0)
        self.assertEqual(CustomerCreditMovement.query.count(), 0)
        self.assertEqual(db.session.get(Product, self.product.id).stock, 20)

        allowed = self.credit_sale(client, credit_override=True)
        self.assertEqual(allowed.status_code, 200)
        movement = CustomerCreditMovement.query.one()
        self.assertEqual(movement.authorized_by_member_id, self.owner_member.id)

    def test_canceling_credit_sale_reverses_balance_and_restores_stock(self):
        client = self.client_for(self.owner)
        sold = self.credit_sale(client)
        self.assertEqual(sold.status_code, 200)
        sale = Sale.query.one()
        canceled = client.post(f"/sales/{sale.id}/cancel")
        self.assertEqual(canceled.status_code, 302)
        self.assertEqual(Sale.query.count(), 0)
        self.assertEqual(db.session.get(Product, self.product.id).stock, 20)
        movements = CustomerCreditMovement.query.order_by(
            CustomerCreditMovement.id
        ).all()
        self.assertEqual(
            [movement.movement_type for movement in movements],
            ["CHARGE", "REVERSAL"],
        )
        self.assertEqual(movements[-1].balance_after, Decimal("0.00"))

    def test_paid_credit_sale_cannot_be_canceled_without_refund_flow(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client).status_code, 200)
        sale = Sale.query.one()
        client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={"amount": "10.00", "payment_method": "card"},
        )
        blocked = client.post(
            f"/sales/{sale.id}/cancel",
            follow_redirects=True,
        )
        self.assertIn("parte de su saldo ya fue pagado", blocked.get_data(as_text=True))
        self.assertEqual(Sale.query.count(), 1)
        self.assertEqual(db.session.get(Product, self.product.id).stock, 19)

    def test_returning_credit_sale_creates_reversal(self):
        client = self.client_for(self.owner)
        self.assertEqual(self.credit_sale(client).status_code, 200)
        sale = Sale.query.one()
        returned = client.post(f"/sales/{sale.id}/return")
        self.assertEqual(returned.status_code, 302)
        self.assertEqual(
            [movement.movement_type for movement in CustomerCreditMovement.query.order_by(CustomerCreditMovement.id)],
            ["CHARGE", "REVERSAL"],
        )
        self.assertEqual(customer_balance(self.customer.id, self.owner_member.organization_id), Decimal("0.00"))

    def test_cashier_override_requires_owner_or_manager_pin(self):
        manager, manager_member = self.add_member(
            "MANAGER", "manager@credit.test", pin="2468"
        )
        cashier, cashier_member = self.add_member("CASHIER", "cashier@credit.test")
        self.customer.credit_limit = Decimal("10.00")
        db.session.commit()
        client = self.client_for(cashier)
        denied = self.credit_sale(client, credit_override=True, override_pin="0000")
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(CustomerCreditMovement.query.count(), 0)
        allowed = self.credit_sale(client, credit_override=True, override_pin="2468")
        self.assertEqual(allowed.status_code, 200)
        movement = CustomerCreditMovement.query.one()
        self.assertEqual(movement.performed_by_member_id, cashier_member.id)
        self.assertEqual(movement.authorized_by_member_id, manager_member.id)

    def test_cashier_pin_override_attempts_are_rate_limited(self):
        cashier, _ = self.add_member("CASHIER", "limited@credit.test")
        self.customer.credit_limit = Decimal("10.00")
        db.session.commit()
        self.app.config["RATELIMIT_ENABLED"] = True
        client = self.client_for(cashier)
        responses = [
            self.credit_sale(
                client,
                credit_override=True,
                override_pin="0000",
            )
            for _ in range(6)
        ]
        self.assertEqual([response.status_code for response in responses[:5]], [409] * 5)
        self.assertEqual(responses[5].status_code, 429)
        self.assertEqual(
            responses[5].get_json()["error_code"],
            "credit_override_rate_limited",
        )
        self.assertEqual(CustomerCreditMovement.query.count(), 0)
        self.assertEqual(Sale.query.count(), 0)

    def test_roles_and_account_interface(self):
        manager, _ = self.add_member("MANAGER", "manager2@credit.test")
        cashier, _ = self.add_member("CASHIER", "cashier2@credit.test")
        self.assertEqual(self.client_for(manager).get("/credit").status_code, 200)
        cashier_client = self.client_for(cashier)
        self.assertEqual(cashier_client.get("/credit").status_code, 403)
        self.assertEqual(
            cashier_client.get(f"/credit/customers/{self.customer.id}").status_code,
            200,
        )
        self.assertEqual(
            cashier_client.post(
                f"/credit/customers/{self.customer.id}/settings",
                data={"credit_enabled": "1", "credit_limit": "500"},
            ).status_code,
            403,
        )
        pos = cashier_client.get("/sell").get_data(as_text=True)
        self.assertIn("selected-customer-account", pos)

    def test_cross_organization_ids_are_not_accessible(self):
        second_owner, second_member = self.add_owner("second@credit.test")
        foreign = Customer(
            organization_id=second_member.organization_id,
            created_by_member_id=second_member.id,
            name="Cliente secreto",
            phone="5551112222",
            phone_normalized="5551112222",
            credit_enabled=True,
            credit_limit=Decimal("500.00"),
        )
        db.session.add(foreign)
        db.session.commit()
        client = self.client_for(self.owner)
        self.assertEqual(client.get(f"/credit/customers/{foreign.id}").status_code, 404)
        self.assertEqual(
            client.post(
                f"/credit/customers/{foreign.id}/payments",
                data={"amount": "1.00", "payment_method": "card"},
            ).status_code,
            404,
        )
        html = client.get("/credit").get_data(as_text=True)
        self.assertNotIn("Cliente secreto", html)

    def test_credit_settings_validation_and_disable_with_balance(self):
        client = self.client_for(self.owner)
        invalid = client.post(
            f"/credit/customers/{self.customer.id}/settings",
            data={"credit_enabled": "1", "credit_limit": "-1"},
            follow_redirects=True,
        )
        self.assertIn("límite de crédito válido", invalid.get_data(as_text=True))
        self.assertEqual(self.credit_sale(client).status_code, 200)
        blocked = client.post(
            f"/credit/customers/{self.customer.id}/settings",
            data={"credit_limit": "0"},
            follow_redirects=True,
        )
        self.assertIn("Liquida el saldo", blocked.get_data(as_text=True))
        self.assertTrue(db.session.get(Customer, self.customer.id).credit_enabled)

    def test_spanish_and_english_copy_and_whatsapp_reminder(self):
        en = self.client_for(self.owner, language="en").get("/credit")
        self.assertIn("Outstanding balances", en.get_data(as_text=True))
        account = self.client_for(self.owner, language="en").get(
            f"/credit/customers/{self.customer.id}"
        )
        html = account.get_data(as_text=True)
        self.assertIn("wa.me/522381234567", html)
        self.assertIn("Remind via WhatsApp", html)

    def test_receivables_only_show_positive_balances_and_hide_after_payment(self):
        client = self.client_for(self.owner)
        empty = client.get("/credit").get_data(as_text=True)
        self.assertIn("Todo está al corriente", empty)
        self.assertNotIn(self.customer.name, empty)

        self.assertEqual(self.credit_sale(client).status_code, 200)
        listed = client.get("/credit").get_data(as_text=True)
        self.assertIn(self.customer.name, listed)
        movement = CustomerCreditMovement.query.filter_by(
            movement_type="CHARGE"
        ).one()
        paid = client.post(
            f"/credit/customers/{self.customer.id}/payments",
            data={
                "amount": str(movement.balance_after),
                "payment_method": "card",
                "request_id": str(uuid.uuid4()),
            },
            follow_redirects=True,
        )
        self.assertIn(
            "ya no tiene saldo pendiente",
            paid.get_data(as_text=True),
        )
        after = client.get("/credit").get_data(as_text=True)
        self.assertNotIn(self.customer.name, after)

    def test_zero_balance_account_does_not_render_payment_form(self):
        body = self.client_for(self.owner).get(
            f"/credit/customers/{self.customer.id}"
        ).get_data(as_text=True)
        self.assertIn("Sin pagos pendientes", body)
        self.assertNotIn('name="amount"', body)
