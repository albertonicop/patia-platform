"""Recipe costing, availability and atomic ingredient consumption."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app import db
from app.inventory.services import record_inventory_movement
from app.models import (
    Product, Recipe, RecipeComponent, RecipeSaleConsumption, Sale,
)
from app.money import MONEY_ZERO, money_decimal
from app.units import convert_quantity, normalize_unit, quantity_decimal


class RecipeError(ValueError):
    pass


class RecipeStockError(RecipeError):
    def __init__(self, shortages):
        self.shortages = shortages
        super().__init__("insufficient_recipe_stock")


def recipe_query(organization_id):
    return Recipe.query.options(
        selectinload(Recipe.sale_product),
        selectinload(Recipe.components).selectinload(RecipeComponent.product),
        selectinload(Recipe.components).selectinload(RecipeComponent.source_recipe),
    ).filter(Recipe.organization_id == organization_id)


def _merge(target, product, quantity):
    entry = target.setdefault(product.id, {"product": product, "quantity": Decimal("0")})
    entry["quantity"] += quantity_decimal(quantity)


def ingredient_requirements(recipe: Recipe, output_quantity=1, *, trail=()):
    """Explode a recipe into base inventory products using exact Decimal math."""
    if recipe.id in trail:
        raise RecipeError("recipe_cycle")
    output = quantity_decimal(output_quantity, positive=True)
    factor = output / quantity_decimal(recipe.yield_quantity, positive=True)
    requirements = {}
    next_trail = (*trail, recipe.id)
    for component in sorted(recipe.components, key=lambda value: value.position):
        component_quantity = quantity_decimal(component.quantity, positive=True) * factor
        if component.product is not None:
            if component.product.organization_id != recipe.organization_id:
                raise RecipeError("cross_organization_ingredient")
            try:
                base_quantity = convert_quantity(
                    component_quantity, component.unit_code, component.product.unit_code
                )
            except ValueError as exc:
                raise RecipeError("incompatible_units") from exc
            _merge(requirements, component.product, base_quantity)
            continue
        source = component.source_recipe
        if source is None or source.organization_id != recipe.organization_id:
            raise RecipeError("invalid_preparation")
        try:
            source_output = convert_quantity(
                component_quantity, component.unit_code, source.yield_unit_code
            )
        except ValueError as exc:
            raise RecipeError("incompatible_units") from exc
        nested = ingredient_requirements(source, source_output, trail=next_trail)
        for value in nested.values():
            _merge(requirements, value["product"], value["quantity"])
    if not requirements:
        raise RecipeError("recipe_without_ingredients")
    return requirements


def recipe_cost(recipe: Recipe, output_quantity=1):
    total = MONEY_ZERO
    details = []
    for value in ingredient_requirements(recipe, output_quantity).values():
        product = value["product"]
        quantity = quantity_decimal(value["quantity"])
        cost = money_decimal(quantity * (product.cost_price or MONEY_ZERO))
        total = money_decimal(total + cost)
        details.append({"product": product, "quantity": quantity, "cost": cost})
    return total, details


def recipe_availability(recipe: Recipe) -> int:
    values = []
    for value in ingredient_requirements(recipe, 1).values():
        required = quantity_decimal(value["quantity"], positive=True)
        available = quantity_decimal(value["product"].stock)
        values.append(int((available / required).to_integral_value(rounding=ROUND_FLOOR)))
    return max(min(values), 0) if values else 0


def recipe_operational_summary(recipe: Recipe):
    """Return one shared operational view of cost, stock and availability."""
    cost, raw_details = recipe_cost(
        recipe,
        1 if recipe.recipe_type == "dish" else recipe.yield_quantity,
    )
    details = []
    limiting = None
    availability = None
    for item in raw_details:
        product = item["product"]
        required = quantity_decimal(item["quantity"], positive=True)
        stock = quantity_decimal(product.stock)
        portions = int(
            (stock / required).to_integral_value(rounding=ROUND_FLOOR)
        )
        row = {
            **item,
            "stock": stock,
            "unit_cost": money_decimal(product.cost_price or MONEY_ZERO),
            "available_outputs": max(portions, 0),
        }
        details.append(row)
        if availability is None or portions < availability:
            availability = portions
            limiting = row
    availability = max(availability or 0, 0)
    for row in details:
        row["is_limiting"] = bool(
            limiting and row["product"].id == limiting["product"].id
        )
    return {
        "cost": cost,
        "ingredients": details,
        "availability": availability,
        "limiting_ingredient": limiting,
    }


def recipes_using_product(organization_id: int, product_id: int):
    """Return recipes whose fully exploded requirements use one product."""
    values = recipe_query(organization_id).filter(
        Recipe.is_active.is_(True)
    ).order_by(Recipe.name).all()
    matches = []
    for recipe in values:
        requirements = ingredient_requirements(
            recipe,
            1 if recipe.recipe_type == "dish" else recipe.yield_quantity,
        )
        if product_id in requirements:
            matches.append(recipe)
    return matches


def restaurant_period_analytics(
    organization_id: int,
    period,
    *,
    currency_code: str,
):
    """Build deterministic Restaurant analysis from historical sale snapshots."""
    filters = (
        Sale.organization_id == organization_id,
        Sale.currency_code == currency_code,
        Sale.created_at >= period["start_at"],
        Sale.created_at < period["end_before"],
        Sale.recipe_id.is_not(None),
    )
    dish_rows = (
        db.session.query(
            Recipe.id.label("recipe_id"),
            Recipe.name.label("name"),
            func.coalesce(func.sum(Sale.quantity), 0).label("units"),
            func.coalesce(func.sum(Sale.total), 0).label("revenue"),
            func.coalesce(
                func.sum(Sale.unit_cost * Sale.quantity), 0
            ).label("cost"),
        )
        .join(Sale, Sale.recipe_id == Recipe.id)
        .filter(*filters, Recipe.organization_id == organization_id)
        .group_by(Recipe.id, Recipe.name)
        .all()
    )
    dishes = []
    for row in dish_rows:
        revenue = money_decimal(row.revenue or 0)
        cost = money_decimal(row.cost or 0)
        profit = money_decimal(revenue - cost, nonnegative=False)
        dishes.append({
            "recipe_id": row.recipe_id,
            "name": row.name,
            "units": int(row.units or 0),
            "revenue": revenue,
            "cost": cost,
            "profit": profit,
            "margin": round(profit / revenue * 100, 1) if revenue else None,
        })

    ingredient_rows = (
        db.session.query(
            RecipeSaleConsumption.ingredient_product_id.label("product_id"),
            RecipeSaleConsumption.ingredient_name.label("name"),
            RecipeSaleConsumption.unit_code.label("unit_code"),
            func.coalesce(
                func.sum(RecipeSaleConsumption.quantity), 0
            ).label("quantity"),
            func.coalesce(
                func.sum(RecipeSaleConsumption.total_cost), 0
            ).label("cost"),
        )
        .join(Sale, Sale.id == RecipeSaleConsumption.sale_id)
        .filter(
            *filters,
            RecipeSaleConsumption.organization_id == organization_id,
        )
        .group_by(
            RecipeSaleConsumption.ingredient_product_id,
            RecipeSaleConsumption.ingredient_name,
            RecipeSaleConsumption.unit_code,
        )
        .all()
    )
    product_ids = [row.product_id for row in ingredient_rows]
    products = {
        product.id: product
        for product in Product.query.filter(
            Product.organization_id == organization_id,
            Product.id.in_(product_ids),
        ).all()
    } if product_ids else {}
    ingredients = []
    for row in ingredient_rows:
        product = products.get(row.product_id)
        ingredients.append({
            "product_id": row.product_id,
            "name": row.name,
            "unit_code": row.unit_code,
            "quantity": quantity_decimal(row.quantity or 0),
            "cost": money_decimal(row.cost or 0),
            "stock": quantity_decimal(product.stock) if product else None,
            "min_stock": quantity_decimal(product.min_stock) if product else None,
            "is_critical": bool(
                product and product.stock <= product.min_stock
            ),
        })

    active_dishes = recipe_query(organization_id).filter(
        Recipe.recipe_type == "dish",
        Recipe.is_active.is_(True),
    ).order_by(Recipe.name).all()
    limited = []
    for recipe in active_dishes:
        summary = recipe_operational_summary(recipe)
        if summary["availability"] <= 10:
            limited.append({
                "recipe_id": recipe.id,
                "name": recipe.name,
                "availability": summary["availability"],
                "limiting_ingredient": (
                    summary["limiting_ingredient"]["product"].name
                    if summary["limiting_ingredient"] else None
                ),
            })
    limited.sort(key=lambda item: (item["availability"], item["name"].casefold()))
    top_selling = sorted(
        dishes, key=lambda item: (-item["units"], item["name"].casefold())
    )
    most_profitable = sorted(
        dishes, key=lambda item: (-item["profit"], item["name"].casefold())
    )
    highest_revenue = sorted(
        dishes, key=lambda item: (-item["revenue"], item["name"].casefold())
    )
    margin_values = [item for item in dishes if item["margin"] is not None]
    lowest_margin = sorted(
        margin_values, key=lambda item: (item["margin"], item["name"].casefold())
    )
    ingredients.sort(
        key=lambda item: (-item["quantity"], item["name"].casefold())
    )
    return {
        "dish_units": sum(item["units"] for item in dishes),
        "dishes": dishes,
        "top_selling": top_selling[:10],
        "most_profitable": most_profitable[:10],
        "highest_revenue": highest_revenue[:10],
        "lowest_margin": lowest_margin[:10],
        "ingredients": ingredients[:20],
        "critical_ingredients": [
            item for item in ingredients if item["is_critical"]
        ][:10],
        "ingredient_cost": money_decimal(
            sum((item["cost"] for item in ingredients), MONEY_ZERO)
        ),
        "limited_dishes": limited[:10],
    }


def validate_recipe_components(recipe, components):
    """Validate ownership, compatible units and cycles before persistence."""
    seen = set()
    for component in components:
        source = component.product or component.source_recipe
        key = ("product" if component.product else "recipe", source.id if source else None)
        if not source or key in seen:
            raise RecipeError("duplicate_or_missing_ingredient")
        seen.add(key)
        if source.organization_id != recipe.organization_id:
            raise RecipeError("cross_organization_ingredient")
        if component.product:
            convert_quantity(component.quantity, component.unit_code, component.product.unit_code)
        else:
            if (
                recipe.id is not None
                and component.source_recipe is not None
                and component.source_recipe.id == recipe.id
            ):
                raise RecipeError("recipe_cycle")
            convert_quantity(
                component.quantity, component.unit_code,
                component.source_recipe.yield_unit_code,
            )
            ingredient_requirements(component.source_recipe, component.quantity, trail=(recipe.id,))


def prepare_recipe_sales(items, organization_id):
    """Lock and validate aggregate ingredient needs for a complete cart."""
    line_requirements = {}
    aggregate = defaultdict(lambda: Decimal("0"))
    product_refs = {}
    for product, quantity in items:
        if product.item_type != "recipe":
            continue
        recipe = recipe_query(organization_id).filter(
            Recipe.sale_product_id == product.id,
            Recipe.is_active.is_(True),
        ).first()
        if not recipe:
            raise RecipeError("inactive_recipe")
        requirements = ingredient_requirements(recipe, quantity)
        line_requirements[product.id] = (recipe, requirements)
        for product_id, value in requirements.items():
            aggregate[product_id] += value["quantity"]
            product_refs[product_id] = value["product"]
    if not aggregate:
        return line_requirements

    locked = {
        product.id: product
        for product in Product.query.filter(
            Product.organization_id == organization_id,
            Product.id.in_(aggregate.keys()),
            Product.is_active.is_(True),
        ).with_for_update().all()
    }
    shortages = []
    for product_id, required in aggregate.items():
        product = locked.get(product_id)
        available = quantity_decimal(product.stock if product else 0)
        if available < required:
            shortages.append({
                "product": product_refs[product_id],
                "required": required,
                "available": available,
                "missing": required - available,
            })
    if shortages:
        raise RecipeStockError(shortages)
    for recipe, requirements in line_requirements.values():
        for product_id, value in requirements.items():
            value["product"] = locked[product_id]
    return line_requirements


def consume_recipe_sale(sale, recipe, requirements, membership, ticket):
    total_cost = MONEY_ZERO
    for value in requirements.values():
        ingredient = value["product"]
        quantity = quantity_decimal(value["quantity"], positive=True)
        before = quantity_decimal(ingredient.stock)
        after = before - quantity
        ingredient.stock = after
        component_cost = money_decimal(quantity * (ingredient.cost_price or MONEY_ZERO))
        total_cost = money_decimal(total_cost + component_cost)
        db.session.add(RecipeSaleConsumption(
            organization_id=membership.organization_id,
            sale=sale,
            sales_ticket_id=ticket.id,
            recipe_id=recipe.id,
            ingredient_product_id=ingredient.id,
            ingredient_name=ingredient.name,
            quantity=quantity,
            unit_code=ingredient.unit_code,
            unit_cost=ingredient.cost_price or MONEY_ZERO,
            total_cost=component_cost,
        ))
        record_inventory_movement(
            ingredient, membership, "SALE", before, after,
            reason=f"{ticket.folio} · {recipe.name}", sale=sale,
            sales_ticket=ticket,
        )
    sale.recipe_id = recipe.id
    sale.unit_cost = money_decimal(total_cost / quantity_decimal(sale.quantity, positive=True))
    sale.cost_is_estimated = False
    return total_cost
