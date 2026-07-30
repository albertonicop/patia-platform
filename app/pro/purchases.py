"""Tenant-scoped purchase suggestions and supplier order workflow."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from math import ceil

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.inventory.services import change_product_stock
from app.models import (
    InventoryRestockEvent,
    Organization,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseReceipt,
    PurchaseReceiptItem,
    Sale,
    Supplier,
)
from app.money import money_decimal


SUGGESTION_WINDOW_DAYS = 30
NO_MOVEMENT_DAYS = 90
TARGET_COVERAGE_DAYS = 21


def purchase_suggestions(organization_id: int, *, now=None):
    """Return actionable stock suggestions with three bounded queries."""
    now = now or datetime.utcnow()
    recent_start = now - timedelta(days=SUGGESTION_WINDOW_DAYS)
    movement_start = now - timedelta(days=NO_MOVEMENT_DAYS)

    recent = (
        db.session.query(
            Sale.product_id.label("product_id"),
            func.coalesce(func.sum(Sale.quantity), 0).label("units"),
        )
        .filter(
            Sale.organization_id == organization_id,
            Sale.created_at >= recent_start,
        )
        .group_by(Sale.product_id)
        .subquery()
    )
    movement = (
        db.session.query(
            Sale.product_id.label("product_id"),
            func.max(Sale.created_at).label("last_sale_at"),
        )
        .filter(Sale.organization_id == organization_id)
        .group_by(Sale.product_id)
        .subquery()
    )
    rows = (
        db.session.query(
            Product,
            func.coalesce(recent.c.units, 0).label("recent_units"),
            movement.c.last_sale_at,
        )
        .outerjoin(recent, recent.c.product_id == Product.id)
        .outerjoin(movement, movement.c.product_id == Product.id)
        .filter(
            Product.organization_id == organization_id,
            Product.is_active.is_(True),
        )
        .order_by(Product.supplier, Product.name)
        .all()
    )
    supplier_names = {
        row.Product.supplier.strip()
        for row in rows
        if row.Product.supplier and row.Product.supplier.strip()
    }
    supplier_ids = {
        supplier.name: supplier.id
        for supplier in Supplier.query.filter(
            Supplier.organization_id == organization_id,
            Supplier.name.in_(supplier_names),
        ).all()
    } if supplier_names else {}

    suggestions = []
    no_movement = []
    for row in rows:
        product = row.Product
        recent_units = int(row.recent_units or 0)
        daily_velocity = Decimal(recent_units) / Decimal(
            SUGGESTION_WINDOW_DAYS
        )
        coverage_days = (
            round(Decimal(product.stock) / daily_velocity, 1)
            if daily_velocity > 0
            else None
        )
        target_stock = max(
            int(product.min_stock) * 2,
            ceil(daily_velocity * TARGET_COVERAGE_DAYS),
            1 if int(product.stock) <= 0 else 0,
        )
        suggested_quantity = max(target_stock - int(product.stock), 0)
        needs_restock = (
            int(product.stock) <= int(product.min_stock)
            or (
                coverage_days is not None
                and coverage_days <= Decimal("14")
            )
        )
        supplier_name = (
            product.supplier.strip()
            if product.supplier and product.supplier.strip()
            else None
        )
        item = {
            "product_id": product.id,
            "name": product.name,
            "sku": product.sku,
            "supplier_name": supplier_name,
            "supplier_id": supplier_ids.get(supplier_name),
            "stock": int(product.stock),
            "min_stock": int(product.min_stock),
            "recent_units": recent_units,
            "daily_velocity": round(daily_velocity, 2),
            "coverage_days": coverage_days,
            "suggested_quantity": suggested_quantity,
            "unit_cost": money_decimal(product.cost_price),
            "estimated_cost": money_decimal(
                product.cost_price * suggested_quantity
            ),
        }
        if needs_restock and suggested_quantity > 0:
            suggestions.append(item)
        if (
            row.last_sale_at is None
            or row.last_sale_at < movement_start
        ):
            no_movement.append(
                {
                    **item,
                    "last_sale_at": row.last_sale_at,
                }
            )

    groups = {}
    for item in suggestions:
        key = item["supplier_name"] or ""
        group = groups.setdefault(
            key,
            {
                "supplier_name": item["supplier_name"],
                "supplier_id": item["supplier_id"],
                "items": [],
                "units": 0,
                "estimated_cost": Decimal("0.00"),
            },
        )
        group["items"].append(item)
        group["units"] += item["suggested_quantity"]
        group["estimated_cost"] += item["estimated_cost"]
    grouped = sorted(
        groups.values(),
        key=lambda group: (
            group["supplier_name"] is None,
            (group["supplier_name"] or "").casefold(),
        ),
    )
    for group in grouped:
        group["estimated_cost"] = money_decimal(
            group["estimated_cost"]
        )
    return {
        "groups": grouped,
        "suggestions": suggestions,
        "no_movement": no_movement,
        "summary": {
            "products": len(suggestions),
            "units": sum(
                item["suggested_quantity"] for item in suggestions
            ),
            "estimated_cost": money_decimal(
                sum(
                    (
                        item["estimated_cost"]
                        for item in suggestions
                    ),
                    Decimal("0.00"),
                )
            ),
            "without_movement": len(no_movement),
        },
        "method": {
            "sales_window_days": SUGGESTION_WINDOW_DAYS,
            "target_coverage_days": TARGET_COVERAGE_DAYS,
            "no_movement_days": NO_MOVEMENT_DAYS,
        },
    }


def _next_order_number(organization_id: int):
    organization = (
        Organization.query.filter_by(id=organization_id)
        .with_for_update()
        .one()
    )
    sequence = int(organization.next_purchase_order_number or 1)
    organization.next_purchase_order_number = sequence + 1
    return f"PED-{sequence:06d}"


def create_purchase_draft(
    membership,
    product_quantities: dict[int, int],
    *,
    supplier_name: str | None = None,
    notes: str | None = None,
):
    """Create one draft atomically from explicitly selected products."""
    clean = {
        int(product_id): int(quantity)
        for product_id, quantity in product_quantities.items()
        if int(quantity) > 0
    }
    if not clean:
        raise ValueError("At least one product is required.")
    products = Product.query.filter(
        Product.organization_id == membership.organization_id,
        Product.is_active.is_(True),
        Product.id.in_(clean),
    ).order_by(Product.name).all()
    if len(products) != len(clean):
        raise ValueError("One or more products are unavailable.")
    normalized_supplier = (supplier_name or "").strip() or None
    if normalized_supplier:
        invalid = [
            product
            for product in products
            if (product.supplier or "").strip() != normalized_supplier
        ]
        if invalid:
            raise ValueError("Products do not belong to that supplier.")
    supplier = (
        Supplier.query.filter_by(
            organization_id=membership.organization_id,
            name=normalized_supplier,
        ).first()
        if normalized_supplier
        else None
    )
    order = PurchaseOrder(
        organization_id=membership.organization_id,
        number=_next_order_number(membership.organization_id),
        supplier_id=supplier.id if supplier else None,
        supplier_name=normalized_supplier or "",
        status="DRAFT",
        notes=(notes or "").strip()[:1000] or None,
        created_by_member_id=membership.id,
    )
    db.session.add(order)
    db.session.flush()
    for product in products:
        db.session.add(
            PurchaseOrderItem(
                purchase_order_id=order.id,
                product_id=product.id,
                product_name=product.name,
                product_sku=product.sku,
                ordered_quantity=clean[product.id],
                received_quantity=0,
                unit_cost=product.cost_price,
            )
        )
    db.session.commit()
    return order


def purchase_order_query(organization_id: int):
    return PurchaseOrder.query.options(
        selectinload(PurchaseOrder.items).selectinload(
            PurchaseOrderItem.product
        ),
        selectinload(PurchaseOrder.receipts).selectinload(
            PurchaseReceipt.items
        ),
    ).filter(PurchaseOrder.organization_id == organization_id)


def update_purchase_draft(
    order: PurchaseOrder,
    quantities: dict[int, int],
    *,
    notes=None,
):
    if order.status != "DRAFT":
        raise ValueError("Only draft orders can be edited.")
    for item in order.items:
        quantity = int(quantities.get(item.id, item.ordered_quantity))
        if quantity <= 0:
            raise ValueError("Ordered quantities must be positive.")
        item.ordered_quantity = quantity
    order.notes = (notes or "").strip()[:1000] or None
    db.session.commit()
    return order


def confirm_purchase_order(order: PurchaseOrder):
    if order.status != "DRAFT" or not order.items:
        raise ValueError("Only a complete draft can be confirmed.")
    order.status = "ORDERED"
    order.ordered_at = datetime.utcnow()
    db.session.commit()
    return order


def cancel_purchase_order(order: PurchaseOrder):
    """Cancel only the outstanding purchase; received stock remains untouched."""
    if order.status not in {"DRAFT", "ORDERED", "PARTIALLY_RECEIVED"}:
        raise ValueError("order_not_cancellable")
    order.status = "CANCELLED"
    order.updated_at = datetime.utcnow()
    db.session.commit()
    return order


def receive_purchase_order(
    order: PurchaseOrder,
    membership,
    quantities: dict[int, int],
    *,
    request_id: str,
):
    """Receive selected units once and append Kardex restocks atomically."""
    request_id = (request_id or "").strip()
    if not request_id or len(request_id) > 64:
        raise ValueError("A valid request identifier is required.")
    existing = PurchaseReceipt.query.filter_by(
        organization_id=membership.organization_id,
        request_id=request_id,
    ).first()
    if existing:
        return existing, False
    if (
        order.organization_id != membership.organization_id
        or order.status not in {"ORDERED", "PARTIALLY_RECEIVED"}
    ):
        raise ValueError("This order cannot be received.")
    order = (
        PurchaseOrder.query.filter_by(
            id=order.id,
            organization_id=membership.organization_id,
        )
        .populate_existing()
        .with_for_update()
        .one()
    )
    if order.status not in {"ORDERED", "PARTIALLY_RECEIVED"}:
        raise ValueError("This order cannot be received.")
    existing = PurchaseReceipt.query.filter_by(
        organization_id=membership.organization_id,
        request_id=request_id,
    ).first()
    if existing:
        return existing, False
    locked_items = (
        PurchaseOrderItem.query.filter_by(
            purchase_order_id=order.id
        )
        .order_by(PurchaseOrderItem.id)
        .with_for_update()
        .populate_existing()
        .all()
    )
    selected = []
    for item in locked_items:
        quantity = int(quantities.get(item.id, 0) or 0)
        if quantity < 0 or quantity > item.pending_quantity:
            raise ValueError("Received quantity exceeds the pending units.")
        if quantity:
            selected.append((item, quantity))
    if not selected:
        raise ValueError("Receive at least one unit.")
    products = {
        product.id: product
        for product in Product.query.filter(
            Product.organization_id == membership.organization_id,
            Product.id.in_(
                [item.product_id for item, _quantity in selected]
            ),
        )
        .with_for_update()
        .all()
    }
    if len(products) != len(selected):
        raise ValueError("One or more products are unavailable.")
    receipt = PurchaseReceipt(
        organization_id=membership.organization_id,
        purchase_order_id=order.id,
        received_by_member_id=membership.id,
        request_id=request_id,
    )
    db.session.add(receipt)
    db.session.flush()
    for item, quantity in selected:
        product = products[item.product_id]
        stock_before = int(product.stock)
        restock = InventoryRestockEvent(
            organization_id=membership.organization_id,
            user_id=membership.user_id,
            product_id=product.id,
            quantity=quantity,
            stock_before=stock_before,
            stock_after=stock_before + quantity,
        )
        db.session.add(restock)
        db.session.flush()
        change_product_stock(
            product,
            membership,
            "RESTOCK",
            delta=quantity,
            reason=f"Recepción de pedido {order.number}",
            restock_event=restock,
        )
        item.received_quantity += quantity
        db.session.add(
            PurchaseReceiptItem(
                purchase_receipt_id=receipt.id,
                purchase_order_item_id=item.id,
                quantity=quantity,
                unit_cost=item.unit_cost,
                restock_event_id=restock.id,
            )
        )
    if all(item.pending_quantity == 0 for item in locked_items):
        order.status = "RECEIVED"
        order.completed_at = datetime.utcnow()
    else:
        order.status = "PARTIALLY_RECEIVED"
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = PurchaseReceipt.query.filter_by(
            organization_id=membership.organization_id,
            request_id=request_id,
        ).first()
        if existing:
            return existing, False
        raise
    return receipt, True
