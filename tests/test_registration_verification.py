import os
import unittest
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "registration-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db, limiter
from app.models import User


class RegistrationVerificationTests(unittest.TestCase):
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
        limiter.reset()
        db.session.rollback()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()

    def register(self, email, plan=None):
        query = "?plan=pro" if plan == "pro" else ""
        data = {
            "email": email,
            "password": "Password123",
            "first_name": "Ana",
            "last_name": "Pérez",
            "company_name": "Tienda Ana",
            "phone": "5555555555",
            "address": "Calle 1",
            "city": "Puebla",
            "state": "Puebla",
            "business_type": "Abarrotes",
            "postal_code": "72000",
        }
        if plan:
            data["plan"] = plan
        with (
            patch("app.routes.validate_email"),
            patch("app.routes.send_email") as send_email,
        ):
            response = self.client.post(f"/register{query}", data=data)
        return response, send_email

    def verification_code_for(self, email):
        return User.query.filter_by(email=email).one().verification_code

    def test_pro_registration_requires_email_verification(self):
        email = "pro@patia.test"
        response, send_email = self.register(email, plan="pro")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/verify-email"))
        self.assertIsNotNone(self.verification_code_for(email))
        send_email.assert_called_once()
        with self.client.session_transaction() as session:
            self.assertEqual(session["post_verify_destination"], "subscribe")

    def test_verified_pro_registration_redirects_to_subscription(self):
        email = "verified-pro@patia.test"
        self.register(email, plan="pro")
        code = self.verification_code_for(email)

        with patch("app.routes.send_email"):
            response = self.client.post("/verify-email", data={"code": code})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/subscribe"))
        self.assertTrue(User.query.filter_by(email=email).one().email_verified)
        with self.client.session_transaction() as session:
            self.assertNotIn("post_verify_destination", session)

    def test_verified_trial_registration_redirects_to_dashboard(self):
        email = "trial@patia.test"
        response, _ = self.register(email)
        self.assertTrue(response.location.endswith("/verify-email"))
        code = self.verification_code_for(email)

        with patch("app.routes.send_email"):
            response = self.client.post("/verify-email", data={"code": code})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))
        self.assertTrue(User.query.filter_by(email=email).one().email_verified)

    def test_unverified_user_cannot_start_checkout(self):
        user = User(email="unverified@patia.test", company_name="Tienda")
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = user.id

        with patch("app.routes.stripe.checkout.Session.create") as checkout:
            response = self.client.post("/create-checkout-session")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/verify-email"))
        checkout.assert_not_called()
        with self.client.session_transaction() as session:
            self.assertEqual(session["post_verify_destination"], "subscribe")

    def test_verification_flash_is_rendered_only_once(self):
        user = User(email="flash@patia.test", company_name="Tienda")
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        message = "Mensaje de verificación recuperable"
        with self.client.session_transaction() as session:
            session["user_id"] = user.id
            session["_flashes"] = [("danger", message)]

        html = self.client.get("/verify-email").get_data(as_text=True)

        self.assertEqual(html.count(message), 1)

    def test_business_type_options_have_explicit_readable_colors(self):
        with open("app/static/css/styles.css", encoding="utf-8") as stylesheet:
            css = stylesheet.read()
        self.assertIn(".auth-v2 .auth-v2__field select option", css)
        self.assertIn("color: #272b3a", css)

    def test_registration_error_preserves_non_sensitive_fields_only(self):
        response, _ = self.register("repeat@patia.test")
        self.assertEqual(response.status_code, 302)

        data = {
            "email": "repeat@patia.test",
            "password": "Password123",
            "first_name": "Ana",
            "last_name": "Pérez",
            "company_name": "Tienda Ana",
            "phone": "5555555555",
            "address": "Calle 1",
            "city": "Puebla",
            "state": "Puebla",
            "business_type": "Abarrotes",
            "postal_code": "72000",
        }
        with patch("app.routes.validate_email"):
            duplicate = self.client.post("/register", data=data)

        html = duplicate.get_data(as_text=True)
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn('value="Tienda Ana"', html)
        self.assertIn('value="repeat@patia.test"', html)
        self.assertIn('value="Abarrotes" selected', html)
        self.assertNotIn('value="Password123"', html)

    def test_incomplete_registration_returns_validation_instead_of_500(self):
        response = self.client.post(
            "/register",
            data={"email": "owner@patia.test", "password": "Password123"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Completa todos los campos obligatorios", response.get_data(as_text=True))
        self.assertEqual(User.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
