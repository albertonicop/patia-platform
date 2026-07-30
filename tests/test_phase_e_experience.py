import os
import unittest
from datetime import datetime, timedelta


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "phase-e-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, Supplier, User
from app.team.services import ensure_owner_organization


class PhaseEExperienceTests(unittest.TestCase):
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
        Supplier.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()

    def login_user(self, *, pro=False, days_used=2):
        user = User(
            email=("pro" if pro else "trial") + "@phase-e.test",
            company_name="Negocio Fase E",
            email_verified=True,
            manual_pro_access=pro,
            created_at=datetime.utcnow() - timedelta(days=days_used),
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = user.id
        return user

    def test_empty_suppliers_has_explanation_and_first_cta(self):
        self.login_user()
        html = self.client.get("/suppliers").get_data(as_text=True)

        self.assertIn("Aún no tienes proveedores", html)
        self.assertIn("Agregar primer proveedor", html)
        self.assertNotIn("<tbody>", html)

    def test_suppliers_with_data_keep_list_whatsapp_and_actions(self):
        user = self.login_user()
        db.session.add(Supplier(organization_id=user.organization_memberships[0].organization_id, user_id=user.id, name="Distribuidora Uno", contact="Ana", phone="238 123-4567"))
        db.session.commit()
        html = self.client.get("/suppliers").get_data(as_text=True)

        self.assertIn("Distribuidora Uno", html)
        self.assertIn("https://wa.me/2381234567", html)
        self.assertIn("/suppliers/1/delete", html)
        self.assertIn('name="csrf_token"', html)

    def test_reports_without_sales_has_no_canvas_or_chart_payload(self):
        self.login_user(pro=True)
        html = self.client.get("/reports").get_data(as_text=True)

        self.assertIn("Aún no hay ventas en este periodo", html)
        self.assertNotIn('id="reportTrendChart"', html)
        self.assertNotIn("window.reportAnalytics", html)
        self.assertNotIn("```", html)

    def test_reports_with_sales_keep_chart_and_existing_data(self):
        user = self.login_user(pro=True)
        organization_id = user.organization_memberships[0].organization_id
        product = Product(organization_id=organization_id, user_id=user.id, sku="REP-1", name="Producto reporte", category="Bebidas", cost_price=10, sale_price=20, stock=4, min_stock=1)
        db.session.add(product)
        db.session.flush()
        db.session.add(Sale(organization_id=organization_id, user_id=user.id, product_id=product.id, quantity=2, unit_price=20, total=40))
        db.session.commit()
        html = self.client.get("/reports").get_data(as_text=True)

        self.assertIn('id="reportTrendChart"', html)
        self.assertIn('id="reportPaymentsChart"', html)
        self.assertIn("window.reportAnalytics", html)
        self.assertIn("Producto reporte", html)

    def test_empty_recommendations_have_useful_state(self):
        self.login_user(pro=True)
        html = self.client.get("/reports").get_data(as_text=True)
        self.assertIn("Aún no hay ventas en este periodo", html)

    def test_dashboard_explains_data_without_claiming_an_ai_copilot(self):
        user = self.login_user(pro=True)
        organization_id = user.organization_memberships[0].organization_id
        product = Product(
            organization_id=organization_id,
            user_id=user.id,
            sku="PULSE-1",
            name="Producto Pulso",
            category="Prueba",
            cost_price=10,
            sale_price=20,
            stock=2,
            min_stock=3,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(
            Sale(
                organization_id=organization_id,
                user_id=user.id,
                product_id=product.id,
                quantity=1,
                unit_price=20,
                unit_cost=10,
                total=20,
            )
        )
        db.session.commit()

        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("Lectura de tu negocio", html)
        self.assertIn("De dónde sale:", html)
        self.assertIn("Pulso PATIA", html)
        self.assertIn("Ver qué surtir", html)
        self.assertNotIn("Copiloto PATIA", html)

    def test_navigation_marks_active_section_and_has_mobile_toggle(self):
        self.login_user()
        html = self.client.get("/suppliers").get_data(as_text=True)

        self.assertIn('href="/suppliers" class="is-active" aria-current="page"', html)
        self.assertIn('class="sidebar-v2__toggle"', html)
        self.assertIn('aria-controls="primary-navigation"', html)

    def test_whatsapp_help_is_contained_in_sidebar_without_losing_accessibility(self):
        self.login_user()
        html = self.client.get("/suppliers").get_data(as_text=True)

        sidebar = html[html.index('<aside class="sidebar sidebar-v2">'):html.index("</aside>")]
        self.assertIn('class="whatsapp-float whatsapp-float-v2"', sidebar)
        self.assertIn('aria-label="Contactar a PATIA por WhatsApp"', sidebar)
        self.assertIn('target="_blank"', sidebar)
        self.assertIn('rel="noopener noreferrer"', sidebar)

    def test_logout_stays_post_with_csrf(self):
        self.login_user()
        html = self.client.get("/suppliers").get_data(as_text=True)

        self.assertIn('method="POST" action="/logout"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertNotIn('href="/logout"', html)

    def test_trial_and_pro_messages_use_central_access(self):
        self.login_user(days_used=4)
        trial_html = self.client.get("/suppliers").get_data(as_text=True)
        self.assertIn("Prueba de Starter", trial_html)
        self.assertIn("10 días restantes · $199 MXN/mes después", trial_html)
        self.assertIn("Comparar planes", trial_html)
        self.assertNotIn("Plan actual: Acceso manual", trial_html)

        self.setUp()
        self.login_user(pro=True)
        pro_html = self.client.get("/suppliers").get_data(as_text=True)
        self.assertIn("Plan actual: Acceso manual", pro_html)
        self.assertIn("Acceso activo", pro_html)
        self.assertNotIn("Comparar planes", pro_html)


if __name__ == "__main__":
    unittest.main()
