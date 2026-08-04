import os
import unittest
from datetime import timedelta
from decimal import Decimal


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "dashboard-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from flask_babel import force_locale
from app.models import Product, Sale, User
from app.routes import analytics
from app.team.services import ensure_owner_organization
from app.timezones import local_date_bounds_utc, local_today


class DashboardOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.engine.dispose()
        cls.context.pop()

    def setUp(self):
        db.session.rollback()
        Sale.query.delete()
        Product.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()

    def make_user(self):
        user = User(
            email="owner@patia.test",
            company_name="Tienda PATIA",
            phone="5555555555",
            city="Puebla",
            state="Puebla",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = user.id
        return user

    def add_product(self, user):
        product = Product(
            organization_id=user.organization_memberships[0].organization_id,
            user_id=user.id,
            sku="SKU-1",
            name="Producto inicial",
            category="General",
            cost_price=10,
            sale_price=20,
            stock=5,
            min_stock=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def add_sale(self, user, product, *, total, created_at, ticket_id):
        sale = Sale(
            organization_id=user.organization_memberships[0].organization_id,
            user_id=user.id,
            product_id=product.id,
            quantity=1,
            unit_price=Decimal(total),
            total=Decimal(total),
            ticket_id=ticket_id,
            created_at=created_at,
        )
        db.session.add(sale)
        return sale

    def dashboard_html(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_empty_account_shows_first_product_onboarding(self):
        self.make_user()

        html = self.dashboard_html()

        self.assertIn('data-onboarding-progress="25"', html)
        self.assertIn("Agregar primer producto", html)
        self.assertIn("Importar catálogo", html)
        self.assertNotIn(">Registrar primera venta</a>", html)

    def test_product_without_sales_enables_first_sale_step(self):
        user = self.make_user()
        self.add_product(user)

        html = self.dashboard_html()

        self.assertIn('data-onboarding-progress="50"', html)
        self.assertIn(">Registrar primera venta</a>", html)
        self.assertNotIn("Importar catálogo</a>", html)

    def test_product_and_sale_hide_completed_onboarding(self):
        user = self.make_user()
        product = self.add_product(user)
        db.session.add(
            Sale(
                organization_id=user.organization_memberships[0].organization_id,
                user_id=user.id,
                product_id=product.id,
                quantity=1,
                unit_price=product.sale_price,
                total=product.sale_price,
            )
        )
        db.session.commit()

        html = self.dashboard_html()

        self.assertNotIn("data-onboarding-progress", html)
        self.assertNotIn("Pon PATIA en marcha", html)
        self.assertIn("Ventas frente al periodo anterior", html)
        self.assertIn('id="dashboardSalesChart"', html)
        self.assertIn('id="dashboardPaymentsChart"', html)
        self.assertIn("No tienes productos agotados", html)
        self.assertIn("Aún no hay un mes anterior comparable.", html)

    def test_inventory_value_uses_current_stock_at_recorded_cost(self):
        user = self.make_user()
        self.add_product(user)

        values = analytics(user)

        self.assertEqual(values["inventory_value"], Decimal("50.00"))

    def test_top_cards_use_out_of_stock_month_and_average_ticket_metrics(self):
        user = self.make_user()
        product = self.add_product(user)
        timezone_name = user.organization_memberships[0].organization.timezone
        today = local_today(timezone_name)
        today_start, _ = local_date_bounds_utc(
            today,
            today + timedelta(days=1),
            timezone_name,
        )
        current_month = today.replace(day=1)
        previous_month_last = current_month - timedelta(days=1)
        previous_month_start, _ = local_date_bounds_utc(
            previous_month_last.replace(day=1),
            current_month,
            timezone_name,
        )

        self.add_sale(
            user,
            product,
            total="10.00",
            created_at=today_start + timedelta(hours=8),
            ticket_id="today-ticket-a",
        )
        self.add_sale(
            user,
            product,
            total="20.00",
            created_at=today_start + timedelta(hours=8, minutes=1),
            ticket_id="today-ticket-a",
        )
        self.add_sale(
            user,
            product,
            total="20.00",
            created_at=today_start + timedelta(hours=9),
            ticket_id="today-ticket-b",
        )
        self.add_sale(
            user,
            product,
            total="20.00",
            created_at=previous_month_start + timedelta(days=1, hours=8),
            ticket_id="previous-ticket-a",
        )
        self.add_sale(
            user,
            product,
            total="20.00",
            created_at=previous_month_start + timedelta(days=2, hours=8),
            ticket_id="previous-ticket-b",
        )
        db.session.commit()

        with self.app.test_request_context("/"):
            values = analytics(user)["dashboard_summary"]
        self.assertEqual(Decimal("50.00"), values["today_sales"])
        self.assertEqual(2, values["today_tickets"])
        self.assertEqual(Decimal("50.00"), values["month_sales"])
        self.assertEqual(2, values["month_tickets"])
        self.assertEqual(Decimal("25.00"), values["month_average_ticket"])
        self.assertEqual(Decimal("25.0"), values["month_sales_change"])
        self.assertEqual(Decimal("25.0"), values["month_average_change"])
        self.assertEqual(0, values["out_of_stock"])

        html = self.dashboard_html()
        self.assertIn("Productos agotados", html)
        self.assertIn("No tienes productos agotados", html)
        self.assertIn(
            'href="/products?out_of_stock=1&amp;source=dashboard"',
            html,
        )
        self.assertIn("Ventas de hoy", html)
        self.assertIn("2 tickets registrados hoy", html)
        self.assertIn('href="/reports?period=today"', html)
        self.assertIn("Ventas del mes", html)
        self.assertNotIn("Ticket promedio", html)
        self.assertEqual(html.count("+25.0% vs. mes anterior"), 1)
        self.assertEqual(html.count('href="/reports?period=this_month"'), 1)
        self.assertNotIn(">Meta mensual<", html)
        self.assertNotIn("Proyección de cierre", html)

    def test_top_cards_explain_missing_comparison_and_empty_sales(self):
        user = self.make_user()

        with self.app.test_request_context("/"):
            values = analytics(user)["dashboard_summary"]
        self.assertEqual(Decimal("0.00"), values["today_sales"])
        self.assertEqual(0, values["today_tickets"])
        self.assertEqual(Decimal("0.00"), values["month_sales"])
        self.assertEqual(Decimal("0.00"), values["month_average_ticket"])
        self.assertIsNone(values["month_sales_change"])
        self.assertIsNone(values["month_average_change"])

        html = self.dashboard_html()
        self.assertIn("No tienes productos agotados", html)
        self.assertIn("0 tickets registrados hoy", html)
        self.assertIn("Aún no hay un mes anterior comparable.", html)
        self.assertNotIn("0.0% vs. mes anterior", html)

    def test_out_of_stock_card_counts_only_the_active_organization(self):
        user = self.make_user()
        own_product = self.add_product(user)
        own_product.stock = 0

        other = User(
            email="other-owner@patia.test",
            company_name="Otra tienda",
            email_verified=True,
        )
        other.set_password("Password123")
        db.session.add(other)
        db.session.flush()
        other_membership = ensure_owner_organization(other)
        db.session.add(Product(
            organization_id=other_membership.organization_id,
            user_id=other.id,
            sku="OTHER-EMPTY",
            name="Producto agotado ajeno",
            category="General",
            cost_price=10,
            sale_price=20,
            stock=0,
            min_stock=1,
        ))
        db.session.commit()

        with self.app.test_request_context("/"):
            values = analytics(user)["dashboard_summary"]
        self.assertEqual(1, values["out_of_stock"])

        html = self.dashboard_html()
        self.assertIn("1 producto sin stock", html)
        filtered = self.client.get("/products?out_of_stock=1&source=dashboard")
        filtered_html = filtered.get_data(as_text=True)
        self.assertEqual(200, filtered.status_code)
        self.assertIn("Productos agotados", filtered_html)
        self.assertIn("Producto inicial", filtered_html)
        self.assertNotIn("Producto agotado ajeno", filtered_html)

        user.preferred_language = "en"
        db.session.commit()
        with self.client.session_transaction() as session:
            session["language"] = "en"
        with force_locale("en"):
            english = self.dashboard_html()
        self.assertIn("Out-of-stock products", english)
        self.assertIn("1 product is out of stock", english)
        self.assertIn("View out-of-stock products", english)

    def test_single_product_chart_and_profit_explanation_are_rendered(self):
        user = self.make_user()
        product = self.add_product(user)
        db.session.add(Sale(
            organization_id=user.organization_memberships[0].organization_id,
            user_id=user.id,
            product_id=product.id,
            quantity=2,
            unit_price=20,
            unit_cost=product.cost_price,
            cost_is_estimated=False,
            total=40,
        ))
        db.session.commit()

        html = self.dashboard_html()

        self.assertIn("producto con más movimiento", html)
        self.assertIn("menos el costo registrado", html)
        with open("app/static/js/app.js", encoding="utf-8") as script:
            chart_source = script.read()
        self.assertIn("initializeDashboardCharts", chart_source)
        self.assertIn("maxBarThickness", chart_source)


if __name__ == "__main__":
    unittest.main()
