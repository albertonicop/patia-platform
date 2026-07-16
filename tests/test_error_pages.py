import os
import unittest

from flask import abort


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "error-page-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app


class ErrorPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=False,
            PROPAGATE_EXCEPTIONS=False,
            RATELIMIT_ENABLED=False,
        )

        @cls.app.route("/_test/error/<int:status_code>")
        def forced_http_error(status_code):
            abort(status_code)

        @cls.app.route("/_test/error-500")
        def forced_internal_error():
            raise RuntimeError("internal detail must not reach the client")

        cls.client = cls.app.test_client()

    def test_http_error_pages_are_consistent_and_keep_security_headers(self):
        expected_titles = {
            400: "No pudimos procesar la solicitud",
            403: "Acceso no permitido",
            404: "Página no encontrada",
            429: "Demasiados intentos",
        }
        for status_code, title in expected_titles.items():
            with self.subTest(status_code=status_code):
                response = self.client.get(f"/_test/error/{status_code}")
                html = response.get_data(as_text=True)
                self.assertEqual(response.status_code, status_code)
                self.assertIn(title, html)
                self.assertIn("Volver a PATIA", html)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_internal_error_is_generic_and_does_not_leak_details(self):
        response = self.client.get("/_test/error-500")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 500)
        self.assertIn("PATIA no pudo completar la operación", html)
        self.assertNotIn("internal detail", html)
        self.assertNotIn("Traceback", html)


if __name__ == "__main__":
    unittest.main()
