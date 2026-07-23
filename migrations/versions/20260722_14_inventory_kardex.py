"""Add immutable inventory movement ledger.

Revision ID: 20260722_14
Revises: 20260722_13
"""

from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "20260722_14"
down_revision = "20260722_13"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {
        "organization",
        "organization_member",
        "product",
        "sale",
        "sales_ticket",
        "inventory_restock_event",
    }
    missing = sorted(required - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(
            "Kardex migration requires existing tables: " + ", ".join(missing)
        )

    op.create_table(
        "inventory_movement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("performed_by_member_id", sa.Integer(), nullable=True),
        sa.Column("sale_id", sa.Integer(), nullable=True),
        sa.Column("sales_ticket_id", sa.Integer(), nullable=True),
        sa.Column("restock_event_id", sa.Integer(), nullable=True),
        sa.Column("movement_type", sa.String(length=30), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column("stock_before", sa.Integer(), nullable=False),
        sa.Column("stock_after", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("product_name", sa.String(length=160), nullable=False),
        sa.Column("product_sku", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "movement_type IN "
            "('OPENING_BALANCE', 'SALE', 'SALE_CANCELLATION', 'RETURN', "
            "'RESTOCK', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT', 'WASTE', "
            "'DAMAGE', 'INTERNAL_USE', 'PHYSICAL_COUNT', 'IMPORT')",
            name="ck_inventory_movement_type",
        ),
        sa.CheckConstraint(
            "stock_before >= 0 AND stock_after >= 0",
            name="ck_inventory_movement_stock_nonnegative",
        ),
        sa.CheckConstraint(
            "quantity_delta = stock_after - stock_before",
            name="ck_inventory_movement_delta_matches_stock",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["performed_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["sale_id"], ["sale.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["sales_ticket_id"], ["sales_ticket.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["restock_event_id"],
            ["inventory_restock_event.id"],
            ondelete="SET NULL",
        ),
    )
    for column in (
        "organization_id",
        "product_id",
        "performed_by_member_id",
        "sale_id",
        "sales_ticket_id",
        "restock_event_id",
    ):
        op.create_index(
            f"ix_inventory_movement_{column}",
            "inventory_movement",
            [column],
        )
    op.create_index(
        "ix_inventory_movement_org_created",
        "inventory_movement",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_inventory_movement_product_created",
        "inventory_movement",
        ["product_id", "created_at", "id"],
    )
    op.create_index(
        "ix_inventory_movement_org_type_created",
        "inventory_movement",
        ["organization_id", "movement_type", "created_at"],
    )

    product_columns = {
        column["name"] for column in inspector.get_columns("product")
    }
    baseline_columns = {
        "id", "organization_id", "name", "sku", "stock", "created_at"
    }
    if not baseline_columns.issubset(product_columns):
        # Very early legacy schemas can be stamped past their historical
        # baseline without containing a complete product model. This migration
        # never invents stock: it creates the ledger and leaves those rows for
        # explicit reconciliation after their product schema is repaired.
        return

    product = sa.table(
        "product",
        sa.column("id", sa.Integer()),
        sa.column("organization_id", sa.Integer()),
        sa.column("name", sa.String()),
        sa.column("sku", sa.String()),
        sa.column("stock", sa.Integer()),
        sa.column("created_at", sa.DateTime()),
    )
    movement = sa.table(
        "inventory_movement",
        sa.column("organization_id", sa.Integer()),
        sa.column("product_id", sa.Integer()),
        sa.column("performed_by_member_id", sa.Integer()),
        sa.column("movement_type", sa.String()),
        sa.column("quantity_delta", sa.Integer()),
        sa.column("stock_before", sa.Integer()),
        sa.column("stock_after", sa.Integer()),
        sa.column("reason", sa.String()),
        sa.column("product_name", sa.String()),
        sa.column("product_sku", sa.String()),
        sa.column("created_at", sa.DateTime()),
    )
    now = datetime.utcnow()
    products = bind.execute(
        sa.select(
            product.c.id,
            product.c.organization_id,
            product.c.name,
            product.c.sku,
            product.c.stock,
            product.c.created_at,
        )
    ).mappings()
    rows = [
        {
            "organization_id": item["organization_id"],
            "product_id": item["id"],
            "performed_by_member_id": None,
            "movement_type": "OPENING_BALANCE",
            "quantity_delta": int(item["stock"] or 0),
            "stock_before": 0,
            "stock_after": int(item["stock"] or 0),
            "reason": "Saldo inicial migrado",
            "product_name": item["name"],
            "product_sku": item["sku"],
            "created_at": item["created_at"] or now,
        }
        for item in products
    ]
    if rows:
        op.bulk_insert(movement, rows)


def downgrade():
    op.drop_table("inventory_movement")
