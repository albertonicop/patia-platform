from __future__ import annotations

import csv
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext
from sqlalchemy.orm import selectinload

from app import db
from app.models import (
    InventoryMovement,
    OrganizationMember,
    Product,
)
from app.team.services import active_membership, require_permission
from app.timezones import safe_timezone_name, utc_to_local

from .services import MANUAL_MOVEMENT_TYPES, change_product_stock, stock_consistency


inventory = Blueprint("inventory", __name__, url_prefix="/inventory")

MOVEMENT_LABELS = {
    "OPENING_BALANCE": "Saldo inicial",
    "SALE": "Venta",
    "SALE_CANCELLATION": "Cancelación de venta",
    "RETURN": "Devolución",
    "RESTOCK": "Reabastecimiento",
    "ADJUSTMENT_IN": "Ajuste de entrada",
    "ADJUSTMENT_OUT": "Ajuste de salida",
    "WASTE": "Merma",
    "DAMAGE": "Daño",
    "INTERNAL_USE": "Uso interno",
    "PHYSICAL_COUNT": "Conteo físico",
    "IMPORT": "Importación",
}


def _current_context():
    from app.routes import current_user

    user = current_user()
    if not user:
        abort(401)
    membership = active_membership(user)
    if not membership:
        abort(403)
    return user, membership


def _translated_labels():
    return {
        "OPENING_BALANCE": gettext("Existencias registradas al comenzar"),
        "SALE": gettext("Venta"),
        "SALE_CANCELLATION": gettext("Venta cancelada"),
        "RETURN": gettext("Producto devuelto"),
        "RESTOCK": gettext("Mercancía recibida"),
        "ADJUSTMENT_IN": gettext("Otra entrada"),
        "ADJUSTMENT_OUT": gettext("Otra salida"),
        "WASTE": gettext("Producto perdido o vencido"),
        "DAMAGE": gettext("Producto dañado"),
        "INTERNAL_USE": gettext("Uso interno"),
        "PHYSICAL_COUNT": gettext("Conteo físico diferente"),
        "IMPORT": gettext("Productos importados"),
    }


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _movement_query(organization_id, timezone_name):
    query = InventoryMovement.query.filter_by(organization_id=organization_id)
    product_id = request.args.get("product_id", type=int)
    movement_type = request.args.get("type", "").strip()
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))
    if product_id:
        query = query.filter(InventoryMovement.product_id == product_id)
    if movement_type in MOVEMENT_LABELS:
        query = query.filter(InventoryMovement.movement_type == movement_type)
    if date_from:
        start_local = datetime.combine(
            date_from, time.min, tzinfo=ZoneInfo(timezone_name)
        )
        query = query.filter(
            InventoryMovement.created_at
            >= start_local.astimezone(timezone.utc).replace(tzinfo=None)
        )
    if date_to:
        end_local = datetime.combine(
            date_to + timedelta(days=1),
            time.min,
            tzinfo=ZoneInfo(timezone_name),
        )
        query = query.filter(
            InventoryMovement.created_at
            < end_local.astimezone(timezone.utc).replace(tzinfo=None)
        )
    return query, {
        "product_id": product_id,
        "movement_type": movement_type,
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
    }


@inventory.route("/kardex")
@require_permission("view_inventory_history")
def index():
    user, membership = _current_context()
    timezone_name = safe_timezone_name(membership.organization.timezone)
    query, filters = _movement_query(
        membership.organization_id, timezone_name
    )
    movements = (
        query.options(
            selectinload(InventoryMovement.product),
            selectinload(InventoryMovement.performed_by_member).selectinload(
                OrganizationMember.user
            ),
            selectinload(InventoryMovement.sales_ticket),
        )
        .order_by(InventoryMovement.created_at.desc(), InventoryMovement.id.desc())
        .limit(500)
        .all()
    )
    for movement in movements:
        movement.local_created_at = utc_to_local(
            movement.created_at, timezone_name
        )
    products = Product.query.filter_by(
        organization_id=membership.organization_id,
    ).order_by(Product.name).all()
    consistency = stock_consistency(membership.organization_id)
    return render_template(
        "inventory_kardex.html",
        user=user,
        movements=movements,
        products=products,
        filters=filters,
        movement_labels=_translated_labels(),
        inconsistencies=[item for item in consistency if not item.is_consistent],
        timezone_name=timezone_name,
        correction_mode=request.args.get("correct") == "1",
    )


@inventory.route("/kardex/export.csv")
@require_permission("view_inventory_history")
def export_csv():
    _, membership = _current_context()
    timezone_name = safe_timezone_name(membership.organization.timezone)
    query, _ = _movement_query(
        membership.organization_id, timezone_name
    )
    movements = (
        query.options(
            selectinload(InventoryMovement.performed_by_member).selectinload(
                OrganizationMember.user
            ),
            selectinload(InventoryMovement.sales_ticket),
        )
        .order_by(InventoryMovement.created_at, InventoryMovement.id)
        .all()
    )
    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(
        [
            gettext("Fecha local"),
            gettext("Producto"),
            gettext("SKU"),
            gettext("Movimiento"),
            gettext("Cantidad"),
            gettext("Stock anterior"),
            gettext("Stock posterior"),
            gettext("Responsable"),
            gettext("Motivo"),
            gettext("Ticket"),
        ]
    )
    labels = _translated_labels()
    for movement in movements:
        member = movement.performed_by_member
        responsible = member.user.email if member and member.user else ""
        folio = movement.sales_ticket.folio if movement.sales_ticket else ""
        writer.writerow(
            [
                utc_to_local(
                    movement.created_at, timezone_name
                ).isoformat(sep=" "),
                _csv_safe(movement.product_name),
                _csv_safe(movement.product_sku),
                labels[movement.movement_type],
                movement.quantity_delta,
                movement.stock_before,
                movement.stock_after,
                _csv_safe(responsible),
                _csv_safe(movement.reason or ""),
                folio,
            ]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": "attachment; filename=historial-inventario-patia.csv"
        },
    )


def _csv_safe(value):
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


@inventory.route("/products/<int:product_id>/adjust", methods=["POST"])
@require_permission("make_inventory_adjustments")
def adjust(product_id):
    _, membership = _current_context()
    movement_type = request.form.get("movement_type", "").strip()
    reason = request.form.get("reason", "").strip()
    if movement_type not in MANUAL_MOVEMENT_TYPES or not reason:
        flash(gettext("Selecciona qué ocurrió y cuéntanos el motivo."), "danger")
        return redirect(url_for("inventory.index", product_id=product_id, correct=1))
    try:
        quantity = int(request.form.get("quantity", ""))
    except (TypeError, ValueError):
        quantity = -1
    if quantity < 0 or (movement_type != "PHYSICAL_COUNT" and quantity == 0):
        flash(gettext("Ingresa una cantidad válida."), "danger")
        return redirect(url_for("inventory.index", product_id=product_id, correct=1))

    product = (
        Product.query.filter_by(
            id=product_id,
            organization_id=membership.organization_id,
            is_active=True,
        )
        .with_for_update()
        .first_or_404()
    )
    try:
        if movement_type == "PHYSICAL_COUNT":
            change_product_stock(
                product,
                membership,
                movement_type,
                target_stock=quantity,
                reason=reason,
            )
        else:
            sign = 1 if movement_type == "ADJUSTMENT_IN" else -1
            change_product_stock(
                product,
                membership,
                movement_type,
                delta=sign * quantity,
                reason=reason,
            )
        db.session.commit()
    except ValueError:
        db.session.rollback()
        flash(gettext("Ese cambio dejaría existencias negativas."), "danger")
        return redirect(url_for("inventory.index", product_id=product_id, correct=1))

    flash(gettext("Existencias actualizadas correctamente."), "success")
    return redirect(url_for("inventory.index", product_id=product_id))
