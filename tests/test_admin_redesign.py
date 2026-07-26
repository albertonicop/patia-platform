import os
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "admin-redesign-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_admin")
os.environ.setdefault("STRIPE_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_STARTER_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "price_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_admin")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Customer, MonthlyOwnerReport, Product, Sale, User
from app.plans import PRO
from app.team.services import ensure_owner_organization


class AdminRedesignTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-admin-v4-")
        database_path = Path(self.temp_dir.name, "admin.db")
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
        self.admin, self.admin_membership = self._add_owner(
            "albertonicopat@gmail.com", "Administracion PATIA"
        )
        self.trial, self.trial_membership = self._add_owner(
            "trial@example.com", "Abarrotes La Prueba"
        )
        self.pro, self.pro_membership = self._add_owner(
            "pro@example.com", "Tienda Pro"
        )
        self.pro.subscription_plan_code = PRO
        self.pro.subscription_status = "active"
        self.pro.stripe_subscription_id = "sub_admin_pro"
        self.pro.current_period_end = datetime.utcnow() + timedelta(days=30)
        self.pro_membership.organization.monthly_report_enabled = True

        product = Product(
            organization_id=self.pro_membership.organization_id,
            user_id=self.pro.id,
            name="Cafe",
            sku="ADMIN-CAFE",
            cost_price=Decimal("10.00"),
            sale_price=Decimal("25.00"),
            stock=5,
            min_stock=2,
        )
        db.session.add(product)
        db.session.flush()
        db.session.add(
            Sale(
                organization_id=self.pro_membership.organization_id,
                user_id=self.pro.id,
                product_id=product.id,
                quantity=2,
                unit_price=Decimal("25.00"),
                unit_cost=Decimal("10.00"),
                total=Decimal("50.00"),
                payment_method="cash",
            )
        )
        db.session.add(
            Customer(
                organization_id=self.pro_membership.organization_id,
                created_by_member_id=self.pro_membership.id,
                name="Cliente Pro",
            )
        )
        self.failed_report = MonthlyOwnerReport(
            organization_id=self.pro_membership.organization_id,
            report_year=2026,
            report_month=7,
            recipient=self.pro.email,
            status="failed",
            attempt_count=1,
            failure_code="DELIVERY_REJECTED",
        )
        db.session.add(self.failed_report)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def _add_owner(self, email, company_name):
        user = User(
            email=email,
            company_name=company_name,
            email_verified=True,
            preferred_language="es",
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def _login(self, user, membership):
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
            flask_session["language"] = "es"

    def test_admin_summary_prioritizes_commercial_metrics_and_attention(self):
        self._login(self.admin, self.admin_membership)

        response = self.client.get("/admin")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Clientes de pago", html)
        self.assertIn("Ingreso mensual estimado", html)
        self.assertIn("Requieren atenci", html)
        self.assertIn("Reporte mensual fallido", html)
        self.assertIn("1 negocio", html)
        self.assertIn("Tienda Pro", html)
        self.assertNotIn("MRR", html)

    def test_admin_filters_search_plan_and_attention(self):
        self._login(self.admin, self.admin_membership)

        response = self.client.get(
            "/admin?q=Tienda+Pro&plan=pro&attention=report"
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tienda Pro", html)
        self.assertNotIn("Abarrotes La Prueba", html)
        self.assertIn("Plan: Pro", html)
        self.assertIn("Reporte fallido", html)

    def test_admin_detail_shows_usage_and_failed_report_action(self):
        self._login(self.admin, self.admin_membership)

        response = self.client.get(
            f"/admin/organizations/{self.pro_membership.organization_id}"
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tienda Pro", html)
        self.assertIn("<dt>Clientes</dt><dd>1</dd>", html)
        self.assertIn("Reporte mensual", html)
        self.assertIn("Fall", html)
        self.assertIn("Reintentar reporte", html)

    def test_non_admin_cannot_open_admin_detail(self):
        self._login(self.trial, self.trial_membership)

        response = self.client.get(
            f"/admin/organizations/{self.pro_membership.organization_id}"
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/", response.location)

    def test_admin_can_retry_failed_report_explicitly(self):
        self._login(self.admin, self.admin_membership)

        def successful_retry(*args, **kwargs):
            self.failed_report.status = "sent"
            return self.failed_report, {}

        with patch(
            "app.monthly_reports.generate_monthly_report",
            side_effect=successful_retry,
        ) as retry:
            response = self.client.post(
                f"/admin/monthly-reports/{self.failed_report.id}/retry"
            )

        self.assertEqual(response.status_code, 302)
        retry.assert_called_once_with(
            self.pro_membership.organization_id,
            2026,
            7,
            send=True,
            force_retry=True,
        )


if __name__ == "__main__":
    unittest.main()
