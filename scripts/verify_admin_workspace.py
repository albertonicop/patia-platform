"""Exercise administration productivity controls with isolated test data."""
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
os.environ.update(DATABASE_URL="sqlite:///:memory:", STRIPE_DISABLED="true",
                  STRIPE_SECRET_KEY="sk_test_fake", STRIPE_WEBHOOK_SECRET="whsec_fake",
                  SECRET_KEY="isolated-admin-browser", PUBLIC_BASE_URL="https://patia.test")

from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server
from app import db
from tests.test_admin_redesign import AdminRedesignTests

fixture = AdminRedesignTests()
fixture.setUp()
fixture.app.config.update(SESSION_COOKIE_SECURE=False)
fixture.trial.created_at = datetime.utcnow() - timedelta(days=12)
fixture.pro.subscription_status = "past_due"
db.session.commit()
for index in range(26):
    fixture._add_owner(f"browser-{index}@example.com", f"Directorio {index:02d}")
fixture._login(fixture.admin, fixture.admin_membership)
cookie = fixture.client.get_cookie("session").value
server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = f"http://127.0.0.1:{server.server_port}"
artifacts = root / "artifacts"
artifacts.mkdir(exist_ok=True)

try:
    with sync_playwright() as pw:
        edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        options = {"headless": True}
        if edge.exists():
            options["executable_path"] = str(edge)
        browser = pw.chromium.launch(**options)
        for width in (1440, 1024, 390, 320):
            context = browser.new_context(viewport={"width": width, "height": 1000})
            context.add_cookies([{"name": "session", "value": cookie, "url": base}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
            page.goto(base + "/admin", wait_until="networkidle")
            assert page.locator("#primary-navigation a").count() == 1
            assert page.locator("#primary-navigation a").get_attribute("href") == "/admin"
            assert page.locator('.app-topbar-v2__notification').count() == 0
            assert page.locator("#admin-search").is_visible()
            assert page.locator("#admin-search").evaluate("el => el.getBoundingClientRect().bottom <= innerHeight"), (width, "search below first screen")
            assert page.locator(".admin-workspace__shortcuts a").count() == 6
            assert page.locator('select[name="sort"]').input_value() == "priority"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, "overflow")
            page.screenshot(path=str(artifacts / f"admin-workspace-{width}.png"), full_page=False)
            page.locator('.admin-workspace__shortcuts a[href*="attention=payment"]').click()
            page.wait_for_load_state("networkidle")
            assert page.locator(".admin-v4__table tbody tr").count() == 1
            page.goto(base + "/admin?q=Directorio&sort=name", wait_until="networkidle")
            page.locator(".admin-workspace__pagination").first.get_by_role("link", name="Siguiente", exact=True).click()
            page.wait_for_load_state("networkidle")
            assert "page=2" in page.url and "q=Directorio" in page.url and "sort=name" in page.url
            assert page.locator(".admin-v4__table tbody tr").count() == 1
            assert "Directorio 25" in page.locator(".admin-v4__directory").inner_text()
            page.locator(".admin-workspace__pagination").first.get_by_role("link", name="Anterior", exact=True).click()
            page.wait_for_load_state("networkidle")
            assert page.locator(".admin-v4__table tbody tr").count() == 25
            page.locator(".admin-v4__filter > summary").click()
            page.locator('select[name="plan"]').select_option("restaurant")
            page.locator("#admin-search").fill("")
            page.get_by_role("button", name="Aplicar filtros", exact=True).click()
            page.wait_for_load_state("networkidle")
            assert "Plan: Cocina" in page.locator(".admin-v4__chips").inner_text()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert not errors, errors
            context.close()
            print(f"PASS {width}px: priority, quick filters, search, pagination, Cocina filter, no page overflow.")
        browser.close()
finally:
    server.shutdown()
    thread.join(timeout=5)
    fixture.tearDown()
