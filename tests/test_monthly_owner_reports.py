import os
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "monthly-report-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_monthly")
os.environ.setdefault("STRIPE_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_STARTER_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "price_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_monthly")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import MonthlyOwnerReport, Product, Sale, User
from app.monthly_reports import (
    MonthlyReportUnavailable,
    generate_monthly_report,
    report_payload,
)
from app.plans import PRO, STARTER
from app.team.services import ensure_owner_organization


class MonthlyOwnerReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="patia-monthly-report-"
        )
        database_path = Path(self.temp_dir.name, "reports.db")
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
        self.owner = User(
            email="owner-monthly@example.com",
            company_name="Tienda Mensual",
            email_verified=True,
            preferred_language="es",
            subscription_plan_code=PRO,
            subscription_status="active",
            stripe_subscription_id="sub_monthly",
            current_period_end=datetime.utcnow() + timedelta(days=30),
        )
        self.owner.set_password("Password123")
        db.session.add(self.owner)
        db.session.flush()
        self.membership = ensure_owner_organization(self.owner)
        self.organization = self.membership.organization
        self.organization.monthly_report_enabled = True
        self.organization.monthly_report_recipient = (
            "reports-monthly@example.com"
        )
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

    def add_sale(
        self,
        *,
        created_at,
        name="Café",
        quantity=2,
        unit_price="30.00",
        unit_cost="10.00",
    ):
        product = Product(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            sku=f"SKU-{Product.query.count() + 1}",
            name=name,
            category="Abarrotes",
            cost_price=Decimal(unit_cost),
            sale_price=Decimal(unit_price),
            stock=8,
            min_stock=10,
        )
        db.session.add(product)
        db.session.flush()
        sale = Sale(
            organization_id=self.organization.id,
            user_id=self.owner.id,
            product_id=product.id,
            quantity=quantity,
            unit_price=Decimal(unit_price),
            unit_cost=Decimal(unit_cost),
            total=Decimal(unit_price) * quantity,
            payment_method="cash",
            created_at=created_at,
        )
        db.session.add(sale)
        db.session.commit()
        return sale

    def test_report_uses_accurate_month_and_previous_month_comparison(self):
        self.add_sale(created_at=datetime(2026, 6, 15, 18), unit_price="20")
        self.add_sale(created_at=datetime(2026, 7, 15, 18), unit_price="30")

        payload = report_payload(self.organization, 2026, 7)

        self.assertEqual(
            payload["analytics"]["report_kpis"]["sales"],
            Decimal("60.00"),
        )
        self.assertEqual(
            payload["analytics"]["report_kpis"]["profit"],
            Decimal("40.00"),
        )
        self.assertEqual(payload["comparison"], 50.0)
        self.assertEqual(
            payload["analytics"]["top_selling_report"][0].name, "Café"
        )
        self.assertEqual(payload["inventory"]["low_stock"][0]["name"], "Café")

    def test_report_without_previous_sales_or_activity_is_valid(self):
        payload = report_payload(self.organization, 2026, 7)

        self.assertEqual(
            payload["analytics"]["report_kpis"]["sales"], Decimal("0.00")
        )
        self.assertIsNone(payload["comparison"])
        self.assertEqual(payload["analytics"]["top_selling_report"], [])

    def test_delivery_is_persistent_idempotent_and_uses_one_recipient(self):
        self.add_sale(created_at=datetime(2026, 7, 15, 18))
        with patch("app.routes.send_email", return_value=True) as send:
            first, _ = generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )
            second, payload = generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )

        self.assertEqual(first.status, "sent")
        self.assertEqual(second.id, first.id)
        self.assertIsNone(payload)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            send.call_args.kwargs["to"], "reports-monthly@example.com"
        )
        self.assertEqual(
            send.call_args.kwargs["idempotency_key"],
            f"patia-monthly-report-{self.organization.id}-2026-07",
        )
        self.assertEqual(MonthlyOwnerReport.query.count(), 1)

    def test_failed_delivery_can_retry_without_duplicate_period(self):
        with patch(
            "app.routes.send_email", side_effect=[False, True]
        ) as send:
            failed, _ = generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )
            retried, _ = generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )

        self.assertEqual(failed.id, retried.id)
        self.assertEqual(retried.status, "sent")
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            send.call_args_list[0].kwargs["idempotency_key"],
            send.call_args_list[1].kwargs["idempotency_key"],
        )
        self.assertEqual(MonthlyOwnerReport.query.count(), 1)

    def test_stale_sending_claim_can_retry_with_provider_idempotency(self):
        record = MonthlyOwnerReport(
            organization_id=self.organization.id,
            report_year=2026,
            report_month=7,
            recipient=self.owner.email,
            status="sending",
            generated_at=datetime.utcnow() - timedelta(hours=2),
        )
        db.session.add(record)
        db.session.commit()

        with patch("app.routes.send_email", return_value=True) as send:
            retried, _ = generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )

        self.assertEqual(retried.id, record.id)
        self.assertEqual(retried.status, "sent")
        send.assert_called_once()

    def test_starter_and_disabled_delivery_are_rejected_but_preview_never_sends(self):
        self.owner.subscription_plan_code = STARTER
        db.session.commit()
        with self.assertRaises(MonthlyReportUnavailable):
            generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )

        self.owner.subscription_plan_code = PRO
        self.organization.monthly_report_enabled = False
        db.session.commit()
        with self.assertRaises(MonthlyReportUnavailable):
            generate_monthly_report(
                self.organization.id, 2026, 7, send=True
            )
        with patch("app.routes.send_email") as send:
            record, payload = generate_monthly_report(
                self.organization.id,
                2026,
                7,
                send=False,
                preview=True,
            )
        self.assertEqual(record.status, "generated")
        self.assertIn("subject", payload)
        send.assert_not_called()
