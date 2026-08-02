import os
import unittest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "low-stock-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import InventoryRestockEvent, Product, User
from app.team.services import ensure_owner_organization
from flask_babel import force_locale


class ActionableLowStockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        cls.context = cls.app.app_context()
        cls.context.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.engine.dispose()
        cls.context.pop()

    def setUp(self):
        db.session.rollback()
        InventoryRestockEvent.query.delete()
        Product.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()
        self.owner = self.make_user("owner@low-stock.test", "Tienda Uno")
        with self.client.session_transaction() as session:
            session["user_id"] = self.owner.id

    def make_user(self, email, company):
        user = User(
            email=email,
            company_name=company,
            phone="5555555555",
            city="Puebla",
            state="Puebla",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        return user

    def add_product(self, user, sku, name, stock, minimum, supplier=None):
        product = Product(
            organization_id=user.organization_memberships[0].organization_id,
            user_id=user.id,
            sku=sku,
            name=name,
            category="General",
            supplier=supplier,
            cost_price=10,
            sale_price=20,
            stock=stock,
            min_stock=minimum,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def test_dashboard_lists_below_and_exact_minimum_with_suggestion(self):
        self.add_product(
            self.owner, "LOW-1", "Producto bajo", 2, 5, "Proveedor Norte"
        )
        self.add_product(self.owner, "LIMIT-1", "Producto en mínimo", 4, 4)

        html = self.client.get("/").get_data(as_text=True)

        self.assertIn("Producto bajo", html)
        self.assertIn("Proveedor Norte", html)
        self.assertIn("LOW-1", html)
        self.assertIn("Producto en mínimo", html)
        self.assertIn("<dd>3</dd>", html)
        self.assertIn("<dd>0</dd>", html)
        self.assertIn('aria-haspopup="dialog"', html)

    def test_inventory_without_alerts_shows_in_order_state(self):
        self.add_product(self.owner, "OK-1", "Producto suficiente", 8, 2)

        html = self.client.get("/products").get_data(as_text=True)
        dashboard = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            "Tu inventario está en orden. No hay productos que requieran "
            "reabastecimiento.",
            html,
        )
        self.assertIn("Ver productos con stock bajo (0)", html)
        self.assertIn(
            "Tu inventario está en orden. No hay productos que requieran "
            "reabastecimiento.",
            dashboard,
        )

    def test_low_stock_is_isolated_between_companies(self):
        other = self.make_user("other@low-stock.test", "Tienda Dos")
        self.add_product(self.owner, "OWN-1", "Producto propio", 1, 3)
        self.add_product(other, "OTHER-1", "Producto ajeno secreto", 0, 8)

        dashboard = self.client.get("/").get_data(as_text=True)
        inventory = self.client.get("/products?low_stock=1").get_data(as_text=True)

        self.assertIn("Producto propio", dashboard)
        self.assertIn("Producto propio", inventory)
        self.assertNotIn("Producto ajeno secreto", dashboard)
        self.assertNotIn("Producto ajeno secreto", inventory)

    def test_inventory_filter_can_be_enabled_and_removed(self):
        self.add_product(self.owner, "LOW-2", "Necesita compra", 1, 2)
        self.add_product(self.owner, "OK-2", "Existencia suficiente", 7, 2)

        filtered = self.client.get("/products?low_stock=1").get_data(as_text=True)
        unfiltered = self.client.get("/products").get_data(as_text=True)

        self.assertIn("Necesita compra", filtered)
        self.assertNotIn("Existencia suficiente", filtered)
        self.assertIn('aria-current="true"', filtered)
        self.assertIn("Limpiar filtros", filtered)
        self.assertIn("Necesita compra", unfiltered)
        self.assertIn("Existencia suficiente", unfiltered)
        self.assertNotIn("Limpiar filtros", unfiltered)

    def test_product_exactly_at_minimum_is_in_filter(self):
        self.add_product(self.owner, "LIMIT-2", "Justo en mínimo", 3, 3)

        html = self.client.get("/products?low_stock=1").get_data(as_text=True)

        self.assertIn("Justo en mínimo", html)
        self.assertIn("Ver productos con stock bajo (1)", html)

    def test_low_stock_copy_is_translated_to_english(self):
        self.add_product(self.owner, "LOW-EN", "User product name", 1, 4)
        self.client.post(
            "/language",
            data={"language": "en", "next": "/products?low_stock=1"},
        )

        with force_locale("en"):
            dashboard = self.client.get("/").get_data(as_text=True)
            inventory = self.client.get("/products?low_stock=1").get_data(as_text=True)

        self.assertIn("Alerts that need attention", dashboard)
        self.assertIn(
            "Prioritize the products that have reached their minimum stock.",
            dashboard,
        )
        self.assertIn("Products with low stock", dashboard)
        self.assertIn("Restock inventory", dashboard)
        self.assertIn("Quantity received", dashboard)
        self.assertIn("New stock", dashboard)
        self.assertIn("View low-stock products (1)", inventory)
        self.assertIn("Clear filters", inventory)

    def test_restock_increases_stock_and_records_audit_event(self):
        product = self.add_product(
            self.owner, "RESTOCK-1", "Producto recibido", 1, 3
        )

        response = self.client.post(
            f"/products/{product.id}/restock",
            data={"quantity": "3"},
            follow_redirects=True,
        )

        db.session.refresh(product)
        event = InventoryRestockEvent.query.one()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(product.stock, 4)
        self.assertEqual(event.user_id, self.owner.id)
        self.assertEqual(event.product_id, product.id)
        self.assertEqual(event.quantity, 3)
        self.assertEqual(event.stock_before, 1)
        self.assertEqual(event.stock_after, 4)
        self.assertIsNotNone(event.created_at)
        self.assertNotIn("Producto recibido</strong>", response.get_data(as_text=True))

    def test_restock_rejects_non_positive_quantity(self):
        product = self.add_product(
            self.owner, "RESTOCK-2", "Producto sin cambio", 1, 3
        )

        response = self.client.post(
            f"/products/{product.id}/restock",
            data={"quantity": "0"},
            follow_redirects=True,
        )

        db.session.refresh(product)
        self.assertEqual(product.stock, 1)
        self.assertEqual(InventoryRestockEvent.query.count(), 0)
        self.assertIn(
            "La cantidad recibida debe ser mayor que cero.",
            response.get_data(as_text=True),
        )

    def test_restock_cannot_access_another_company_product(self):
        other = self.make_user("restock-other@patia.test", "Otra Tienda")
        product = self.add_product(
            other, "RESTOCK-OTHER", "Producto de otra empresa", 1, 3
        )

        response = self.client.post(
            f"/products/{product.id}/restock",
            data={"quantity": "5"},
        )

        db.session.refresh(product)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(product.stock, 1)
        self.assertEqual(InventoryRestockEvent.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
