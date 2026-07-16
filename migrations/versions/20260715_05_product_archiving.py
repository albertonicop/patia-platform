"""Archive sold products without deleting historical sales.

Revision ID: 20260715_05
Revises: 20260715_04
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_05"
down_revision = "20260715_04"
branch_labels = None
depends_on = None


def _product_columns():
    inspector = sa.inspect(op.get_bind())
    if "product" not in inspector.get_table_names():
        raise RuntimeError("Cannot add product archiving: product table does not exist")
    return {column["name"] for column in inspector.get_columns("product")}


def upgrade():
    if "is_active" not in _product_columns():
        op.add_column(
            "product",
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )


def downgrade():
    if "is_active" in _product_columns():
        op.drop_column("product", "is_active")
