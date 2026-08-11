import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from decimal import Decimal

os.environ.setdefault("SECRET_KEY", "currency-test-secret")

from app import create_app, db
from app.currencies import (
    format_currency,
    parse_localized_decimal,
)
from app.inventory.imports import inspect_catalog
from app.models import OrganizationMember, Product, Sale, SalesTicket, User
from app.routes import analytics
from app.team.services import ensure_owner_organization


class OrganizationCurrencyTests(unittest.TestCase):
    def setUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.previous_url = os.environ.get("DATABASE_URL")
        self.previous_stripe_disabled = os.environ.get("STRIPE_DISABLED")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.database_path}"
        os.environ["STRIPE_DISABLED"] = "true"
        self.app = create_app()
        self.app.config.update(
            TESTING=True, WTF_CSRF_ENABLED=False, RATELIMIT_ENABLED=False,
            STRIPE_DISABLED=True,
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            owner = User(
                email="currency-owner@patia.test",
                company_name="Negocio global",
                email_verified=True,
                manual_pro_access=True,
            )
            owner.set_password("Password123")
            db.session.add(owner)
            db.session.flush()
            membership = ensure_owner_organization(owner)
            product = Product(
                organization_id=membership.organization_id,
                user_id=owner.id,
                sku="CUR-1",
                name="Producto moneda",
                category="General",
                cost_price=Decimal("500.00"),
                sale_price=Decimal("1250.50"),
                stock=10,
                min_stock=1,
            )
            db.session.add(product)
            db.session.commit()
            self.owner_id = owner.id
            self.organization_id = membership.organization_id
            self.product_id = product.id
        with self.client.session_transaction() as session:
            session["user_id"] = self.owner_id
            session["organization_id"] = self.organization_id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()
        os.remove(self.database_path)
        if self.previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.previous_url
        if self.previous_stripe_disabled is None:
            os.environ.pop("STRIPE_DISABLED", None)
        else:
            os.environ["STRIPE_DISABLED"] = self.previous_stripe_disabled

    def test_all_supported_currency_formats_are_exact(self):
        cases = (
            ("1250.50", "MXN", "es_MX", "$1,250.50 MXN"),
            ("1250.50", "USD", "en_US", "$1,250.50 USD"),
            ("1250.50", "EUR", "es_ES", "1.250,50 €"),
            ("1250500", "COP", "es_CO", "$1.250.500 COP"),
            ("1250500", "CLP", "es_CL", "$1.250.500 CLP"),
            ("1250.50", "PEN", "es_PE", "S/ 1,250.50"),
        )
        for value, currency, locale, expected in cases:
            with self.subTest(currency=currency):
                self.assertEqual(
                    format_currency(value, currency, locale), expected
                )

    def test_settings_persist_whitelisted_currency_without_converting_data(self):
        response = self.client.post(
            "/settings",
            data={
                "company_name": "Negocio global",
                "timezone": "America/Mexico_City",
                "country_code": "ES",
                "currency_code": "EUR",
                "locale_code": "es_ES",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            membership = db.session.get(OrganizationMember, 1)
            self.assertEqual(membership.organization.currency_code, "EUR")
            self.assertEqual(membership.organization.locale_code, "es_ES")
            self.assertEqual(
                db.session.get(Product, self.product_id).sale_price,
                Decimal("1250.50"),
            )
        html = self.client.get("/products").get_data(as_text=True)
        self.assertIn("1.250,50 €", html)

    def test_invalid_currency_is_rejected(self):
        response = self.client.post(
            "/settings",
            data={
                "company_name": "Negocio global",
                "timezone": "America/Mexico_City",
                "country_code": "MX",
                "currency_code": "BTC",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(
                db.session.get(OrganizationMember, 1).organization.currency_code,
                "MXN",
            )

    def test_ticket_reprint_keeps_frozen_currency_after_org_change(self):
        with self.app.app_context():
            ticket = SalesTicket(
                organization_id=self.organization_id,
                user_id=self.owner_id,
                number=1,
                public_id="frozen-currency",
                payment_method="card",
                currency_code="USD",
                locale_code="en_US",
            )
            db.session.add(ticket)
            db.session.flush()
            db.session.add(Sale(
                organization_id=self.organization_id,
                user_id=self.owner_id,
                product_id=self.product_id,
                quantity=1,
                unit_price=Decimal("1250.50"),
                total=Decimal("1250.50"),
                ticket_id=ticket.public_id,
                sales_ticket_id=ticket.id,
                payment_method="card",
                currency_code="USD",
                locale_code="en_US",
            ))
            membership = db.session.get(OrganizationMember, 1)
            membership.organization.currency_code = "EUR"
            membership.organization.locale_code = "es_ES"
            db.session.commit()
        for path in ("/ticket/frozen-currency", "/ticket/frozen-currency?print=1"):
            html = self.client.get(path).get_data(as_text=True)
            self.assertIn("$1,250.50 USD", html)
            self.assertNotIn("1.250,50 €", html)

    def test_new_pos_sale_freezes_the_organization_currency(self):
        with self.app.app_context():
            organization = db.session.get(OrganizationMember, 1).organization
            organization.country_code = "US"
            organization.currency_code = "USD"
            organization.locale_code = "en_US"
            organization.currency = "USD"
            db.session.commit()
        response = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [{"product_id": self.product_id, "quantity": 1}],
            },
        )
        self.assertEqual(response.status_code, 200)
        with self.app.app_context():
            ticket = SalesTicket.query.one()
            sale = Sale.query.one()
            self.assertEqual(
                (ticket.currency_code, ticket.locale_code), ("USD", "en_US")
            )
            self.assertEqual(
                (sale.currency_code, sale.locale_code), ("USD", "en_US")
            )

    def test_import_parses_locale_and_rejects_ambiguous_values(self):
        content = b"Nombre;Precio;Stock\nTaladro;1.250,50;2\n"
        imported = inspect_catalog(
            "catalogo.csv", content,
            currency_code="EUR", locale_code="es_ES",
        )
        self.assertEqual(imported.rows[0]["sale_price"], Decimal("1250.50"))
        self.assertEqual(
            parse_localized_decimal("1.250.500", "COP", "es_CO"),
            Decimal("1250500.00"),
        )
        for value, currency, locale in (
            ("1,50", "MXN", "es_MX"),
            ("1.50", "EUR", "es_ES"),
            ("1,2345", "MXN", "es_MX"),
        ):
            with self.subTest(value=value, currency=currency):
                with self.assertRaisesRegex(ValueError, "ambiguous_number"):
                    parse_localized_decimal(value, currency, locale)

    def test_two_organizations_render_independent_currencies(self):
        with self.app.app_context():
            second = User(
                email="second-currency@patia.test",
                company_name="Segundo negocio",
                email_verified=True,
            )
            second.set_password("Password123")
            db.session.add(second)
            db.session.flush()
            second_membership = ensure_owner_organization(second)
            second_membership.organization.currency_code = "PEN"
            second_membership.organization.locale_code = "es_PE"
            db.session.commit()
            self.assertEqual(
                format_currency("1250.50", second_membership.organization.currency_code,
                                second_membership.organization.locale_code),
                "S/ 1,250.50",
            )
            first = db.session.get(OrganizationMember, 1).organization
            self.assertEqual(format_currency("1250.50", first.currency_code, first.locale_code), "$1,250.50 MXN")

    def test_dashboard_does_not_sum_historical_different_currencies(self):
        with self.app.app_context():
            membership = db.session.get(OrganizationMember, 1)
            membership.organization.currency_code = "USD"
            membership.organization.locale_code = "en_US"
            membership.organization.currency = "USD"
            for currency, amount in (("MXN", "900.00"), ("USD", "100.00")):
                db.session.add(Sale(
                    organization_id=self.organization_id,
                    user_id=self.owner_id,
                    product_id=self.product_id,
                    quantity=1,
                    unit_price=Decimal(amount),
                    total=Decimal(amount),
                    payment_method="card",
                    currency_code=currency,
                    locale_code="es_MX" if currency == "MXN" else "en_US",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                ))
            db.session.commit()
            with self.app.test_request_context("/"):
                summary = analytics(
                    db.session.get(User, self.owner_id)
                )["dashboard_summary"]
            self.assertEqual(summary["today_sales"], Decimal("100.00"))


if __name__ == "__main__":
    unittest.main()
