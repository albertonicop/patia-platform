import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from babel.messages.pofile import read_po

from app import create_app, db
from app.models import User


class InternationalizationTests(unittest.TestCase):
    def setUp(self):
        self.old_stripe_disabled = os.environ.get("STRIPE_DISABLED")
        self.old_public_base_url = os.environ.get("PUBLIC_BASE_URL")
        os.environ["STRIPE_DISABLED"] = "true"
        os.environ["PUBLIC_BASE_URL"] = "http://localhost"
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with self.app.app_context():
            db.create_all()
            user = User(
                email="language@example.com",
                company_name="Language Shop",
                email_verified=True,
                preferred_language="es",
            )
            user.set_password("secure-password")
            db.session.add(user)
            db.session.commit()
            self.user_id = user.id
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()
        os.remove(self.db_path)
        if self.old_stripe_disabled is None:
            os.environ.pop("STRIPE_DISABLED", None)
        else:
            os.environ["STRIPE_DISABLED"] = self.old_stripe_disabled
        if self.old_public_base_url is None:
            os.environ.pop("PUBLIC_BASE_URL", None)
        else:
            os.environ["PUBLIC_BASE_URL"] = self.old_public_base_url

    def test_spanish_is_the_default_language(self):
        response = self.client.get("/")
        self.assertIn('lang="es"', response.get_data(as_text=True))
        self.assertIn("Vende, controla tu inventario", response.get_data(as_text=True))

    def test_visitor_can_switch_to_english_and_session_persists_it(self):
        response = self.client.post(
            "/language",
            data={"language": "en", "next": "/"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn('lang="en"', html)
        self.assertIn("Sell, control inventory", html)
        self.assertIn("Sell, control inventory", self.client.get("/").get_data(as_text=True))

    def test_authenticated_language_is_saved_and_restored_on_login(self):
        self.client.post(
            "/login",
            data={"email": "language@example.com", "password": "secure-password"},
        )
        self.client.post("/language", data={"language": "en", "next": "/"})
        with self.app.app_context():
            self.assertEqual(db.session.get(User, self.user_id).preferred_language, "en")

        fresh_client = self.app.test_client()
        fresh_client.post(
            "/login",
            data={"email": "language@example.com", "password": "secure-password"},
        )
        html = fresh_client.get("/").get_data(as_text=True)
        self.assertIn('lang="en"', html)
        self.assertIn("Executive dashboard", html)

    def test_unsupported_language_falls_back_to_spanish(self):
        response = self.client.post(
            "/language",
            data={"language": "fr", "next": "/"},
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIn('lang="es"', html)
        self.assertIn("Vende, controla tu inventario", html)

    def test_english_catalog_has_no_missing_or_fuzzy_messages(self):
        catalog_path = (
            Path(__file__).parents[1]
            / "app"
            / "translations"
            / "en"
            / "LC_MESSAGES"
            / "messages.po"
        )
        with catalog_path.open(encoding="utf-8") as handle:
            catalog = read_po(handle)
        missing = [message.id for message in catalog if message.id and not message.string]
        fuzzy = [message.id for message in catalog if "fuzzy" in message.flags]
        self.assertEqual([], missing)
        self.assertEqual([], fuzzy)

    def test_templates_have_no_unmarked_visible_literal_text(self):
        templates = Path(__file__).parents[1] / "app" / "templates"
        text_pattern = re.compile(
            r">([^<>{}\n]*[A-Za-zÁÉÍÓÚÜÑáéíóúüñ¿¡][^<>{}\n]*)<"
        )
        attribute_pattern = re.compile(
            r'(?:placeholder|aria-label|title|data-label)="'
            r'[^"{}]*[A-Za-zÁÉÍÓÚÜÑáéíóúüñ¿¡][^"{}]*"'
        )
        findings = []
        for path in templates.glob("*.html"):
            in_script = False
            in_style = False
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                lowered = line.lower()
                in_script = in_script or "<script" in lowered
                in_style = in_style or "<style" in lowered
                if not in_script and not in_style and "{#" not in line:
                    if text_pattern.search(line) or attribute_pattern.search(line):
                        findings.append(f"{path.name}:{line_number}")
                if "</script>" in lowered:
                    in_script = False
                if "</style>" in lowered:
                    in_style = False
        self.assertEqual([], findings)

    def test_json_errors_follow_the_selected_language(self):
        self.client.post(
            "/login",
            data={"email": "language@example.com", "password": "secure-password"},
        )
        self.client.post("/language", data={"language": "en", "next": "/sell"})
        response = self.client.post("/sell-cart", json={"items": []})
        self.assertEqual(400, response.status_code)
        self.assertEqual("The cart is empty", response.get_json()["error"])

    @patch("app.routes.validate_email")
    @patch("app.routes.send_email")
    def test_registration_email_uses_selected_language(self, send_email, _validate):
        send_email.return_value = True
        self.client.post("/language", data={"language": "en", "next": "/register"})
        response = self.client.post(
            "/register",
            data={
                "first_name": "Ana",
                "last_name": "Test",
                "company_name": "Test Shop",
                "phone": "2381234567",
                "address": "Main 1",
                "city": "Test City",
                "state": "Test State",
                "business_type": "Otro",
                "postal_code": "75700",
                "email": "new-language@example.com",
                "password": "secure-password",
            },
        )
        self.assertEqual(302, response.status_code)
        email_call = send_email.call_args.kwargs
        self.assertEqual("Verify your email in PATIA", email_call["subject"])
        self.assertIn("Your verification code is:", email_call["html"])
        self.assertNotIn("Tu código de verificación es:", email_call["html"])


if __name__ == "__main__":
    unittest.main()
