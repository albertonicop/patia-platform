import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = PROJECT_ROOT / "app" / "templates"


class PremiumPresentationTests(unittest.TestCase):
    def template(self, name):
        return (TEMPLATES / name).read_text(encoding="utf-8")

    def test_landing_explains_a_concrete_business_outcome(self):
        landing = self.template("landing.html")

        self.assertIn("El control total de tu negocio,", landing)
        self.assertIn("en una sola plataforma.", landing)
        self.assertIn("Inventario, ventas, caja, clientes y decisiones en un solo lugar.", landing)
        self.assertIn("Software para negocios en México", landing)
        self.assertIn("Probar 14 días gratis", landing)
        self.assertIn("Ver demo", landing)
        self.assertIn("videos/patia-demo.mp4", landing)
        self.assertIn("videos/patia-demo-en.mp4", landing)
        self.assertIn("Demo en video próximamente", landing)
        self.assertNotIn("data-demo-scene", landing)
        self.assertNotIn("Probar PATIA gratis", landing)
        self.assertNotIn("PATIA Pro · $199/mes", landing)
        self.assertIn('{{ _("Precios") }}', landing)
        self.assertIn("Empieza gratis. Elige tu plan cuando estés listo.", landing)
        self.assertIn("Comenzar prueba con %(plan)s", landing)
        self.assertNotIn("landing-v5__terms", landing)
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
        self.assertIn("Pulso PATIA", landing)
        self.assertIn("plan.code == 'PRO'", landing)
        self.assertIn("patia-starter.jpg", landing)
        self.assertIn("patia-pro.jpg", landing)
        self.assertIn("patia-pos.jpg", landing)
        self.assertIn('loading="lazy"', landing)
        self.assertIn("Detección automática de columnas", landing)
        self.assertIn("Preparado para miles de productos", landing)
        self.assertIn("{{ plan.price }}", landing)
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
            "legal.html", "reset_password.html",
        )
        for name in consumers:
            self.assertIn("styles.css') }}?v=131", self.template(name), name)
        landing = self.template("landing.html")
        self.assertIn("patia-v2-landing.css') }}?v=2", landing)
        self.assertNotIn("styles.css", landing)
        self.assertNotIn("patia-v11.css", landing)
        self.assertNotIn("landing-motion.css", landing)

    def test_visual_system_v11_uses_explicit_module_identities(self):
        base = self.template("base.html")
        landing = self.template("landing.html")
        visual_css = (
            PROJECT_ROOT / "app" / "static" / "css" / "patia-v11.css"
        ).read_text(encoding="utf-8")
        module_css = (
            PROJECT_ROOT / "app" / "static" / "css"
            / "patia-v11-modules.css"
        ).read_text(encoding="utf-8")

        self.assertIn("patia-landing-v2", landing)
        self.assertIn("patia-v2-landing.css') }}?v=2", landing)
        self.assertIn("patia-v11.css') }}?v=3", base)
        self.assertIn("patia-v11-modules.css') }}?v=4", base)
        self.assertIn("patia-v11--dashboard", base)
        self.assertIn("patia-v11--inventory", base)
        for scope in (
            "patia-v11--reports",
            "patia-v11--decisions",
            "patia-v11--purchases",
            "patia-v11--cash",
            "patia-v11--crm",
            "patia-v11--suppliers",
            "patia-v11--settings",
            "patia-v11--pos",
        ):
            self.assertIn(scope, base)
            self.assertIn(f".{scope}", module_css)
        self.assertNotIn("body:not(", visual_css)
        self.assertNotIn("body:not(", module_css)
        self.assertIn("Divider refinement", visual_css)
        self.assertIn("Divider refinement", module_css)
        self.assertIn(".patia-v11--workspace .sidebar-v2__logout", visual_css)
        self.assertIn(".dashboard-v2__recommendation", visual_css)
        self.assertIn(".dashboard-v3__alerts-list > a", visual_css)
        self.assertIn(".inventory-v2__catalog", visual_css)
        self.assertIn(".reports-v3__kpi::after", module_css)
        self.assertIn(".pro-purchases-v1__guide li", module_css)
        self.assertIn(".customers-v1__empty", module_css)
        self.assertIn(".subscription-v2__details", module_css)
        self.assertIn(
            ".patia-v11--dashboard .dashboard-v3__quick-summary "
            ".dashboard-v2__method-note",
            visual_css,
        )
        self.assertIn(
            ".patia-v11--inventory .inventory-v2 .inventory-v2__catalog",
            visual_css,
        )
        self.assertIn(
            ".patia-v11--reports .reports-v3__executive-empty",
            module_css,
        )
        self.assertIn(
            ".patia-v11--purchases .pro-purchases-v1__group > header",
            module_css,
        )
        self.assertIn(
            ".patia-v11--crm .customers-simple-v1__list-head",
            module_css,
        )
        for scope in (
            ".patia-v11--dashboard",
            ".patia-v11--inventory",
        ):
            self.assertIn(scope, visual_css)
        self.assertIn(
            ".patia-v11--decisions .pro-hub-v1__pulse-reading b",
            module_css,
        )
        self.assertIn("width: auto;", module_css)
        self.assertIn(
            ".patia-v11--crm .receivables-v2__accounts table",
            module_css,
        )
        self.assertIn(
            ".patia-v11--suppliers .suppliers-v2__list-card table",
            module_css,
        )
        self.assertIn("min-width: 0;", module_css)

    def test_landing_v2_is_isolated_accessible_and_uses_brand_variants(self):
        landing = self.template("landing.html")
        css = (
            PROJECT_ROOT / "app" / "static" / "css" / "patia-v2-landing.css"
        ).read_text(encoding="utf-8")

        self.assertIn('class="patia-landing-v2"', landing)
        self.assertIn('href="#contenido"', landing)
        self.assertIn('aria-modal="true"', landing)
        self.assertIn("event.key === 'Escape'", landing)
        self.assertIn("video?.pause()", landing)
        self.assertIn("button[data-demo-close]", landing)
        self.assertIn("patia-logo-original.png", landing)
        self.assertIn("patia-mark-original.png", landing)
        self.assertIn("object-fit: contain", css)
        self.assertIn(".pl2-frame--offset { transform: none; }", css)
        self.assertIn(".pl2-frame img { display: block; width: 100%; height: auto; object-fit: contain;", css)
        self.assertTrue((PROJECT_ROOT / "app/static/img/brand/patia-logo-original-dark.png").is_file())
        self.assertIn(".patia-landing-v2", css)
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("@media (max-width: 700px)", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertNotIn(".sidebar-v2", css)
        self.assertNotIn(".dashboard-v2", css)

    def test_landing_pricing_supports_three_plans_without_mobile_overflow(self):
        css = (
            PROJECT_ROOT / "app" / "static" / "css" / "patia-v2-landing.css"
        ).read_text(encoding="utf-8")

        self.assertIn(
            ".pl2-plans { max-width: 1180px; margin: auto; display: grid; "
            "grid-template-columns: repeat(3, minmax(0,1fr));",
            css,
        )
        self.assertIn(
            ".pl2-plans { max-width: 760px; grid-template-columns: 1fr; }",
            css,
        )
        self.assertIn(".pl2-plan { position: relative; display: flex;", css)
        self.assertIn("min-width: 0;", css)

    def test_sidebar_keeps_distinct_pro_routes_without_legacy_dashboard(self):
        base = self.template("base.html")
        reports = self.template("reports.html")

        for endpoint in ("pro.hub", "pro.purchases"):
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
