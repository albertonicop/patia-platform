from __future__ import annotations

import csv
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_babel import gettext, ngettext
from sqlalchemy.orm import selectinload

from app import db
from app.models import (
    InventoryMovement,
    OrganizationMember,
    Product,
)
from app.plans import has_entitlement
from app.team.services import active_membership, require_permission
from app.timezones import safe_timezone_name, utc_to_local

from .services import change_product_stock, stock_consistency


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
        "RETURN": gettext("Devolución"),
        "RESTOCK": gettext("Mercancía recibida"),
        "ADJUSTMENT_IN": gettext("Mercancía recibida"),
        "ADJUSTMENT_OUT": gettext("Existencias retiradas"),
        "WASTE": gettext("Producto vencido"),
        "DAMAGE": gettext("Producto dañado"),
        "INTERNAL_USE": gettext("Uso interno"),
        "PHYSICAL_COUNT": gettext("Corrección por conteo"),
        "IMPORT": gettext("Productos importados"),
    }


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _movement_query(
    organization_id, timezone_name, *, advanced_history=True
):
    query = InventoryMovement.query.filter_by(organization_id=organization_id)
    product_id = request.args.get("product_id", type=int)
    movement_type = (
        request.args.get("type", "").strip() if advanced_history else ""
    )
    movement_group = (
        request.args.get("group", "").strip() if advanced_history else ""
    )
    date_from = (
        _parse_date(request.args.get("date_from"))
        if advanced_history
        else None
    )
    date_to = (
        _parse_date(request.args.get("date_to"))
        if advanced_history
        else None
    )
    if product_id:
        query = query.filter(InventoryMovement.product_id == product_id)
    if movement_type in MOVEMENT_LABELS:
        query = query.filter(InventoryMovement.movement_type == movement_type)
    elif movement_group == "corrections":
        query = query.filter(
            InventoryMovement.movement_type.in_(
                ("ADJUSTMENT_IN", "ADJUSTMENT_OUT", "PHYSICAL_COUNT")
            )
        )
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
    if not advanced_history:
        query = query.filter(
            InventoryMovement.created_at
            >= datetime.utcnow() - timedelta(days=90)
        )
    return query, {
        "product_id": product_id,
        "movement_type": movement_type,
        "movement_group": movement_group,
        "source": request.args.get("source", "") if advanced_history else "",
        "date_from": (
            request.args.get("date_from", "") if advanced_history else ""
        ),
        "date_to": (
            request.args.get("date_to", "") if advanced_history else ""
        ),
    }


@inventory.route("/kardex")
@require_permission("view_inventory_history")
def index():
    user, membership = _current_context()
    timezone_name = safe_timezone_name(membership.organization.timezone)
    advanced_history = has_entitlement(
        membership.organization.owner, "advanced_inventory_history"
    )
    query, filters = _movement_query(
        membership.organization_id,
        timezone_name,
        advanced_history=advanced_history,
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
        .limit(500 if advanced_history else 100)
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
    selected_product = next(
        (
            product
            for product in products
            if product.id == filters["product_id"]
        ),
        None,
    )
    labels = _translated_labels()
    for movement in movements:
        movement.human_summary = _human_movement_summary(
            movement, labels
        )
    return render_template(
        "inventory_kardex.html",
        user=user,
        movements=movements,
        products=products,
        filters=filters,
        movement_labels=labels,
        inconsistencies=[item for item in consistency if not item.is_consistent],
        timezone_name=timezone_name,
        selected_product=selected_product,
        advanced_history=advanced_history,
    )


@inventory.route("/kardex/export.csv")
@require_permission("view_inventory_history")
def export_csv():
    _, membership = _current_context()
    advanced_export = has_entitlement(
        membership.organization.owner, "advanced_exports"
    )
    timezone_name = safe_timezone_name(membership.organization.timezone)
    query, _ = _movement_query(
        membership.organization_id,
        timezone_name,
        advanced_history=advanced_export,
    )
    movements = (
        query.options(
            selectinload(InventoryMovement.performed_by_member).selectinload(
                OrganizationMember.user
            ),
            selectinload(InventoryMovement.sales_ticket),
        )
        .order_by(InventoryMovement.created_at, InventoryMovement.id)
        .limit(None if advanced_export else 100)
        .all()
    )
    output = StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    columns = [
        gettext("Fecha local"),
        gettext("Producto"),
        gettext("SKU"),
        gettext("Movimiento"),
        gettext("Cantidad"),
        gettext("Stock anterior"),
        gettext("Stock posterior"),
    ]
    if advanced_export:
        columns.extend(
            [
                gettext("Responsable"),
                gettext("Motivo"),
                gettext("Ticket"),
            ]
        )
    writer.writerow(columns)
    labels = _translated_labels()
    for movement in movements:
        member = movement.performed_by_member
        responsible = member.user.email if member and member.user else ""
        folio = movement.sales_ticket.folio if movement.sales_ticket else ""
        row = [
            utc_to_local(
                movement.created_at, timezone_name
            ).isoformat(sep=" "),
            _csv_safe(movement.product_name),
            _csv_safe(movement.product_sku),
            labels[movement.movement_type],
            movement.quantity_delta,
            movement.stock_before,
            movement.stock_after,
        ]
        if advanced_export:
            row.extend(
                [
                    _csv_safe(responsible),
                    _csv_safe(movement.reason or ""),
                    folio,
                ]
            )
        writer.writerow(row)
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


def _human_movement_summary(movement, labels):
    quantity = abs(int(movement.quantity_delta))
    if movement.movement_type == "SALE":
        action = ngettext(
            "Se vendió %(count)s unidad.",
            "Se vendieron %(count)s unidades.",
            quantity,
            count=quantity,
        )
        result = gettext(
            "Quedaron %(count)s.", count=movement.stock_after
        )
    elif movement.movement_type == "PHYSICAL_COUNT":
        action = gettext(
            "Se corrigió el conteo de %(before)s a %(after)s.",
            before=movement.stock_before,
            after=movement.stock_after,
        )
        result = (
            gettext("Motivo: %(reason)s.", reason=movement.reason)
            if movement.reason
            else ""
        )
    elif movement.quantity_delta > 0:
        action = ngettext(
            "Se recibió %(count)s unidad.",
            "Se recibieron %(count)s unidades.",
            quantity,
            count=quantity,
        )
        result = gettext(
            "Ahora hay %(count)s.", count=movement.stock_after
        )
    else:
        action = ngettext(
            "Salió %(count)s unidad.",
            "Salieron %(count)s unidades.",
            quantity,
            count=quantity,
        )
        result = gettext(
            "Ahora hay %(count)s.", count=movement.stock_after
        )
    return {"action": action, "result": result, "label": labels[movement.movement_type]}


@inventory.get("/products/<int:product_id>/adjust")
@require_permission("make_inventory_adjustments")
def adjust_form(product_id):
    _, membership = _current_context()
    product = Product.query.filter_by(
        id=product_id,
        organization_id=membership.organization_id,
        is_active=True,
    ).first_or_404()
    mode = request.args.get("mode", "count")
    if mode not in {"count", "receive", "loss"}:
        mode = "count"
    return render_template(
        "inventory_adjust.html",
        product=product,
        mode=mode,
    )


@inventory.post("/products/<int:product_id>/adjust")
@require_permission("make_inventory_adjustments")
def adjust(product_id):
    _, membership = _current_context()
    legacy_type = request.form.get("movement_type", "").strip()
    legacy_map = {
        "PHYSICAL_COUNT": ("count", "physical"),
        "ADJUSTMENT_IN": ("receive", "received"),
        "ADJUSTMENT_OUT": ("loss", "other"),
        "DAMAGE": ("loss", "damage"),
        "WASTE": ("loss", "expired"),
        "INTERNAL_USE": ("loss", "internal"),
    }
    if legacy_type in legacy_map:
        mode, reason_code = legacy_map[legacy_type]
        note = request.form.get("reason", "").strip()
    else:
        mode = request.form.get("mode", "count").strip()
        reason_code = request.form.get("reason_code", "").strip()
        note = request.form.get("note", "").strip()
    reason_labels = {
        "physical": gettext("Conteo físico diferente"),
        "damage": gettext("Producto dañado"),
        "expired": gettext("Producto vencido"),
        "lost": gettext("Producto perdido"),
        "internal": gettext("Uso interno"),
        "received": gettext("Mercancía recibida"),
        "other": gettext("Otra razón"),
    }
    allowed_reasons = {
        "count": set(reason_labels),
        "receive": {"received", "other"},
        "loss": {"damage", "expired", "lost", "internal", "other"},
    }
    if (
        mode not in allowed_reasons
        or reason_code not in allowed_reasons[mode]
    ):
        flash(gettext("Selecciona por qué cambió el inventario."), "danger")
        return redirect(
            url_for(
                "inventory.adjust_form",
                product_id=product_id,
                mode=mode,
            )
        )
    reason = reason_labels[reason_code]
    if note:
        reason = f"{reason}: {note[:180]}"
    try:
        quantity = int(request.form.get("quantity", ""))
    except (TypeError, ValueError):
        quantity = -1
    if quantity < 0 or (mode != "count" and quantity == 0):
        flash(gettext("Ingresa una cantidad válida."), "danger")
        return redirect(
            url_for(
                "inventory.adjust_form",
                product_id=product_id,
                mode=mode,
            )
        )

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
        if mode == "count":
            change_product_stock(
                product,
                membership,
                "PHYSICAL_COUNT",
                target_stock=quantity,
                reason=reason,
            )
        elif mode == "receive":
            movement_type = (
                "ADJUSTMENT_IN"
                if legacy_type == "ADJUSTMENT_IN"
                else "RESTOCK"
            )
            change_product_stock(
                product,
                membership,
                movement_type,
                delta=quantity,
                reason=reason,
            )
        elif mode == "loss":
            movement_type = {
                "damage": "DAMAGE",
                "expired": "WASTE",
                "lost": "ADJUSTMENT_OUT",
                "internal": "INTERNAL_USE",
                "other": "ADJUSTMENT_OUT",
            }.get(reason_code, "ADJUSTMENT_OUT")
            change_product_stock(
                product,
                membership,
                movement_type,
                delta=-quantity,
                reason=reason,
            )
        else:
            raise ValueError("unsupported adjustment mode")
        db.session.commit()
    except ValueError:
        db.session.rollback()
        flash(gettext("Ese cambio dejaría existencias negativas."), "danger")
        return redirect(
            url_for(
                "inventory.adjust_form",
                product_id=product_id,
                mode=mode,
            )
        )

    flash(gettext("Existencias actualizadas correctamente."), "success")
    return redirect(url_for("inventory.index", product_id=product_id))
