import os
import tempfile
import unittest
import uuid
from datetime import date, datetime, time, timedelta


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "reports-sprint2-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, SalesTicket, User
from app.routes import _parse_report_period, _report_analytics


class ReportSprint2Tests(unittest.TestCase):
    def setUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.database_path}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.user = self.add_user("reports@patia.test")
        self.client = self.app.test_client()
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = self.user.id

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        os.remove(self.database_path)
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def add_user(self, email):
        user = User(
            email=email,
            company_name=email,
            email_verified=True,
            manual_pro_access=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        return user

    def product(self, user, name, sku, cost=10):
        product = Product(
            user_id=user.id,
            name=name,
            sku=sku,
            category="General",
            cost_price=cost,
            sale_price=100,
            stock=20,
            min_stock=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def ticket_sale(
        self,
        user,
        product,
        *,
        number,
        method,
        quantity,
        unit_price,
        unit_cost,
        created_at,
    ):
        ticket = SalesTicket(
            user_id=user.id,
            number=number,
            public_id=str(uuid.uuid4()),
            payment_method=method,
            created_at=created_at,
        )
        db.session.add(ticket)
        db.session.flush()
        sale = Sale(
            user_id=user.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=unit_price,
            unit_cost=unit_cost,
            cost_is_estimated=False,
            total=quantity * unit_price,
            ticket_id=ticket.public_id,
            sales_ticket_id=ticket.id,
            payment_method=method,
            created_at=created_at,
        )
        db.session.add(sale)
        db.session.commit()
        return ticket, sale

    def parse_period(self, args, *, today=None):
        with self.app.test_request_context("/reports"):
            return _parse_report_period(args, today=today)

    def report_analytics(self, user_id, period):
        with self.app.test_request_context("/reports"):
            return _report_analytics(user_id, period)

    def test_period_parser_supports_presets_custom_and_safe_fallbacks(self):
        today = date(2026, 7, 17)
        self.assertEqual(
            self.parse_period({"period": "today"}, today=today)["start_date"],
            today,
        )
        self.assertEqual(
            self.parse_period({"period": "30d"}, today=today)["start_date"],
            date(2026, 6, 18),
        )
        previous = self.parse_period({"period": "previous_month"}, today=today)
        self.assertEqual((previous["start_date"], previous["end_date"]), (date(2026, 6, 1), date(2026, 6, 30)))
        custom = self.parse_period(
            {"period": "custom", "start": "2026-01-01", "end": "2026-12-31"},
            today=today,
        )
        self.assertEqual(custom["period"], "custom")
        self.assertIsNone(custom["error"])

        for args in (
            {"period": "unsupported"},
            {"period": "custom", "start": "2026-07-18", "end": "2026-07-17"},
            {"period": "custom", "start": "2025-01-01", "end": "2026-07-17"},
            {"period": "custom", "start": "not-a-date", "end": "2026-07-17"},
        ):
            parsed = self.parse_period(args, today=today)
            self.assertEqual(parsed["period"], "7d")
            self.assertIsNotNone(parsed["error"])

    def test_kpis_profitability_payments_and_isolation_use_historical_cost(self):
        known = self.product(self.user, "Producto rentable", "KNOWN", cost=999)
        unknown = self.product(self.user, "Producto sin costo", "UNKNOWN", cost=1)
        now = datetime.combine(datetime.utcnow().date(), time(12))
        self.ticket_sale(
            self.user,
            known,
            number=1,
            method="cash",
            quantity=2,
            unit_price=100,
            unit_cost=50,
            created_at=now,
        )
        self.ticket_sale(
            self.user,
            unknown,
            number=2,
            method="card",
            quantity=1,
            unit_price=50,
            unit_cost=None,
            created_at=now,
        )
        other_user = self.add_user("other@patia.test")
        other_product = self.product(other_user, "Producto ajeno", "OTHER")
        self.ticket_sale(
            other_user,
            other_product,
            number=1,
            method="cash",
            quantity=1,
            unit_price=900,
            unit_cost=1,
            created_at=now,
        )

        period = self.parse_period({"period": "today"}, today=now.date())
        report = self.report_analytics(self.user.id, period)

        self.assertEqual(report["report_kpis"]["sales"], 250)
        self.assertEqual(report["report_kpis"]["profit"], 100)
        self.assertEqual(report["report_kpis"]["margin"], 50)
        self.assertEqual(report["report_kpis"]["average_ticket"], 125)
        self.assertEqual(report["report_kpis"]["ticket_count"], 2)
        self.assertEqual(report["unknown_cost_lines"], 1)
        payments = {item["key"]: item for item in report["payments_report"]}
        self.assertEqual(payments["cash"]["amount"], 200)
        self.assertEqual(payments["cash"]["tickets"], 1)
        self.assertEqual(payments["card"]["amount"], 50)
        profitable = {
            item["name"]: item
            for item in report["profitable_products_report"]
        }
        self.assertEqual(profitable["Producto rentable"]["cost"], 100)
        self.assertEqual(profitable["Producto rentable"]["profit"], 100)
        self.assertEqual(profitable["Producto rentable"]["margin"], 50)
        self.assertIsNone(profitable["Producto sin costo"]["profit"])
        self.assertNotIn("Producto ajeno", profitable)

    def test_period_filter_excludes_sales_outside_range(self):
        product = self.product(self.user, "Producto", "FILTER")
        now = datetime.utcnow()
        self.ticket_sale(
            self.user,
            product,
            number=1,
            method="transfer",
            quantity=1,
            unit_price=75,
            unit_cost=25,
            created_at=now,
        )
        self.ticket_sale(
            self.user,
            product,
            number=2,
            method="other",
            quantity=1,
            unit_price=500,
            unit_cost=100,
            created_at=now - timedelta(days=40),
        )

        report = self.report_analytics(
            self.user.id,
            self.parse_period({"period": "30d"}, today=now.date()),
        )
        self.assertEqual(report["report_kpis"]["sales"], 75)
        self.assertEqual(report["report_kpis"]["ticket_count"], 1)

    def test_report_renders_both_languages_and_unknown_cost_warning(self):
        product = self.product(self.user, "Nombre sin traducir", "LANG")
        self.ticket_sale(
            self.user,
            product,
            number=1,
            method="other",
            quantity=1,
            unit_price=40,
            unit_cost=None,
            created_at=datetime.utcnow(),
        )

        self.client.post("/language", data={"language": "en"})
        english_client = self.app.test_client()
        with english_client.session_transaction() as flask_session:
            flask_session["user_id"] = self.user.id
            flask_session["language"] = "en"
        english = english_client.get("/reports?period=today").get_data(as_text=True)
        self.assertIn("Sales vs Profit", english)
        self.assertIn("Not available", english)
        self.assertIn(
            "Add the product cost to include it in profit and margin calculations.",
            english,
        )
        self.assertIn("Nombre sin traducir", english)

    def test_custom_range_is_conditional_and_preserves_invalid_values(self):
        for period in ("today", "7d", "30d", "this_month", "previous_month"):
            html = self.client.get(
                f"/reports?period={period}"
            ).get_data(as_text=True)
            self.assertIn(
                'id="custom-period" class="reports-v3__custom-period"',
                html,
            )
            custom_form = html.split('id="custom-period"', 1)[1].split(">", 1)[0]
            self.assertIn("hidden", custom_form)

        fallback = self.client.get(
            "/reports?show_custom=1"
        ).get_data(as_text=True)
        custom_form = fallback.split('id="custom-period"', 1)[1].split(">", 1)[0]
        self.assertNotIn("hidden", custom_form)
        self.assertIn('aria-expanded="true"', fallback)

        invalid = self.client.get(
            "/reports?period=custom&start=2026-07-18&end=2026-07-17"
        ).get_data(as_text=True)
        custom_form = invalid.split('id="custom-period"', 1)[1].split(">", 1)[0]
        self.assertNotIn("hidden", custom_form)
        self.assertIn('value="2026-07-18"', invalid)
        self.assertIn('value="2026-07-17"', invalid)

    def test_invalid_custom_range_is_visible_and_falls_back(self):
        html = self.client.get(
            "/reports?period=custom&start=2026-07-18&end=2026-07-17"
        ).get_data(as_text=True)
        self.assertIn("La fecha inicial no puede ser posterior", html)
        self.assertIn("Ventas totales", html)
        self.assertIn('href="/reports?period=7d"', html)


if __name__ == "__main__":
    unittest.main()
