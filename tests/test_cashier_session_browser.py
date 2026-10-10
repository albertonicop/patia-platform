"""Cashier journey in Edge against an isolated local HTTP server with CSRF."""
import html
import importlib.util
import os
from pathlib import Path
import re
import unittest
from unittest.mock import patch
from threading import Thread

import requests
from werkzeug.serving import make_server, WSGIRequestHandler
from app import db
from app.models import CashMovement, CashRegisterSession, InventoryMovement, Sale, SalesTicket
from test_cashier_session import CashierFixture


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class CashierSessionBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright, Error
        self.audit = CashierFixture(create_cashier=False)
        self.addCleanup(self.audit.close)
        self.audit.app.config["WTF_CSRF_ENABLED"] = True
        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass
        self.server = make_server("127.0.0.1", 0, self.audit.app, request_handler=QuietHandler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        def stop_server():
            self.server.shutdown()
            self.server_thread.join(timeout=5)
            self.server.server_close()
        self.addCleanup(stop_server)
        self.owner_client = requests.Session()
        self.addCleanup(self.owner_client.close)
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
        self.failures = []
        self.page.on("response", lambda response: self.failures.append((response.url, response.status)) if response.url.startswith(self.base_url) and response.status >= 400 else None)

    def screenshot(self, name, selector=None):
        folder = os.environ.get("PATIA_CASHIER_ARTIFACTS")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            target = self.page.locator(selector) if selector else self.page
            target.screenshot(path=str(path / f"{name}.png"))

    def login(self, email, password):
        self.page.goto(self.base_url + "/login")
        self.page.locator('input[name="email"]').fill(email)
        self.page.locator('input[name="password"]').fill(password)
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator('#auth-form button[type="submit"]').click()
        self.page.wait_for_url(re.compile(re.escape(self.base_url) + r"/(?:sell)?$"))

    def open_menu(self):
        toggle = self.page.locator(".sidebar-v2__toggle")
        if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
            toggle.click()

    def logout(self):
        self.open_menu()
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.locator('.sidebar-v2__logout button').click()
        self.page.wait_for_url(self.base_url + "/")

    def test_desktop_cashier_journey(self):
        self.journey(1440)

    def test_mobile_cashier_journey(self):
        self.journey(390)

    def journey(self, width):
        audit = self.audit
        self.page.set_viewport_size({"width": width, "height": 900})
        self.login(audit.owner.email, "Password123")
        self.page.goto(self.base_url + "/settings")
        self.page.locator('.settings-v2__tabs a[href="/team"]').click()
        self.page.locator('.team-v2__solo details summary').click()
        self.page.locator('#solo-team-email').fill(audit.email)
        self.page.locator('#solo-team-role').select_option("CASHIER")
        with patch("app.team.routes.secrets.token_urlsafe", return_value=audit.token), patch("app.routes.send_email", return_value=True):
            with self.page.expect_navigation(wait_until="domcontentloaded"):
                self.page.locator('form[action="/team/invite"] button[type="submit"]').click()
        self.assertIn(audit.email, self.page.content())
        self.screenshot(f"personal-{width}")
        self.logout()
        self.page.goto(self.base_url + "/team/accept/" + audit.token)
        self.page.locator('input[name="first_name"]').fill("Lucia")
        self.page.locator('input[name="last_name"]').fill("Caja Demo")
        self.page.locator('input[name="password"]').fill(audit.password)
        self.page.locator('.team-accept-v1 form button[type="submit"]').click()
        self.page.wait_for_url(self.base_url + "/sell")
        db.session.expire_all()
        audit.refresh_cashier()
        self.logout()
        self.login(audit.email, audit.password)
        self.page.wait_for_url(self.base_url + "/sell")
        self.open_menu()
        navigation = self.page.locator('#primary-navigation')
        self.assertEqual(navigation.locator('a').count(), 2)
        self.assertIn("Vender", navigation.inner_text())
        self.assertIn("Caja del", navigation.inner_text())
        self.assertEqual(self.page.locator('.language-switcher').count(), 1)
        language = self.page.locator('.app-topbar-v2 .language-switcher').bounding_box()
        menu = navigation.bounding_box()
        self.assertTrue(language['y'] >= menu['y'] + menu['height'] or language['x'] >= menu['x'] + menu['width'])
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        self.screenshot(f"menu-cajero-{width}")
        toggle = self.page.locator('.sidebar-v2__toggle')
        if toggle.is_visible():
            toggle.click()
        self.screenshot(f"inicio-cajero-{width}")
        self.page.locator('.cash-pos-status a').click()
        self.page.locator('#opening-cash').fill("100")
        self.page.locator('form[action="/cash-register/open"] button').click()
        self.page.wait_for_selector('#opening-cash', state='detached')
        self.assertEqual(CashRegisterSession.query.one().opened_by_member_id, audit.member.id)
        missing_csrf = requests.post(self.base_url + "/sell-cart", json={"items": []}, timeout=10)
        self.assertEqual(missing_csrf.status_code, 400)
        self.assertEqual(Sale.query.count(), 0)
        self.page.goto(self.base_url + "/sell")
        for _ in range(2):
            self.page.locator('#product-search').fill("Rol de canela")
            self.page.locator('#product-results button').first.click()
        self.page.locator('#amount-received').fill("50")
        self.assertIn("29.50", self.page.locator('#cash-result').inner_text())
        self.screenshot(f"cobro-cajero-{width}", '.pos-v2__checkout')
        self.page.locator('#checkout-button').click()
        self.page.wait_for_selector('#sale-confirmation:not([hidden])')
        db.session.expire_all()
        ticket = SalesTicket.query.one()
        self.assertEqual(ticket.cashier_member_id, audit.member.id)
        self.assertEqual(str(ticket.amount_received), "50.00")
        self.assertEqual(str(ticket.change_amount), "29.50")
        for movement in [*CashMovement.query.all(), *InventoryMovement.query.all()]:
            self.assertEqual(movement.performed_by_member_id, audit.member.id)
        self.page.goto(self.base_url + "/ticket/" + ticket.public_id)
        self.assertIn("Lucia Caja Demo", self.page.content())
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        self.screenshot(f"ticket-cajero-{width}")
        self.page.goto(self.base_url + "/cash-register")
        self.page.locator('#counted-cash').fill("120.50")
        self.page.locator('#closing-notes').fill("Turno ficticio conciliado")
        self.assertEqual(self.page.locator('#cash-actions').count(), 0)
        self.screenshot(f"cierre-cajero-{width}", '.cash-v2__close')
        self.page.locator('form[action="/cash-register/close"] button').click()
        self.page.wait_for_selector('#opening-cash')
        db.session.expire_all()
        cash = CashRegisterSession.query.one()
        self.assertEqual(cash.status, "CLOSED")
        self.assertEqual(cash.closed_by_member_id, audit.member.id)
        self.assertEqual(str(cash.difference), "0.00")
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        self.assertEqual(self.failures, [])
        self.screenshot(f"caja-cerrada-{width}")
        # The owner disables access while the cashier's authenticated browser stays open.
        login_page = self.owner_client.get(self.base_url + '/login', timeout=10)
        login_csrf = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1))
        self.assertEqual(self.owner_client.post(self.base_url + '/login', allow_redirects=False, timeout=10, data={'csrf_token': login_csrf, 'email': audit.owner.email, 'password': 'Password123'}).status_code, 302)
        page = self.owner_client.get(self.base_url + '/team', timeout=10)
        csrf = html.unescape(re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1))
        self.assertEqual(self.owner_client.post(self.base_url + f'/team/members/{audit.member.id}/toggle', allow_redirects=False, timeout=10, data={'csrf_token': csrf}).status_code, 302)
        sale_count = Sale.query.count()
        self.page.goto(self.base_url + "/sell")
        self.page.wait_for_url(re.compile(re.escape(self.base_url) + r'/login'))
        self.assertIn("desactivado", self.page.content())
        self.assertEqual(Sale.query.count(), sale_count)
        self.screenshot(f"acceso-desactivado-{width}")
