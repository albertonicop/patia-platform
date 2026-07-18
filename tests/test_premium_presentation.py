import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "app" / "templates"


class PremiumPresentationTests(unittest.TestCase):
    def template(self, name):
        return (TEMPLATES / name).read_text(encoding="utf-8")

    def test_landing_explains_a_concrete_business_outcome(self):
        landing = self.template("landing.html")

        self.assertIn("Tu negocio, bajo control. Todos los días.", landing)
        self.assertIn("Ver demo de 60 segundos", landing)
        self.assertIn("Menos decisiones a ciegas", landing)
        self.assertIn("Reportes completos", landing)
        self.assertIn("$199", landing)
        self.assertIn("14 días", landing)

    def test_pro_comparison_reflects_real_trial_boundaries(self):
        subscribe = self.template("subscribe.html")

        self.assertIn("Prueba de 14 días", subscribe)
        self.assertIn("Reportes y recomendaciones IA", subscribe)
        self.assertIn("Disponible en Pro", subscribe)
        self.assertIn("Inventario, ventas y proveedores", subscribe)
        self.assertIn("$199 MXN al mes", subscribe)
        self.assertNotIn("usuarios ilimitados", subscribe.lower())
        self.assertNotIn("sucursales ilimitadas", subscribe.lower())

    def test_checkout_form_and_csrf_are_unchanged(self):
        subscribe = self.template("subscribe.html")

        self.assertEqual(subscribe.count('action="/create-checkout-session"'), 1)
        self.assertIn('action="/create-checkout-session" method="POST"', subscribe)
        self.assertIn('name="csrf_token" value="{{ csrf_token() }}"', subscribe)

    def test_subscription_management_forms_keep_post_and_csrf(self):
        subscription = self.template("subscription.html")

        for endpoint in ("main.billing_portal", "main.cancel_subscription", "main.reactivate_subscription"):
            self.assertIn(endpoint, subscription)
        self.assertEqual(subscription.count('method="POST"'), 3)
        self.assertEqual(subscription.count('name="csrf_token"'), 3)
        self.assertNotIn("PATIA Pro manual", subscription)
        self.assertIn("Acceso activo", subscription)

    def test_all_css_consumers_use_the_same_cache_version(self):
        consumers = (
            "auth.html", "base.html", "base_clean.html", "forgot_password.html",
            "landing.html", "legal.html", "reset_password.html",
        )
        for name in consumers:
            self.assertIn("styles.css') }}?v=111", self.template(name), name)


if __name__ == "__main__":
    unittest.main()
