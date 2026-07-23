import os
from pathlib import Path
import tempfile
import unittest


os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost")
os.environ.setdefault("SECRET_KEY", "quick-load-test-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_quick_load")
os.environ.setdefault("STRIPE_PRICE_ID", "price_quick_load")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_quick_load")

from app import create_app, db
from app.barcodes import lookup_barcode
from app.models import InventoryRestockEvent, Product, User
from app.team.services import ensure_owner_organization
from sqlalchemy.exc import IntegrityError


class QuickLoadScannerTests(unittest.TestCase):
    def setUp(self):
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner = self.make_user("scanner-owner@patia.test", "Scanner Shop")
        self.other = self.make_user("scanner-other@patia.test", "Other Shop")
        self.client = self.authenticated_client(self.owner)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        os.remove(self.db_path)

    def make_user(self, email, company):
        user = User(
            email=email,
            company_name=company,
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        return user

    def authenticated_client(self, user):
        client = self.app.test_client()
        with client.session_transaction() as user_session:
            user_session["user_id"] = user.id
        return client

    def add_product(
        self,
        user,
        *,
        barcode,
        sku="SKU-1",
        name="Producto existente",
        active=True,
        stock=3,
    ):
        product = Product(
            organization_id=user.organization_memberships[0].organization_id,
            user_id=user.id,
            barcode=barcode,
            sku=sku,
            name=name,
            category="Abarrotes",
            supplier="Proveedor Uno",
            cost_price=10,
            sale_price=19.5,
            stock=stock,
            min_stock=5,
            is_active=active,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def product_payload(self, barcode, **overrides):
        payload = {
            "barcode": barcode,
            "name": "Agua 1 L",
            "sku": "AGUA-1",
            "category": "Bebidas",
            "supplier": "Distribuidora",
            "cost_price": "8.25",
            "sale_price": "14.50",
            "stock": "6",
            "min_stock": "2",
        }
        payload.update(overrides)
        return payload

    def test_inventory_links_to_dedicated_quick_load_page(self):
        inventory = self.client.get("/products").get_data(as_text=True)
        page = self.client.get("/products/quick-load").get_data(as_text=True)

        self.assertIn("Carga rápida con escáner", inventory)
        self.assertIn('href="/products/quick-load"', inventory)
        self.assertIn('id="barcode-input"', page)
        self.assertIn("autofocus", page)
        self.assertNotIn("quick-load", Path("app/templates/base.html").read_text("utf-8"))

    def test_existing_active_product_is_returned_with_company_data(self):
        product = self.add_product(self.owner, barcode="750001", stock=4)

        response = self.client.get("/api/products/quick-load/lookup?barcode=750001")
        data = response.get_json()

        self.assertEqual(200, response.status_code)
        self.assertTrue(data["found"])
        self.assertEqual(product.id, data["product"]["id"])
        self.assertEqual("Proveedor Uno", data["product"]["supplier"])
        self.assertTrue(data["product"]["is_active"])
        self.assertIn(f"/products/{product.id}/edit", data["product"]["edit_url"])

    def test_archived_product_is_not_duplicated_and_can_be_restored(self):
        product = self.add_product(
            self.owner,
            barcode="750002",
            sku="ARCHIVED",
            active=False,
            stock=1,
        )

        lookup = self.client.get(
            "/api/products/quick-load/lookup?barcode=750002"
        ).get_json()
        response = self.client.post(
            f"/api/products/{product.id}/quick-restock",
            json={"quantity": 4},
        )

        db.session.refresh(product)
        self.assertTrue(lookup["found"])
        self.assertFalse(lookup["product"]["is_active"])
        self.assertIsNone(lookup["product"]["edit_url"])
        self.assertEqual(200, response.status_code)
        self.assertTrue(product.is_active)
        self.assertEqual(5, product.stock)
        self.assertEqual(1, Product.query.filter_by(barcode="750002").count())

    def test_new_product_preserves_leading_zero_barcode_and_all_fields(self):
        response = self.client.post(
            "/api/products/quick-load",
            json=self.product_payload("001234567890"),
        )
        product = Product.query.filter_by(user_id=self.owner.id).one()

        self.assertEqual(201, response.status_code)
        self.assertEqual("001234567890", product.barcode)
        self.assertEqual("Agua 1 L", product.name)
        self.assertEqual("Bebidas", product.category)
        self.assertEqual("Distribuidora", product.supplier)
        self.assertEqual(8.25, product.cost_price)
        self.assertEqual(14.5, product.sale_price)
        self.assertEqual(6, product.stock)
        self.assertEqual(2, product.min_stock)

    def test_sku_is_generated_and_remains_editable_in_page(self):
        lookup = self.client.get(
            "/api/products/quick-load/lookup?barcode=0000123"
        ).get_json()
        payload = self.product_payload("0000123", sku="")
        response = self.client.post("/api/products/quick-load", json=payload)

        product = Product.query.filter_by(user_id=self.owner.id).one()
        page = self.client.get("/products/quick-load").get_data(as_text=True)
        self.assertEqual("BC-0000123", lookup["suggested_sku"])
        self.assertEqual(201, response.status_code)
        self.assertEqual("BC-0000123", product.sku)
        self.assertIn('id="quick-sku"', page)
        self.assertNotIn('id="quick-sku" name="sku" readonly', page)

    def test_duplicate_barcode_returns_existing_product_without_creating(self):
        existing = self.add_product(self.owner, barcode="DUP-100")

        response = self.client.post(
            "/api/products/quick-load",
            json=self.product_payload("DUP-100", sku="OTHER"),
        )
        data = response.get_json()

        self.assertEqual(409, response.status_code)
        self.assertTrue(data["duplicate"])
        self.assertEqual(existing.id, data["product"]["id"])
        self.assertEqual(1, Product.query.count())

    def test_database_constraint_closes_concurrent_duplicate_race(self):
        first = Product(
            organization_id=self.owner.organization_memberships[0].organization_id,
            user_id=self.owner.id,
            barcode="RACE-1",
            sku="RACE-A",
            name="Race A",
            cost_price=0,
            sale_price=1,
            stock=0,
            min_stock=0,
        )
        second = Product(
            organization_id=self.owner.organization_memberships[0].organization_id,
            user_id=self.owner.id,
            barcode="RACE-1",
            sku="RACE-B",
            name="Race B",
            cost_price=0,
            sale_price=1,
            stock=0,
            min_stock=0,
        )
        db.session.add_all([first, second])
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()
        self.assertEqual(0, Product.query.filter_by(barcode="RACE-1").count())

    def test_same_barcode_is_allowed_for_different_companies(self):
        self.add_product(self.owner, barcode="SHARED-1", sku="OWNER-SKU")
        self.add_product(self.other, barcode="SHARED-1", sku="OTHER-SKU")

        self.assertEqual(2, Product.query.filter_by(barcode="SHARED-1").count())

    def test_lookup_and_restock_are_isolated_between_companies(self):
        foreign = self.add_product(
            self.other,
            barcode="PRIVATE-1",
            sku="PRIVATE-SKU",
            name="Producto secreto",
        )

        lookup = self.client.get(
            "/api/products/quick-load/lookup?barcode=PRIVATE-1"
        ).get_json()
        restock = self.client.post(
            f"/api/products/{foreign.id}/quick-restock",
            json={"quantity": 5},
        )

        db.session.refresh(foreign)
        self.assertFalse(lookup["found"])
        self.assertEqual(404, restock.status_code)
        self.assertEqual(3, foreign.stock)

    def test_restock_records_existing_audit_history(self):
        product = self.add_product(self.owner, barcode="RESTOCK-1", stock=2)

        response = self.client.post(
            f"/api/products/{product.id}/quick-restock",
            json={"quantity": "3"},
        )
        event = InventoryRestockEvent.query.one()

        self.assertEqual(200, response.status_code)
        self.assertEqual(2, event.stock_before)
        self.assertEqual(5, event.stock_after)
        self.assertEqual(3, event.quantity)
        self.assertEqual(self.owner.id, event.user_id)

    def test_invalid_inputs_do_not_change_inventory(self):
        for barcode in ("", " " * 4, "x" * 65, "ABC\x01"):
            response = self.client.get(
                "/api/products/quick-load/lookup",
                query_string={"barcode": barcode},
            )
            self.assertEqual(400, response.status_code)

        response = self.client.post(
            "/api/products/quick-load",
            json=self.product_payload("VALID-1", stock="-1"),
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual(0, Product.query.count())

    def test_multiple_consecutive_products_can_be_saved(self):
        first = self.client.post(
            "/api/products/quick-load",
            json=self.product_payload("NEXT-1", sku="NEXT-1"),
        )
        second = self.client.post(
            "/api/products/quick-load",
            json=self.product_payload("NEXT-2", sku="NEXT-2", name="Segundo"),
        )

        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        self.assertEqual(2, Product.query.filter_by(user_id=self.owner.id).count())

    def test_keyboard_safe_dom_and_double_submit_guards_are_present(self):
        template = Path("app/templates/quick_load.html").read_text("utf-8")

        self.assertIn('event.key === "Enter"', template)
        self.assertIn("event.ctrlKey || event.metaKey", template)
        self.assertIn("productForm.requestSubmit()", template)
        self.assertIn("if (busy)", template)
        self.assertIn("productSubmit.disabled = true", template)
        self.assertIn("showProduct(data.product, true)", template)
        self.assertIn("window.setTimeout(() => barcodeInput.focus()", template)
        self.assertIn(".textContent =", template)
        self.assertNotIn("innerHTML", template)

    def test_english_interface_and_errors_are_translated(self):
        self.client.post(
            "/language",
            data={"language": "en", "next": "/products/quick-load"},
        )
        page = self.client.get("/products/quick-load").get_data(as_text=True)
        invalid = self.client.get(
            "/api/products/quick-load/lookup?barcode="
        ).get_json()

        self.assertIn("Quick scanner entry", page)
        self.assertIn("Scan or enter the barcode", page)
        self.assertIn("End session", page)
        self.assertEqual("Scan or enter a valid barcode.", invalid["error"])

    def test_external_catalog_adapter_is_inert(self):
        self.assertIsNone(lookup_barcode("7500000000000"))


if __name__ == "__main__":
    unittest.main()
