"""Browser regressions using an isolated database and intercepted HTTP requests."""
import importlib.util
import unittest

import test_cash_register as cash_fixture
from app import db
from app.models import CashMovement, Sale


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is optional")
class PosRetryBrowserTests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright, Error

        self.fixture = cash_fixture.CashRegisterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = self.fixture.client_for(self.fixture.owner)
        self.fixture.open_register(self.client, "0.00")
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
        self.sale_requests = []
        self.lose_first_response = False
        self.page.route("https://patia.test/**", self.serve)

    def serve(self, route):
        request = route.request
        path = request.url.split("patia.test", 1)[1]
        response = self.client.open(
            path,
            method=request.method,
            data=request.post_data,
            content_type=request.headers.get("content-type"),
        )
        if path == "/sell-cart":
            self.sale_requests.append(request.post_data_json)
            if self.lose_first_response and len(self.sale_requests) == 1:
                # The transaction committed; only its response was lost.
                self.assertEqual(response.status_code, 200)
                route.abort("connectionclosed")
                return
        route.fulfill(
            status=response.status_code,
            body=response.data,
            content_type=response.content_type,
            headers={k: v for k, v in response.headers if k.lower() == "location"},
        )

    def test_retry_after_lost_success_response_preserves_request_id(self):
        self.lose_first_response = True
        self.page.goto("https://patia.test/sell")
        self.page.evaluate("selectProduct(PRODUCTS[0]);")
        self.page.locator("#amount-received").fill("10.00")
        self.page.locator("#checkout-button").click()
        self.page.wait_for_function("!saleSubmitting")
        self.assertEqual(Sale.query.count(), 1)
        self.page.locator("#checkout-button").click()
        self.page.wait_for_function("document.querySelector('#sale-confirmation') && !document.querySelector('#sale-confirmation').hidden")
        self.assertEqual(len(self.sale_requests), 2)
        self.assertEqual(
            self.sale_requests[0]["request_id"], self.sale_requests[1]["request_id"]
        )
        self.assertEqual(Sale.query.count(), 1)
        self.assertEqual(CashMovement.query.filter_by(movement_type="SALE_CASH").count(), 1)
        db.session.refresh(self.fixture.product)
        self.assertEqual(self.fixture.product.stock, 29)

    def test_cash_readiness_uses_exact_cent_total(self):
        self.page.goto("https://patia.test/sell")
        result = self.page.evaluate("""() => {
            cart = [{id: 1, price: 0.10, quantity: 1}, {id: 2, price: 0.20, quantity: 1}];
            paymentMethod.value = 'cash';
            amountReceived.value = '0.30';
            return {total: cartAmount(), ready: cashPaymentIsReady()};
        }""")
        self.assertEqual(result["total"], 0.30)
        self.assertTrue(result["ready"])
