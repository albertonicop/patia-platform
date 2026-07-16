import os
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "pos-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, User


class PosFirstSaleTests(unittest.TestCase):
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
        Sale.query.delete()
        Product.query.delete()
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()
        self.user = User(
            email="pos@patia.test",
            company_name="Tienda POS",
            phone="5555555555",
            city="Puebla",
            state="Puebla",
            email_verified=True,
        )
        self.user.set_password("Password123")
        db.session.add(self.user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user.id

    def add_product(self, name="Café Premium", sku="CAFE-1", barcode="750123", stock=5):
        product = Product(
            user_id=self.user.id,
            name=name,
            sku=sku,
            barcode=barcode,
            category="Abarrotes",
            cost_price=10,
            sale_price=25,
            stock=stock,
            min_stock=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def pos_html(self):
        response = self.client.get("/sell")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_pos_without_products_hides_sale_controls(self):
        html = self.pos_html()

        self.assertIn("Agrega inventario antes de vender", html)
        self.assertIn("Ir a inventario", html)
        self.assertNotIn('id="product-search"', html)
        self.assertNotIn('id="cart-body"', html)
        self.assertNotIn('id="checkout-button"', html)

    def test_search_supports_name_sku_and_barcode(self):
        self.add_product()
        html = self.pos_html()

        self.assertIn('name: "Caf\\u00e9 Premium"', html)
        self.assertIn('sku: "CAFE-1"', html)
        self.assertIn('barcode: "750123"', html)
        self.assertIn("normalizeSearch(product.name).includes", html)
        self.assertIn("normalizeSearch(product.sku).includes", html)
        self.assertIn("normalizeSearch(product.barcode).includes", html)

    def test_zero_stock_product_cannot_be_added(self):
        self.add_product(stock=0)
        html = self.pos_html()

        self.assertIn("option.disabled = product.stock <= 0", html)
        self.assertIn("if (product.stock <= 0)", html)
        self.assertIn("Sin existencias", html)

    def test_keyboard_selection_is_available(self):
        self.add_product()
        html = self.pos_html()

        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(key, html)
        self.assertIn('role="combobox"', html)
        self.assertIn('role="listbox"', html)
        self.assertIn('option.setAttribute("role", "option")', html)

    def test_checkout_is_locked_while_submitting_and_empty_cart_cannot_charge(self):
        self.add_product()
        html = self.pos_html()

        self.assertIn('id="checkout-button" type="button" disabled', html)
        self.assertIn("if (!cart.length || saleSubmitting) return", html)
        self.assertIn("saleSubmitting = true", html)
        self.assertIn('checkoutButton.textContent = "Registrando venta…"', html)
        self.assertIn("saleSubmitting = false", html)

    def test_successful_sale_returns_confirmation_and_updates_stock(self):
        product = self.add_product(stock=5)
        request_id = str(uuid.uuid4())

        response = self.client.post(
            "/sell-cart",
            json={"request_id": request_id, "payment_method": "transfer", "items": [{"product_id": product.id, "quantity": 2}]},
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["ticket_id"], request_id)
        self.assertEqual(data["total"], 50)
        self.assertEqual(data["payment_method"], "Transferencia")
        self.assertIsInstance(data["single_sale_id"], int)
        db.session.refresh(product)
        self.assertEqual(product.stock, 3)
        self.assertEqual(Sale.query.one().payment_method, "transfer")

    def test_sale_rejects_unknown_payment_method(self):
        product = self.add_product()
        response = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "stripe",
                "items": [{"product_id": product.id, "quantity": 1}],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Sale.query.count(), 0)

    def test_every_line_in_grouped_ticket_uses_same_payment_method(self):
        first = self.add_product(sku="UNO", barcode="111")
        second = self.add_product(sku="DOS", barcode="222")
        response = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [
                    {"product_id": first.id, "quantity": 1},
                    {"product_id": second.id, "quantity": 2},
                ],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual({sale.payment_method for sale in Sale.query.all()}, {"card"})

    def test_pos_explains_payment_methods_without_processing_cards(self):
        self.add_product()
        html = self.pos_html()
        for value, label in (("cash", "Efectivo"), ("card", "Tarjeta"), ("transfer", "Transferencia"), ("other", "Otro")):
            self.assertIn(f'value="{value}"', html)
            self.assertIn(label, html)
        self.assertIn("PATIA no procesa pagos con tarjeta", html)
        self.assertIn("payment_method: paymentMethod.value", html)

    def test_repeated_request_id_does_not_charge_twice(self):
        product = self.add_product(stock=5)
        request_id = str(uuid.uuid4())
        payload = {"request_id": request_id, "items": [{"product_id": product.id, "quantity": 2}]}

        first = self.client.post("/sell-cart", json=payload)
        second = self.client.post("/sell-cart", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["duplicate"])
        db.session.refresh(product)
        self.assertEqual(product.stock, 3)
        self.assertEqual(Sale.query.filter_by(user_id=self.user.id).count(), 1)

    def test_inventory_is_locked_before_final_stock_and_idempotency_checks(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "routes.py").read_text(
            encoding="utf-8"
        )
        sell_cart = source[source.index("def sell_cart():"):source.index("@main.route(\"/reports\")")]

        lock_position = sell_cart.index(".with_for_update()")
        final_idempotency_position = sell_cart.index(
            "# Repetir la verificación tras bloquear inventario"
        )
        stock_check_position = sell_cart.index("if product.stock < quantity")
        self.assertLess(lock_position, final_idempotency_position)
        self.assertLess(final_idempotency_position, stock_check_position)

    def test_legacy_single_sale_rejects_malformed_product_without_500(self):
        response = self.client.post(
            "/sell",
            data={"product_id": "no-es-id", "quantity": "1"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Selecciona un producto y una cantidad válida", response.get_data(as_text=True))

    def test_confirmation_uses_only_temporary_session_storage(self):
        self.add_product()
        html = self.pos_html()

        self.assertIn("Venta registrada correctamente", html)
        self.assertIn('sessionStorage.setItem("patia_sale_confirmation"', html)
        self.assertIn('sessionStorage.removeItem("patia_sale_confirmation"', html)
        self.assertIn("data.total", html)
        self.assertIn("data.folio", html)
        self.assertIn("data.ticket_url", html)

    def test_recent_sales_empty_and_populated_states(self):
        product = self.add_product()
        empty_html = self.pos_html()
        self.assertIn("Aún no hay ventas registradas", empty_html)

        db.session.add(Sale(
            user_id=self.user.id,
            product_id=product.id,
            quantity=1,
            unit_price=25,
            total=25,
            ticket_id=str(uuid.uuid4()),
        ))
        db.session.commit()
        populated_html = self.pos_html()
        self.assertIn("La cancelación se mantiene por producto", populated_html)
        self.assertIn("Café Premium", populated_html)
        self.assertIn("Cancelar línea", populated_html)
        self.assertIn('data-label="Método"', populated_html)
        self.assertIn('data-label="Total"', populated_html)
        self.assertIn('data-label="Acciones"', populated_html)

    def test_product_names_are_never_written_with_inner_html(self):
        self.add_product(name="<img src=x onerror=alert(1)>")
        html = self.pos_html()

        self.assertNotIn("innerHTML", html)
        self.assertIn("name.textContent = product.name", html)
        self.assertIn("name.textContent = item.name", html)


if __name__ == "__main__":
    unittest.main()
