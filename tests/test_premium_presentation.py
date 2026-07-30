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
        self.assertIn("Ver demo", landing)
        self.assertIn("videos/patia-demo.mp4", landing)
        self.assertIn("videos/patia-demo-en.mp4", landing)
        self.assertIn("Demo en video próximamente", landing)
        self.assertNotIn("data-demo-scene", landing)
        self.assertNotIn("Probar PATIA gratis", landing)
        self.assertNotIn("PATIA Pro · $199/mes", landing)
        self.assertIn('{{ _("Precios") }}', landing)
        self.assertIn("Planes simples para cada etapa de tu negocio", landing)
        self.assertIn("Comenzar prueba con %(plan)s", landing)
        self.assertEqual(landing.count('class="landing-v5__terms"'), 1)
        for filename in ("patia-demo.mp4", "patia-demo-en.mp4"):
            video = PROJECT_ROOT / "app" / "static" / "videos" / filename
            self.assertTrue(video.is_file(), filename)
            self.assertGreater(video.stat().st_size, 1_000_000, filename)

        generator = (
            PROJECT_ROOT / "scripts" / "generate_patia_demo.py"
        ).read_text(encoding="utf-8")
        self.assertIn("target_rms", generator)
        self.assertIn("warm_voice", generator)
        self.assertNotIn("sweep_phase", generator)
        self.assertNotIn("A simple ascending motif", generator)

        app_factory = (PROJECT_ROOT / "app" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PATIA_DEMO_VIDEO_AVAILABLE=_env_flag(", app_factory)
        self.assertIn("default=True", app_factory)
        self.assertIn("Menos decisiones a ciegas", landing)
        self.assertIn("PATIA Pro", landing)
        self.assertIn("patia-starter.jpg", landing)
        self.assertIn("patia-pro.jpg", landing)
        self.assertIn('loading="lazy"', landing)
        self.assertIn("pantallas reales de PATIA", landing)
        self.assertIn("$199", landing)
        self.assertIn("14 días", landing)

    def test_pro_comparison_reflects_real_trial_boundaries(self):
        subscribe = self.template("subscribe.html")
        plan_service = (PROJECT_ROOT / "app" / "plans.py").read_text(encoding="utf-8")
        plan_experience = subscribe + plan_service

        self.assertIn("Prueba de 14 días", subscribe)
        self.assertIn("Historial y reportes avanzados", plan_experience)
        self.assertIn("Soporte prioritario", plan_experience)
        self.assertIn("Productos y ventas sin límites artificiales", plan_experience)
        self.assertIn("Elegir %(plan)s por $%(price)s al mes", subscribe)
        self.assertIn('"price": 199', plan_service)
        self.assertIn('"price": 349', plan_service)
        self.assertIn("STRIPE", (PROJECT_ROOT / "app" / "plans.py").read_text(encoding="utf-8"))
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
        self.assertGreaterEqual(subscription.count('method="POST"'), 5)
        self.assertEqual(
            subscription.count('method="POST"'),
            subscription.count('name="csrf_token"'),
        )
        self.assertNotIn("PATIA Pro manual", subscription)
        self.assertIn("Acceso activo", subscription)
        self.assertIn('current_plan_code == "TRIAL"', subscription)
        self.assertIn("Funciones incluidas durante tu prueba", subscription)
        self.assertIn("Funciones incluidas en tu plan Starter", subscription)
        self.assertIn("Funciones incluidas en tu plan Pro", subscription)
        self.assertIn("Funciones incluidas en tu acceso actual", subscription)
        self.assertNotIn("Lo que mantienes activo con PATIA Pro", subscription)

    def test_tablet_header_keeps_language_and_menu_controls_separate(self):
        styles = (PROJECT_ROOT / "app" / "static" / "css" / "styles.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("@media (min-width: 481px) and (max-width: 900px)", styles)
        self.assertIn("top: 78px", styles)
        self.assertIn("padding-bottom: 72px", styles)

    def test_all_css_consumers_use_the_same_cache_version(self):
        consumers = (
            "auth.html", "base.html", "base_clean.html", "forgot_password.html",
            "landing.html", "legal.html", "reset_password.html",
        )
        for name in consumers:
            self.assertIn("styles.css') }}?v=128", self.template(name), name)

    def test_sidebar_keeps_pro_routes_without_visual_plan_badges(self):
        base = self.template("base.html")
        reports = self.template("reports.html")

        for endpoint in (
            "pro.hub",
            "pro.purchases",
        ):
            self.assertIn(endpoint, base)
        self.assertNotIn("pro.dashboard", base)
        self.assertNotIn("pro.monthly_reports", base)
        sidebar = base[base.index("<aside"):base.index("</aside>")]
        self.assertNotIn("pro.alerts", sidebar)
        self.assertIn("pro.alerts", base)
        self.assertIn("pro.monthly_reports", reports)
        self.assertNotIn("<small>{{ _(\"Pro\") }}</small>", base)


if __name__ == "__main__":
    unittest.main()
