import os
import unittest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "dashboard-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, User
from app.team.services import ensure_owner_organization


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
        self.assertIn("Productos más vendidos", html)

    def test_inventory_value_uses_current_stock_at_recorded_cost(self):
        user = self.make_user()
        self.add_product(user)

        html = self.dashboard_html()

        self.assertIn("$50.00 MXN", html)

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
        self.assertIn("isSingleBar", chart_source)
        self.assertIn("maxBarThickness", chart_source)


if __name__ == "__main__":
    unittest.main()
