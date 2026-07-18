import os
import tempfile
import unittest
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "timezone-test-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, SalesTicket, User
from app.routes import _parse_report_period, _report_analytics
from app.timezones import (
    DEFAULT_TIMEZONE,
    local_today,
    safe_timezone_name,
    utc_to_local,
)


class BusinessTimezoneTests(unittest.TestCase):
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
            STRIPE_DISABLED=True,
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            self.user = User(
                email="timezone@example.com",
                company_name="Tienda Horaria",
                email_verified=True,
                manual_pro_access=True,
                timezone=DEFAULT_TIMEZONE,
            )
            self.user.set_password("Password123")
            db.session.add(self.user)
            db.session.flush()
            product = Product(
                user_id=self.user.id,
                sku="TZ-1",
                name="Producto horario",
                category="Pruebas",
                cost_price=40,
                sale_price=75,
                stock=10,
                min_stock=1,
            )
            db.session.add(product)
            db.session.flush()
            ticket = SalesTicket(
                user_id=self.user.id,
                number=1,
                public_id="timezone-ticket",
                payment_method="cash",
                created_at=datetime(2026, 7, 18, 0, 26),
            )
            db.session.add(ticket)
            db.session.flush()
            sale = Sale(
                user_id=self.user.id,
                product_id=product.id,
                sales_ticket_id=ticket.id,
                ticket_id=ticket.public_id,
                payment_method="cash",
                quantity=1,
                unit_price=75,
                unit_cost=40,
                total=75,
                created_at=datetime(2026, 7, 18, 0, 26),
            )
            db.session.add(sale)
            db.session.commit()
            self.user_id = self.user.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()
        if os.path.exists(self.database_path):
            os.remove(self.database_path)
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def login(self):
        response = self.client.post(
            "/login",
            data={"email": "timezone@example.com", "password": "Password123"},
        )
        self.assertEqual(response.status_code, 302)

    def test_ticket_and_reprint_show_mexico_city_local_time(self):
        self.login()
        for url in ("/ticket/timezone-ticket", "/ticket/timezone-ticket?print=1"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("17/07/2026", html)
            self.assertIn("18:26", html)
            self.assertNotIn("18/07/2026", html)
            self.assertNotIn("00:26", html)

    def test_today_uses_local_midnight_and_daily_grouping(self):
        with self.app.app_context():
            product = Product.query.filter_by(user_id=self.user_id).first()
            late_local = Sale(
                user_id=self.user_id,
                product_id=product.id,
                quantity=1,
                unit_price=100,
                unit_cost=50,
                total=100,
                payment_method="card",
                created_at=datetime(2026, 7, 18, 5, 59),
            )
            next_local_day = Sale(
                user_id=self.user_id,
                product_id=product.id,
                quantity=1,
                unit_price=200,
                unit_cost=100,
                total=200,
                payment_method="card",
                created_at=datetime(2026, 7, 18, 6, 1),
            )
            db.session.add_all((late_local, next_local_day))
            db.session.commit()

            period = _parse_report_period(
                {"period": "today"},
                timezone_name=DEFAULT_TIMEZONE,
                now_utc=datetime(2026, 7, 18, 5, 30, tzinfo=timezone.utc),
            )
            with self.app.test_request_context("/reports?period=today"):
                report = _report_analytics(
                    self.user_id,
                    period,
                    timezone_name=DEFAULT_TIMEZONE,
                )

            self.assertEqual(period["start_at"], datetime(2026, 7, 17, 6, 0))
            self.assertEqual(period["end_before"], datetime(2026, 7, 18, 6, 0))
            self.assertEqual(report["report_kpis"]["sales"], 175)
            self.assertEqual(report["daily_report"], [{
                "date": "2026-07-17",
                "sales": 175.0,
                "profit": 85.0,
            }])

    def test_near_midnight_changes_local_day_without_changing_storage(self):
        stored = datetime(2026, 7, 18, 5, 30)
        mexico = utc_to_local(stored, "America/Mexico_City")
        tijuana = utc_to_local(stored, "America/Tijuana")
        self.assertEqual((mexico.day, mexico.hour, mexico.minute), (17, 23, 30))
        self.assertEqual((tijuana.day, tijuana.hour, tijuana.minute), (17, 22, 30))
        self.assertEqual(stored, datetime(2026, 7, 18, 5, 30))

    def test_invalid_timezone_falls_back_safely(self):
        self.assertEqual(safe_timezone_name("Invalid/Timezone"), DEFAULT_TIMEZONE)
        self.assertEqual(
            local_today(
                "Invalid/Timezone",
                datetime(2026, 7, 18, 2, 0, tzinfo=timezone.utc),
            ).isoformat(),
            "2026-07-17",
        )

        self.login()
        response = self.client.post(
            "/settings",
            data={
                "company_name": "Tienda Horaria",
                "timezone": "Invalid/Timezone",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            self.assertEqual(user.timezone, DEFAULT_TIMEZONE)

    def test_changing_timezone_changes_presentation_not_stored_timestamp(self):
        self.login()
        response = self.client.post(
            "/settings",
            data={
                "company_name": "Tienda Horaria",
                "timezone": "America/Tijuana",
            },
        )
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            sale = Sale.query.filter_by(ticket_id="timezone-ticket").one()
            self.assertEqual(sale.created_at, datetime(2026, 7, 18, 0, 26))
            self.assertEqual(
                db.session.get(User, self.user_id).timezone,
                "America/Tijuana",
            )

        ticket = self.client.get("/ticket/timezone-ticket").get_data(as_text=True)
        self.assertIn("17/07/2026", ticket)
        self.assertIn("17:26", ticket)

    def test_settings_offer_supported_timezones_and_recent_sales_are_local(self):
        self.login()
        settings = self.client.get("/settings").get_data(as_text=True)
        for timezone_name in (
            "America/Mexico_City",
            "America/Cancun",
            "America/Tijuana",
            "America/Hermosillo",
            "America/Chihuahua",
        ):
            self.assertIn(timezone_name, settings)

        sales = self.client.get("/sell").get_data(as_text=True)
        self.assertIn("17/07/2026 18:26", sales)


if __name__ == "__main__":
    unittest.main()
