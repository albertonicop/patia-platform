"""add persisted payment details to sales tickets

Revision ID: 20260730_24
Revises: 20260730_23
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_24"
down_revision = "20260730_23"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.add_column(
            sa.Column("amount_received", sa.Numeric(14, 2), nullable=True)
        )
        batch_op.add_column(
            sa.Column("change_amount", sa.Numeric(14, 2), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cashier_member_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_sales_ticket_cashier_member",
            "organization_member",
            ["cashier_member_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_sales_ticket_cashier_member_id",
            ["cashier_member_id"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.drop_index("ix_sales_ticket_cashier_member_id")
        batch_op.drop_constraint(
            "fk_sales_ticket_cashier_member",
            type_="foreignkey",
        )
        batch_op.drop_column("cashier_member_id")
        batch_op.drop_column("change_amount")
        batch_op.drop_column("amount_received")
