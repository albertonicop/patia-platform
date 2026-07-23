import os
import unittest
import uuid


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "ticket-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_ticket")
os.environ.setdefault("STRIPE_PRICE_ID", "price_ticket")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_ticket")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.models import Product, Sale, User
from app.team.services import ensure_owner_organization


class ProfessionalTicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False)
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
        User.query.delete()
        db.session.commit()
        self.client = self.app.test_client()
        self.owner = self.make_user("owner@tickets.test", company_name="Abarrotes Luna")
        self.login(self.owner)

    def tearDown(self):
        db.session.remove()

    def make_user(self, email, **values):
        user = User(email=email, email_verified=True, **values)
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        ensure_owner_organization(user)
        db.session.commit()
        return user

    def login(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = user.id

    def product(self, owner, sku, name, price, stock=10):
        product = Product(
            organization_id=owner.organization_memberships[0].organization_id,
            user_id=owner.id, sku=sku, name=name, category="General",
            cost_price=price / 2, sale_price=price, stock=stock, min_stock=1,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def grouped_sale(self, owner=None):
        owner = owner or self.owner
        first = self.product(owner, f"A-{owner.id}", "Café de olla", 25)
        second = self.product(owner, f"B-{owner.id}", "Pan artesanal largo", 15)
        ticket_id = str(uuid.uuid4())
        lines = [
            Sale(organization_id=owner.organization_memberships[0].organization_id, user_id=owner.id, product_id=first.id, quantity=2, unit_price=25, total=50, ticket_id=ticket_id, payment_method="card"),
            Sale(organization_id=owner.organization_memberships[0].organization_id, user_id=owner.id, product_id=second.id, quantity=3, unit_price=15, total=45, ticket_id=ticket_id, payment_method="card"),
        ]
        db.session.add_all(lines)
        db.session.commit()
        return ticket_id, lines

    def expected_folio(self, ticket_id):
        return f"V-{uuid.UUID(ticket_id).int % 1_000_000:06d}"

    def test_owner_can_open_grouped_ticket_with_real_total_and_short_folio(self):
        ticket_id, lines = self.grouped_sale()
        response = self.client.get(f"/ticket/{ticket_id}")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Café de olla", html)
        self.assertIn("Pan artesanal largo", html)
        self.assertIn("$95.00 MXN", html)
        self.assertIn(self.expected_folio(ticket_id), html)
        self.assertNotIn(ticket_id, html)

    def test_cross_company_ticket_and_unknown_ticket_are_not_found(self):
        ticket_id, _ = self.grouped_sale()
        outsider = self.make_user("outsider@tickets.test", company_name="Otra empresa")
        self.login(outsider)

        self.assertEqual(self.client.get(f"/ticket/{ticket_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/ticket/{uuid.uuid4()}").status_code, 404)

    def test_configured_business_data_appears_and_empty_data_does_not(self):
        self.owner.address = "Av. Reforma 10"
        self.owner.city = "Puebla"
        self.owner.phone = "2221234567"
        self.owner.rfc = "XAXX010101000"
        db.session.commit()
        ticket_id, _ = self.grouped_sale()
        html = self.client.get(f"/ticket/{ticket_id}").get_data(as_text=True)

        self.assertIn("Abarrotes Luna", html)
        self.assertIn("Av. Reforma 10, Puebla", html)
        self.assertIn("2221234567", html)
        self.assertIn("XAXX010101000", html)
        self.assertNotIn("Código postal:", html)

    def test_placeholder_business_data_is_hidden_and_folio_is_not_repeated_in_footer(self):
        self.owner.address = "Dirección no configurada"
        self.owner.city = "000"
        self.owner.state = "No configurado"
        self.owner.postal_code = "00000"
        self.owner.phone = "000-000-0000"
        db.session.commit()
        ticket_id, _ = self.grouped_sale()

        html = self.client.get(f"/ticket/{ticket_id}").get_data(as_text=True)

        self.assertNotIn("Dirección no configurada", html)
        self.assertNotIn("000-000-0000", html)
        self.assertNotIn("Ticket:", html)
        self.assertIn("Gracias por su compra", html)
        self.assertIn("Powered by PATIA", html)

    def test_ticket_has_print_pdf_actions_and_thermal_styles(self):
        ticket_id, _ = self.grouped_sale()
        html = self.client.get(f"/ticket/{ticket_id}").get_data(as_text=True)
        css_response = self.client.get("/static/css/styles.css")
        css = css_response.get_data(as_text=True)
        css_response.close()

        self.assertIn("Imprimir ticket", html)
        self.assertIn("Guardar o descargar como PDF", html)
        self.assertIn("window.print()", html)
        self.assertIn("58 mm", html)
        self.assertIn("80 mm", html)
        self.assertIn("@media print", css)
        self.assertIn("@page ticket58", css)
        self.assertIn("@page ticket80", css)
        self.assertIn(".sidebar-v2", css)
        self.assertIn(".no-print-v2", css)

    def test_recent_sales_group_operation_once_and_offer_reprint(self):
        ticket_id, lines = self.grouped_sale()
        html = self.client.get("/sell").get_data(as_text=True)

        self.assertEqual(html.count(self.expected_folio(ticket_id)), 1)
        self.assertIn("5 artículos", html)
        self.assertIn("$95.00 MXN", html)
        self.assertIn(f"/ticket/{ticket_id}", html)
        self.assertIn("Reimprimir", html)

    def test_confirmation_modal_is_accessible_and_uses_group_ticket(self):
        first = self.product(self.owner, "MODAL-1", "Producto modal", 20)
        second = self.product(self.owner, "MODAL-2", "Otro producto", 30)
        response = self.client.post("/sell-cart", json={
            "request_id": str(uuid.uuid4()),
            "items": [{"product_id": first.id, "quantity": 1}, {"product_id": second.id, "quantity": 1}],
        })
        data = response.get_json()
        html = self.client.get("/sell").get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertRegex(data["folio"], r"^TKT-\d{6}$")
        self.assertTrue(data["ticket_url"].startswith("/ticket/"))
        self.assertIn('role="dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn("Ver ticket", html)
        self.assertIn("Imprimir ticket", html)
        self.assertIn("Nueva venta", html)
        self.assertIn('event.key === "Escape"', html)

    def test_payment_method_is_shown_and_no_tax_is_invented(self):
        ticket_id, _ = self.grouped_sale()
        html = self.client.get(f"/ticket/{ticket_id}").get_data(as_text=True)

        self.assertIn("Método de pago", html)
        self.assertIn("Tarjeta", html)
        self.assertNotIn("IVA (16%)", html)
        self.assertNotIn("Descuento", html)

    def test_historical_sale_without_payment_method_remains_compatible(self):
        product = self.product(self.owner, "HIST", "Histórico", 10)
        sale = Sale(organization_id=self.owner.organization_memberships[0].organization_id, user_id=self.owner.id, product_id=product.id, quantity=1, unit_price=10, total=10)
        db.session.add(sale)
        db.session.commit()
        html = self.client.get(f"/receipt/{sale.id}", follow_redirects=True).get_data(as_text=True)
        self.assertIn("No especificado", html)

    def test_line_cancellation_still_restores_only_selected_product(self):
        ticket_id, lines = self.grouped_sale()
        first_product = lines[0].product
        second_product = lines[1].product
        first_stock = first_product.stock
        second_stock = second_product.stock

        response = self.client.post(f"/sales/{lines[0].id}/cancel")

        self.assertEqual(response.status_code, 302)
        db.session.refresh(first_product)
        db.session.refresh(second_product)
        self.assertEqual(first_product.stock, first_stock + lines[0].quantity)
        self.assertEqual(second_product.stock, second_stock)
        self.assertIsNotNone(db.session.get(Sale, lines[1].id))
        remaining_html = self.client.get(f"/ticket/{ticket_id}").get_data(as_text=True)
        self.assertIn(self.expected_folio(ticket_id), remaining_html)


if __name__ == "__main__":
    unittest.main()
