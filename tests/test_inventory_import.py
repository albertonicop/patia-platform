import io
import os
import re
import time
import unittest


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "inventory-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_patia")
os.environ.setdefault("STRIPE_PRICE_ID", "price_patia_pro")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_patia")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, User
from app.team.services import ensure_owner_organization


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
        db.session.flush()
        ensure_owner_organization(self.user)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user.id

    def add_product(self, sku="EXISTE", barcode="7501", stock=5):
        product = Product(
            organization_id=self.user.organization_memberships[0].organization_id,
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
        self.assertIn('data-label="Producto"', html)
        self.assertIn('data-label="Acciones"', html)

    def test_search_without_results_keeps_real_catalog_context(self):
        self.add_product()

        html = self.client.get("/products?q=no-existe").get_data(as_text=True)

        self.assertIn("No encontramos productos", html)
        self.assertIn("Limpiar filtros", html)
        self.assertIn("0 de 1 productos", html)
        self.assertNotIn("Tu catálogo empieza aquí", html)

    def test_large_catalog_is_paginated_without_losing_filters(self):
        organization_id = self.user.organization_memberships[0].organization_id
        db.session.add_all(
            Product(
                organization_id=organization_id,
                user_id=self.user.id,
                sku=f"PAG-{index:03d}",
                barcode=f"7509999{index:05d}",
                name=f"Producto paginado {index:03d}",
                category="Ferretería",
                cost_price=10,
                sale_price=20,
                stock=10,
                min_stock=2,
            )
            for index in range(205)
        )
        db.session.commit()

        first_page = self.client.get("/products").get_data(as_text=True)
        second_page = self.client.get("/products?page=2").get_data(as_text=True)
        last_page = self.client.get("/products?page=999").get_data(as_text=True)
        filtered_page = self.client.get(
            "/products?q=Producto&page=2"
        ).get_data(as_text=True)

        self.assertEqual(first_page.count("inventory-v3__row-actions"), 100)
        self.assertIn("Producto paginado 000", first_page)
        self.assertNotIn("Producto paginado 150", first_page)
        self.assertIn("Página 1 de 3", first_page)
        self.assertIn("Mostrando 1–100 de 205", first_page)
        self.assertIn("Producto paginado 150", second_page)
        self.assertIn("Página 2 de 3", second_page)
        self.assertIn("q=Producto", filtered_page)
        self.assertIn("Producto paginado 204", last_page)
        self.assertIn("Página 3 de 3", last_page)

    def test_required_sku_is_visible_before_advanced_options(self):
        html = self.inventory_html()

        sku_position = html.index('name="sku"')
        advanced_position = html.index('class="inventory-v2__advanced"')
        self.assertLess(sku_position, advanced_position)

    def test_duplicate_manual_sku_is_rejected_without_server_error(self):
        self.add_product(sku="DUPLICADO")

        response = self.client.post(
            "/products/new",
            data={
                "sku": "DUPLICADO",
                "name": "Segundo producto",
                "cost_price": "5",
                "sale_price": "10",
                "stock": "2",
                "min_stock": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Ya existe un producto con ese SKU", response.get_data(as_text=True))
        self.assertEqual(Product.query.filter_by(user_id=self.user.id).count(), 1)

    def test_duplicate_manual_barcode_is_rejected_without_server_error(self):
        self.add_product(barcode="750123")
        response = self.client.post(
            "/products/new",
            data={
                "sku": "SKU-LIBRE", "barcode": "750123", "name": "Segundo producto",
                "cost_price": "5", "sale_price": "10", "stock": "2", "min_stock": "1",
            },
            follow_redirects=True,
        )
        self.assertIn("Ya existe un producto con ese código de barras", response.get_data(as_text=True))
        self.assertEqual(Product.query.filter_by(user_id=self.user.id).count(), 1)

    def test_product_edit_preserves_historical_sale_values(self):
        product = self.add_product()
        sale = Sale(organization_id=self.user.organization_memberships[0].organization_id, user_id=self.user.id, product_id=product.id, quantity=2, unit_price=20, total=40)
        db.session.add(sale)
        db.session.commit()

        response = self.client.post(
            f"/products/{product.id}/edit",
            data={
                "name": "Producto editado", "sku": "EDITADO", "barcode": "8800",
                "category": "Nueva", "supplier": "Proveedor", "cost_price": "12",
                "sale_price": "35", "stock": "9", "min_stock": "3",
            },
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        db.session.refresh(product)
        db.session.refresh(sale)
        self.assertEqual((product.name, product.sale_price, product.stock), ("Producto editado", 35, 9))
        self.assertEqual((sale.unit_price, sale.total), (20, 40))

    def test_product_edit_rejects_other_company_product(self):
        product = self.add_product()
        other = User(email="other@patia.test", company_name="Otra", email_verified=True)
        other.set_password("Password123")
        db.session.add(other)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["user_id"] = other.id

        response = self.client.get(f"/products/{product.id}/edit")

        self.assertEqual(response.status_code, 404)

    def test_product_edit_rejects_duplicate_sku_and_barcode(self):
        product = self.add_product(sku="ORIGINAL", barcode="100")
        self.add_product(sku="OCUPADO", barcode="200")
        payload = {
            "name": "Producto", "sku": "OCUPADO", "barcode": "100",
            "cost_price": "1", "sale_price": "2", "stock": "1", "min_stock": "1",
        }
        self.assertEqual(self.client.post(f"/products/{product.id}/edit", data=payload).status_code, 409)
        payload["sku"] = "LIBRE"
        payload["barcode"] = "200"
        self.assertEqual(self.client.post(f"/products/{product.id}/edit", data=payload).status_code, 409)

    def test_product_edit_validates_numbers_without_server_error(self):
        product = self.add_product()
        response = self.client.post(
            f"/products/{product.id}/edit",
            data={
                "name": "Producto", "sku": "VALIDO", "cost_price": "-1",
                "sale_price": "2", "stock": "1", "min_stock": "1",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_product_with_sales_is_archived_and_history_is_preserved(self):
        product = self.add_product()
        sale = Sale(
            organization_id=self.user.organization_memberships[0].organization_id,
            user_id=self.user.id,
            product_id=product.id,
            quantity=1,
            unit_price=20,
            total=20,
        )
        db.session.add(sale)
        db.session.commit()

        response = self.client.post(
            f"/products/{product.id}/delete", follow_redirects=True
        )

        html = response.get_data(as_text=True)
        self.assertIn("retirado del catálogo", html)
        self.assertNotIn("Producto existente", html)
        self.assertIsNotNone(db.session.get(Product, product.id))
        self.assertFalse(db.session.get(Product, product.id).is_active)
        self.assertIsNotNone(db.session.get(Sale, sale.id))

    def test_unsold_product_is_physically_deleted_and_empty_state_updates(self):
        product = self.add_product()
        response = self.client.post(
            f"/products/{product.id}/delete",
            follow_redirects=True,
        )
        html = response.get_data(as_text=True)
        self.assertIsNone(db.session.get(Product, product.id))
        self.assertIn("Agrega tu primer producto", html)
        self.assertNotIn("Buscar producto…", html)

    def test_archived_product_disappears_from_search_pos_and_dashboard_metrics(self):
        product = self.add_product(stock=5)
        db.session.add(Sale(
            organization_id=self.user.organization_memberships[0].organization_id,
            user_id=self.user.id,
            product_id=product.id,
            quantity=1,
            unit_price=20,
            total=20,
        ))
        db.session.commit()
        self.client.post(f"/products/{product.id}/delete")

        self.assertNotIn("Producto existente", self.client.get("/products?q=Producto").get_data(as_text=True))
        pos_html = self.client.get("/sell").get_data(as_text=True)
        self.assertIn("Agrega inventario antes de vender", pos_html)
        self.assertIn("Producto existente", pos_html)  # El historial de ventas permanece legible.
        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn("Agrega tu primer producto", dashboard)
        self.assertEqual(Sale.query.filter_by(user_id=self.user.id).count(), 1)

    def test_adding_archived_sku_reactivates_record_without_losing_sales(self):
        product = self.add_product(sku="ARCHIVO", barcode="700", stock=5)
        sale = Sale(organization_id=self.user.organization_memberships[0].organization_id, user_id=self.user.id, product_id=product.id, quantity=1, unit_price=20, total=20)
        db.session.add(sale)
        db.session.commit()
        self.client.post(f"/products/{product.id}/delete")

        response = self.client.post(
            "/products/new",
            data={
                "sku": "ARCHIVO", "barcode": "700", "name": "Producto reactivado",
                "cost_price": "8", "sale_price": "16", "stock": "4", "min_stock": "1",
            },
            follow_redirects=True,
        )

        db.session.refresh(product)
        self.assertTrue(product.is_active)
        self.assertEqual(product.name, "Producto reactivado")
        self.assertIn("Producto reactivado", response.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(Sale, sale.id))

    def test_delete_all_preserves_products_with_sales(self):
        protected = self.add_product(sku="CON-VENTA")
        removable = self.add_product(sku="SIN-VENTA", barcode="7502")
        db.session.add(Sale(
            organization_id=self.user.organization_memberships[0].organization_id,
            user_id=self.user.id,
            product_id=protected.id,
            quantity=1,
            unit_price=20,
            total=20,
        ))
        db.session.commit()

        response = self.client.post("/products/delete-all", follow_redirects=True)

        self.assertIn("retiramos 1", response.get_data(as_text=True))
        self.assertIsNotNone(db.session.get(Product, protected.id))
        self.assertFalse(db.session.get(Product, protected.id).is_active)
        self.assertIsNone(db.session.get(Product, removable.id))
        self.assertEqual(Sale.query.filter_by(user_id=self.user.id).count(), 1)

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

    def test_fractional_or_non_finite_inventory_values_are_rejected(self):
        response = self.import_csv(
            "FRACCION,7505,Producto,General,,10,20,1.5,1\n"
            "INFINITO,7506,Producto,General,,inf,20,2,1\n"
        )

        self.assertIn("0 creados, 0 actualizados, 0 omitidos y 2 errores", response.get_data(as_text=True))
        self.assertEqual(Product.query.filter_by(user_id=self.user.id).count(), 0)

    def test_csv_error_log_reports_the_actual_file_row(self):
        with self.assertLogs("app", level="WARNING") as captured:
            self.import_csv(
                "VALIDO,7507,Producto,General,,10,20,2,1\n"
                "MALO,7508,Producto,General,,texto,20,2,1\n"
            )

        self.assertTrue(any("fila 3" in message for message in captured.output))

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

    def test_imports_one_thousand_products_in_one_safe_batch(self):
        rows = "".join(
            f"FER-{index:04d},{7500000000000 + index},Producto {index},"
            f"Ferretería,Proveedor Uno,1.25,2.50,10,2\n"
            for index in range(1000)
        )

        started_at = time.perf_counter()
        response = self.import_csv(rows)
        elapsed = time.perf_counter() - started_at

        self.assertEqual(response.status_code, 200)
        self.assertIn("1000 creados", response.get_data(as_text=True))
        self.assertEqual(
            Product.query.filter_by(
                organization_id=self.user.organization_memberships[0].organization_id
            ).count(),
            1000,
        )
        self.assertLess(elapsed, 30)

    def test_import_form_blocks_double_submission(self):
        html = self.inventory_html()

        self.assertIn("let importSubmitting = false", html)
        self.assertIn("if (importSubmitting)", html)
        self.assertIn("importButton.disabled = true", html)
        self.assertIn("Validando e importando el catálogo…", html)


if __name__ == "__main__":
    unittest.main()
