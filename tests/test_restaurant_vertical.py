import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "restaurant-tests-secret")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_restaurant")
os.environ.setdefault("STRIPE_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_STARTER_PRICE_ID", "price_starter")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "price_pro")
os.environ.setdefault("STRIPE_RESTAURANT_PRICE_ID", "price_restaurant")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "whsec_restaurant")
os.environ.setdefault("PUBLIC_BASE_URL", "https://patia.test")

from app import create_app, db
from app.inventory.imports import apply_catalog, inspect_catalog
from app.models import (
    InventoryMovement,
    OrganizationMember,
    Product,
    Recipe,
    RecipeComponent,
    RecipeSaleConsumption,
    Sale,
    SalesTicket,
    User,
)
from app.monthly_reports import (
    SNAPSHOT_VERSION,
    build_report_snapshot,
    payload_from_snapshot,
    report_payload,
)
from app.plans import (
    PRO,
    RESTAURANT,
    STARTER,
    PLAN_PRICES_MXN,
    capabilities_for,
    entitlements_for,
)
from app.pro.purchases import purchase_suggestions
from app.recipes.services import (
    RecipeError,
    ingredient_requirements,
    recipe_availability,
    recipe_cost,
    validate_recipe_components,
)
from app.team.services import ensure_owner_organization
from app.units import convert_quantity
from flask_babel import refresh
from flask import render_template


class RestaurantVerticalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="patia-restaurant-")
        self.previous_url = os.environ.get("DATABASE_URL")
        path = Path(self.temp_dir.name, "restaurant.db")
        os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
        self.app = create_app()
        self.app.config.update(
            TESTING=True,
            WTF_CSRF_ENABLED=False,
            RATELIMIT_ENABLED=False,
            STRIPE_RESTAURANT_PRICE_ID="price_restaurant",
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.owner, self.membership = self._owner(
            "owner@restaurant.test", RESTAURANT, "restaurant"
        )
        self.client = self._client(self.owner, self.membership)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp_dir.cleanup()
        if self.previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.previous_url

    def _owner(self, email, plan=STARTER, business_type="general"):
        user = User(
            email=email,
            company_name="Negocio de prueba",
            email_verified=True,
            trial_plan_code=plan,
        )
        user.set_password("Password123")
        db.session.add(user)
        db.session.flush()
        membership = ensure_owner_organization(user)
        membership.organization.business_type = business_type
        db.session.commit()
        return user, membership

    def _member(self, role):
        user = User(
            email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@restaurant.test",
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

    def _client(self, user, membership):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user.id
            session["organization_id"] = membership.organization_id
        return client

    def _product(
        self, name, sku, *, unit="piece", stock="10", cost="1", minimum="0"
    ):
        product = Product(
            organization_id=self.membership.organization_id,
            user_id=self.owner.id,
            name=name,
            sku=sku,
            category="Ingredientes",
            cost_price=Decimal(cost),
            sale_price=Decimal(cost),
            stock=Decimal(stock),
            min_stock=Decimal(minimum),
            unit_code=unit,
        )
        db.session.add(product)
        db.session.commit()
        return product

    def _create_dish(self, *, price="135"):
        bread = self._product("Pan", "PAN", stock="20", cost="6")
        meat = self._product(
            "Carne molida", "CARNE", unit="kg", stock="12.500", cost="180"
        )
        response = self.client.post(
            "/recipes/new",
            data={
                "name": "Hamburguesa clásica",
                "category": "Hamburguesas",
                "recipe_type": "dish",
                "sale_price": price,
                "yield_quantity": "1",
                "yield_unit_code": "portion",
                "is_active": "1",
                "components_json": json.dumps(
                    [
                        {
                            "source_type": "product",
                            "source_id": bread.id,
                            "quantity": "1",
                            "unit_code": "piece",
                        },
                        {
                            "source_type": "product",
                            "source_id": meat.id,
                            "quantity": "180",
                            "unit_code": "g",
                        },
                    ]
                ),
            },
        )
        self.assertEqual(response.status_code, 302)
        return Recipe.query.filter_by(name="Hamburguesa clásica").one(), bread, meat

    def test_plan_matrix_and_pricing_are_centralized(self):
        self.assertEqual(PLAN_PRICES_MXN[RESTAURANT], 360)
        self.assertTrue(entitlements_for(RESTAURANT).recipes)
        self.assertTrue(entitlements_for(RESTAURANT).advanced_reports)
        self.assertTrue(entitlements_for(RESTAURANT).monthly_owner_report)
        self.assertTrue(entitlements_for(RESTAURANT).executive_dashboard)
        self.assertFalse(entitlements_for(STARTER).recipes)
        self.assertFalse(entitlements_for(PRO).recipes)
        self.assertIn("ingredient_depletion", capabilities_for(RESTAURANT))

    def test_sidebar_and_direct_access_require_type_and_entitlement(self):
        restaurant_page = self.client.get("/")
        self.assertIn("Recetas", restaurant_page.get_data(as_text=True))
        general_owner, general_member = self._owner(
            "general@restaurant.test", RESTAURANT, "general"
        )
        general_client = self._client(general_owner, general_member)
        self.assertNotIn("Recetas", general_client.get("/").get_data(as_text=True))
        self.assertEqual(general_client.get("/recipes").status_code, 403)
        starter_owner, starter_member = self._owner(
            "starter@restaurant.test", STARTER, "restaurant"
        )
        starter_client = self._client(starter_owner, starter_member)
        self.assertNotIn("Recetas", starter_client.get("/").get_data(as_text=True))
        self.assertEqual(starter_client.get("/recipes").status_code, 403)

    def test_owner_manager_can_manage_and_cashier_can_only_sell(self):
        manager, manager_member = self._member("MANAGER")
        cashier, cashier_member = self._member("CASHIER")
        self.assertEqual(self.client.get("/recipes").status_code, 200)
        self.assertEqual(self._client(manager, manager_member).get("/recipes").status_code, 200)
        self.assertEqual(self._client(cashier, cashier_member).get("/recipes").status_code, 403)
        self._create_dish()
        self.assertEqual(self._client(cashier, cashier_member).get("/sell").status_code, 200)

    def test_exact_unit_conversions_and_incompatible_families(self):
        self.assertEqual(convert_quantity("180", "g", "kg"), Decimal("0.180"))
        self.assertEqual(convert_quantity("250", "ml", "L"), Decimal("0.250"))
        self.assertEqual(convert_quantity("2", "dozen", "piece"), Decimal("24.000"))
        with self.assertRaises(ValueError):
            convert_quantity("1", "kg", "L")

    def test_recipe_cost_margin_availability_and_listing(self):
        recipe, _, meat = self._create_dish()
        cost, details = recipe_cost(recipe)
        self.assertEqual(cost, Decimal("38.40"))
        self.assertEqual(len(details), 2)
        self.assertEqual(recipe_availability(recipe), 20)
        page = self.client.get("/recipes")
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Hamburguesa clásica", html)
        self.assertIn("38.40", html)
        self.assertIn("71.6", html)
        meat.stock = Decimal("0.100")
        db.session.commit()
        self.assertEqual(recipe_availability(recipe), 0)

    def test_preparation_explodes_and_cycles_are_rejected(self):
        tomato = self._product("Tomate", "TOM", unit="kg", stock="10", cost="30")
        sauce = Recipe(
            organization_id=self.membership.organization_id,
            name="Salsa de la casa",
            recipe_type="preparation",
            yield_quantity=Decimal("5"),
            yield_unit_code="L",
        )
        sauce.components.append(
            RecipeComponent(product=tomato, quantity=Decimal("2"), unit_code="kg")
        )
        dish = Recipe(
            organization_id=self.membership.organization_id,
            name="Platillo con salsa",
            recipe_type="dish",
            yield_quantity=1,
            yield_unit_code="portion",
        )
        dish.components.append(
            RecipeComponent(source_recipe=sauce, quantity=Decimal("30"), unit_code="ml")
        )
        db.session.add_all([sauce, dish])
        db.session.commit()
        requirements = ingredient_requirements(dish)
        self.assertEqual(requirements[tomato.id]["quantity"], Decimal("0.012"))
        cycle = RecipeComponent(
            source_recipe=dish, quantity=Decimal("1"), unit_code="portion"
        )
        with self.assertRaises(RecipeError):
            validate_recipe_components(sauce, [cycle])

    def test_preparation_sale_consumes_physical_stock_once_and_cancel_restores_it(self):
        tomato = self._product(
            "Tomate para salsa", "TOM-SALE", unit="kg", stock="10", cost="30"
        )
        preparation = self.client.post(
            "/recipes/new",
            data={
                "name": "Salsa operativa",
                "recipe_type": "preparation",
                "yield_quantity": "5",
                "yield_unit_code": "L",
                "is_active": "1",
                "components_json": json.dumps([{
                    "source_type": "product", "source_id": tomato.id,
                    "quantity": "2", "unit_code": "kg",
                }]),
            },
        )
        self.assertEqual(preparation.status_code, 302)
        sauce = Recipe.query.filter_by(name="Salsa operativa").one()
        dish = self.client.post(
            "/recipes/new",
            data={
                "name": "Platillo con salsa operativa",
                "recipe_type": "dish",
                "sale_price": "100",
                "yield_quantity": "1",
                "yield_unit_code": "portion",
                "is_active": "1",
                "components_json": json.dumps([{
                    "source_type": "recipe", "source_id": sauce.id,
                    "quantity": "30", "unit_code": "ml",
                }]),
            },
        )
        self.assertEqual(dish.status_code, 302, dish.get_data(as_text=True))
        recipe = Recipe.query.filter_by(name="Platillo con salsa operativa").one()
        sold = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 2}],
            },
        )
        self.assertEqual(sold.status_code, 200)
        db.session.refresh(tomato)
        self.assertEqual(tomato.stock, Decimal("9.976"))
        consumption = RecipeSaleConsumption.query.one()
        self.assertEqual(consumption.quantity, Decimal("0.024"))
        self.assertEqual(InventoryMovement.query.filter_by(
            product_id=tomato.id, movement_type="SALE"
        ).count(), 1)
        sale = Sale.query.one()
        self.assertEqual(self.client.post(f"/sales/{sale.id}/cancel").status_code, 302)
        db.session.refresh(tomato)
        self.assertEqual(tomato.stock, Decimal("10.000"))

    def test_sale_decrements_ingredients_and_freezes_historical_cost(self):
        recipe, bread, meat = self._create_dish()
        response = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 2}],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        db.session.refresh(bread)
        db.session.refresh(meat)
        self.assertEqual(bread.stock, Decimal("18.000"))
        self.assertEqual(meat.stock, Decimal("12.140"))
        sale = Sale.query.one()
        self.assertEqual(sale.unit_cost, Decimal("38.40"))
        snapshots = RecipeSaleConsumption.query.order_by(
            RecipeSaleConsumption.ingredient_name
        ).all()
        self.assertEqual(len(snapshots), 2)
        frozen = sum(row.total_cost for row in snapshots)
        self.assertEqual(frozen, Decimal("76.80"))
        meat.cost_price = Decimal("250")
        db.session.commit()
        self.assertEqual(Sale.query.one().unit_cost, Decimal("38.40"))
        self.assertEqual(
            sum(row.total_cost for row in RecipeSaleConsumption.query.all()),
            Decimal("76.80"),
        )

    def test_insufficient_stock_is_atomic_and_explains_missing_ingredient(self):
        recipe, bread, meat = self._create_dish()
        meat.stock = Decimal("0.100")
        db.session.commit()
        response = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "transfer",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 1}],
            },
        )
        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["error_code"], "insufficient_recipe_stock")
        self.assertEqual(payload["shortages"][0]["product"], "Carne molida")
        self.assertEqual(payload["shortages"][0]["missing"], "0.080")
        db.session.refresh(bread)
        db.session.refresh(meat)
        self.assertEqual(bread.stock, Decimal("20.000"))
        self.assertEqual(meat.stock, Decimal("0.100"))
        self.assertEqual(Sale.query.count(), 0)
        self.assertEqual(SalesTicket.query.count(), 0)

    def test_kardex_uses_ingredient_quantities_and_ticket_reference(self):
        recipe, _, _ = self._create_dish()
        self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "transfer",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 1}],
            },
        )
        ticket = SalesTicket.query.one()
        movements = InventoryMovement.query.filter_by(movement_type="SALE").all()
        self.assertEqual(len(movements), 2)
        self.assertTrue(all(row.sales_ticket_id == ticket.id for row in movements))
        self.assertEqual(
            {row.quantity_delta for row in movements},
            {Decimal("-1.000"), Decimal("-0.180")},
        )
        self.assertTrue(all("Hamburguesa clásica" in row.reason for row in movements))

    def test_ticket_reprint_and_currency_are_frozen_at_sale_time(self):
        recipe, _, _ = self._create_dish(price="135.25")
        organization = self.membership.organization
        organization.country_code = "US"
        organization.currency_code = "USD"
        organization.locale_code = "en_US"
        db.session.commit()
        sold = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 1}],
            },
        )
        self.assertEqual(sold.status_code, 200)
        sale = Sale.query.one()
        ticket = SalesTicket.query.one()
        self.assertEqual((sale.currency_code, ticket.currency_code), ("USD", "USD"))
        organization.country_code = "MX"
        organization.currency_code = "MXN"
        organization.locale_code = "es_MX"
        db.session.commit()
        first = self.client.get(sold.get_json()["ticket_url"])
        second = self.client.get(sold.get_json()["ticket_url"] + "?print=1")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn("$135.25", first.get_data(as_text=True))
        self.assertEqual(SalesTicket.query.one().currency_code, "USD")

    def test_cancellation_restores_exact_recipe_ingredients(self):
        recipe, bread, meat = self._create_dish()
        sold = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "transfer",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 2}],
            },
        )
        self.assertEqual(sold.status_code, 200)
        sale = Sale.query.one()
        canceled = self.client.post(f"/sales/{sale.id}/cancel")
        self.assertEqual(canceled.status_code, 302)
        db.session.refresh(bread)
        db.session.refresh(meat)
        self.assertEqual(bread.stock, Decimal("20.000"))
        self.assertEqual(meat.stock, Decimal("12.500"))
        restored = InventoryMovement.query.filter_by(
            movement_type="SALE_CANCELLATION"
        ).all()
        self.assertEqual(
            {row.quantity_delta for row in restored},
            {Decimal("2.000"), Decimal("0.360")},
        )

    def test_duplicate_toggle_and_safe_business_type_change(self):
        recipe, _, _ = self._create_dish()
        duplicated = self.client.post(f"/recipes/{recipe.id}/duplicate")
        self.assertEqual(duplicated.status_code, 302)
        copy = Recipe.query.filter(Recipe.id != recipe.id).one()
        self.assertIn("copia", copy.name.lower())
        self.assertFalse(copy.is_active)
        toggled = self.client.post(f"/recipes/{copy.id}/toggle")
        self.assertEqual(toggled.status_code, 302)
        self.assertTrue(db.session.get(Recipe, copy.id).is_active)
        settings = {
            "company_name": "Negocio de prueba",
            "timezone": "America/Mexico_City",
            "country_code": "MX",
            "currency_code": "MXN",
            "locale_code": "es_MX",
            "business_type": "general",
        }
        warning = self.client.post("/settings", data=settings)
        self.assertEqual(warning.status_code, 302)
        self.assertIn("confirm_business_type_change=1", warning.location)
        self.assertEqual(self.membership.organization.business_type, "restaurant")
        settings["confirm_business_type_change"] = "1"
        changed = self.client.post("/settings", data=settings)
        self.assertEqual(changed.status_code, 302)
        self.assertEqual(self.membership.organization.business_type, "general")
        self.assertEqual(Recipe.query.count(), 2)

    def test_restaurant_products_do_not_pollute_inventory_but_ingredients_restock(self):
        recipe, _, meat = self._create_dish()
        meat.stock = Decimal("0.100")
        meat.min_stock = Decimal("1.000")
        db.session.commit()
        inventory = self.client.get("/products").get_data(as_text=True)
        self.assertIn("Carne molida", inventory)
        self.assertNotIn("Hamburguesa clásica", inventory)
        suggestions = purchase_suggestions(self.membership.organization_id)
        ids = {item["product_id"] for item in suggestions["suggestions"]}
        self.assertIn(meat.id, ids)
        self.assertNotIn(recipe.sale_product_id, ids)

    def test_import_accepts_fractional_stock_and_base_unit(self):
        content = (
            "Producto,Unidad,Existencias,Precio de venta,Costo,Stock minimo\n"
            "Aceite,L,8.5,60,42,1.25\n"
        ).encode()
        inspected = inspect_catalog("ingredientes.csv", content)
        self.assertEqual(inspected.rows[0]["stock"], Decimal("8.500"))
        self.assertEqual(inspected.rows[0]["unit_code"], "L")
        result = apply_catalog(
            inspected,
            self.membership.organization_id,
            self.owner.id,
            self.membership,
        )
        db.session.commit()
        self.assertEqual(result["created"], 1)
        product = Product.query.filter_by(name="Aceite").one()
        self.assertEqual(product.stock, Decimal("8.500"))
        self.assertEqual(product.min_stock, Decimal("1.250"))

    def test_organization_isolation_blocks_foreign_ingredients_and_routes(self):
        foreign_owner, foreign_membership = self._owner(
            "foreign@restaurant.test", RESTAURANT, "restaurant"
        )
        foreign_product = Product(
            organization_id=foreign_membership.organization_id,
            user_id=foreign_owner.id,
            name="Ingrediente ajeno",
            sku="FOREIGN",
            cost_price=1,
            sale_price=1,
            stock=10,
            min_stock=0,
        )
        db.session.add(foreign_product)
        db.session.commit()
        response = self.client.post(
            "/recipes/new",
            data={
                "name": "Receta inválida",
                "recipe_type": "dish",
                "sale_price": "10",
                "yield_quantity": "1",
                "yield_unit_code": "portion",
                "is_active": "1",
                "components_json": json.dumps([{
                    "source_type": "product", "source_id": foreign_product.id,
                    "quantity": "1", "unit_code": "piece",
                }]),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Recipe.query.filter_by(name="Receta inválida").count(), 0)
        self.assertNotIn("Ingrediente ajeno", response.get_data(as_text=True))

    def test_language_and_registration_business_type(self):
        register = self.app.test_client().get("/register")
        html = register.get_data(as_text=True)
        self.assertIn("¿Qué tipo de negocio tienes?", html)
        self.assertIn('value="restaurant"', html)
        language = self.client.post(
            "/language",
            data={"language": "en", "next": "/recipes"},
            follow_redirects=True,
        )
        self.assertEqual(language.status_code, 200)
        refresh()
        page = self.client.get("/recipes")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Recipes", page.get_data(as_text=True))
        form = self.client.get("/recipes/new").get_data(as_text=True)
        self.assertIn("How much does this recipe yield?", form)
        self.assertIn("Enter the total quantity", form)

    def test_complete_restaurant_flow_connects_inventory_reports_and_monthly_snapshot(self):
        meat = self._product(
            "Carne", "MEAT-E2E", unit="kg", stock="10", cost="180",
            minimum="9",
        )
        bread = self._product(
            "Pan hamburguesa", "BREAD-E2E", stock="100", cost="6",
            minimum="20",
        )
        cheese = self._product(
            "Queso", "CHEESE-E2E", unit="kg", stock="5", cost="150",
            minimum="4.7",
        )
        created = self.client.post(
            "/recipes/new",
            data={
                "name": "Hamburguesa E2E",
                "category": "Hamburguesas",
                "recipe_type": "dish",
                "sale_price": "135",
                "yield_quantity": "1",
                "yield_unit_code": "portion",
                "is_active": "1",
                "components_json": json.dumps([
                    {"source_type": "product", "source_id": meat.id, "quantity": "180", "unit_code": "g"},
                    {"source_type": "product", "source_id": bread.id, "quantity": "1", "unit_code": "piece"},
                    {"source_type": "product", "source_id": cheese.id, "quantity": "40", "unit_code": "g"},
                ]),
            },
        )
        self.assertEqual(created.status_code, 302)
        recipe = Recipe.query.filter_by(name="Hamburguesa E2E").one()

        product_page = self.client.get(f"/products/{meat.id}/edit")
        self.assertIn("Usado en recetas", product_page.get_data(as_text=True))
        self.assertIn("Hamburguesa E2E", product_page.get_data(as_text=True))
        detail = self.client.get(f"/recipes/{recipe.id}")
        detail_html = detail.get_data(as_text=True)
        self.assertIn("Stock disponible", detail_html)
        self.assertIn("Ingrediente limitante", detail_html)

        sold = self.client.post(
            "/sell-cart",
            json={
                "request_id": str(uuid.uuid4()),
                "payment_method": "card",
                "items": [{"product_id": recipe.sale_product_id, "quantity": 10}],
            },
        )
        self.assertEqual(sold.status_code, 200, sold.get_data(as_text=True))
        for product, expected in (
            (meat, Decimal("8.200")),
            (bread, Decimal("90.000")),
            (cheese, Decimal("4.600")),
        ):
            db.session.refresh(product)
            self.assertEqual(product.stock, expected)
        sale = Sale.query.one()
        self.assertEqual(sale.unit_cost, Decimal("44.40"))
        self.assertEqual(RecipeSaleConsumption.query.count(), 3)
        self.assertEqual(
            InventoryMovement.query.filter_by(movement_type="SALE").count(), 3
        )

        suggestions = purchase_suggestions(self.membership.organization_id)
        by_id = {item["product_id"]: item for item in suggestions["suggestions"]}
        self.assertIn(meat.id, by_id)
        self.assertIn(cheese.id, by_id)
        self.assertNotIn(recipe.sale_product_id, by_id)
        self.assertEqual(by_id[meat.id]["recent_units"], Decimal("1.800"))

        reports = self.client.get("/reports?period=this_month")
        reports_html = reports.get_data(as_text=True)
        self.assertIn("Resultados por platillo", reports_html)
        self.assertIn("Ingredientes consumidos", reports_html)
        self.assertIn("Hamburguesa E2E", reports_html)
        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn("Lectura Restaurant", dashboard)
        self.assertIn("Hamburguesa E2E", dashboard)
        decision_center = self.client.get("/pro/hub")
        self.assertEqual(decision_center.status_code, 200)
        self.assertIn(
            "Repón Carne para tus recetas",
            decision_center.get_data(as_text=True),
        )

        now = datetime.utcnow()
        payload = report_payload(
            self.membership.organization, now.year, now.month
        )
        with self.app.test_request_context("/"):
            email_html = render_template(
                "emails/monthly_owner_report.html",
                reports_url="https://patia.test/pro/monthly-reports",
                **payload,
            )
        self.assertIn("Platillos destacados", email_html)
        self.assertIn("Hamburguesa E2E", email_html)
        snapshot = build_report_snapshot(payload, "Reporte Restaurant")
        self.assertEqual(snapshot["version"], SNAPSHOT_VERSION)
        self.assertEqual(snapshot["report_type"], "restaurant")
        self.assertEqual(snapshot["restaurant"]["dish_units"], 10)
        self.assertEqual(
            snapshot["restaurant"]["top_selling"][0]["cost"], "444.00"
        )
        frozen = json.dumps(snapshot, sort_keys=True)
        legacy = dict(snapshot)
        legacy["version"] = 3
        legacy.pop("restaurant", None)
        legacy.pop("report_type", None)
        legacy_payload = payload_from_snapshot(SimpleNamespace(
            snapshot_json=json.dumps(legacy)
        ))
        self.assertIsNone(legacy_payload["restaurant"])

        self.assertEqual(self.client.get("/pro/purchases").status_code, 200)
        self.assertEqual(self.client.get("/pro/monthly-reports").status_code, 200)
        monthly_preview = self.client.get("/pro/monthly-reports/preview")
        self.assertEqual(monthly_preview.status_code, 200)
        self.assertIn(
            "Reporte mensual Restaurant",
            monthly_preview.get_data(as_text=True),
        )

        # Changing the current recipe must not affect the sold snapshot or
        # the exact quantities restored from that historical sale.
        recipe.components[0].quantity = Decimal("160")
        meat.cost_price = Decimal("250")
        db.session.commit()
        self.assertEqual(json.dumps(snapshot, sort_keys=True), frozen)
        canceled = self.client.post(f"/sales/{sale.id}/cancel")
        self.assertEqual(canceled.status_code, 302)
        for product, expected in (
            (meat, Decimal("10.000")),
            (bread, Decimal("100.000")),
            (cheese, Decimal("5.000")),
        ):
            db.session.refresh(product)
            self.assertEqual(product.stock, expected)
        after_cancellation = purchase_suggestions(
            self.membership.organization_id
        )
        self.assertNotIn(
            meat.id,
            {item["product_id"] for item in after_cancellation["suggestions"]},
        )

    def test_general_business_does_not_receive_restaurant_surfaces(self):
        general_owner, general_membership = self._owner(
            "general-surfaces@restaurant.test", PRO, "general"
        )
        general_client = self._client(general_owner, general_membership)
        self.assertNotIn(
            "Lectura Restaurant", general_client.get("/").get_data(as_text=True)
        )
        self.assertNotIn(
            "Resultados por platillo",
            general_client.get("/reports").get_data(as_text=True),
        )


if __name__ == "__main__":
    unittest.main()
