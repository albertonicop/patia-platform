import io
import os
import re
import unittest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "inventory-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, User


CSV_HEADER = (
    "SKU,Codigo de barras,Nombre del producto,Categoria,Proveedor,"
    "Costo,Precio de venta,Stock inicial,Stock minimo\n"
)


class InventoryImportTests(unittest.TestCase):
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
            email="inventory@patia.test",
            company_name="Tienda Inventario",
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

    def add_product(self, sku="EXISTE", barcode="7501", stock=5):
        product = Product(
            user_id=self.user.id,
            sku=sku,
            barcode=barcode,
            name="Producto existente",
            category="General",
            cost_price=10,
            sale_price=20,
            stock=stock,
            min_stock=2,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def inventory_html(self):
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def import_csv(self, rows, filename="catalog.csv"):
        content = (CSV_HEADER + rows).encode("utf-8")
        return self.client.post(
            "/import-products",
            data={"catalog_file": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def test_empty_inventory_hides_catalog_controls(self):
        html = self.inventory_html()

        self.assertIn("Agregar primer producto", html)
        self.assertIn("Importar catálogo", html)
        self.assertNotIn("Buscar producto…", html)
        self.assertNotIn("Seleccionar productos", html)
        self.assertNotIn("Borrar todo el catálogo", html)
        self.assertNotIn("<table>", html)

    def test_inventory_with_products_keeps_catalog_controls(self):
        self.add_product()

        html = self.inventory_html()

        self.assertIn("Buscar producto…", html)
        self.assertIn("Seleccionar productos", html)
        self.assertIn("Borrar todo el catálogo", html)
        self.assertIn("Producto existente", html)
        self.assertIn("<table>", html)

    def test_manual_form_keeps_names_and_numeric_validation(self):
        html = self.inventory_html()
        for name in (
            "name", "cost_price", "sale_price", "stock", "sku",
            "barcode", "category", "supplier", "min_stock",
        ):
            self.assertRegex(html, rf'<input[^>]*name="{name}"')

        self.assertRegex(html, r'<input[^>]*name="name"[^>]*required')
        for name, step in (("cost_price", "0.01"), ("sale_price", "0.01"),
                           ("stock", "1"), ("min_stock", "1")):
            tag = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
            self.assertIn('min="0"', tag)
            self.assertIn(f'step="{step}"', tag)
        self.assertIn("Opciones avanzadas", html)

    def test_import_creates_new_product(self):
        response = self.import_csv("NUEVO,7502,Café,Abarrotes,,12,24,8,3\n")

        product = Product.query.filter_by(user_id=self.user.id, sku="NUEVO").one()
        self.assertEqual(product.stock, 8)
        self.assertIn("1 creados, 0 actualizados, 0 omitidos y 0 errores", response.get_data(as_text=True))

    def test_sku_match_adds_stock_and_updates_values(self):
        product = self.add_product(stock=5)
        self.import_csv("EXISTE,9999,Actualizado,General,,15,30,3,4\n")

        db.session.refresh(product)
        self.assertEqual(product.stock, 8)
        self.assertEqual(product.cost_price, 15)
        self.assertEqual(product.sale_price, 30)
        self.assertEqual(product.min_stock, 4)
        self.assertEqual(product.barcode, "9999")

    def test_barcode_match_replaces_stock_and_prices(self):
        product = self.add_product(sku="ORIGINAL", barcode="7503", stock=9)
        self.import_csv("DISTINTO,7503,Otro nombre,General,,7,18,2,1\n")

        db.session.refresh(product)
        self.assertEqual(product.sku, "ORIGINAL")
        self.assertEqual(product.stock, 2)
        self.assertEqual(product.cost_price, 7)
        self.assertEqual(product.sale_price, 18)
        self.assertEqual(product.min_stock, 1)

    def test_invalid_row_is_counted_without_internal_error_details(self):
        response = self.import_csv("MALO,7504,Producto,General,,no-es-numero,20,2,1\n")
        html = response.get_data(as_text=True)

        self.assertIn("0 creados, 0 actualizados, 0 omitidos y 1 errores", html)
        self.assertNotIn("could not convert", html)
        self.assertEqual(Product.query.filter_by(user_id=self.user.id).count(), 0)

    def test_missing_essential_columns_rejects_file(self):
        response = self.client.post(
            "/import-products",
            data={"catalog_file": (io.BytesIO(b"SKU,Nombre del producto\nA,Producto\n"), "bad.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)

        self.assertIn("no contiene las columnas obligatorias", html)
        self.assertEqual(Product.query.filter_by(user_id=self.user.id).count(), 0)

    def test_import_summary_counts_every_result(self):
        self.add_product(sku="POR-SKU", barcode="111", stock=4)
        self.add_product(sku="POR-CODIGO", barcode="222", stock=7)
        rows = (
            "NUEVO,333,Nuevo,General,,4,8,2,1\n"
            "POR-SKU,444,SKU,General,,5,10,3,2\n"
            "OTRO,222,Codigo,General,,6,12,1,2\n"
            ",,,,,,,,\n"
            "ERROR,555,Error,General,,texto,10,1,1\n"
        )

        response = self.import_csv(rows)

        self.assertIn(
            "1 creados, 2 actualizados, 1 omitidos y 1 errores",
            response.get_data(as_text=True),
        )

    def test_import_form_blocks_double_submission(self):
        html = self.inventory_html()

        self.assertIn("let importSubmitting = false", html)
        self.assertIn("if (importSubmitting)", html)
        self.assertIn("importButton.disabled = true", html)
        self.assertIn("Importando catálogo…", html)


if __name__ == "__main__":
    unittest.main()
