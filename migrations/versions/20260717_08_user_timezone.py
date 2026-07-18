"""Add the business timezone preference.

Revision ID: 20260717_08
Revises: 20260717_07
Create Date: 2026-07-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_08"
down_revision = "20260717_07"
branch_labels = None
depends_on = None


def _user_columns():
    inspector = sa.inspect(op.get_bind())
    if "user" not in inspector.get_table_names():
        raise RuntimeError("Cannot add timezone preference: user table is missing.")
    return {column["name"] for column in inspector.get_columns("user")}


def upgrade():
    if "timezone" not in _user_columns():
        op.add_column(
            "user",
            sa.Column(
                "timezone",
                sa.String(length=64),
                nullable=False,
                server_default="America/Mexico_City",
            ),
        )


def downgrade():
    # Non-destructive by policy: retain the preference and its data.
    pass
