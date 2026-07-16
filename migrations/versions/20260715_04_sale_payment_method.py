"""Persist the payment method used by each grouped sale.

Revision ID: 20260715_04
Revises: 20260715_03
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_04"
down_revision = "20260715_03"
branch_labels = None
depends_on = None


def _sale_columns():
    inspector = sa.inspect(op.get_bind())
    if "sale" not in inspector.get_table_names():
        raise RuntimeError("Cannot add payment method: sale table does not exist")
    return {column["name"] for column in inspector.get_columns("sale")}


def upgrade():
    if "payment_method" not in _sale_columns():
        op.add_column(
            "sale",
            sa.Column("payment_method", sa.String(20), nullable=True),
        )


def downgrade():
    if "payment_method" in _sale_columns():
        op.drop_column("sale", "payment_method")
