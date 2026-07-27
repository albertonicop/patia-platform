"""add organization monthly sales goal

Revision ID: 20260726_21
Revises: 20260726_20
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_21"
down_revision = "20260726_20"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "organization" not in inspector.get_table_names():
        raise RuntimeError(
            "Cannot add the executive dashboard goal because organization is missing."
        )
    columns = {
        column["name"] for column in inspector.get_columns("organization")
    }
    if "monthly_sales_goal" not in columns:
        op.add_column(
            "organization",
            sa.Column(
                "monthly_sales_goal",
                sa.Numeric(precision=14, scale=2),
                nullable=True,
            ),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "organization" not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns("organization")
    }
    if "monthly_sales_goal" in columns:
        op.drop_column("organization", "monthly_sales_goal")
