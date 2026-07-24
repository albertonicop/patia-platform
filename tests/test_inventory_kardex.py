import os
from decimal import Decimal
import tempfile
import unittest
import uuid
from pathlib import Path


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "kardex-tests")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_kardex")
os.environ.setdefault("STRIPE_PRICE_ID", "price_kardex")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_kardex")
os.environ.setdefault("PUBLIC_BASE_URL", "http://127.0.0.1:5000")
from app import create_app, db
from app.inventory.services import (
    change_product_stock,
    record_opening_balance,
    stock_consistency,
)
from app.models import (
    InventoryMovement,
    OrganizationMember,
    Product,
    Sale,
    User,
)
from app.team.services import ensure_owner_organization


class InventoryKardexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-kardex-")
        database_path = Path(self.temp_dir.name, "kardex.db")
        self.original_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.membership = self.add_owner("owner@kardex.test")
        self.product = Product(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            sku="KDX-1",
            name="Producto Kardex",
            category="General",
            cost_price=Decimal("5.00"),
            sale_price=Decimal("10.00"),
            stock=20,
            min_stock=3,
        )
        db.session.add(self.product)
        record_opening_balance(
            self.product, self.membership, reason="Saldo de prueba"
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.original_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_database_url

    def add_owner(self, email):
        user = User(
            email=email,
            company_name=email.split("@")[0],
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        db.session.commit()
        return user, membership

    def add_member(self, role, email):
        user = User(
            email=email,
            company_name="Empleado",
            email_verified=True,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = OrganizationMember(
            organization_id=self.membership.organization_id,
            user_id=user.id,
            role=role,
            is_active=True,
        )
        db.session.add(membership)
        db.session.commit()
        return user, membership

    def client_for(self, user, organization_id=None):
        client = self.app.test_client()
        with client.session_transaction() as flask_session:
            flask_session["user_id"] = user.id
            flask_session["organization_id"] = (
                organization_id or self.membership.organization_id
            )
        return client

    def sell(self, client, quantity=2):
        return client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [
                    {"product_id": self.product.id, "quantity": quantity}
                ],
            },
        )

    def test_sale_and_cancellation_are_immutable_linked_movements(self):
        client = self.client_for(self.owner)
        sold = self.sell(client, 2)
        self.assertEqual(sold.status_code, 200)
        sale = Sale.query.one()
        sale_movement = InventoryMovement.query.filter_by(
            movement_type="SALE"
        ).one()
        self.assertEqual(
            (sale_movement.stock_before, sale_movement.stock_after),
            (20, 18),
        )
        self.assertEqual(sale_movement.quantity_delta, -2)
        self.assertEqual(sale_movement.sale_id, sale.id)
        self.assertEqual(
            sale_movement.sales_ticket_id, sale.sales_ticket_id
        )

        self.assertEqual(
            client.post(f"/sales/{sale.id}/cancel").status_code, 302
        )
        cancellation = InventoryMovement.query.filter_by(
            movement_type="SALE_CANCELLATION"
        ).one()
        self.assertEqual(
            (cancellation.stock_before, cancellation.stock_after), (18, 20)
        )
        self.assertEqual(cancellation.quantity_delta, 2)
        self.assertEqual(cancellation.sales_ticket_id, sale_movement.sales_ticket_id)
        self.assertIsNone(sale_movement.sale_id)

        cancellation.reason = "Intento de edición"
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()
        db.session.delete(cancellation)
        with self.assertRaises(ValueError):
            db.session.commit()
        db.session.rollback()

    def test_restock_adjustments_waste_damage_internal_use_and_count(self):
        client = self.client_for(self.owner)
        restock = client.post(
            f"/products/{self.product.id}/restock",
            data={"quantity": "5"},
        )
        self.assertEqual(restock.status_code, 302)
        movement = InventoryMovement.query.filter_by(
            movement_type="RESTOCK"
        ).one()
        self.assertEqual(movement.quantity_delta, 5)
        self.assertIsNotNone(movement.restock_event_id)

        cases = (
            ("ADJUSTMENT_IN", 2, 27),
            ("ADJUSTMENT_OUT", 1, 26),
            ("WASTE", 2, 24),
            ("DAMAGE", 1, 23),
            ("INTERNAL_USE", 3, 20),
            ("PHYSICAL_COUNT", 18, 18),
        )
        for movement_type, quantity, expected in cases:
            response = client.post(
                f"/inventory/products/{self.product.id}/adjust",
                data={
                    "movement_type": movement_type,
                    "quantity": str(quantity),
                    "reason": f"Prueba {movement_type}",
                },
            )
            self.assertEqual(response.status_code, 302)
            db.session.refresh(self.product)
            self.assertEqual(self.product.stock, expected)
        self.assertEqual(
            {
                item.movement_type
                for item in InventoryMovement.query.filter(
                    InventoryMovement.movement_type.in_(
                        {case[0] for case in cases}
                    )
                )
            },
            {case[0] for case in cases},
        )

    def test_invalid_adjustment_does_not_make_stock_negative(self):
        client = self.client_for(self.owner)
        response = client.post(
            f"/inventory/products/{self.product.id}/adjust",
            data={
                "movement_type": "WASTE",
                "quantity": "21",
                "reason": "Demasiado",
            },
        )
        self.assertEqual(response.status_code, 302)
        db.session.refresh(self.product)
        self.assertEqual(self.product.stock, 20)
        self.assertEqual(
            InventoryMovement.query.filter_by(movement_type="WASTE").count(),
            0,
        )

    def test_manual_creation_edit_and_delete_keep_auditable_snapshots(self):
        client = self.client_for(self.owner)
        created = client.post(
            "/products/new",
            data={
                "name": "Producto temporal",
                "sku": "TMP-1",
                "cost_price": "2.00",
                "sale_price": "4.00",
                "stock": "7",
                "min_stock": "1",
            },
        )
        self.assertEqual(created.status_code, 302)
        product = Product.query.filter_by(sku="TMP-1").one()
        opening = InventoryMovement.query.filter_by(
            product_id=product.id,
            movement_type="OPENING_BALANCE",
        ).one()
        self.assertEqual((opening.stock_before, opening.stock_after), (0, 7))

        edited = client.post(
            f"/products/{product.id}/edit",
            data={
                "name": product.name,
                "sku": product.sku,
                "cost_price": "2.00",
                "sale_price": "4.00",
                "stock": "5",
                "min_stock": "1",
            },
        )
        self.assertEqual(edited.status_code, 302)
        count = InventoryMovement.query.filter_by(
            product_id=product.id,
            movement_type="PHYSICAL_COUNT",
        ).one()
        self.assertEqual((count.stock_before, count.stock_after), (7, 5))

        self.assertEqual(
            client.post(f"/products/{product.id}/delete").status_code, 302
        )
        db.session.expire_all()
        self.assertIsNone(db.session.get(Product, product.id))
        snapshots = InventoryMovement.query.filter_by(
            product_sku="TMP-1"
        ).order_by(InventoryMovement.id).all()
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(all(item.product_id is None for item in snapshots))

    def test_customer_return_restores_stock_and_keeps_ticket_reference(self):
        client = self.client_for(self.owner)
        response = self.sell(client, 3)
        self.assertEqual(response.status_code, 200)
        sale = Sale.query.one()
        ticket_id = sale.sales_ticket_id
        returned = client.post(f"/sales/{sale.id}/return")
        self.assertEqual(returned.status_code, 302)
        db.session.refresh(self.product)
        self.assertEqual(self.product.stock, 20)
        movement = InventoryMovement.query.filter_by(
            movement_type="RETURN"
        ).one()
        self.assertEqual(movement.quantity_delta, 3)
        self.assertEqual(movement.sales_ticket_id, ticket_id)

    def test_history_filters_csv_consistency_and_languages(self):
        client = self.client_for(self.owner)
        self.sell(client, 1)
        page = client.get(
            f"/inventory/kardex?product_id={self.product.id}&type=SALE"
        )
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("Producto Kardex", html)
        self.assertIn("Venta", html)
        self.assertIn("Todo está en orden", html)
        self.assertNotIn(">Kardex<", html)
        self.assertIn("Aquí puedes ver por qué aumentaron", html)

        csv_response = client.get(
            f"/inventory/kardex/export.csv?product_id={self.product.id}&type=SALE"
        )
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("text/csv", csv_response.content_type)
        self.assertIn("Producto Kardex", csv_response.get_data(as_text=True))

        consistency = stock_consistency(self.membership.organization_id)
        self.assertTrue(all(item.is_consistent for item in consistency))
        self.product.stock += 1
        db.session.commit()
        self.assertFalse(
            stock_consistency(self.membership.organization_id)[0].is_consistent
        )

    def test_english_kardex_copy(self):
        client = self.client_for(self.owner)
        with client.session_transaction() as flask_session:
            flask_session["language"] = "en"
        english = client.get("/inventory/kardex").get_data(as_text=True)
        self.assertIn("Inventory history", english)
        self.assertIn("Download CSV", english)

    def test_role_permissions_and_organization_isolation(self):
        cashier, _ = self.add_member("CASHIER", "cashier@kardex.test")
        cashier_client = self.client_for(cashier)
        self.assertEqual(
            cashier_client.get("/inventory/kardex").status_code, 403
        )
        self.assertEqual(
            cashier_client.post(
                f"/inventory/products/{self.product.id}/adjust",
                data={
                    "movement_type": "ADJUSTMENT_IN",
                    "quantity": "1",
                    "reason": "No permitido",
                },
            ).status_code,
            403,
        )

        other_owner, other_membership = self.add_owner("other@kardex.test")
        other_client = self.client_for(
            other_owner, other_membership.organization_id
        )
        other_page = other_client.get("/inventory/kardex")
        self.assertEqual(other_page.status_code, 200)
        self.assertNotIn(
            "Producto Kardex", other_page.get_data(as_text=True)
        )
        self.assertEqual(
            other_client.post(
                f"/inventory/products/{self.product.id}/adjust",
                data={
                    "movement_type": "ADJUSTMENT_IN",
                    "quantity": "1",
                    "reason": "Cruce",
                },
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
