import os
import unittest
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "checkout-browser-security-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CheckoutBrowserSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        cls.client = cls.app.test_client()

    def csp_directives(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        header = response.headers["Content-Security-Policy"]
        return {
            parts[0]: parts[1:]
            for directive in header.split(";")
            if (parts := directive.strip().split())
        }

    def test_csp_allows_only_required_stripe_form_destinations(self):
        directives = self.csp_directives()

        self.assertEqual(
            directives["form-action"],
            ["'self'", "https://checkout.stripe.com", "https://billing.stripe.com"],
        )
        self.assertNotIn("*", directives["form-action"])

    def test_csp_preserves_other_security_controls(self):
        response = self.client.get("/login")
        directives = self.csp_directives()

        self.assertEqual(directives["base-uri"], ["'self'"])
        self.assertEqual(directives["frame-ancestors"], ["'none'"])
        self.assertEqual(directives["object-src"], ["'none'"])
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_checkout_form_remains_post_with_csrf_and_no_script_interception(self):
        template = (PROJECT_ROOT / "app" / "templates" / "subscribe.html").read_text(
            encoding="utf-8"
        )
        app_script = (PROJECT_ROOT / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('action="/create-checkout-session" method="POST"', template)
        self.assertIn('name="csrf_token" value="{{ csrf_token() }}"', template)
        self.assertNotIn("preventDefault", template)
        self.assertNotIn("subscription-v2__checkout-form", app_script)
        self.assertNotIn("preventDefault", app_script)


if __name__ == "__main__":
    unittest.main()
