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
from app.work_style import OwnerWorkPreference


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
            STRIPE_COCINA_PRICE_ID="price_restaurant",
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
        db.session.add(OwnerWorkPreference(user_id=user.id, mode="solo"))
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
        self.membership.organization.business_type = "restaurant"
        db.session.commit()
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

    def test_store_cannot_buy_or_switch_to_cocina(self):
        client = self.client_for(self.owner)
        html = client.get("/subscribe").get_data(as_text=True)
        self.assertIn("Solo para restaurantes", html)
        with patch("app.routes.stripe.checkout.Session.create") as checkout:
            response = client.post("/create-checkout-session", data={"plan_code": RESTAURANT})
        self.assertEqual(response.status_code, 302)
        checkout.assert_not_called()
        self.activate(PRO)
        with patch("app.routes.stripe.Subscription.modify") as modify, patch("app.routes.stripe.Subscription.retrieve") as retrieve:
            response = client.post("/subscription/change-plan", data={"plan_code": RESTAURANT})
        self.assertEqual(response.status_code, 302)
        modify.assert_not_called()
        retrieve.assert_not_called()
        self.assertEqual(self.owner.subscription_plan_code, PRO)
        self.assertEqual(self.membership.organization.business_type, "general")

    def test_new_names_prices_and_cocina_includes_control(self):
        from app.plans import commercial_plans, capabilities_for
        plans = commercial_plans(self.app.config)
        self.assertEqual([(p["name"], p["price"]) for p in plans],
                         [("Esencial", 199), ("Control", 349), ("Cocina", 499)])
        self.assertTrue(capabilities_for(PRO) <= capabilities_for(RESTAURANT))

    def test_cocina_registration_defaults_to_restaurant_and_rejects_store(self):
        client = self.app.test_client()
        html = client.get("/register?plan=restaurant").get_data(as_text=True)
        self.assertIn('<option value="restaurant" selected>', html)
        data = {
            "email": "new-cocina@commercial.test", "password": "Password123",
            "company_name": "Cocina nueva", "plan": "restaurant",
            "business_type": "general",
            "first_name": "Ana", "last_name": "Prueba", "phone": "5555555555",
            "address": "Calle ficticia 1", "city": "Puebla", "state": "Puebla",
            "postal_code": "72000",
        }
        with patch("app.routes.send_email", return_value=True), patch("app.routes.validate_email"):
            response = client.post("/register", data=data)
            self.assertEqual(response.status_code, 400)
            self.assertIsNone(User.query.filter_by(email=data["email"]).first())
            data["business_type"] = "restaurant"
            response = client.post("/register", data=data)
        self.assertEqual(response.status_code, 302)
        user = User.query.filter_by(email=data["email"]).one()
        self.assertEqual(user.trial_plan_code, RESTAURANT)
        self.assertEqual(OrganizationMember.query.filter_by(user_id=user.id, role="OWNER").one().organization.business_type, "restaurant")
        with patch("app.routes.send_email"), patch("app.routes.stripe.checkout.Session.create") as checkout:
            verified = client.post("/verify-email", data={"code": user.verification_code})
            self.assertEqual(verified.status_code, 302)
            self.assertEqual(client.get("/recipes").location, "/settings/work-style/setup")
            self.assertEqual(client.post("/settings/work-style", data={"work_style": "solo", "source": "onboarding"}).status_code, 303)
            self.assertEqual(client.get("/recipes").status_code, 200)
        self.assertTrue(has_entitlement(user, "recipes", has_paid_access=False))
        self.assertIsNone(user.stripe_subscription_id)
        checkout.assert_not_called()

    def test_legacy_restaurant_price_is_recognized_but_not_sold_as_cocina(self):
        from app.plans import configured_price_plan, price_id_for
        self.app.config["STRIPE_COCINA_PRICE_ID"] = "price_cocina_499"
        self.assertEqual(configured_price_plan(self.app.config, "price_restaurant"), RESTAURANT)
        self.assertEqual(configured_price_plan(self.app.config, "price_cocina_499"), RESTAURANT)
        self.assertEqual(price_id_for(self.app.config, RESTAURANT), "price_cocina_499")
        self.app.config["STRIPE_COCINA_PRICE_ID"] = None
        self.assertIsNone(price_id_for(self.app.config, RESTAURANT))

    def test_settings_cannot_switch_business_type_and_still_saves_other_fields(self):
        client = self.client_for(self.owner)
        html = client.get("/settings").get_data(as_text=True)
        self.assertNotIn('name="business_type"', html)
        self.assertIn('readonly', html)
        response = client.post("/settings", data={"company_name": "Unwanted change", "business_type": "restaurant"})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.membership.organization)
        db.session.refresh(self.owner)
        self.assertEqual(self.membership.organization.business_type, "general")
        self.assertEqual(self.owner.company_name, "Tienda Planes")
        response = client.post("/settings", data={"company_name": "Updated store", "timezone": "America/Mexico_City"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.membership.organization.name, "Updated store")
        self.assertEqual(self.membership.organization.business_type, "general")

    def test_landing_presents_restaurant_without_fake_checkout(self):
        self.app.config["STRIPE_COCINA_PRICE_ID"] = None
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertEqual(html.count('<article class="pl2-plan'), 3)
        self.assertIn("PATIA Cocina", html)
        self.assertIn("$499", html)
        self.assertIn("Control especializado para restaurantes.", html)
        self.assertIn("Costeo real por platillo", html)
        self.assertIn("Kg, g, L, ml y piezas", html)
        self.assertIn("Disponibilidad de platillos", html)
        self.assertIn("Disponible próximamente", html)
        self.assertNotIn("/register?plan=restaurant", html)

    def test_landing_enables_restaurant_only_with_its_own_price(self):
        self.app.config.update(
            STRIPE_RESTAURANT_PRICE_ID="price_restaurant",
            STRIPE_COCINA_PRICE_ID="price_restaurant",
            STRIPE_STARTER_PRICE_ID="price_starter",
            STRIPE_PRO_PRICE_ID="price_pro",
        )
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("/register?plan=restaurant", html)
        self.assertIn("Comenzar prueba con Cocina", html)

        self.app.config["STRIPE_COCINA_PRICE_ID"] = None
        html = self.app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("/register?plan=restaurant", html)
        self.assertIn("/register?plan=starter", html)
        self.assertIn("/register?plan=pro", html)

    def test_landing_presents_restaurant_in_english(self):
        self.app.config["STRIPE_COCINA_PRICE_ID"] = None
        client = self.app.test_client()
        client.post("/language", data={"language": "en", "next": "/"})
        html = client.get("/").get_data(as_text=True)
        self.assertIn("PATIA Cocina", html)
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
