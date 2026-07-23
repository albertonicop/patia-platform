"""Add customer credit and immutable accounts receivable ledger.

Revision ID: 20260723_16
Revises: 20260723_15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_16"
down_revision = "20260723_15"
branch_labels = None
depends_on = None
MONEY = sa.Numeric(14, 2)


def upgrade():
    inspector = sa.inspect(op.get_bind())
    required = {"customer", "organization_member", "sales_ticket", "cash_register_session", "cash_movement"}
    missing = sorted(required - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError("Credit migration requires existing tables: " + ", ".join(missing))
    with op.batch_alter_table("customer") as batch:
        batch.add_column(sa.Column("credit_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("credit_limit", MONEY, nullable=False, server_default="0.00"))
    op.create_table(
        "customer_credit_movement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("performed_by_member_id", sa.Integer(), nullable=True),
        sa.Column("authorized_by_member_id", sa.Integer(), nullable=True),
        sa.Column("sales_ticket_id", sa.Integer(), nullable=True),
        sa.Column("cash_register_session_id", sa.Integer(), nullable=True),
        sa.Column("movement_type", sa.String(20), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("balance_before", MONEY, nullable=False),
        sa.Column("balance_after", MONEY, nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("movement_type IN ('CHARGE', 'PAYMENT', 'REVERSAL')", name="ck_customer_credit_movement_type"),
        sa.CheckConstraint("amount > 0 AND balance_before >= 0 AND balance_after >= 0", name="ck_customer_credit_movement_amounts"),
        sa.CheckConstraint("(movement_type = 'CHARGE' AND balance_after = balance_before + amount) OR (movement_type IN ('PAYMENT', 'REVERSAL') AND balance_after = balance_before - amount)", name="ck_customer_credit_movement_balance"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customer.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["performed_by_member_id"], ["organization_member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["authorized_by_member_id"], ["organization_member.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["sales_ticket_id"], ["sales_ticket.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["cash_register_session_id"], ["cash_register_session.id"], ondelete="SET NULL"),
    )
    for column in ("organization_id","customer_id","performed_by_member_id","authorized_by_member_id","sales_ticket_id","cash_register_session_id"):
        op.create_index(f"ix_customer_credit_movement_{column}", "customer_credit_movement", [column])
    op.create_index("ix_customer_credit_org_created", "customer_credit_movement", ["organization_id","created_at"])
    op.create_index("ix_customer_credit_customer_created", "customer_credit_movement", ["customer_id","created_at","id"])
    with op.batch_alter_table("cash_movement") as batch:
        batch.drop_constraint("ck_cash_movement_type", type_="check")
        batch.create_check_constraint("ck_cash_movement_type", "movement_type IN ('OPENING', 'SALE_CASH', 'CREDIT_PAYMENT', 'CASH_IN', 'WITHDRAWAL', 'EXPENSE', 'REFUND')")


def downgrade():
    with op.batch_alter_table("cash_movement") as batch:
        batch.drop_constraint("ck_cash_movement_type", type_="check")
        batch.create_check_constraint("ck_cash_movement_type", "movement_type IN ('OPENING', 'SALE_CASH', 'CASH_IN', 'WITHDRAWAL', 'EXPENSE', 'REFUND')")
    op.drop_table("customer_credit_movement")
    with op.batch_alter_table("customer") as batch:
        batch.drop_column("credit_limit")
        batch.drop_column("credit_enabled")
