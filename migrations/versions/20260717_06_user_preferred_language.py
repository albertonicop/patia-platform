"""Add the user's preferred interface language.

Revision ID: 20260717_06
Revises: 20260715_05
Create Date: 2026-07-17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_06"
down_revision = "20260715_05"
branch_labels = None
depends_on = None


def _user_columns():
    inspector = sa.inspect(op.get_bind())
    if "user" not in inspector.get_table_names():
        raise RuntimeError("Cannot add preferred language: user table does not exist")
    return {column["name"] for column in inspector.get_columns("user")}


def upgrade():
    if "preferred_language" not in _user_columns():
        op.add_column(
            "user",
            sa.Column(
                "preferred_language",
                sa.String(length=5),
                nullable=False,
                server_default="es",
            ),
        )


def downgrade():
    if "preferred_language" in _user_columns():
        op.drop_column("user", "preferred_language")
