from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func

from app import db
from app.models import InventoryMovement, Product
from app.units import quantity_decimal


MANUAL_MOVEMENT_TYPES = frozenset(
    {
        "ADJUSTMENT_IN",
        "ADJUSTMENT_OUT",
        "WASTE",
        "DAMAGE",
        "INTERNAL_USE",
        "PHYSICAL_COUNT",
    }
)


def record_inventory_movement(
    product: Product,
    membership,
    movement_type: str,
    stock_before,
    stock_after,
    *,
    reason: str | None = None,
    sale=None,
    sales_ticket=None,
    restock_event=None,
) -> InventoryMovement:
    """Append one immutable movement without committing the transaction."""
    if product.organization_id != membership.organization_id:
        raise ValueError("Product does not belong to the active organization.")
    stock_before = quantity_decimal(stock_before)
    stock_after = quantity_decimal(stock_after)
    if stock_before < 0 or stock_after < 0:
        raise ValueError("Stock cannot be negative.")
    movement = InventoryMovement(
        organization_id=membership.organization_id,
        product_id=product.id,
        performed_by_member_id=membership.id,
        movement_type=movement_type,
        quantity_delta=stock_after - stock_before,
        stock_before=stock_before,
        stock_after=stock_after,
        reason=(reason or "").strip()[:255] or None,
        product_name=product.name,
        product_sku=product.sku,
        sale_id=sale.id if sale is not None else None,
        sales_ticket_id=(
            sales_ticket.id if sales_ticket is not None else None
        ),
        restock_event_id=(
            restock_event.id if restock_event is not None else None
        ),
    )
    db.session.add(movement)
    return movement


def change_product_stock(
    product: Product,
    membership,
    movement_type: str,
    *,
    delta=None,
    target_stock=None,
    reason: str | None = None,
    sale=None,
    sales_ticket=None,
    restock_event=None,
) -> InventoryMovement:
    """Change stock and append its ledger entry in the current transaction."""
    if (delta is None) == (target_stock is None):
        raise ValueError("Provide either delta or target_stock.")
    before = quantity_decimal(product.stock)
    after = (
        quantity_decimal(target_stock)
        if target_stock is not None
        else before + quantity_decimal(delta, allow_negative=True)
    )
    if after < 0:
        raise ValueError("Stock cannot be negative.")
    product.stock = after
    return record_inventory_movement(
        product,
        membership,
        movement_type,
        before,
        after,
        reason=reason,
        sale=sale,
        sales_ticket=sales_ticket,
        restock_event=restock_event,
    )


def record_opening_balance(
    product: Product,
    membership,
    *,
    reason: str | None = None,
) -> InventoryMovement:
    db.session.flush()
    return record_inventory_movement(
        product,
        membership,
        "OPENING_BALANCE",
        0,
        quantity_decimal(product.stock),
        reason=reason,
    )


@dataclass(frozen=True)
class StockConsistency:
    product_id: int
    product_name: str
    current_stock: object
    ledger_stock: object
    continuity_errors: int = 0

    @property
    def difference(self):
        return self.current_stock - self.ledger_stock

    @property
    def is_consistent(self) -> bool:
        return self.difference == 0 and self.continuity_errors == 0


def stock_consistency(organization_id: int) -> list[StockConsistency]:
    totals = (
        db.session.query(
            InventoryMovement.product_id.label("product_id"),
            func.coalesce(func.sum(InventoryMovement.quantity_delta), 0).label(
                "ledger_stock"
            ),
        )
        .filter(
            InventoryMovement.organization_id == organization_id,
            InventoryMovement.product_id.isnot(None),
        )
        .group_by(InventoryMovement.product_id)
        .subquery()
    )
    ordered = (
        db.session.query(
            InventoryMovement.product_id.label("product_id"),
            InventoryMovement.stock_before.label("stock_before"),
            func.lag(InventoryMovement.stock_after)
            .over(
                partition_by=InventoryMovement.product_id,
                order_by=(
                    InventoryMovement.created_at,
                    InventoryMovement.id,
                ),
            )
            .label("previous_after"),
        )
        .filter(
            InventoryMovement.organization_id == organization_id,
            InventoryMovement.product_id.isnot(None),
        )
        .subquery()
    )
    gaps = (
        db.session.query(
            ordered.c.product_id,
            func.count().label("continuity_errors"),
        )
        .filter(
            ordered.c.stock_before
            != func.coalesce(ordered.c.previous_after, 0)
        )
        .group_by(ordered.c.product_id)
        .subquery()
    )
    rows = (
        db.session.query(
            Product.id,
            Product.name,
            Product.stock,
            func.coalesce(totals.c.ledger_stock, 0),
            func.coalesce(gaps.c.continuity_errors, 0),
        )
        .outerjoin(totals, totals.c.product_id == Product.id)
        .outerjoin(gaps, gaps.c.product_id == Product.id)
        .filter(Product.organization_id == organization_id)
        .order_by(Product.name)
        .all()
    )
    return [
        StockConsistency(
            product_id=row[0],
            product_name=row[1],
            current_stock=quantity_decimal(row[2]),
            ledger_stock=quantity_decimal(row[3]),
            continuity_errors=int(row[4]),
        )
        for row in rows
    ]
