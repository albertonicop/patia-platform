import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from secrets import token_urlsafe

from flask import g


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "logout-experience-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_logout")
os.environ.setdefault("STRIPE_PRICE_ID", "price_test_logout")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_logout")
os.environ.setdefault("PUBLIC_BASE_URL", "https://logout.test")

from app import create_app, db
from app.models import OrganizationMember, User
from app.team.services import ensure_owner_organization


class LogoutExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="patia-logout-experience-"
        )
        database_path = Path(self.temp_dir.name, "logout.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=True,
            WTF_CSRF_TIME_LIMIT=3600,
            RATELIMIT_ENABLED=False,
            SESSION_COOKIE_SECURE=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner = User(
            email="owner@logout.test",
            company_name="Negocio Logout",
            email_verified=True,
            manual_pro_access=True,
        )
        self.owner.set_password("Password123")
        db.session.add(self.owner)
        db.session.flush()
        self.owner_membership = ensure_owner_organization(self.owner)
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

    def _member(self, role):
        user = User(
            email=f"{role.lower()}@logout.test",
            company_name="Equipo Logout",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = OrganizationMember(
            organization_id=self.owner_membership.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
        return user, membership

    def _client(self, user=None, membership=None, *, language="es"):
        user = user or self.owner
        membership = membership or self.owner_membership
        login_token = token_urlsafe(24)
        user.session_token = login_token
        db.session.commit()
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = user.id
            browser_session["organization_id"] = membership.organization_id
            browser_session["session_token"] = login_token
            browser_session["language"] = language
        return client

    @staticmethod
    def _csrf(html):
        match = re.search(
            r'name="csrf_token"\s+value="([^"]+)"',
            html,
        )
        if not match:
            raise AssertionError("CSRF token not found")
        return match.group(1)

    def _page_token(self, client, path="/products"):
        # The suite keeps one application context open per test case. Clear
        # Flask-WTF's request token cache so every simulated browser receives
        # a token backed by its own cookie session, as it does in production.
        g.pop("csrf_token", None)
        response = client.get(path, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        return self._csrf(response.get_data(as_text=True))

    def test_valid_post_logs_out_redirects_to_login_and_preserves_language(self):
        client = self._client(language="es")
        token = self._page_token(client)

        response = client.post(
            "/logout",
            data={"csrf_token": token},
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Has cerrado sesión correctamente.", html)
        with client.session_transaction() as browser_session:
            self.assertNotIn("user_id", browser_session)
            self.assertNotIn("organization_id", browser_session)
            self.assertEqual(browser_session["language"], "es")

    def test_valid_logout_message_is_translated_to_english(self):
        client = self._client(language="en")
        token = self._page_token(client)

        response = client.post(
            "/logout",
            data={"csrf_token": token},
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn("You have signed out successfully.", html)
        self.assertNotIn("Has cerrado sesión correctamente", html)

    def test_double_submit_and_second_tab_never_show_generic_400(self):
        client = self._client()
        first_tab_token = self._page_token(client, "/reports")
        second_tab_token = self._page_token(client, "/products")

        first = client.post(
            "/logout",
            data={"csrf_token": first_tab_token},
        )
        second = client.post(
            "/logout",
            data={"csrf_token": second_tab_token},
            follow_redirects=True,
        )

        self.assertEqual(first.status_code, 302)
        self.assertEqual(first.location, "/login")
        self.assertEqual(second.status_code, 200)
        second_html = second.get_data(as_text=True)
        self.assertIn(
            "Tu sesión ya terminó. Inicia sesión nuevamente.",
            second_html,
        )
        self.assertNotIn("Error 400", second_html)

    def test_expired_token_keeps_valid_session_and_explains_recovery(self):
        client = self._client()
        self.app.config["WTF_CSRF_TIME_LIMIT"] = 1
        token = self._page_token(client)
        time.sleep(2.1)

        response = client.post(
            "/logout",
            data={"csrf_token": token},
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn("la página perdió vigencia", html)
        self.assertNotIn("Error 400", html)
        with client.session_transaction() as browser_session:
            self.assertEqual(browser_session["user_id"], self.owner.id)

    def test_already_cleared_session_and_missing_csrf_recover_cleanly(self):
        client = self._client()
        stale_token = self._page_token(client)
        with client.session_transaction() as browser_session:
            browser_session.clear()
            browser_session["language"] = "es"

        response = client.post(
            "/logout",
            data={"csrf_token": stale_token},
            follow_redirects=True,
        )

        html = response.get_data(as_text=True)
        self.assertIn(
            "Tu sesión ya terminó. Inicia sesión nuevamente.",
            html,
        )
        self.assertNotIn("Error 400", html)

    def test_post_without_csrf_does_not_log_out_valid_session(self):
        client = self._client()

        response = client.post("/logout", follow_redirects=True)

        html = response.get_data(as_text=True)
        self.assertIn("la página perdió vigencia", html)
        self.assertNotIn("Error 400", html)
        with client.session_transaction() as browser_session:
            self.assertEqual(browser_session["user_id"], self.owner.id)

    def test_direct_get_is_non_destructive_for_authenticated_and_anonymous(self):
        client = self._client()

        authenticated = client.get("/logout", follow_redirects=True)

        self.assertIn(
            "utiliza el botón Cerrar sesión",
            authenticated.get_data(as_text=True),
        )
        with client.session_transaction() as browser_session:
            self.assertEqual(browser_session["user_id"], self.owner.id)

        anonymous = self.app.test_client().get(
            "/logout",
            follow_redirects=True,
        )
        self.assertIn(
            "Tu sesión ya terminó. Inicia sesión nuevamente.",
            anonymous.get_data(as_text=True),
        )

    def test_owner_manager_and_cashier_share_safe_logout_flow(self):
        manager, manager_membership = self._member("MANAGER")
        cashier, cashier_membership = self._member("CASHIER")
        roles = (
            ("OWNER", self.owner, self.owner_membership, "/reports"),
            ("MANAGER", manager, manager_membership, "/reports"),
            ("CASHIER", cashier, cashier_membership, "/sell"),
        )

        for role, user, membership, page in roles:
            with self.subTest(role=role):
                client = self._client(user, membership)
                token = self._page_token(client, page)
                response = client.post(
                    "/logout",
                    data={"csrf_token": token},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    "Has cerrado sesión correctamente.",
                    response.get_data(as_text=True),
                )


if __name__ == "__main__":
    unittest.main()
