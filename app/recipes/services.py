"""Recipe costing, availability and atomic ingredient consumption."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR

from sqlalchemy.orm import selectinload

from app import db
from app.inventory.services import record_inventory_movement
from app.models import (
    Product, Recipe, RecipeComponent, RecipeSaleConsumption,
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
            if component.source_recipe_id == recipe.id:
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
