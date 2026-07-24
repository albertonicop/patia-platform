import os
from decimal import Decimal
import tempfile
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "customer-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_customers")
os.environ.setdefault("STRIPE_PRICE_ID", "price_customers")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_customers")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

from app import create_app, db
from app.customers.services import customer_summaries
from app.models import (
    Customer,
    OrganizationMember,
    Product,
    SalesTicket,
    User,
)
from app.team.services import ensure_owner_organization


class CustomerModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-customers-")
        database_path = Path(self.temp_dir.name, "customers.db")
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
        self.owner, self.membership = self.add_owner("owner@customers.test")
        self.product = Product(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            sku="CLIENT-1",
            name="Producto cliente",
            category="General",
            cost_price=Decimal("5.00"),
            sale_price=Decimal("25.50"),
            stock=20,
            min_stock=2,
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
        membership = OrganizationMember(
            organization_id=self.membership.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
        return user, membership

    def client_for(self, user, organization_id=None, language="es"):
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = (
                organization_id or self.membership.organization_id
            )
            flask_session["language"] = language
        return client

    def create_customer(self, **overrides):
        values = {
            "organization_id": self.membership.organization_id,
            "created_by_member_id": self.membership.id,
            "name": "Ana López",
            "phone": "238 123 4567",
            "phone_normalized": "2381234567",
            "email": "ana@example.com",
            "notes": "Cliente frecuente",
        }
        values.update(overrides)
        customer = Customer(**values)
        db.session.add(customer)
        db.session.commit()
        return customer

    def sell(self, client, customer_id=None, quantity=1):
        return client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "customer_id": customer_id,
                "items": [
                    {"product_id": self.product.id, "quantity": quantity}
                ],
            },
        )

    def test_owner_creates_searches_edits_and_deactivates_customer(self):
        client = self.client_for(self.owner)
        created = client.post(
            "/customers/new",
            data={
                "name": "Ana López",
                "phone": "238 123 4567",
                "email": "ANA@example.com",
                "notes": "Prefiere WhatsApp",
            },
        )
        self.assertEqual(created.status_code, 302)
        customer = Customer.query.one()
        self.assertEqual(customer.phone_normalized, "2381234567")
        self.assertEqual(customer.email, "ana@example.com")

        by_name = client.get("/customers?q=ana")
        by_phone = client.get("/customers?q=1234567")
        self.assertIn("Ana López", by_name.get_data(as_text=True))
        self.assertIn("Ana López", by_phone.get_data(as_text=True))

        edited = client.post(
            f"/customers/{customer.id}/edit",
            data={
                "name": "Ana Pérez",
                "phone": "2381234567",
                "email": "",
                "notes": "",
            },
        )
        self.assertEqual(edited.status_code, 302)
        self.assertEqual(db.session.get(Customer, customer.id).name, "Ana Pérez")

        self.assertEqual(
            client.post(f"/customers/{customer.id}/toggle").status_code,
            302,
        )
        self.assertFalse(db.session.get(Customer, customer.id).is_active)
        self.assertNotIn(
            "Ana Pérez",
            client.get("/customers").get_data(as_text=True),
        )
        self.assertIn(
            "Ana Pérez",
            client.get("/customers?status=all").get_data(as_text=True),
        )

    def test_cashier_can_search_and_quick_create_but_not_manage_module(self):
        self.create_customer()
        cashier, _ = self.add_member("CASHIER", "cashier@customers.test")
        client = self.client_for(cashier)

        self.assertEqual(client.get("/customers").status_code, 403)
        found = client.get("/customers/api/search?q=Ana")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(len(found.get_json()["customers"]), 1)
        created = client.post(
            "/customers/api/quick",
            json={"name": "Cliente mostrador", "phone": "2387654321"},
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(Customer.query.count(), 2)
        self.assertEqual(
            client.post(
                f"/customers/{Customer.query.first().id}/toggle"
            ).status_code,
            403,
        )

    def test_manager_has_complete_access(self):
        manager, _ = self.add_member("MANAGER", "manager@customers.test")
        client = self.client_for(manager)
        self.assertEqual(client.get("/customers").status_code, 200)
        response = client.post(
            "/customers/new",
            data={"name": "Cliente gerente", "phone": "2381112233"},
        )
        self.assertEqual(response.status_code, 302)

    def test_sales_link_customer_and_history_uses_ticket_totals(self):
        customer = self.create_customer()
        client = self.client_for(self.owner)
        self.assertEqual(self.sell(client, customer.id, 2).status_code, 200)
        self.assertEqual(self.sell(client, customer.id, 1).status_code, 200)

        tickets = SalesTicket.query.order_by(SalesTicket.id).all()
        self.assertEqual([ticket.customer_id for ticket in tickets], [customer.id] * 2)
        summary = customer_summaries(self.membership.organization_id)[0]
        self.assertEqual(summary.ticket_count, 2)
        self.assertEqual(summary.purchase_total, Decimal("76.50"))
        detail = client.get(f"/customers/{customer.id}")
        body = detail.get_data(as_text=True)
        self.assertEqual(detail.status_code, 200)
        self.assertIn("$76.50 MXN", body)
        self.assertIn(tickets[0].folio, body)
        self.assertIn(tickets[1].folio, body)

    def test_old_ticket_without_customer_remains_compatible(self):
        client = self.client_for(self.owner)
        sold = self.sell(client)
        self.assertEqual(sold.status_code, 200)
        ticket = SalesTicket.query.one()
        self.assertIsNone(ticket.customer_id)
        self.assertEqual(
            client.get(f"/ticket/{ticket.public_id}").status_code,
            200,
        )

    def test_inactive_or_foreign_customer_cannot_be_attached_to_sale(self):
        inactive = self.create_customer(is_active=False)
        owner_two, member_two = self.add_owner("other@customers.test")
        foreign = Customer(
            organization_id=member_two.organization_id,
            created_by_member_id=member_two.id,
            name="Cliente ajeno",
            phone="5551234567",
            phone_normalized="5551234567",
        )
        db.session.add(foreign)
        db.session.commit()
        client = self.client_for(self.owner)
        self.assertEqual(self.sell(client, inactive.id).status_code, 400)
        self.assertEqual(self.sell(client, foreign.id).status_code, 400)
        self.assertEqual(SalesTicket.query.count(), 0)

    def test_cross_organization_routes_search_and_export_are_isolated(self):
        own = self.create_customer()
        owner_two, member_two = self.add_owner("second@customers.test")
        foreign = Customer(
            organization_id=member_two.organization_id,
            created_by_member_id=member_two.id,
            name="Ana secreta",
            phone="5559998888",
            phone_normalized="5559998888",
        )
        db.session.add(foreign)
        db.session.commit()
        client = self.client_for(self.owner)

        self.assertEqual(client.get(f"/customers/{foreign.id}").status_code, 404)
        self.assertEqual(
            client.get(f"/customers/{foreign.id}/edit").status_code,
            404,
        )
        search = client.get("/customers/api/search?q=Ana")
        self.assertEqual(
            [item["id"] for item in search.get_json()["customers"]],
            [own.id],
        )
        csv_body = client.get("/customers/export.csv?status=all").get_data(
            as_text=True
        )
        self.assertIn(own.name, csv_body)
        self.assertNotIn(foreign.name, csv_body)

    def test_validation_and_spanish_visible_content(self):
        client = self.client_for(self.owner)
        invalid = client.post(
            "/customers/api/quick",
            json={"name": "", "phone": "12"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(Customer.query.count(), 0)

        es = client.get("/customers").get_data(as_text=True)
        self.assertIn("Agregar cliente", es)

    def test_english_visible_content_in_module_and_pos(self):
        client = self.client_for(self.owner, language="en")
        en = client.get("/customers").get_data(as_text=True)
        self.assertIn("Customers", en)
        pos = client.get("/sell").get_data(as_text=True)
        self.assertIn("Quick add", pos)

    def test_simplified_customer_list_has_clear_toolbar_and_zero_balance_copy(self):
        self.create_customer()
        body = self.client_for(self.owner).get("/customers").get_data(as_text=True)
        self.assertIn("Buscar por nombre o teléfono", body)
        self.assertIn("Agregar cliente", body)
        self.assertIn("Sin saldo pendiente", body)
        self.assertNotIn("Total comprado</th>", body)

    def test_empty_customer_list_explains_the_first_action(self):
        body = self.client_for(self.owner).get("/customers").get_data(as_text=True)
        self.assertIn("Todavía no tienes clientes", body)
        self.assertIn("Agregar primer cliente", body)

    def test_customer_list_aggregates_do_not_use_n_plus_one(self):
        from sqlalchemy import event

        for index in range(12):
            self.create_customer(
                name=f"Cliente {index}",
                phone=f"238000{index:04d}",
                phone_normalized=f"238000{index:04d}",
                email=None,
            )
        query_count = {"value": 0}

        def count_query(*_args):
            query_count["value"] += 1

        event.listen(db.engine, "before_cursor_execute", count_query)
        try:
            rows = customer_summaries(self.membership.organization_id)
        finally:
            event.remove(db.engine, "before_cursor_execute", count_query)
        self.assertEqual(len(rows), 12)
        self.assertLessEqual(query_count["value"], 2)


if __name__ == "__main__":
    unittest.main()
