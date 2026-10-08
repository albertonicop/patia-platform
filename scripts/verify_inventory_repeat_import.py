"""Browser regression for repeated inventory imports. Uses only an in-memory DB.

Run manually with Playwright and Chromium installed:
    python scripts/verify_inventory_repeat_import.py
"""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(DATABASE_URL="sqlite:///:memory:", STRIPE_DISABLED="true",
                  SECRET_KEY="isolated-import-browser", PATIA_AI_ENABLED="false")

from app import create_app, db
from app.models import User, Product
from app.team.services import ensure_owner_organization
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def verify():
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                      RATELIMIT_ENABLED=False, SESSION_COOKIE_SECURE=False)
    with app.app_context():
        db.create_all()
        user = User(email="repeat-import@patia.test", company_name="QA", email_verified=True)
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        user_id = user.id
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
    cookie = client.get_cookie("session").value
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            options = {"headless": True}
            edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
            if edge.exists():
                options["executable_path"] = str(edge)
            browser = pw.chromium.launch(**options)
            for width in (1440, 390):
                context = browser.new_context(viewport={"width": width, "height": 900})
                context.add_cookies([{"name": "session", "value": cookie, "url": base}])
                page = context.new_page()
                page.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
                page.goto(base + "/products", wait_until="networkidle")
                for stock in (3, 9):
                    content = f"SKU,Nombre del producto,Precio de venta,Stock inicial\nREPEAT-{width},Producto ficticio,15,{stock}\n".encode()
                    page.locator("#catalog-file").set_input_files({"name": "inventario.csv", "mimeType": "text/csv", "buffer": content})
                    with page.expect_response("**/api/products/import/preview") as preview:
                        page.locator("#catalog-import-submit").click()
                    data = preview.value.json()
                    assert data["ready"] and not data["errors"]
                    assert data["summary"]["new" if stock == 3 else "updated"] == 1
                    confirm = page.locator("#catalog-import-confirm")
                    assert confirm.is_visible(), "Import confirmation stays hidden after a previous import"
                    assert not page.locator("#catalog-import-finish").is_visible()
                    assert not confirm.is_disabled()
                    with page.expect_response("**/api/products/import/commit") as commit:
                        confirm.click()
                    assert commit.value.json()["ok"]
                    assert page.locator("#catalog-import-finish").is_visible()
                    page.locator("#catalog-import-dialog [data-import-close]").first.click()
                with app.app_context():
                    product = Product.query.filter_by(sku=f"REPEAT-{width}").one()
                    assert product.stock == 9
                print(f"PASS {width}px: first import and second import without page reload", flush=True)
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()


if __name__ == "__main__":
    verify()
