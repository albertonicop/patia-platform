"""Actual Chromium print PDFs from isolated cashier sales, never customer data."""
import importlib.util
import io
import os
from decimal import Decimal
from pathlib import Path
from threading import Thread
import unittest

from werkzeug.serving import make_server, WSGIRequestHandler
from app import db
from app.models import Product, Sale
from test_cashier_session import CashierFixture


@unittest.skipUnless(importlib.util.find_spec('playwright') and importlib.util.find_spec('pypdf'),
                     'Print PDF checks require optional Playwright and pypdf')
class TicketPrintFormatTests(unittest.TestCase):
    def test_four_formats_short_long_returned_and_device_preferences(self):
        from playwright.sync_api import sync_playwright
        from pypdf import PdfReader

        audit = CashierFixture()
        self.addCleanup(audit.close)
        audit.client.post('/cash-register/open', data={'opening_cash': '100'})
        short = audit.sell()
        self.assertEqual(short.status_code, 200)
        short_path = short.json['ticket_url']
        sale_id = Sale.query.one().id
        audit.product.stock = 123456
        audit.product.sale_price = Decimal('123456.78')
        db.session.commit()
        large = audit.sell(payment_method='card', items=[{'product_id': audit.product.id, 'quantity': 123456}])
        self.assertEqual(large.status_code, 200)
        large_path = large.json['ticket_url']
        products = []
        for index in range(65):
            product = Product(organization_id=audit.member.organization_id, user_id=audit.owner.id,
                              sku=f'PRINT-{index}', name=f'Producto {index:03d} artesanal con descripción muy larga para revisar impresión ' + ('X' * 70 if index == 0 else 'y empaque familiar'),
                              category='Demo', cost_price=Decimal('1.00'), sale_price=Decimal('1234.56'),
                              stock=10, min_stock=0)
            db.session.add(product)
            products.append(product)
        db.session.commit()
        long = audit.sell(payment_method='card', items=[{'product_id': p.id, 'quantity': 3} for p in products])
        self.assertEqual(long.status_code, 200)
        long_path = long.json['ticket_url']
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
        folder = Path(os.environ['PATIA_TICKET_PRINT_ARTIFACTS']) if os.environ.get('PATIA_TICKET_PRINT_ARTIFACTS') else None
        if folder:
            folder.mkdir(parents=True, exist_ok=True)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge')
            try:
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                page = context.new_page()
                page.goto(origin + '/login', wait_until='domcontentloaded')
                page.locator('input[name="email"]').fill(audit.email)
                page.locator('input[name="password"]').fill(audit.password)
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('#auth-form button[type="submit"]').click()
                for case, path in [('corto', short_path), ('largo', long_path), ('importes-grandes', large_path), ('devuelto', short_path)]:
                    if case == 'devuelto':
                        audit.app.config['WTF_CSRF_ENABLED'] = False
                        self.assertEqual(audit.owner_client.post(f'/sales/{sale_id}/return').status_code, 302)
                        audit.app.config['WTF_CSRF_ENABLED'] = True
                    for format_, width_mm, height_mm in [('58', 58, None), ('80', 80, None), ('letter', 215.9, 279.4), ('a4', 210, 297)]:
                        with self.subTest(case=case, format=format_):
                            page.emulate_media(media='screen')
                            page.set_viewport_size({'width': 1440, 'height': 1000})
                            self.assertEqual(page.goto(origin + path).status, 200)
                            page.locator(f'input[name="ticket_width"][value="{format_}"]').check()
                            page.wait_for_function('document.fonts.status === "loaded"')
                            ticket_text = page.locator('#printable-ticket').text_content()
                            for viewport in (1440, 390):
                                page.set_viewport_size({'width': viewport, 'height': 900})
                                self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                                self.assertTrue(page.locator('#printable-ticket').evaluate('(ticket) => { const r = ticket.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth + 1; }'), 'Receipt preview must fit the viewport, even when body overflow is hidden')
                                self.assertTrue(page.locator('.ticket-page-v2__actions').evaluate('(options) => options.getBoundingClientRect().right <= innerWidth + 1'))
                                if viewport == 390:
                                    self.assertTrue(page.locator('#printable-ticket tbody').evaluate('(body) => [...body.querySelectorAll("td")].every(cell => cell.getBoundingClientRect().width >= body.getBoundingClientRect().width - 1)'), 'Mobile details must use full width, not narrow desktop columns')
                                self.assertEqual(page.locator('input[name="ticket_width"]:checked').input_value(), format_)
                                if folder and case == 'corto':
                                    page.screenshot(path=str(folder / f'{format_}-{viewport}.png'), full_page=True)
                            page.emulate_media(media='print')
                            # Native print-menu preparation, not just our button.
                            page.evaluate('dispatchEvent(new Event("beforeprint"))')
                            self.assertFalse(page.locator('.sidebar').is_visible())
                            self.assertFalse(page.locator('#print-ticket').is_visible())
                            self.assertEqual(page.locator('#printable-ticket').text_content(), ticket_text)
                            pdf = page.pdf(prefer_css_page_size=True, display_header_footer=False)
                            reader = PdfReader(io.BytesIO(pdf))
                            text = '\n'.join(p.extract_text() for p in reader.pages)
                            self.assertIn('Lucia Caja Demo', text)
                            self.assertIn('Gracias por su compra', text)
                            for forbidden in ('Cerrar sesión', 'Opciones del ticket', 'Guardar o descargar', 'Volver al POS'):
                                self.assertNotIn(forbidden, text)
                            for pdf_page in reader.pages:
                                self.assertAlmostEqual(float(pdf_page.mediabox.width) * 25.4 / 72, width_mm, delta=.4)
                                if height_mm:
                                    self.assertAlmostEqual(float(pdf_page.mediabox.height) * 25.4 / 72, height_mm, delta=.4)
                            if importlib.util.find_spec('pymupdf'):
                                import pymupdf
                                with pymupdf.open(stream=pdf, filetype='pdf') as document:
                                    for pdf_page in document:
                                        for word in pdf_page.get_text('words'):
                                            self.assertGreaterEqual(word[0], -.5)
                                            self.assertGreaterEqual(word[1], -.5)
                                            self.assertLessEqual(word[2], pdf_page.rect.width + .5)
                                            self.assertLessEqual(word[3], pdf_page.rect.height + .5)
                            if case == 'largo':
                                compact_text = ''.join(text.split())
                                for i in range(65):
                                    self.assertIn(f'Producto {i:03d}', text)
                                    self.assertIn(''.join(products[i].name.split()), compact_text)
                                self.assertEqual(text.count('1,234.56'), 65)
                                self.assertEqual(text.count('3,703.68'), 65)
                                for pdf_page in reader.pages:
                                    page_text = pdf_page.extract_text()
                                    self.assertEqual(page_text.count('1,234.56'), page_text.count('3,703.68'), 'A product row must remain on the same page')
                                self.assertIn('240,739.20', text)
                                if height_mm:
                                    self.assertGreater(len(reader.pages), 1)
                            elif case == 'importes-grandes':
                                self.assertIn('123456', text)
                                self.assertIn('123,456.78', text)
                                self.assertIn('15,241,480,231.68', text)
                            else:
                                self.assertIn('20.50', text)
                                self.assertIn('29.50', text)
                                self.assertIn('50.00', text)
                                if case == 'devuelto':
                                    self.assertIn('Ticket devuelto completamente', ' '.join(text.split()))
                                    self.assertIn('Devuelto', text)
                            if not height_mm:
                                self.assertEqual(len(reader.pages), 1, 'Roll must not produce blank sheets or clip the last line')
                            if folder:
                                (folder / f'ticket-{case}-{format_}.pdf').write_bytes(pdf)
                page.emulate_media(media='screen')
                page.goto(origin + short_path)
                page.locator('input[value="letter"]').check()
                page.reload()
                self.assertEqual(page.locator('input[name="ticket_width"]:checked').input_value(), 'letter')
                another = context.new_page()
                another.goto(origin + short_path)
                self.assertEqual(another.locator('input[name="ticket_width"]:checked').input_value(), 'letter')
                # A separate device/browser profile does not inherit this preference.
                separate = browser.new_context(storage_state={'cookies': context.cookies(), 'origins': []})
                separate.route('**/*', lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                other = separate.new_page()
                other.goto(origin + short_path)
                self.assertEqual(other.locator('input[name="ticket_width"]:checked').input_value(), '80')
                other.evaluate('localStorage.setItem("patia.ticket.print-format.v1", "invalid")')
                other.reload()
                self.assertEqual(other.locator('input[name="ticket_width"]:checked').input_value(), '80')
                separate.close()
                # Storage denied, printing still works; reload/reprint never changes stock.
                page.add_init_script('Object.defineProperty(window, "localStorage", {get() {throw new Error("denied")}}); window.print = () => {window.printCalls = (window.printCalls || 0) + 1};')
                page.goto(origin + short_path + '?print=1')
                page.wait_for_function('window.printCalls === 1')
                page.locator('input[value="58"]').check()
                page.locator('#print-ticket').click()
                page.wait_for_function('window.printCalls === 2')
                context.close()
                self.assertEqual(Sale.query.count(), 66)
            finally:
                browser.close()
