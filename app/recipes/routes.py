from __future__ import annotations

import json
import uuid
from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext
from sqlalchemy import func

from app import db
from app.models import Product, Recipe, RecipeComponent, Sale
from app.money import MONEY_ZERO, money_decimal
from app.plans import has_entitlement
from app.team.services import active_membership, require_permission
from app.units import UNITS, compatible_units, normalize_unit, quantity_decimal
from .services import (
    RecipeError, recipe_availability, recipe_cost, recipe_query,
    validate_recipe_components,
)


recipes = Blueprint("recipes", __name__, url_prefix="/recipes")


def _context():
    from app.routes import current_organization_owner, current_user

    user = current_user()
    membership = active_membership(user) if user else None
    owner = current_organization_owner(user) if user else None
    if not membership or not owner:
        abort(403)
    if (
        membership.organization.business_type != "restaurant"
        or not has_entitlement(owner, "recipes")
    ):
        abort(403)
    return membership, owner


def _options(organization_id, recipe_id=None):
    products = Product.query.filter_by(
        organization_id=organization_id, is_active=True, item_type="inventory"
    ).order_by(Product.name).all()
    preparations = recipe_query(organization_id).filter(
        Recipe.recipe_type == "preparation", Recipe.is_active.is_(True)
    )
    if recipe_id:
        preparations = preparations.filter(Recipe.id != recipe_id)
    preparation_values = preparations.order_by(Recipe.name).all()
    for preparation in preparation_values:
        batch_cost, _ = recipe_cost(
            preparation, preparation.yield_quantity
        )
        preparation.preview_unit_cost = money_decimal(
            batch_cost / quantity_decimal(
                preparation.yield_quantity, positive=True
            )
        )
    return products, preparation_values


def _component_payload(organization_id, raw):
    try:
        rows = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise RecipeError("invalid_components") from exc
    components = []
    for position, row in enumerate(rows):
        source_type = str(row.get("source_type") or "product")
        source_id = int(row.get("source_id") or 0)
        quantity = quantity_decimal(row.get("quantity"), positive=True)
        unit_code = normalize_unit(row.get("unit_code"), default="")
        if unit_code not in UNITS:
            raise RecipeError("invalid_unit")
        component = RecipeComponent(
            quantity=quantity, unit_code=unit_code, position=position,
        )
        if source_type == "product":
            component.product = Product.query.filter_by(
                id=source_id, organization_id=organization_id,
                is_active=True, item_type="inventory",
            ).first()
        elif source_type == "recipe":
            component.source_recipe = Recipe.query.filter_by(
                id=source_id, organization_id=organization_id,
                is_active=True, recipe_type="preparation",
            ).first()
        else:
            raise RecipeError("invalid_component_source")
        components.append(component)
    if not components:
        raise RecipeError("recipe_without_ingredients")
    return components


def _save_recipe(recipe, membership):
    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip() or gettext("Platillos")
    description = request.form.get("description", "").strip() or None
    recipe_type = request.form.get("recipe_type", "dish").strip()
    if recipe_type not in {"dish", "preparation"} or not name:
        raise RecipeError("invalid_recipe")
    yield_quantity = quantity_decimal(
        request.form.get("yield_quantity") or 1, positive=True
    )
    yield_unit = normalize_unit(
        request.form.get("yield_unit_code"),
        default="portion" if recipe_type == "dish" else "",
    )
    if yield_unit not in UNITS:
        raise RecipeError("invalid_unit")
    components = _component_payload(
        membership.organization_id, request.form.get("components_json")
    )
    recipe.name = name
    recipe.category = category
    recipe.description = description
    recipe.recipe_type = recipe_type
    recipe.yield_quantity = yield_quantity
    recipe.yield_unit_code = yield_unit
    recipe.is_active = request.form.get("is_active") == "1"
    recipe.organization_id = membership.organization_id
    validate_recipe_components(recipe, components)
    recipe.components[:] = components

    if recipe_type == "dish":
        sale_price = money_decimal(
            request.form.get("sale_price") or 0, nonnegative=False
        )
        if sale_price < MONEY_ZERO:
            raise RecipeError("invalid_price")
        if recipe.sale_product is None:
            recipe.sale_product = Product(
                organization_id=membership.organization_id,
                user_id=membership.organization.owner_user_id,
                sku=f"REC-{uuid.uuid4().hex[:10].upper()}",
                name=name,
                category=category,
                cost_price=MONEY_ZERO,
                sale_price=sale_price,
                stock=0,
                min_stock=0,
                unit_code="portion",
                item_type="recipe",
                is_active=recipe.is_active,
            )
        else:
            recipe.sale_product.name = name
            recipe.sale_product.category = category
            recipe.sale_product.sale_price = sale_price
            recipe.sale_product.is_active = recipe.is_active
        db.session.add(recipe)
        db.session.flush()
        recipe.sale_product.cost_price = recipe_cost(recipe, 1)[0]
    elif recipe.sale_product is not None:
        recipe.sale_product.is_active = False


@recipes.get("")
@require_permission("manage_recipes")
def index():
    membership, _ = _context()
    values = recipe_query(membership.organization_id).order_by(Recipe.name).all()
    sales_rows = (
        db.session.query(
            Sale.recipe_id,
            func.coalesce(func.sum(Sale.quantity), 0),
            func.coalesce(func.sum(Sale.total), 0),
            func.coalesce(func.sum(Sale.unit_cost * Sale.quantity), 0),
        )
        .filter(
            Sale.organization_id == membership.organization_id,
            Sale.recipe_id.is_not(None),
        )
        .group_by(Sale.recipe_id)
        .all()
    )
    sales = {
        recipe_id: (units, revenue, historical_cost)
        for recipe_id, units, revenue, historical_cost in sales_rows
    }
    cards = []
    for recipe in values:
        cost, _ = recipe_cost(recipe, 1 if recipe.recipe_type == "dish" else recipe.yield_quantity)
        price = recipe.sale_product.sale_price if recipe.sale_product else MONEY_ZERO
        margin = (
            ((price - cost) / price * Decimal("100"))
            if recipe.recipe_type == "dish" and price > 0 else None
        )
        sold = sales.get(recipe.id, (0, MONEY_ZERO, MONEY_ZERO))
        cards.append({
            "recipe": recipe, "cost": cost, "price": price,
            "profit": money_decimal(price - cost), "margin": margin,
            "availability": recipe_availability(recipe),
            "units_sold": sold[0], "revenue": sold[1], "historical_cost": sold[2],
        })
    return render_template("recipes.html", recipes=cards)


@recipes.route("/new", methods=["GET", "POST"])
@require_permission("manage_recipes")
def create():
    membership, _ = _context()
    recipe = Recipe(organization_id=membership.organization_id)
    if request.method == "POST":
        try:
            _save_recipe(recipe, membership)
            db.session.commit()
            flash(gettext("Receta creada correctamente."), "success")
            return redirect(url_for("recipes.detail", recipe_id=recipe.id))
        except (RecipeError, ValueError) as exc:
            db.session.rollback()
            flash(_error_message(str(exc)), "danger")
    products, preparations = _options(membership.organization_id)
    return render_template(
        "recipe_form.html", recipe=recipe, products=products,
        preparations=preparations, units=UNITS, components_json=request.form.get("components_json", "[]"),
    )


@recipes.route("/<int:recipe_id>/edit", methods=["GET", "POST"])
@require_permission("manage_recipes")
def edit(recipe_id):
    membership, _ = _context()
    recipe = recipe_query(membership.organization_id).filter_by(id=recipe_id).first_or_404()
    if request.method == "POST":
        try:
            _save_recipe(recipe, membership)
            db.session.commit()
            flash(gettext("Receta actualizada correctamente."), "success")
            return redirect(url_for("recipes.detail", recipe_id=recipe.id))
        except (RecipeError, ValueError) as exc:
            db.session.rollback()
            flash(_error_message(str(exc)), "danger")
    products, preparations = _options(membership.organization_id, recipe.id)
    initial = [
        {
            "source_type": "product" if row.product_id else "recipe",
            "source_id": row.product_id or row.source_recipe_id,
            "quantity": str(row.quantity), "unit_code": row.unit_code,
        }
        for row in recipe.components
    ]
    return render_template(
        "recipe_form.html", recipe=recipe, products=products,
        preparations=preparations, units=UNITS,
        components_json=request.form.get("components_json") or json.dumps(initial),
    )


@recipes.get("/<int:recipe_id>")
@require_permission("manage_recipes")
def detail(recipe_id):
    membership, _ = _context()
    recipe = recipe_query(membership.organization_id).filter_by(id=recipe_id).first_or_404()
    output = 1 if recipe.recipe_type == "dish" else recipe.yield_quantity
    cost, details = recipe_cost(recipe, output)
    price = recipe.sale_product.sale_price if recipe.sale_product else MONEY_ZERO
    margin = ((price - cost) / price * Decimal("100")) if price > 0 else None
    return render_template(
        "recipe_detail.html", recipe=recipe, ingredient_costs=details,
        cost=cost, price=price, margin=margin,
        availability=recipe_availability(recipe),
    )


@recipes.post("/<int:recipe_id>/duplicate")
@require_permission("manage_recipes")
def duplicate(recipe_id):
    membership, _ = _context()
    source = recipe_query(membership.organization_id).filter_by(id=recipe_id).first_or_404()
    base_name = f"{source.name} ({gettext('copia')})"
    name = base_name
    suffix = 2
    while Recipe.query.filter_by(
        organization_id=membership.organization_id, name=name
    ).first():
        name = f"{base_name} {suffix}"
        suffix += 1
    copy = Recipe(
        organization_id=membership.organization_id,
        name=name,
        category=source.category, description=source.description,
        recipe_type=source.recipe_type, yield_quantity=source.yield_quantity,
        yield_unit_code=source.yield_unit_code, is_active=False,
    )
    for component in source.components:
        copy.components.append(RecipeComponent(
            product_id=component.product_id,
            source_recipe_id=component.source_recipe_id,
            quantity=component.quantity, unit_code=component.unit_code,
            position=component.position,
        ))
    if source.recipe_type == "dish":
        copy.sale_product = Product(
            organization_id=membership.organization_id,
            user_id=membership.organization.owner_user_id,
            sku=f"REC-{uuid.uuid4().hex[:10].upper()}", name=copy.name,
            category=copy.category, cost_price=source.sale_product.cost_price,
            sale_price=source.sale_product.sale_price, stock=0, min_stock=0,
            unit_code="portion", item_type="recipe", is_active=False,
        )
    db.session.add(copy)
    db.session.commit()
    flash(gettext("Receta duplicada. Revísala antes de activarla."), "success")
    return redirect(url_for("recipes.edit", recipe_id=copy.id))


@recipes.post("/<int:recipe_id>/toggle")
@require_permission("manage_recipes")
def toggle(recipe_id):
    membership, _ = _context()
    recipe = recipe_query(membership.organization_id).filter_by(id=recipe_id).first_or_404()
    recipe.is_active = not recipe.is_active
    if recipe.sale_product:
        recipe.sale_product.is_active = recipe.is_active
    db.session.commit()
    flash(gettext("Estado de la receta actualizado."), "success")
    return redirect(url_for("recipes.index"))


def _error_message(code):
    return {
        "recipe_cycle": gettext("Una preparación no puede depender de sí misma."),
        "incompatible_units": gettext("La unidad no es compatible con el ingrediente."),
        "recipe_without_ingredients": gettext("Agrega al menos un ingrediente."),
        "duplicate_or_missing_ingredient": gettext("Revisa los ingredientes duplicados o incompletos."),
        "cross_organization_ingredient": gettext("El ingrediente no pertenece a este negocio."),
        "invalid_quantity": gettext("Escribe cantidades válidas mayores que cero."),
        "invalid_components": gettext("No pudimos leer los ingredientes. Intenta nuevamente."),
        "invalid_unit": gettext("Selecciona una unidad válida."),
        "invalid_component_source": gettext("Selecciona un ingrediente válido."),
        "invalid_preparation": gettext("La preparación seleccionada ya no está disponible."),
        "invalid_recipe": gettext("Escribe un nombre y selecciona un tipo de receta válido."),
        "invalid_price": gettext("El precio no puede ser negativo."),
    }.get(code, gettext("Revisa la información de la receta."))
