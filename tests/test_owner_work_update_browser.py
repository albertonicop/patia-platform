"""Real CSRF-protected buttons, desktop/mobile and fresh-device persistence."""
import importlib.util
import os
from pathlib import Path
from threading import Thread
import unittest

from werkzeug.serving import make_server, WSGIRequestHandler
from app import db, limiter
from app.work_style import OwnerWorkUpdateNotice
from test_cashier_session import CashierFixture


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is optional')
class OwnerWorkUpdateBrowserTests(unittest.TestCase):
    def test_update_buttons_and_fresh_browser_device(self):
        from playwright.sync_api import sync_playwright

        audit = CashierFixture()
        self.addCleanup(audit.close)
        previous_enabled = limiter.enabled
        limiter.enabled = False
        self.addCleanup(setattr, limiter, 'enabled', previous_enabled)
        other, _ = audit.fixture.add_owner('personalize-update@example.com')
        other.manual_pro_access = True
        db.session.commit()
        audit.app.config['WTF_CSRF_ENABLED'] = True
        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass
        server = make_server('127.0.0.1', 0, audit.app, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(stop)
        origin = f'http://127.0.0.1:{server.server_port}'
        folder = Path(os.environ['PATIA_WORK_UPDATE_ARTIFACTS']) if os.environ.get('PATIA_WORK_UPDATE_ARTIFACTS') else None
        if folder:
            folder.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge')
            try:
                def login(email):
                    context = browser.new_context(viewport={'width':1440, 'height':1000})
                    context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                    page = context.new_page()
                    page.goto(origin + '/login', wait_until='domcontentloaded')
                    page.locator('input[name="email"]').fill(email)
                    page.locator('input[name="password"]').fill('Password123')
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        page.locator('#auth-form button[type="submit"]').click()
                    return context, page
                context, page = login(audit.owner.email)
                self.assertTrue(page.locator('[data-work-update]').is_visible())
                for width in (1440, 390):
                    page.set_viewport_size({'width':width, 'height':1000})
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    self.assertTrue(page.locator('[data-work-update] button[value="personalize"]').is_visible())
                    self.assertTrue(page.locator('[data-work-update] button[value="later"]').is_visible())
                    if folder:
                        page.screenshot(path=str(folder / f'aviso-{width}.png'), full_page=True)
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('[data-work-update] button[value="later"]').click()
                self.assertEqual(page.locator('[data-work-update]').count(), 0)
                toggle = page.locator('.sidebar-v2__toggle')
                if toggle.is_visible() and toggle.get_attribute('aria-expanded') != 'true':
                    toggle.click()
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('#primary-navigation button[type="submit"]').click()
                context.close()
                context, page = login(audit.owner.email)
                self.assertEqual(page.locator('[data-work-update]').count(), 0)
                self.assertEqual(page.locator('[data-work-nav]').count(), 0)
                context.close()
                context, page = login(other.email)
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('[data-work-update] button[value="personalize"]').click()
                self.assertTrue(page.url.endswith('/settings/work-style/setup'))
                self.assertEqual(page.locator('[data-work-style-form]').count(), 1)
                self.assertEqual(page.locator('[data-work-nav]').count(), 0)
                page.locator('input[name="work_style"][value="solo"]').check()
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('[data-work-style-form] button[value="save"]').click()
                self.assertEqual(page.locator('[data-work-update]').count(), 0)
                self.assertGreater(page.locator('[data-work-nav]').count(), 0)
                context.close()
            finally:
                browser.close()
        db.session.expire_all()
        self.assertEqual(db.session.get(OwnerWorkUpdateNotice, audit.owner.id).action, 'later')
