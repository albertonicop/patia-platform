import io
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "release-blocker-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_release")
os.environ.setdefault("STRIPE_PRICE_ID", "price_release")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_release")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, Supplier, User


class ReleaseBlockerTests(unittest.TestCase):
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
        db.session.remove()
        Sale.query.delete()
        Product.query.delete()
        Supplier.query.delete()
        User.query.delete()
        db.session.commit()
        db.session.remove()
        self.client = self.app.test_client()

    def user(self, email="owner@release.test", expired=False):
        user = User(
            email=email,
            company_name="Negocio RC",
            email_verified=True,
            created_at=datetime.utcnow() - timedelta(days=15 if expired else 1),
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.commit()
        return user

    def login(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = user.id

    def sale(self, user):
        product = Product(
            user_id=user.id,
            sku="RC-1",
            name="Producto RC",
            cost_price=10,
            sale_price=20,
            stock=4,
            min_stock=1,
        )
        db.session.add(product)
        db.session.flush()
        sale = Sale(
            user_id=user.id,
            product_id=product.id,
            quantity=1,
            unit_price=20,
            total=20,
        )
        db.session.add(sale)
        db.session.commit()
        return sale

    def test_legacy_receipt_redirects_to_professional_ticket(self):
        owner = self.user()
        sale = self.sale(owner)
        self.login(owner)

        response = self.client.get(f"/receipt/{sale.id}")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith(f"/ticket/sale-{sale.id}"))

    def test_cross_company_receipt_is_not_found(self):
        owner = self.user()
        sale = self.sale(owner)
        outsider = self.user("other@release.test")
        self.login(outsider)

        self.assertEqual(self.client.get(f"/receipt/{sale.id}").status_code, 404)

    def test_missing_receipt_is_not_found(self):
        self.login(self.user())
        self.assertEqual(self.client.get("/receipt/999999").status_code, 404)

    def test_expired_trial_blocks_sensitive_post_endpoints(self):
        user = self.user(expired=True)
        self.login(user)

        product_response = self.client.post(
            "/products/new",
            data={
                "sku": "BLOCKED",
                "name": "No crear",
                "cost_price": "1",
                "sale_price": "2",
                "stock": "1",
                "min_stock": "0",
            },
        )
        import_response = self.client.post(
            "/import-products",
            data={"catalog_file": (io.BytesIO(b"not used"), "catalog.csv")},
            content_type="multipart/form-data",
        )
        cart_response = self.client.post(
            "/sell-cart", json={"items": [{"product_id": 1, "quantity": 1}]}
        )

        self.assertEqual(product_response.status_code, 403)
        self.assertEqual(import_response.status_code, 403)
        self.assertEqual(cart_response.status_code, 403)
        self.assertFalse(cart_response.json["ok"])
        self.assertEqual(Product.query.filter_by(user_id=user.id).count(), 0)

    def test_negative_product_values_are_rejected_server_side(self):
        user = self.user()
        self.login(user)
        response = self.client.post(
            "/products/new",
            data={
                "sku": "NEG",
                "name": "Inválido",
                "cost_price": "-1",
                "sale_price": "2",
                "stock": "1",
                "min_stock": "0",
            },
            follow_redirects=True,
        )
        self.assertIn("no pueden ser negativos", response.get_data(as_text=True))
        self.assertEqual(Product.query.count(), 0)

    def test_reset_password_rejects_short_password(self):
        user = self.user()
        user.reset_token = "valid-token"
        user.reset_token_expires = datetime.utcnow() + timedelta(minutes=10)
        original_password = user.password
        db.session.commit()

        response = self.client.post(
            "/reset-password/valid-token", data={"password": "short"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("al menos 8 caracteres", response.get_data(as_text=True))
        self.assertEqual(user.password, original_password)

    def test_resend_failure_is_visible_and_can_be_retried(self):
        user = self.user()
        user.email_verified = False
        db.session.commit()
        self.login(user)

        with patch("app.routes.send_email", side_effect=[False, True]):
            failed = self.client.post("/resend-verification", follow_redirects=True)
            retried = self.client.post("/resend-verification", follow_redirects=True)

        self.assertIn("No pudimos enviar", failed.get_data(as_text=True))
        self.assertIn("nuevo código", retried.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
