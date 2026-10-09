"""Check plan settings on desktop/mobile with isolated SQLite and mocked Stripe."""
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(
    DATABASE_URL="sqlite:///:memory:", STRIPE_DISABLED="false",
    SECRET_KEY="isolated-plan-browser", STRIPE_SECRET_KEY="sk_test_fake",
    STRIPE_WEBHOOK_SECRET="whsec_fake", STRIPE_PRICE_ID="price_starter",
    PUBLIC_BASE_URL="https://patia.test",
)

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app.plans import STARTER
from tests.test_commercial_plans import CommercialPlanTests

fixture = CommercialPlanTests()
fixture.setUp()
fixture.app.config.update(SESSION_COOKIE_SECURE=False)
fixture.activate(STARTER)
subscription = fixture.stripe_subscription()
subscription["items"]["data"][0]["price"].update(unit_amount=19900, currency="mxn")
client = fixture.client_for(fixture.owner)
cookie = client.get_cookie("session").value
server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"

def modify(_subscription_id, **changes):
    subscription.update(changes)
    return subscription

try:
    with patch("app.routes.stripe.Subscription.retrieve", return_value=subscription), \
         patch("app.routes.stripe.Subscription.modify", side_effect=modify) as stripe_modify, \
         patch("app.routes.stripe.Customer.retrieve", return_value={"id": "cus_commercial"}), \
         sync_playwright() as pw:
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        options = {"headless": True}
        if edge.exists():
            options["executable_path"] = str(edge)
        browser = pw.chromium.launch(**options)
        for width in (1440, 390):
            context = browser.new_context(viewport={"width": width, "height": 900})
            context.add_cookies([{"name": "session", "value": cookie, "url": base}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
            page.goto(base + "/settings", wait_until="networkidle")
            field = page.locator("#organization-business-type")
            field.scroll_into_view_if_needed()
            assert field.is_visible() and field.get_attribute("readonly") is not None
            assert page.locator('select[name="business_type"]').count() == 0
            page.goto(base + "/subscribe", wait_until="networkidle")
            restaurant_only = page.get_by_role("button", name="Solo para restaurantes", exact=True)
            restaurant_only.scroll_into_view_if_needed()
            assert restaurant_only.is_visible() and restaurant_only.is_disabled()
            assert page.locator('form input[name="plan_code"][value="RESTAURANT"]').count() == 0
            page.goto(base + "/subscription", wait_until="networkidle")
            button = page.get_by_role("button", name="Cancelar suscripción", exact=True)
            button.scroll_into_view_if_needed()
            assert button.is_visible() and button.is_enabled()
            assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth"), width
            button.click()
            page.wait_for_load_state("networkidle")
            reactivate = page.get_by_role("button", name="Reactivar suscripción", exact=True)
            reactivate.scroll_into_view_if_needed()
            assert reactivate.is_visible()
            reactivate.click()
            page.wait_for_load_state("networkidle")
            assert page.get_by_role("button", name="Cancelar suscripción", exact=True).count() == 1
            assert not errors, errors
            context.close()
        assert stripe_modify.call_count == 4
        browser.close()
        print("PASS: settings locked; cancellation/reactivation at 1440px and 390px; Stripe mocked.")
finally:
    server.shutdown()
    thread.join(timeout=5)
    fixture.tearDown()
