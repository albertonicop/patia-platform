"""Cashier end-to-end authorization and accounting on a disposable database."""
from decimal import Decimal
import uuid
import unittest
from unittest.mock import patch

from flask import g
import test_cash_register as cash_fixture
from app import db
from app.models import CashMovement, CashRegisterSession, Customer, CustomerCreditMovement, InventoryMovement, OrganizationInvitation, OrganizationMember, Product, Sale, SalesTicket, User
from app.cash.services import expected_cash


class CashierFixture:
    email = "caja.demo@example.com"
    password = "CashierDemo123"
    token = "cashier-isolated-invitation"

    def __init__(self, create_cashier=True):
        self.fixture = cash_fixture.CashRegisterTests()
        self.fixture.setUp()
        self.app = self.fixture.app
        self.owner = self.fixture.owner
        self.owner.company_name = "Panaderia Demo PATIA"
        self.owner.first_name = "Propietario"
        self.owner.last_name = "Demo"
        self.owner.manual_pro_access = True
        self.owner_member = self.fixture.owner_member
        self.owner_member.organization.name = self.owner.company_name
        self.product = self.fixture.product
        self.product.name = "Rol de canela demo"
        self.product.sale_price = Decimal("10.25")
        self.product.cost_price = Decimal("4.37")
        db.session.commit()
        # The fixture holds an app context between requests; real server requests
        # have fresh g objects. Clear only this test cache to reproduce that scope.
        @self.app.before_request
        def fresh_membership_cache():
            g.pop("_active_membership_cache", None)
        self.owner_client = self.app.test_client()
        self.owner_client.post("/login", data={"email": self.owner.email, "password": "Password123"})
        self.client = self.app.test_client()
        if create_cashier:
            with patch("app.team.routes.secrets.token_urlsafe", return_value=self.token), patch("app.routes.send_email", return_value=True):
                response = self.owner_client.post("/team/invite", data={"email": self.email, "role": "CASHIER"})
                assert response.status_code == 302
            response = self.client.post("/team/accept/" + self.token, data={"first_name": "Lucia", "last_name": "Caja Demo", "password": self.password})
            assert response.status_code == 302
            self.refresh_cashier()
            self.client.post("/logout")
            self.login()

    def refresh_cashier(self):
        self.cashier = User.query.filter_by(email=self.email).one()
        self.member = OrganizationMember.query.filter_by(user_id=self.cashier.id).one()

    def login(self):
        return self.client.post("/login", data={"email": self.email, "password": self.password})

    def sell(self, **overrides):
        payload = {"request_id": str(uuid.uuid4()), "payment_method": "cash", "amount_received": "50.00", "items": [{"product_id": self.product.id, "quantity": 2}]}
        payload.update(overrides)
        return self.client.post("/sell-cart", json=payload)

    def close(self):
        self.fixture.tearDown()


class CashierSessionTests(unittest.TestCase):
    def setUp(self):
        self.audit = CashierFixture()
        self.addCleanup(self.audit.close)
        self.client = self.audit.client

    def test_invitation_login_logout_and_minimal_menu(self):
        audit = self.audit
        self.assertEqual(audit.member.role, "CASHIER")
        self.assertEqual(audit.member.organization_id, audit.owner_member.organization_id)
        self.assertEqual(OrganizationMember.query.filter_by(user_id=audit.cashier.id).count(), 1)
        self.assertTrue(audit.cashier.check_password(audit.password))
        self.assertEqual(self.client.get("/team/accept/" + audit.token).status_code, 410)
        self.assertEqual(self.client.get("/").location, "/sell")
        html = self.client.get("/sell").get_data(as_text=True)
        self.assertIn('href="/sell"', html)
        self.assertIn('href="/cash-register"', html)
        for path in ("/products", "/reports", "/team", "/subscription", "/settings"):
            self.assertNotIn(f'href="{path}"', html)
        self.assertNotIn('cost_price', html)
        self.assertNotIn('unit_cost', html)
        self.assertEqual(self.client.post("/logout").location, "/")
        self.assertEqual(self.client.post("/sell-cart", json={}).status_code, 401)
        response = self.client.post("/login", data={"email": audit.email, "password": "incorrect-password"})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
        self.assertEqual(audit.login().status_code, 302)
        self.assertEqual(self.client.get("/sell").status_code, 200)

    def test_cash_sale_change_ticket_actor_retry_and_close(self):
        audit = self.audit
        self.assertEqual(audit.sell().status_code, 409)
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        response = audit.sell()
        self.assertEqual(response.status_code, 200)
        ticket = SalesTicket.query.one()
        sale = Sale.query.one()
        cash = CashRegisterSession.query.one()
        self.assertEqual(ticket.cashier_member_id, audit.member.id)
        self.assertEqual(ticket.organization_id, audit.member.organization_id)
        self.assertEqual(ticket.amount_received, Decimal("50.00"))
        self.assertEqual(ticket.change_amount, Decimal("29.50"))
        self.assertEqual(sale.total, Decimal("20.50"))
        self.assertEqual(expected_cash(cash.id), Decimal("120.50"))
        self.assertEqual(cash.opened_by_member_id, audit.member.id)
        db.session.refresh(audit.product)
        self.assertEqual(audit.product.stock, 28)
        for movement in [*CashMovement.query.all(), *InventoryMovement.query.all()]:
            self.assertEqual(movement.organization_id, audit.member.organization_id)
            self.assertEqual(movement.performed_by_member_id, audit.member.id)
        html = self.client.get(response.get_json()["ticket_url"]).get_data(as_text=True)
        self.assertIn("Lucia Caja Demo", html)
        self.assertIn("29.50", html)
        self.assertIn("50.00", html)
        self.assertNotIn("4.37", html)
        self.assertNotIn("unit_cost", html)
        self.assertEqual(audit.sell(request_id=ticket.public_id).status_code, 200)
        self.assertEqual(Sale.query.count(), 1)
        self.assertEqual(CashMovement.query.filter_by(movement_type="SALE_CASH").count(), 1)
        result = self.client.post("/cash-register/close", data={"counted_cash": "120.50", "closing_notes": "Turno demo correcto"})
        self.assertEqual(result.location, "/cash-register")
        db.session.refresh(cash)
        self.assertEqual(cash.status, "CLOSED")
        self.assertEqual(cash.closed_by_member_id, audit.member.id)
        self.assertEqual(cash.difference, Decimal("0.00"))
        self.assertEqual(self.client.get(f"/cash-register/{cash.id}").status_code, 403)
        self.assertEqual(audit.sell().status_code, 409)

    def test_direct_requests_cannot_change_privileged_data_or_prices(self):
        audit = self.audit
        blocked_get = ("/settings", "/team", "/subscription", "/subscribe", "/products", "/download-template", "/reports", "/suppliers", "/inventory/kardex", "/recipes", "/customers", "/customers/export.csv", "/credit", "/pro/hub", "/pro/monthly-reports")
        for path in blocked_get:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)
        blocked_post = ("/settings", "/team/invite", f"/team/members/{audit.owner_member.id}/toggle", f"/team/members/{audit.owner_member.id}/role", f"/team/members/{audit.owner_member.id}/pin", "/subscription/change-plan", "/cancel-subscription", "/create-checkout-session", "/billing-portal", "/products/new", f"/products/{audit.product.id}/edit", f"/products/{audit.product.id}/restock", "/api/products/import/commit", "/cash-register/movement")
        for path in blocked_post:
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data={"amount": "1", "role": "OWNER"}).status_code, 403)
                self.assertEqual(self.client.post(path, json={"amount": "1", "role": "OWNER"}).status_code, 403)
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        self.assertEqual(audit.sell(items=[{"product_id": audit.product.id, "quantity": 1, "unit_price": "0.01", "cost_price": "0.01"}], discount="99.99", total="0.01").status_code, 200)
        sale = Sale.query.one()
        self.assertEqual(sale.unit_price, Decimal("10.25"))
        self.assertEqual(sale.unit_cost, Decimal("4.37"))
        self.assertEqual(sale.total, Decimal("10.25"))
        self.assertEqual(audit.product.sale_price, Decimal("10.25"))

    def test_customer_lookup_creation_and_credit_payment_respect_permissions(self):
        audit = self.audit
        response = self.client.post("/customers/api/quick", json={"name": "Cliente demo", "phone": "2380000000", "credit_enabled": True, "credit_limit": "9999"})
        self.assertEqual(response.status_code, 201)
        customer = Customer.query.one()
        self.assertEqual(customer.created_by_member_id, audit.member.id)
        self.assertFalse(customer.credit_enabled)
        self.assertEqual(self.client.get("/customers/api/search?q=Cliente").get_json()["customers"][0]["id"], customer.id)
        self.assertEqual(self.client.get(f"/credit/customers/{customer.id}").status_code, 200)
        self.assertEqual(self.client.post(f"/credit/customers/{customer.id}/settings", json={"credit_enabled": "1", "credit_limit": "9999"}).status_code, 403)
        enabled = audit.owner_client.post(f"/credit/customers/{customer.id}/settings", json={"credit_enabled": "1", "credit_limit": "50"})
        self.assertEqual(enabled.status_code, 200)
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        self.assertEqual(audit.sell(payment_method="credit", customer_id=customer.id, amount_received=None).status_code, 200)
        charge = CustomerCreditMovement.query.filter_by(movement_type="CHARGE").one()
        self.assertEqual(charge.performed_by_member_id, audit.member.id)
        request_id = str(uuid.uuid4())
        for _ in range(2):
            paid = self.client.post(f"/credit/customers/{customer.id}/payments", data={"amount": "5", "payment_method": "cash", "request_id": request_id})
            self.assertEqual(paid.status_code, 302)
        payment = CustomerCreditMovement.query.filter_by(movement_type="PAYMENT").one()
        self.assertEqual(payment.performed_by_member_id, audit.member.id)
        self.assertEqual(payment.balance_after, Decimal("15.50"))
        cash_payment = CashMovement.query.filter_by(movement_type="CREDIT_PAYMENT").one()
        self.assertEqual(cash_payment.performed_by_member_id, audit.member.id)
        self.assertEqual(expected_cash(CashRegisterSession.query.one().id), Decimal("105.00"))

    def test_cashier_cannot_reverse_but_owner_and_manager_are_recorded(self):
        audit = self.audit
        manager, manager_member = audit.fixture.add_member("MANAGER", "manager.demo@example.com")
        manager_client = audit.fixture.client_for(manager)
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        for actor_client, actor in ((audit.owner_client, audit.owner_member), (manager_client, manager_member)):
            for action, kind in (("cancel", "SALE_CANCELLATION"), ("return", "RETURN")):
                with self.subTest(role=actor.role, action=action):
                    self.assertEqual(audit.sell().status_code, 200)
                    sale = Sale.query.one()
                    ticket_id = sale.sales_ticket_id
                    html = self.client.get("/sell").get_data(as_text=True)
                    self.assertNotIn(f'action="/sales/{sale.id}/{action}"', html)
                    self.assertEqual(self.client.post(f"/sales/{sale.id}/{action}").status_code, 403)
                    self.assertEqual(self.client.post(f"/sales/{sale.id}/{action}", json={}).status_code, 403)
                    self.assertEqual(actor_client.post(f"/sales/{sale.id}/{action}").status_code, 302)
                    movement = InventoryMovement.query.filter_by(sales_ticket_id=ticket_id, movement_type=kind).one()
                    refund = CashMovement.query.filter_by(sales_ticket_id=ticket_id, movement_type="REFUND").one()
                    self.assertEqual(movement.performed_by_member_id, actor.id)
                    self.assertEqual(refund.performed_by_member_id, actor.id)
                    self.assertEqual(SalesTicket.query.filter_by(id=ticket_id).one().cashier_member_id, audit.member.id)
                    self.assertEqual(expected_cash(CashRegisterSession.query.one().id), Decimal("100.00"))
                    db.session.refresh(audit.product)
                    self.assertEqual(audit.product.stock, 30)

    def test_other_business_is_inaccessible_and_deactivation_revokes_open_session(self):
        audit = self.audit
        foreign_owner, foreign_member = audit.fixture.add_owner("foreign.demo@example.com")
        foreign_client = self.app_client(foreign_owner, foreign_member)
        foreign_product = Product(organization_id=foreign_member.organization_id, user_id=foreign_owner.id, name="Producto secreto ajeno", sku="FOREIGN-DEMO", cost_price=3, sale_price=7, stock=10, min_stock=1)
        db.session.add(foreign_product)
        db.session.commit()
        foreign_client.post("/cash-register/open", data={"opening_cash": "200"})
        response = foreign_client.post("/sell-cart", json={"request_id": str(uuid.uuid4()), "payment_method": "cash", "amount_received": "10", "items": [{"product_id": foreign_product.id, "quantity": 1}]})
        self.assertEqual(response.status_code, 200)
        foreign_sale = Sale.query.filter_by(organization_id=foreign_member.organization_id).one()
        foreign_cash = CashRegisterSession.query.filter_by(organization_id=foreign_member.organization_id).one()
        foreign_customer_response = foreign_client.post("/customers/api/quick", json={"name": "Cliente secreto ajeno"})
        self.assertEqual(foreign_customer_response.status_code, 201)
        foreign_customer_id = foreign_customer_response.get_json()["customer"]["id"]
        self.assertEqual(self.client.get("/customers/api/search?q=secreto").get_json()["customers"], [])
        self.assertEqual(self.client.get(f"/credit/customers/{foreign_customer_id}").status_code, 404)
        self.assertEqual(self.client.post(f"/credit/customers/{foreign_customer_id}/payments", data={"amount": "1", "payment_method": "card"}).status_code, 404)
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        self.assertEqual(self.client.get(response.get_json()["ticket_url"]).status_code, 404)
        self.assertEqual(self.client.get(f"/receipt/{foreign_sale.id}").status_code, 404)
        self.assertNotIn("Producto secreto ajeno", self.client.get("/sell").get_data(as_text=True))
        self.assertEqual(audit.sell(items=[{"product_id": foreign_product.id, "quantity": 1}]).status_code, 404)
        with self.client.session_transaction() as session:
            session["organization_id"] = foreign_member.organization_id
        self.assertEqual(self.client.get("/sell").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["organization_id"], audit.member.organization_id)
        sale_count, movement_count = Sale.query.count(), CashMovement.query.count()
        self.assertEqual(audit.owner_client.post(f"/team/members/{audit.member.id}/toggle").status_code, 302)
        for path, payload in (("/sell-cart", {"items": []}), ("/cash-register/open", {"opening_cash": "0"}), ("/cash-register/close", {"counted_cash": "100"})):
            with self.subTest(path=path):
                response = self.client.post(path, json=payload)
                self.assertEqual(response.status_code, 401)
        self.assertEqual(Sale.query.count(), sale_count)
        self.assertEqual(CashMovement.query.count(), movement_count)
        self.assertEqual(foreign_cash.status, "OPEN")
        login = audit.login()
        html = self.client.get(login.location).get_data(as_text=True)
        self.assertIn("desactivado", html)
        self.assertNotIn("est\u00c3", html)
        self.assertEqual(OrganizationMember.query.filter_by(user_id=audit.cashier.id).count(), 1)

    def app_client(self, user, member):
        client = self.audit.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["organization_id"] = member.organization_id
        return client

    def test_current_policy_shares_register_and_tickets_within_one_business(self):
        audit = self.audit
        audit.owner_client.post("/cash-register/open", data={"opening_cash": "20"})
        response = audit.fixture.sell(audit.owner_client, method="card")
        self.assertEqual(response.status_code, 200)
        ticket = SalesTicket.query.one()
        self.assertEqual(ticket.cashier_member_id, audit.owner_member.id)
        self.assertEqual(self.client.get(response.get_json()["ticket_url"]).status_code, 200)
        self.assertIn(ticket.folio, self.client.get("/sell").get_data(as_text=True))
        closed = self.client.post("/cash-register/close", data={"counted_cash": "20"})
        self.assertEqual(closed.status_code, 302)
        cash = CashRegisterSession.query.one()
        self.assertEqual(cash.opened_by_member_id, audit.owner_member.id)
        self.assertEqual(cash.closed_by_member_id, audit.member.id)
        self.assertEqual(cash.status, "CLOSED")

    def test_legacy_cash_form_cannot_skip_received_amount(self):
        audit = self.audit
        self.client.post("/cash-register/open", data={"opening_cash": "100"})
        for received in (None, "", "   ", "-1", "1", "NaN"):
            with self.subTest(received=received):
                data = {"product_id": audit.product.id, "quantity": "1", "payment_method": "cash"}
                if received is not None:
                    data["amount_received"] = received
                self.assertEqual(self.client.post("/sell", data=data).status_code, 302)
                self.assertEqual(Sale.query.count(), 0)
                self.assertEqual(SalesTicket.query.count(), 0)
                self.assertEqual(CashMovement.query.filter_by(movement_type="SALE_CASH").count(), 0)
                db.session.refresh(audit.product)
                self.assertEqual(audit.product.stock, 30)
        response = self.client.post("/sell", data={"product_id": audit.product.id, "quantity": "1", "payment_method": "cash", "amount_received": "20"})
        self.assertEqual(response.status_code, 302)
        ticket = SalesTicket.query.one()
        self.assertEqual(ticket.amount_received, Decimal("20.00"))
        self.assertEqual(ticket.change_amount, Decimal("9.75"))
