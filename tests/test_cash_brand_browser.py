"""Workspace/public presentation and cash opening on isolated browser fixtures."""
import importlib.util
import os
from pathlib import Path
import unittest

import test_cash_register as cash_fixture
from app.models import CashRegisterSession


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class CashBrandBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright, Error

        self.fixture = cash_fixture.CashRegisterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client_for(self.fixture.owner)
        self.public_client = self.fixture.app.test_client()
        self.public = False
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        try:
            self.browser = self.playwright.chromium.launch(channel="msedge")
        except Error:
            try:
                self.browser = self.playwright.chromium.launch()
            except Error:
                self.skipTest("No Playwright-compatible browser installed")
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page()
        self.page.route("**/*", self.serve)

    def serve(self, route):
        request = route.request
        if not request.url.startswith("https://patia.test/"):
            route.abort()
            return
        path = request.url.split("patia.test", 1)[1]
        client = self.public_client if self.public else self.client
        response = client.open(
            path, method=request.method, data=request.post_data,
            content_type=request.headers.get("content-type"),
        )
        route.fulfill(
            status=response.status_code, body=response.data,
            content_type=response.content_type,
            headers={k: v for k, v in response.headers if k.lower() == "location"},
        )

    def screenshot(self, name):
        folder = os.environ.get("PATIA_UI_SCREENSHOTS")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path / f"{name}.png"), full_page=True)
            if name.startswith("public-"):
                self.page.locator(".pl2-header").screenshot(path=str(path / f"{name}-header.png"))
                self.page.locator(".pl2-footer").screenshot(path=str(path / f"{name}-footer.png"))

    def assert_logo(self, selector):
        logo = self.page.locator(selector)
        logo.wait_for(state="visible")
        self.page.wait_for_function(
            "selector => [...document.querySelectorAll(selector)].every(i => i.complete && i.naturalWidth > 0)",
            arg=selector,
        )
        properties = logo.evaluate("""img => {
            const style = getComputedStyle(img);
            const box = img.getBoundingClientRect();
            return {src: img.getAttribute('src'), filter: style.filter,
                opacity: style.opacity, width: box.width, height: box.height,
                contentWidth: box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight),
                contentHeight: box.height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom),
                ratio: img.naturalWidth / img.naturalHeight,
                background: style.backgroundColor};
        }""")
        self.assertEqual(properties["src"], "/static/img/brand/patia-logo-original-dark.png")
        self.assertEqual(properties["filter"], "none")
        self.assertEqual(properties["opacity"], "1")
        self.assertAlmostEqual(properties["contentWidth"] / properties["contentHeight"], properties["ratio"], delta=0.05)
        return properties

    def test_public_and_dashboard_brand_and_footer_at_desktop_and_mobile(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.public = True
                self.page.goto("https://patia.test/")
                for selector in (".pl2-brand img", ".pl2-footer__inner > div > img"):
                    logo = self.assert_logo(selector)
                    self.assertEqual(logo["background"], "rgb(234, 231, 224)")
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
                self.screenshot(f"public-{width}")
                self.public = False
                self.page.goto("https://patia.test/")
                self.assert_logo(".sidebar-v2__brand .logo-img")
                footer = self.page.locator(".app-main-v2 > .patia-social-footer")
                self.assertNotIn("PATIA en redes", footer.inner_text())
                self.assertEqual(footer.locator("a").count(), 2)
                self.assertIn("Instagram", footer.inner_text())
                self.assertIn("TikTok", footer.inner_text())
                self.assertEqual(self.page.locator(".app-legal-footer-v1 a").count(), 2)
                self.assertLessEqual(footer.bounding_box()["height"], 70)
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
                self.screenshot(f"dashboard-{width}")

    def test_opening_field_is_empty_required_and_accepts_explicit_zero(self):
        for width in (1440, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 900})
                self.page.goto("https://patia.test/cash-register")
                field = self.page.locator("#opening-cash")
                self.assertEqual(field.input_value(), "")
                self.assertFalse(field.evaluate("input => input.checkValidity()"))
                self.page.locator("form[action$='/open'] button[type='submit']").click()
                self.assertEqual(CashRegisterSession.query.filter_by(status="OPEN").count(), 0)
                self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), width)
                self.screenshot(f"cash-{width}")
                field.fill("0")
                self.assertTrue(field.evaluate("input => input.checkValidity()"))
                self.page.locator("form[action$='/open'] button[type='submit']").click()
                self.page.wait_for_selector("#opening-cash", state="detached")
                cash_session = CashRegisterSession.query.filter_by(status="OPEN").one()
                self.assertEqual(str(cash_session.opening_cash), "0.00")
                self.client.post("/cash-register/close", data={"counted_cash": "0"})
