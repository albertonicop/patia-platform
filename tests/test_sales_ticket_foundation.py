import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "ticket-foundation-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

from app import create_app, db
from app.models import Product, Sale, SalesTicket, User
from app.routes import _create_sales_ticket


class SalesTicketFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-ticket-")
        database_path = Path(self.temp_dir.name, "tickets.db")
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def add_user(self, email):
        user = User(
            email=email,
            company_name=f"Empresa {email}",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        return user

    def add_product(self, user, *, sku="SKU-1", cost=10, price=25, stock=20):
        product = Product(
            user_id=user.id,
            name=f"Producto {sku}",
            sku=sku,
            category="General",
            cost_price=cost,
            sale_price=price,
            stock=stock,
            min_stock=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def client_for(self, user):
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
        return client

    def sell(self, user, product, payment_method="cash", quantity=1):
        return self.client_for(user).post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": payment_method,
                "items": [{"product_id": product.id, "quantity": quantity}],
            },
        )

    def test_companies_have_independent_sequences_and_isolated_public_tickets(self):
        first = self.add_user("first@patia.test")
        second = self.add_user("second@patia.test")
        first_product = self.add_product(first, sku="FIRST")
        second_product = self.add_product(second, sku="SECOND")

        first_response = self.sell(first, first_product)
        second_response = self.sell(second, second_product)

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        tickets = SalesTicket.query.order_by(SalesTicket.user_id).all()
        self.assertEqual([ticket.number for ticket in tickets], [1, 1])
        self.assertEqual([ticket.folio for ticket in tickets], ["TKT-000001"] * 2)
        self.assertEqual(
            self.client_for(first).get(
                f"/ticket/{second_response.get_json()['ticket_id']}"
            ).status_code,
            404,
        )

    def test_new_sale_freezes_cost_and_reprint_keeps_folio(self):
        user = self.add_user("cost@patia.test")
        product = self.add_product(user, cost=9.5, price=22)
        response = self.sell(user, product, payment_method="card", quantity=2)
        self.assertEqual(response.status_code, 200)
        ticket_ref = response.get_json()["ticket_id"]

        line = Sale.query.one()
        self.assertEqual(line.unit_cost, 9.5)
        self.assertFalse(line.cost_is_estimated)
        self.assertEqual(line.total, 44)
        product.cost_price = 18
        db.session.commit()
        db.session.refresh(line)
        self.assertEqual(line.unit_cost, 9.5)

        client = self.client_for(user)
        first_print = client.get(f"/ticket/{ticket_ref}").get_data(as_text=True)
        second_print = client.get(f"/ticket/{ticket_ref}").get_data(as_text=True)
        self.assertIn("<strong>TKT-000001</strong>", first_print)
        self.assertNotIn("Ticket: TKT-000001", first_print)
        self.assertEqual(
            first_print.count("<strong>TKT-000001</strong>"),
            second_print.count("<strong>TKT-000001</strong>"),
        )

    def test_all_supported_payment_methods_are_stored_on_header_and_lines(self):
        user = self.add_user("payments@patia.test")
        product = self.add_product(user, stock=20)

        for method in ("cash", "card", "transfer", "other"):
            response = self.sell(user, product, payment_method=method)
            self.assertEqual(response.status_code, 200)

        self.assertEqual(
            [ticket.payment_method for ticket in SalesTicket.query.order_by(SalesTicket.number)],
            ["cash", "card", "transfer", "other"],
        )
        self.assertEqual(
            [sale.payment_method for sale in Sale.query.order_by(Sale.id)],
            ["cash", "card", "transfer", "other"],
        )

    def test_unknown_historical_cost_remains_null_for_new_sale(self):
        user = self.add_user("unknown-cost@patia.test")
        product = self.add_product(user, cost=0)
        self.assertEqual(self.sell(user, product).status_code, 200)
        self.assertIsNone(Sale.query.one().unit_cost)
        self.assertFalse(Sale.query.one().cost_is_estimated)

    def test_payment_microcopy_is_available_in_english(self):
        user = self.add_user("english-pos@patia.test")
        user.preferred_language = "en"
        self.add_product(user)
        db.session.commit()
        client = self.client_for(user)
        with client.session_transaction() as flask_session:
            flask_session["language"] = "en"

        html = client.get("/sell").get_data(as_text=True)

        self.assertIn(
            "Select how the customer paid. This information will be used to record the sale.",
            html,
        )

    def test_legacy_sale_without_header_still_reprints(self):
        user = self.add_user("legacy@patia.test")
        product = self.add_product(user)
        public_id = str(uuid.uuid4())
        legacy = Sale(
            user_id=user.id,
            product_id=product.id,
            quantity=1,
            unit_price=25,
            total=25,
            ticket_id=public_id,
            payment_method="cash",
        )
        db.session.add(legacy)
        db.session.commit()

        response = self.client_for(user).get(f"/ticket/{public_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("V-", response.get_data(as_text=True))

    def test_atomic_counter_has_no_duplicate_numbers_under_concurrency(self):
        user = self.add_user("concurrent@patia.test")
        barrier = threading.Barrier(2)

        def allocate():
            with self.app.app_context():
                barrier.wait(timeout=10)
                ticket = _create_sales_ticket(user.id, "cash")
                number = ticket.number
                db.session.commit()
                db.session.remove()
                return number

        with ThreadPoolExecutor(max_workers=2) as executor:
            numbers = sorted(executor.map(lambda _: allocate(), range(2)))

        self.assertEqual(numbers, [1, 2])
        self.assertEqual(
            db.session.query(SalesTicket.number).distinct().count(),
            2,
        )


if __name__ == "__main__":
    unittest.main()
