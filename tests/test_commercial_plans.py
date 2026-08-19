import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "commercial-plan-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_commercial")
os.environ.setdefault("STRIPE_PRICE_ID", "price_starter_legacy")
os.environ.setdefault("STRIPE_STARTER_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "price_pro")
os.environ.setdefault("STRIPE_RESTAURANT_PRICE_ID", "price_restaurant")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_commercial")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.inventory.services import record_opening_balance
from app.models import (
    OrganizationInvitation,
    OrganizationMember,
    Product,
    User,
)
from app.plans import (
    GRANDFATHERED,
    MANUAL,
    PRO,
    RESTAURANT,
    STARTER,
    current_plan_code,
    entitlements_for,
    has_entitlement,
)
from app.team.services import ensure_owner_organization


class CommercialPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="patia-commercial-plans-"
        )
        database_path = Path(self.temp_dir.name, "plans.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
            STRIPE_STARTER_PRICE_ID="price_starter",
            STRIPE_PRO_PRICE_ID="price_pro",
            STRIPE_RESTAURANT_PRICE_ID="price_restaurant",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.membership = self.add_owner(
            "owner@commercial.test"
        )

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def add_owner(self, email, *, trial_plan=STARTER):
        user = User(
            email=email,
            company_name="Tienda Planes",
            email_verified=True,
            trial_plan_code=trial_plan,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def add_member(self, role="CASHIER"):
        user = User(
            email=f"{role.lower()}-{OrganizationMember.query.count()}@commercial.test",
            company_name="Empleado",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        member = OrganizationMember(
            organization_id=self.membership.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(member)
        db.session.commit()
        return user, member

    def client_for(self, user, membership=None):
        membership = membership or self.membership
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
        return client

    def activate(self, plan_code):
        self.owner.subscription_plan_code = plan_code
        self.owner.subscription_status = "active"
        self.owner.stripe_customer_id = "cus_commercial"
        self.owner.stripe_subscription_id = "sub_commercial"
        self.owner.current_period_end = datetime.utcnow() + timedelta(days=30)
        db.session.commit()

    def stripe_subscription(self, plan_code=STARTER):
        price = "price_starter" if plan_code == STARTER else "price_pro"
        end = int((datetime.utcnow() + timedelta(days=30)).timestamp())
        return {
            "id": "sub_commercial",
            "customer": "cus_commercial",
            "status": "active",
            "current_period_start": end - 2_592_000,
            "current_period_end": end,
            "cancel_at_period_end": False,
            "metadata": {
                "user_id": str(self.owner.id),
                "organization_id": str(self.membership.organization_id),
                "plan_code": plan_code,
            },
            "items": {
                "data": [
                    {
                        "id": "si_commercial",
                        "quantity": 1,
                        "price": {"id": price},
                        "current_period_end": end,
                    }
                ]
            },
        }

    def test_official_entitlement_matrix(self):
        starter = entitlements_for(STARTER)
        pro = entitlements_for(PRO)

        self.assertEqual(starter.max_members, 2)
        self.assertFalse(starter.advanced_roles)
        self.assertFalse(starter.advanced_inventory_history)
        self.assertFalse(starter.advanced_reports)
        self.assertFalse(starter.advanced_exports)
        self.assertFalse(starter.monthly_owner_report)
        self.assertFalse(starter.executive_dashboard)
        self.assertEqual(pro.max_members, 5)
        self.assertTrue(pro.advanced_roles)
        self.assertTrue(pro.advanced_inventory_history)
        self.assertTrue(pro.advanced_reports)
        self.assertTrue(pro.advanced_exports)
        self.assertTrue(pro.monthly_owner_report)
        self.assertTrue(pro.priority_support)
        self.assertTrue(pro.executive_dashboard)

    def test_starter_blocks_manager_and_third_person_in_backend(self):
        client = self.client_for(self.owner)
        with patch("app.team.routes._send_invitation_email"):
            manager = client.post(
                "/team/invite",
                data={"email": "manager-commercial@example.com", "role": "MANAGER"},
            )
            self.assertEqual(manager.status_code, 302)
            self.assertIn("/subscribe", manager.location)
            self.assertEqual(OrganizationInvitation.query.count(), 0)

            self.add_member("CASHIER")
            third = client.post(
                "/team/invite",
                data={"email": "third-commercial@example.com", "role": "CASHIER"},
            )
            self.assertEqual(third.status_code, 302)
            self.assertIn("/subscribe", third.location)
            self.assertEqual(OrganizationInvitation.query.count(), 0)

    def test_pro_trial_allows_manager_but_employees_cannot_manage_billing(self):
        self.owner.trial_plan_code = PRO
        db.session.commit()
        client = self.client_for(self.owner)
        with patch(
            "app.team.routes._send_invitation_email", return_value=True
        ):
            response = client.post(
                "/team/invite",
                data={"email": "manager-commercial@example.com", "role": "MANAGER"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrganizationInvitation.query.one().role, "MANAGER")

        manager_user, manager_member = self.add_member("MANAGER")
        manager_client = self.client_for(manager_user, manager_member)
        self.assertEqual(manager_client.get("/subscription").status_code, 403)
        self.assertEqual(manager_client.get("/subscribe").status_code, 403)

    def test_trial_entitlements_follow_selected_plan(self):
        self.assertFalse(has_entitlement(self.owner, "advanced_reports"))
        self.owner.trial_plan_code = PRO
        db.session.commit()
        self.assertTrue(has_entitlement(self.owner, "advanced_reports"))

    def test_grandfathered_and_manual_access_are_preserved(self):
        self.owner.subscription_status = "active"
        self.owner.stripe_subscription_id = "sub_legacy"
        self.owner.current_period_end = datetime.utcnow() + timedelta(days=10)
        db.session.commit()
        self.assertEqual(current_plan_code(self.owner), GRANDFATHERED)
        self.assertFalse(has_entitlement(self.owner, "monthly_owner_report"))

        self.owner.manual_pro_access = True
        db.session.commit()
        self.assertEqual(current_plan_code(self.owner), MANUAL)
        self.assertTrue(has_entitlement(self.owner, "monthly_owner_report"))

    def test_checkout_uses_requested_paid_plan_prices(self):
        client = self.client_for(self.owner)
        for plan_code, expected_price in (
            (STARTER, "price_starter"),
            (PRO, "price_pro"),
            (RESTAURANT, "price_restaurant"),
        ):
            with patch(
                "app.routes.stripe.checkout.Session.create",
                return_value=SimpleNamespace(
                    url="https://checkout.stripe.com/test"
                ),
            ) as create:
                response = client.post(
                    "/create-checkout-session",
                    data={"plan_code": plan_code},
                )
            self.assertEqual(response.status_code, 303)
            params = create.call_args.kwargs
            self.assertEqual(
                params["line_items"][0]["price"], expected_price
            )
            self.assertEqual(params["metadata"]["plan_code"], plan_code)
            self.assertIn(plan_code.lower(), params["idempotency_key"])

    def test_landing_presents_restaurant_without_fake_checkout(self):
        self.app.config["STRIPE_RESTAURANT_PRICE_ID"] = None
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertEqual(html.count('<article class="pl2-plan'), 3)
        self.assertIn("PATIA Restaurant", html)
        self.assertIn("$360", html)
        self.assertIn("Control especializado para restaurantes.", html)
        self.assertIn("Costeo real por platillo", html)
        self.assertIn("Kg, g, L, ml y piezas", html)
        self.assertIn("Disponibilidad de platillos", html)
        self.assertIn("Disponible próximamente", html)
        self.assertNotIn("/register?plan=restaurant", html)

    def test_landing_enables_restaurant_only_with_its_own_price(self):
        self.app.config.update(
            STRIPE_RESTAURANT_PRICE_ID="price_restaurant",
            STRIPE_STARTER_PRICE_ID="price_starter",
            STRIPE_PRO_PRICE_ID="price_pro",
        )
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("/register?plan=restaurant", html)
        self.assertIn("Comenzar prueba con Restaurant", html)

        self.app.config["STRIPE_RESTAURANT_PRICE_ID"] = None
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("/register?plan=restaurant", html)
        self.assertIn("/register?plan=starter", html)
        self.assertIn("/register?plan=pro", html)

    def test_landing_presents_restaurant_in_english(self):
        self.app.config["STRIPE_RESTAURANT_PRICE_ID"] = None
        client = self.app.test_client()
        client.post("/language", data={"language": "en", "next": "/"})
        html = client.get("/").get_data(as_text=True)
        self.assertIn("PATIA Restaurant", html)
        self.assertIn("For restaurants that want to control recipes", html)
        self.assertIn("Specialized control for restaurants.", html)
        self.assertIn("Real cost per dish", html)
        self.assertIn("Dish availability", html)
        self.assertIn("Coming soon", html)

    def test_checkout_does_not_fake_pro_when_price_is_missing(self):
        self.app.config["STRIPE_PRO_PRICE_ID"] = None
        client = self.client_for(self.owner)
        with patch("app.routes.stripe.checkout.Session.create") as create:
            response = client.post(
                "/create-checkout-session",
                data={"plan_code": PRO},
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/subscribe", response.location)
        create.assert_not_called()

    def test_upgrade_is_pending_until_stripe_confirms(self):
        self.activate(STARTER)
        client = self.client_for(self.owner)
        subscription = self.stripe_subscription(STARTER)
        with (
            patch(
                "app.routes.stripe.Subscription.retrieve",
                return_value=subscription,
            ),
            patch("app.routes.stripe.Subscription.modify") as modify,
        ):
            response = client.post(
                "/subscription/change-plan",
                data={"plan_code": PRO},
            )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.owner)
        self.assertEqual(self.owner.subscription_plan_code, STARTER)
        self.assertEqual(self.owner.pending_plan_code, PRO)
        self.assertEqual(
            modify.call_args.kwargs["proration_behavior"],
            "create_prorations",
        )
        self.assertEqual(
            modify.call_args.kwargs["payment_behavior"],
            "pending_if_incomplete",
        )

    def test_downgrade_never_deletes_people_and_requires_starter_limits(self):
        self.activate(PRO)
        self.add_member("CASHIER")
        _manager_user, manager = self.add_member("MANAGER")
        before = OrganizationMember.query.count()
        client = self.client_for(self.owner)

        response = client.post(
            "/subscription/change-plan",
            data={"plan_code": STARTER},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/team", response.location)
        self.assertEqual(OrganizationMember.query.count(), before)
        self.assertTrue(db.session.get(OrganizationMember, manager.id).is_active)

    def test_starter_gets_basic_csv_and_pro_gets_advanced_columns(self):
        product = Product(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            sku="PLAN-1",
            name="Producto plan",
            category="General",
            cost_price=10,
            sale_price=20,
            stock=4,
            min_stock=1,
        )
        db.session.add(product)
        record_opening_balance(product, self.membership)
        db.session.commit()
        client = self.client_for(self.owner)

        starter_csv = client.get("/inventory/kardex/export.csv")
        self.assertEqual(starter_csv.status_code, 200)
        starter_text = starter_csv.get_data(as_text=True)
        self.assertNotIn("Responsable", starter_text)
        self.assertIn("Producto plan", starter_text)

        self.owner.trial_plan_code = PRO
        db.session.commit()
        pro_text = client.get(
            "/inventory/kardex/export.csv"
        ).get_data(as_text=True)
        self.assertIn("Responsable", pro_text)
        self.assertIn("Motivo", pro_text)


if __name__ == "__main__":
    unittest.main()
