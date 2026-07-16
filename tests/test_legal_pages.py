import os
import unittest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "legal-pages-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_legal")
os.environ.setdefault("STRIPE_PRICE_ID", "price_legal")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_legal")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app


class LegalPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
        cls.client = cls.app.test_client()

    def test_terms_and_privacy_are_public_and_marked_as_drafts(self):
        for path, title in (("/terminos", "Términos de servicio"), ("/privacidad", "Aviso de privacidad")):
            response = self.client.get(path)
            html = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(title, html)
            self.assertIn("Borrador operativo sujeto a revisión legal", html)
            self.assertIn("Última actualización", html)
            self.assertIn("PENDIENTE DE DEFINIR", html)

    def test_public_entry_pages_link_both_documents(self):
        for path in ("/", "/login", "/register"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn('href="/terminos"', html)
            self.assertIn('href="/privacidad"', html)

    def test_legal_copy_mentions_required_operational_topics(self):
        terms = self.client.get("/terminos").get_data(as_text=True)
        privacy = self.client.get("/privacidad").get_data(as_text=True)
        for text in ("14 días", "$199 MXN al mes", "cancelarse", "Render", "Stripe", "Resend"):
            self.assertIn(text, terms)
        for text in ("Datos almacenados", "Cookies y sesiones", "Render", "Stripe", "Resend", "riesgo cero"):
            self.assertIn(text, privacy)


if __name__ == "__main__":
    unittest.main()
