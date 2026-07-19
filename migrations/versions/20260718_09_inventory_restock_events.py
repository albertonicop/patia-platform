"""Add lightweight inventory restock events.

Revision ID: 20260718_09
Revises: 20260717_08
Create Date: 2026-07-18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260718_09"
down_revision = "20260717_08"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "inventory_restock_event" in inspector.get_table_names():
        return

    op.create_table(
        "inventory_restock_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("stock_before", sa.Integer(), nullable=False),
        sa.Column("stock_after", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "quantity > 0",
            name="ck_restock_quantity_positive",
        ),
        sa.CheckConstraint(
            "stock_before >= 0",
            name="ck_restock_stock_before_nonnegative",
        ),
        sa.CheckConstraint(
            "stock_after >= stock_before",
            name="ck_restock_stock_after_valid",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_inventory_restock_event_user_id",
        "inventory_restock_event",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_inventory_restock_event_product_id",
        "inventory_restock_event",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        "ix_restock_user_created_at",
        "inventory_restock_event",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_restock_product_created_at",
        "inventory_restock_event",
        ["product_id", "created_at"],
        unique=False,
    )


def downgrade():
    # Non-destructive by policy: retain the audit trail and its data.
    pass
