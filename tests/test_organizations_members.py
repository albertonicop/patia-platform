import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "organization-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")

from app import create_app, db
from app.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    Product,
    Sale,
    Supplier,
    User,
)
from app.team.services import (
    active_membership,
    ensure_owner_organization,
    has_permission,
)


class OrganizationMemberTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-organizations-")
        database_path = Path(self.temp_dir.name, "organizations.db")
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

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def add_user(self, email, company):
        user = User(email=email, company_name=company, email_verified=True)
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        return user

    def login_as(self, client, user, membership):
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
            flask_session["language"] = "es"

    def test_existing_owner_gets_one_organization_and_idempotent_membership(self):
        user = self.add_user("owner@patia.test", "Tienda Uno")
        first = ensure_owner_organization(user)
        second = ensure_owner_organization(user)
        db.session.commit()

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.role, "OWNER")
        self.assertEqual(first.organization.name, "Tienda Uno")
        self.assertEqual(Organization.query.count(), 1)
        self.assertEqual(OrganizationMember.query.count(), 1)

    def test_missing_session_user_redirects_to_login_with_safe_next(self):
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = 999999
            flask_session["language"] = "es"

        response = client.get("/customers")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/customers", response.location)
        login = client.get(response.location)
        self.assertIn(
            "Tu sesión expiró. Inicia sesión para continuar.",
            login.get_data(as_text=True),
        )

    def test_revoked_json_session_returns_structured_401(self):
        user = self.add_user("revoked@patia.test", "Revocada")
        membership = ensure_owner_organization(user)
        user.session_token = "current-token"
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
            flask_session["session_token"] = "old-token"

        response = client.post("/sell-cart", json={"items": []})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json()["error_code"],
            "session_revoked",
        )

    def test_revoked_dashboard_session_recovers_through_login(self):
        user = self.add_user("dashboard-revoked@patia.test", "Revocada")
        membership = ensure_owner_organization(user)
        user.session_token = "current-token"
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = membership.organization_id
            flask_session["session_token"] = "old-token"

        response = client.get("/")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login?next=/", response.location)
        login = client.get(response.location)
        self.assertIn(
            "se inició sesión en otro dispositivo",
            login.get_data(as_text=True),
        )

    def test_memberships_are_isolated_between_organizations(self):
        first_user = self.add_user("first@patia.test", "Primera")
        second_user = self.add_user("second@patia.test", "Segunda")
        first = ensure_owner_organization(first_user)
        second = ensure_owner_organization(second_user)
        db.session.commit()

        with self.app.test_request_context("/"):
            from flask import session as flask_session

            flask_session["organization_id"] = second.organization_id
            selected = active_membership(first_user)

        self.assertEqual(selected.organization_id, first.organization_id)
        self.assertNotEqual(selected.organization_id, second.organization_id)

    def test_roles_have_deliberately_limited_permissions(self):
        user = self.add_user("roles@patia.test", "Roles")
        owner = ensure_owner_organization(user)
        manager = OrganizationMember(role="MANAGER", is_active=True)
        cashier = OrganizationMember(role="CASHIER", is_active=True)

        self.assertTrue(has_permission(owner, "manage_subscription"))
        self.assertTrue(has_permission(owner, "view_dashboard"))
        self.assertTrue(has_permission(manager, "manage_inventory"))
        self.assertTrue(has_permission(manager, "use_pos"))
        self.assertFalse(has_permission(manager, "manage_subscription"))
        self.assertFalse(has_permission(cashier, "manage_customers"))
        self.assertTrue(has_permission(cashier, "lookup_customers"))
        self.assertTrue(has_permission(cashier, "create_customers"))
        self.assertTrue(has_permission(cashier, "use_pos"))
        self.assertFalse(has_permission(cashier, "view_dashboard"))
        self.assertFalse(has_permission(cashier, "view_costs"))
        self.assertFalse(has_permission(None, "manage_customers"))

    def test_cashier_pin_is_hashed_and_verifiable(self):
        member = OrganizationMember(role="CASHIER", is_active=True)
        member.set_pin("4827")

        self.assertNotEqual(member.pin_hash, "4827")
        self.assertTrue(member.check_pin("4827"))
        self.assertFalse(member.check_pin("0000"))

    def test_registration_creates_owner_membership_and_session_tenant(self):
        client = self.app.test_client()
        with patch("app.routes.validate_email"), patch(
            "app.routes.send_email", return_value=True
        ):
            response = client.post(
                "/register",
                data={
                    "first_name": "Ana",
                    "last_name": "López",
                    "email": "ana@patia.test",
                    "password": "Password123",
                    "company_name": "Miscelánea Ana",
                    "phone": "5555555555",
                    "address": "Calle Uno",
                    "city": "Ciudad de México",
                    "state": "CDMX",
                    "business_type": "Abarrotes",
                    "postal_code": "01000",
                    "language": "es",
                },
            )

        self.assertEqual(response.status_code, 302)
        membership = OrganizationMember.query.one()
        self.assertEqual(membership.role, "OWNER")
        with client.session_transaction() as flask_session:
            self.assertEqual(
                flask_session["organization_id"], membership.organization_id
            )

    def test_owner_invites_new_employee_and_acceptance_creates_cashier(self):
        owner_user = self.add_user("owner@patia.test", "Tienda Uno")
        owner = ensure_owner_organization(owner_user)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, owner_user, owner)

        with patch("app.routes.send_email", return_value=True):
            response = client.post(
                "/team/invite",
                data={"email": "cashier@example.com", "role": "CASHIER"},
            )
        self.assertEqual(response.status_code, 302)
        invitation = OrganizationInvitation.query.one()
        self.assertNotIn("cashier@example.com", invitation.token_hash)

        raw_token = None
        with patch("app.team.routes.secrets.token_urlsafe", return_value="known-token"), patch(
            "app.routes.send_email", return_value=True
        ):
            client.post(
                "/team/invite",
                data={"email": "cashier@example.com", "role": "CASHIER"},
            )
            raw_token = "known-token"
        client.post("/logout")
        accepted = client.post(
            f"/team/accept/{raw_token}",
            data={
                "first_name": "Caja",
                "last_name": "Uno",
                "password": "Password123",
            },
        )
        self.assertEqual(accepted.status_code, 302)
        employee = User.query.filter_by(email="cashier@example.com").one()
        membership = OrganizationMember.query.filter_by(user_id=employee.id).one()
        self.assertEqual(membership.organization_id, owner.organization_id)
        self.assertEqual(membership.role, "CASHIER")
        self.assertTrue(membership.is_active)
        self.assertEqual(client.get(f"/team/accept/{raw_token}").status_code, 410)

    def test_expired_invitation_cannot_be_accepted(self):
        from datetime import datetime, timedelta
        import hashlib

        owner_user = self.add_user("expired-owner@patia.test", "Expirada")
        owner = ensure_owner_organization(owner_user)
        token = "expired-token"
        db.session.add(
            OrganizationInvitation(
                organization_id=owner.organization_id,
                invited_by_member_id=owner.id,
                email="expired@example.com",
                role="CASHIER",
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=datetime.utcnow() - timedelta(minutes=1),
            )
        )
        db.session.commit()
        client = self.app.test_client()
        self.assertEqual(client.get(f"/team/accept/{token}").status_code, 410)
        self.assertIsNone(User.query.filter_by(email="expired@example.com").first())

    def test_stale_session_cannot_accept_invitation_for_existing_user(self):
        import hashlib
        from datetime import datetime, timedelta

        owner_user = self.add_user("stale-owner@patia.test", "Sesiones")
        employee = self.add_user("stale-employee@patia.test", "Sesiones")
        owner = ensure_owner_organization(owner_user)
        member = OrganizationMember(
            organization_id=owner.organization_id,
            user_id=employee.id,
            role="CASHIER",
        )
        token = "stale-session-invitation"
        invitation = OrganizationInvitation(
            organization_id=owner.organization_id,
            invited_by_member_id=owner.id,
            email=employee.email,
            role="MANAGER",
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        employee.session_token = "current-session-token"
        db.session.add_all((member, invitation))
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as stale_session:
            stale_session["user_id"] = employee.id
            stale_session["session_token"] = "revoked-session-token"
            stale_session["organization_id"] = owner.organization_id

        response = client.post(f"/team/accept/{token}")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)
        db.session.refresh(invitation)
        db.session.refresh(member)
        self.assertIsNone(invitation.accepted_at)
        self.assertEqual(member.role, "CASHIER")

    def test_existing_member_of_another_company_cannot_be_invited(self):
        first_user = self.add_user("existing-owner-a@example.com", "A")
        second_user = self.add_user("existing-owner-b@example.com", "B")
        first = ensure_owner_organization(first_user)
        ensure_owner_organization(second_user)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, first_user, first)
        with patch("app.routes.send_email", return_value=True) as email_sender:
            response = client.post(
                "/team/invite",
                data={"email": second_user.email, "role": "MANAGER"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OrganizationInvitation.query.count(), 0)
        email_sender.assert_not_called()

    def test_pending_invitation_can_be_resent_and_revoked_only_by_owner(self):
        from datetime import datetime, timedelta

        owner_user = self.add_user("invite-admin@patia.test", "Invites")
        owner = ensure_owner_organization(owner_user)
        invitation = OrganizationInvitation(
            organization_id=owner.organization_id,
            invited_by_member_id=owner.id,
            email="pending@example.com",
            role="MANAGER",
            token_hash="a" * 64,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        db.session.add(invitation)
        db.session.commit()
        original_hash = invitation.token_hash
        client = self.app.test_client()
        self.login_as(client, owner_user, owner)
        with patch("app.routes.send_email", return_value=True):
            self.assertEqual(
                client.post(f"/team/invitations/{invitation.id}/resend").status_code,
                302,
            )
        db.session.refresh(invitation)
        self.assertNotEqual(invitation.token_hash, original_hash)
        self.assertEqual(
            client.post(f"/team/invitations/{invitation.id}/revoke").status_code,
            302,
        )
        self.assertIsNone(db.session.get(OrganizationInvitation, invitation.id))

    def test_owner_changes_role_resets_pin_and_deactivates_member(self):
        owner_user = self.add_user("owner-actions@patia.test", "Acciones")
        owner_user.trial_plan_code = "PRO"
        employee = self.add_user("employee@patia.test", "Acciones")
        owner = ensure_owner_organization(owner_user)
        member = OrganizationMember(
            organization_id=owner.organization_id,
            user_id=employee.id,
            role="CASHIER",
        )
        db.session.add(member)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, owner_user, owner)

        self.assertEqual(
            client.post(f"/team/members/{member.id}/role", data={"role": "MANAGER"}).status_code,
            302,
        )
        client.post(f"/team/members/{member.id}/pin", data={"pin": "4827"})
        db.session.refresh(member)
        self.assertEqual(member.role, "MANAGER")
        self.assertTrue(member.check_pin("4827"))
        client.post(f"/team/members/{member.id}/pin", data={"pin": ""})
        db.session.refresh(member)
        self.assertIsNone(member.pin_hash)
        client.post(f"/team/members/{member.id}/toggle")
        db.session.refresh(member)
        self.assertFalse(member.is_active)

        employee_client = self.app.test_client()
        response = employee_client.post(
            "/login",
            data={"email": employee.email, "password": "Password123"},
        )
        self.assertEqual(response.status_code, 302)
        with employee_client.session_transaction() as employee_session:
            self.assertNotIn("user_id", employee_session)
        self.assertEqual(Organization.query.count(), 1)

    def test_owner_cannot_manage_member_or_invitation_from_another_tenant(self):
        first_user = self.add_user("owner-a@patia.test", "A")
        second_user = self.add_user("owner-b@patia.test", "B")
        employee = self.add_user("employee-b@patia.test", "B")
        first = ensure_owner_organization(first_user)
        second = ensure_owner_organization(second_user)
        foreign_member = OrganizationMember(
            organization_id=second.organization_id,
            user_id=employee.id,
            role="CASHIER",
        )
        db.session.add(foreign_member)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, first_user, first)

        client.post(
            f"/team/members/{foreign_member.id}/role",
            data={"role": "MANAGER"},
        )
        db.session.refresh(foreign_member)
        self.assertEqual(foreign_member.role, "CASHIER")
        self.assertEqual(
            client.post(f"/team/invitations/999999/revoke").status_code,
            404,
        )

    def test_cashier_permissions_and_cross_organization_isolation(self):
        first_user = self.add_user("first-owner@patia.test", "Primera")
        second_user = self.add_user("second-owner@patia.test", "Segunda")
        cashier_user = self.add_user("cashier-isolation@patia.test", "Primera")
        first = ensure_owner_organization(first_user)
        second = ensure_owner_organization(second_user)
        cashier = OrganizationMember(
            organization_id=first.organization_id,
            user_id=cashier_user.id,
            role="CASHIER",
        )
        foreign_product = Product(
            organization_id=second.organization_id,
            user_id=second_user.id,
            sku="FOREIGN",
            name="Producto ajeno",
            cost_price=1,
            sale_price=2,
            stock=5,
            min_stock=1,
        )
        own_product = Product(
            organization_id=first.organization_id,
            user_id=first_user.id,
            sku="OWN",
            name="Producto propio",
            cost_price=1,
            sale_price=2,
            stock=5,
            min_stock=1,
        )
        db.session.add_all((cashier, foreign_product, own_product))
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, cashier_user, cashier)

        sell_response = client.get("/sell")
        self.assertEqual(sell_response.status_code, 200)
        sell_html = sell_response.get_data(as_text=True)
        self.assertIn("Producto propio", sell_html)
        self.assertNotIn("Producto ajeno", sell_html)
        self.assertEqual(client.get("/").location, "/sell")
        denied_requests = (
            ("get", "/products"),
            ("get", "/products/quick-load"),
            ("get", "/api/products/quick-load/lookup?barcode=123"),
            ("get", "/download-template"),
            ("post", "/products/new"),
            ("post", "/import-products"),
            ("get", f"/products/{own_product.id}/edit"),
            ("post", f"/products/{own_product.id}/restock"),
            ("post", f"/products/{own_product.id}/delete"),
            ("post", "/products/delete-all"),
            ("get", "/suppliers"),
            ("get", "/reports"),
            ("get", "/settings"),
            ("get", "/team"),
            ("get", "/subscription"),
            ("post", "/create-checkout-session"),
        )
        for method, url in denied_requests:
            self.assertEqual(getattr(client, method)(url).status_code, 403, url)

    def test_manager_can_operate_but_cannot_manage_team_or_subscription(self):
        owner_user = self.add_user("manager-owner@patia.test", "Gerencia")
        manager_user = self.add_user("manager@patia.test", "Gerencia")
        owner = ensure_owner_organization(owner_user)
        owner_user.manual_pro_access = True
        manager = OrganizationMember(
            organization_id=owner.organization_id,
            user_id=manager_user.id,
            role="MANAGER",
        )
        db.session.add(manager)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, manager_user, manager)

        self.assertEqual(client.get("/reports").status_code, 200)
        self.assertEqual(client.get("/products").status_code, 200)
        self.assertEqual(client.get("/suppliers").status_code, 200)
        self.assertEqual(client.get("/sell").status_code, 200)
        self.assertEqual(client.get("/team").status_code, 403)
        self.assertEqual(client.get("/subscription").status_code, 403)
        db.session.refresh(manager_user)
        self.assertEqual(manager_user.plan, "trial")
        self.assertTrue(owner_user.manual_pro_access)

    def test_all_business_routes_reject_foreign_organization_records(self):
        from app.models import SalesTicket

        first_user = self.add_user("tenant-one@patia.test", "Tenant One")
        second_user = self.add_user("tenant-two@patia.test", "Tenant Two")
        first_user.manual_pro_access = True
        first = ensure_owner_organization(first_user)
        second = ensure_owner_organization(second_user)
        own_product = Product(
            organization_id=first.organization_id,
            user_id=first_user.id,
            sku="TENANT-ONE",
            name="Producto visible",
            cost_price=5,
            sale_price=10,
            stock=8,
            min_stock=1,
        )
        foreign_product = Product(
            organization_id=second.organization_id,
            user_id=second_user.id,
            sku="TENANT-TWO",
            name="Producto secreto",
            cost_price=50,
            sale_price=100,
            stock=80,
            min_stock=1,
        )
        foreign_supplier = Supplier(
            organization_id=second.organization_id,
            user_id=second_user.id,
            name="Proveedor secreto",
        )
        db.session.add_all((own_product, foreign_product, foreign_supplier))
        db.session.flush()
        foreign_ticket = SalesTicket(
            organization_id=second.organization_id,
            user_id=second_user.id,
            number=1,
            public_id="00000000-0000-0000-0000-000000000002",
            payment_method="cash",
        )
        db.session.add(foreign_ticket)
        db.session.flush()
        foreign_sale = Sale(
            organization_id=second.organization_id,
            user_id=second_user.id,
            product_id=foreign_product.id,
            sales_ticket_id=foreign_ticket.id,
            ticket_id=foreign_ticket.public_id,
            quantity=1,
            unit_price=100,
            total=100,
        )
        db.session.add(foreign_sale)
        db.session.commit()

        client = self.app.test_client()
        self.login_as(client, first_user, first)
        for url in ("/", "/products", "/sell", "/suppliers", "/reports"):
            response = client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertNotIn("Producto secreto", response.get_data(as_text=True), url)
            self.assertNotIn("Proveedor secreto", response.get_data(as_text=True), url)
        self.assertEqual(client.get(f"/receipt/{foreign_sale.id}").status_code, 404)
        self.assertEqual(client.get(f"/ticket/{foreign_ticket.public_id}").status_code, 404)
        self.assertEqual(
            client.post(f"/sales/{foreign_sale.id}/cancel").status_code,
            404,
        )
        self.assertIsNotNone(db.session.get(Sale, foreign_sale.id))

    def test_team_interface_defaults_to_spanish(self):
        owner_user = self.add_user("team-language@patia.test", "Idiomas")
        owner = ensure_owner_organization(owner_user)
        db.session.commit()
        client = self.app.test_client()
        self.login_as(client, owner_user, owner)

        spanish = client.get("/team").get_data(as_text=True)
        self.assertIn("Agregar persona", spanish)
        self.assertIn("¿Alguien más trabaja contigo?", spanish)
        self.assertNotIn("Personas con acceso", spanish)
        self.assertNotIn("Autorizaciones de seguridad", spanish)

    def test_team_interface_is_available_in_english(self):
        owner_user = self.add_user("team-language-en@patia.test", "Languages")
        owner = ensure_owner_organization(owner_user)
        db.session.commit()

        english_client = self.app.test_client()
        self.login_as(english_client, owner_user, owner)
        with english_client.session_transaction() as language_session:
            language_session["language"] = "en"
        english = english_client.get("/team").get_data(as_text=True)
        self.assertIn("Add person", english)
        self.assertIn("Does anyone else work with you?", english)
        self.assertNotIn("People with access", english)


if __name__ == "__main__":
    unittest.main()
