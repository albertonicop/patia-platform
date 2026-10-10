"""Reprint a returned receipt in a real browser on a disposable HTTP server."""
import importlib.util
import os
from pathlib import Path
from threading import Thread
import unittest

from werkzeug.serving import make_server, WSGIRequestHandler
from app.models import Sale
from test_cashier_session import CashierFixture


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is optional')
class ReturnedTicketBrowserTests(unittest.TestCase):
    def test_cashier_reprints_returned_ticket_on_desktop_and_mobile(self):
        from playwright.sync_api import sync_playwright
        audit = CashierFixture()
        self.addCleanup(audit.close)
        audit.client.post('/cash-register/open', data={'opening_cash': '100'})
        response = audit.sell()
        ticket_path = response.json['ticket_url']
        sale_id = Sale.query.one().id
        audit.owner_client.post(f'/sales/{sale_id}/return')
        audit.app.config['WTF_CSRF_ENABLED'] = True

        class QuietHandler(WSGIRequestHandler):
            def log(self, *args, **kwargs):
                pass

        server = make_server('127.0.0.1', 0, audit.app, request_handler=QuietHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(stop_server)
        origin = f'http://127.0.0.1:{server.server_port}'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge')
            page = browser.new_page()
            page.goto(origin + '/login')
            page.locator('input[name="email"]').fill(audit.email)
            page.locator('input[name="password"]').fill(audit.password)
            with page.expect_navigation():
                page.locator('#auth-form button[type="submit"]').click()
            for width in (1440, 390):
                with self.subTest(width=width):
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(origin + '/sell')
                    reprint = page.locator(f'a[href="{ticket_path}?print=1"]')
                    self.assertEqual(reprint.count(), 1)
                    self.assertIn('Ticket devuelto completamente', page.content())
                    self.assertEqual(page.goto(origin + ticket_path).status, 200)
                    ticket = page.locator('#printable-ticket')
                    self.assertIn('Ticket devuelto completamente', ticket.inner_text())
                    self.assertIn('Lucia Caja Demo', ticket.inner_text())
                    self.assertIn('20.50', ticket.inner_text())
                    self.assertEqual(ticket.locator('tbody tr').count(), 1)
                    self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    # Verify the print handler, without pretending to test physical hardware.
                    page.evaluate('() => { window.printCalls=0; window.print=()=>{window.printCalls++}; }')
                    page.locator('#print-ticket').click()
                    self.assertEqual(page.evaluate('window.printCalls'), 1)
                    page.locator('input[name="ticket_width"][value="58"]').check()
                    self.assertEqual(page.locator('html').get_attribute('data-ticket-width'), '58')
                    page.emulate_media(media='print')
                    self.assertTrue(ticket.is_visible())
                    self.assertIn('Ticket devuelto completamente', ticket.inner_text())
                    page.emulate_media(media='screen')
                    folder = os.environ.get('PATIA_CASHIER_ARTIFACTS')
                    if folder:
                        Path(folder).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(folder) / f'ticket-devuelto-{width}.png'), full_page=True)
            browser.close()
