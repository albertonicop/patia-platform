import os
import unittest
import calendar
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "stripe-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import StripeWebhookEvent, User
from app.routes import has_pro_access


def utc_timestamp(value):
    return calendar.timegm(value.utctimetuple())


class StripeFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
            STRIPE_PRICE_ID="price_patia_pro",
            STRIPE_STARTER_PRICE_ID="price_patia_starter",
            STRIPE_PRO_PRICE_ID="price_patia_pro",
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
        StripeWebhookEvent.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()

    def make_user(self, email="owner@patia.test", **values):
        user = User(email=email, company_name="Negocio PATIA", **values)
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        return user

    def login(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = user.id

    def subscription(self, user, status="active", period_end=None):
        period_end = period_end or datetime.utcnow() + timedelta(days=30)
        return {
            "id": user.stripe_subscription_id or "sub_patia",
            "customer": user.stripe_customer_id or "cus_patia",
            "status": status,
            "current_period_end": utc_timestamp(period_end),
            "cancel_at_period_end": False,
            "metadata": {"user_id": str(user.id)},
            "items": {
                "data": [
                    {
                        "price": {"id": "price_patia_pro"},
                        "current_period_end": utc_timestamp(period_end),
                    }
                ]
            },
        }

    def send_event(self, event, subscription=None):
        subscription = subscription or {}
        with (
            patch("app.routes.stripe.Webhook.construct_event", return_value=event),
            patch("app.routes.stripe.Subscription.retrieve", return_value=subscription),
        ):
            return self.client.post(
                "/stripe-webhook",
                data=b"{}",
                headers={"Stripe-Signature": "valid"},
            )

    def invoice_event(self, event_id, event_type, user, created, **extra):
        invoice = {
            "id": f"in_{event_id}",
            "customer": user.stripe_customer_id,
            "subscription": user.stripe_subscription_id,
        }
        invoice.update(extra)
        return {
            "id": event_id,
            "type": event_type,
            "created": created,
            "data": {"object": invoice},
        }

    def test_duplicate_webhook_is_processed_once(self):
        user = self.make_user(
            stripe_customer_id="cus_1",
            stripe_subscription_id="sub_1",
        )
        created = utc_timestamp(datetime.utcnow())
        event = self.invoice_event("evt_paid_once", "invoice.paid", user, created)
        subscription = self.subscription(user)

        first = self.send_event(event, subscription)
        second = self.send_event(event, subscription)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(StripeWebhookEvent.query.count(), 1)
        self.assertEqual(user.subscription_status, "active")
        self.assertEqual(user.plan, "pro")

    def test_checkout_completed_binds_ids_without_granting_pro(self):
        user = self.make_user()
        subscription = self.subscription(user, status="active")
        event = {
            "id": "evt_checkout_completed",
            "type": "checkout.session.completed",
            "created": utc_timestamp(datetime.utcnow()),
            "data": {
                "object": {
                    "id": "cs_patia",
                    "mode": "subscription",
                    "client_reference_id": str(user.id),
                    "metadata": {"user_id": str(user.id)},
                    "customer": "cus_patia",
                    "subscription": "sub_patia",
                }
            },
        }
        response = self.send_event(event, subscription)

        self.assertEqual(response.status_code, 200)
        db.session.refresh(user)
        self.assertEqual(user.stripe_customer_id, "cus_patia")
        self.assertEqual(user.stripe_subscription_id, "sub_patia")
        self.assertEqual(user.plan, "trial")
        self.assertIsNone(user.subscription_status)

    def test_subscription_updated_handles_all_supported_statuses(self):
        user = self.make_user(
            stripe_customer_id="cus_states",
            stripe_subscription_id="sub_states",
        )
        expected_access = {
            "trialing": True,
            "active": True,
            "past_due": True,
            "unpaid": False,
            "canceled": False,
            "incomplete": False,
            "incomplete_expired": False,
            "paused": False,
        }
        created = utc_timestamp(datetime.utcnow())
        for offset, (status, allowed) in enumerate(expected_access.items(), start=1):
            subscription = self.subscription(user, status=status)
            event = {
                "id": f"evt_status_{status}",
                "type": "customer.subscription.updated",
                "created": created + offset,
                "data": {"object": subscription},
            }
            response = self.send_event(event)
            self.assertEqual(response.status_code, 200, status)
            db.session.refresh(user)
            self.assertEqual(has_pro_access(user), allowed, status)

    def test_older_paid_event_cannot_override_deleted(self):
        user = self.make_user(
            stripe_customer_id="cus_2",
            stripe_subscription_id="sub_2",
            subscription_status="active",
        )
        now = utc_timestamp(datetime.utcnow())
        deleted_subscription = self.subscription(user, status="canceled")
        deleted = {
            "id": "evt_deleted_new",
            "type": "customer.subscription.deleted",
            "created": now,
            "data": {"object": deleted_subscription},
        }
        self.assertEqual(self.send_event(deleted).status_code, 200)

        paid = self.invoice_event("evt_paid_old", "invoice.paid", user, now - 60)
        active_subscription = self.subscription(user, status="active")
        self.assertEqual(self.send_event(paid, active_subscription).status_code, 200)

        db.session.refresh(user)
        self.assertEqual(user.subscription_status, "canceled")
        self.assertEqual(user.plan, "trial")

    def test_older_invoice_paid_is_processed_after_active_subscription_update(self):
        user = self.make_user(
            stripe_customer_id="cus_order_a",
            stripe_subscription_id="sub_order_a",
        )
        now = utc_timestamp(datetime.utcnow())
        updated = {
            "id": "evt_subscription_newer",
            "type": "customer.subscription.updated",
            "created": now,
            "data": {"object": self.subscription(user, status="active")},
        }
        self.assertEqual(self.send_event(updated).status_code, 200)

        paid = self.invoice_event(
            "evt_invoice_older",
            "invoice.paid",
            user,
            now - 30,
        )
        self.assertEqual(
            self.send_event(paid, self.subscription(user, status="active")).status_code,
            200,
        )

        record = StripeWebhookEvent.query.filter_by(
            stripe_event_id="evt_invoice_older"
        ).one()
        db.session.refresh(user)
        self.assertEqual(record.status, "processed")
        self.assertEqual(user.stripe_invoice_updated_at, datetime.utcfromtimestamp(now - 30))
        self.assertEqual(user.subscription_status, "active")

    def test_newer_invoice_paid_is_processed_after_subscription_update(self):
        user = self.make_user(
            stripe_customer_id="cus_order_b",
            stripe_subscription_id="sub_order_b",
        )
        now = utc_timestamp(datetime.utcnow())
        updated = {
            "id": "evt_subscription_older",
            "type": "customer.subscription.updated",
            "created": now - 30,
            "data": {"object": self.subscription(user, status="active")},
        }
        self.assertEqual(self.send_event(updated).status_code, 200)

        paid = self.invoice_event(
            "evt_invoice_newer",
            "invoice.paid",
            user,
            now,
        )
        self.assertEqual(
            self.send_event(paid, self.subscription(user, status="active")).status_code,
            200,
        )

        db.session.refresh(user)
        self.assertEqual(user.stripe_invoice_updated_at, datetime.utcfromtimestamp(now))
        self.assertEqual(user.subscription_status, "active")

    def test_double_click_uses_same_checkout_idempotency_key(self):
        user = self.make_user(email_verified=True)
        self.login(user)
        checkout = SimpleNamespace(url="https://checkout.stripe.test/session")
        with patch("app.routes.stripe.checkout.Session.create", return_value=checkout) as create:
            first = self.client.post("/create-checkout-session")
            second = self.client.post("/create-checkout-session")

        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        self.assertEqual(create.call_count, 2)
        first_key = create.call_args_list[0].kwargs["idempotency_key"]
        second_key = create.call_args_list[1].kwargs["idempotency_key"]
        self.assertEqual(first_key, second_key)

    def test_invoice_paid_grants_and_renews_access(self):
        user = self.make_user(
            stripe_customer_id="cus_paid",
            stripe_subscription_id="sub_paid",
        )
        period_end = datetime.utcnow() + timedelta(days=31)
        event = self.invoice_event(
            "evt_invoice_paid",
            "invoice.paid",
            user,
            utc_timestamp(datetime.utcnow()),
        )
        response = self.send_event(event, self.subscription(user, period_end=period_end))

        self.assertEqual(response.status_code, 200)
        db.session.refresh(user)
        self.assertEqual(user.plan, "pro")
        self.assertEqual(user.subscription_status, "active")
        self.assertAlmostEqual(
            utc_timestamp(user.current_period_end), utc_timestamp(period_end), delta=1
        )

    def test_payment_failed_records_retry_and_past_due(self):
        user = self.make_user(
            stripe_customer_id="cus_failed",
            stripe_subscription_id="sub_failed",
            subscription_status="active",
        )
        retry = datetime.utcnow() + timedelta(days=1)
        event = self.invoice_event(
            "evt_payment_failed",
            "invoice.payment_failed",
            user,
            utc_timestamp(datetime.utcnow()),
            next_payment_attempt=utc_timestamp(retry),
        )
        response = self.send_event(event, self.subscription(user))

        self.assertEqual(response.status_code, 200)
        db.session.refresh(user)
        self.assertEqual(user.subscription_status, "past_due")
        self.assertAlmostEqual(
            utc_timestamp(user.next_payment_attempt), utc_timestamp(retry), delta=1
        )

    def test_subscription_deleted_revokes_paid_access(self):
        user = self.make_user(
            stripe_customer_id="cus_deleted",
            stripe_subscription_id="sub_deleted",
            subscription_status="active",
            current_period_end=datetime.utcnow() + timedelta(days=10),
            plan="pro",
        )
        subscription = self.subscription(user, status="canceled")
        event = {
            "id": "evt_subscription_deleted",
            "type": "customer.subscription.deleted",
            "created": utc_timestamp(datetime.utcnow()),
            "data": {"object": subscription},
        }
        response = self.send_event(event)

        self.assertEqual(response.status_code, 200)
        db.session.refresh(user)
        self.assertEqual(user.subscription_status, "canceled")
        self.assertEqual(user.plan, "trial")
        self.assertFalse(has_pro_access(user))

    def test_manual_pro_access_is_independent_from_stripe(self):
        user = self.make_user(
            manual_pro_access=True,
            subscription_status="canceled",
            plan="pro",
        )
        self.assertTrue(has_pro_access(user))

    def test_legacy_plan_pro_does_not_become_manual_pro(self):
        user = self.make_user(plan="pro")
        db.session.refresh(user)
        self.assertFalse(user.manual_pro_access)
        self.assertFalse(has_pro_access(user))

    def test_login_trial_warning_uses_central_access(self):
        user = self.make_user(
            email="legacy-pro@patia.test",
            plan="pro",
            created_at=datetime.utcnow() - timedelta(days=13),
        )
        with patch("app.routes.send_email") as send_email:
            response = self.client.post(
                "/login",
                data={"email": user.email, "password": "Password123"},
            )
        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()

    def test_dashboard_and_base_use_central_access(self):
        legacy = self.make_user(email="visual-trial@patia.test", plan="pro")
        self.login(legacy)
        trial_page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Prueba de Starter", trial_page)
        self.assertNotIn("Plan actual: Acceso manual", trial_page)
        self.assertIn('href="/reports"', trial_page)

        manual = self.make_user(
            email="visual-manual@patia.test",
            plan="trial",
            manual_pro_access=True,
        )
        self.login(manual)
        pro_page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Plan actual: Acceso manual", pro_page)
        self.assertIn('href="/reports"', pro_page)

    def test_active_subscriber_is_sent_to_portal_not_checkout(self):
        user = self.make_user(
            stripe_customer_id="cus_active",
            stripe_subscription_id="sub_active",
            subscription_status="active",
            current_period_end=datetime.utcnow() + timedelta(days=20),
            plan="pro",
            email_verified=True,
        )
        self.login(user)
        portal = SimpleNamespace(url="https://billing.stripe.test/portal")
        with (
            patch("app.routes.stripe.billing_portal.Session.create", return_value=portal),
            patch("app.routes.stripe.checkout.Session.create") as checkout,
        ):
            response = self.client.post("/create-checkout-session")

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, portal.url)
        checkout.assert_not_called()

    def test_cancel_and_reactivate_subscription(self):
        user = self.make_user(
            stripe_customer_id="cus_manage",
            stripe_subscription_id="sub_manage",
            subscription_status="active",
        )
        self.login(user)
        with patch("app.routes.stripe.Subscription.modify") as modify:
            cancel = self.client.post("/cancel-subscription")
            db.session.refresh(user)
            self.assertTrue(user.cancel_at_period_end)
            reactivate = self.client.post("/reactivate-subscription")
            db.session.refresh(user)
            self.assertFalse(user.cancel_at_period_end)

        self.assertEqual(cancel.status_code, 302)
        self.assertEqual(reactivate.status_code, 302)
        self.assertEqual(modify.call_count, 2)

    def test_admin_cannot_delete_user_with_managed_subscription(self):
        admin = self.make_user(email="albertonicopat@gmail.com")
        subscriber = self.make_user(
            email="subscriber@patia.test",
            stripe_customer_id="cus_keep",
            stripe_subscription_id="sub_keep",
            subscription_status="past_due",
        )
        self.login(admin)
        response = self.client.post(f"/admin/delete-user/{subscriber.id}")

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(User, subscriber.id))


if __name__ == "__main__":
    unittest.main()
