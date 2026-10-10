"""Preserve original ticket lines separately from active sales after reversal.

Revision ID: 20261010_27
Revises: 20260818_26
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_27"
down_revision = "20260818_26"
branch_labels = None
depends_on = None


def upgrade():
    # SQLite otherwise reuses the last deleted sale ID, making an old reversal
    # URL refer to a different sale. PostgreSQL already uses a sequence.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("sale", recreate="always", table_kwargs={"sqlite_autoincrement": True}):
            pass
    op.create_table(
        "reversed_sale_line",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organization.id", ondelete="CASCADE"), nullable=False),
        sa.Column("original_sale_id", sa.Integer(), nullable=False),
        sa.Column("sales_ticket_id", sa.Integer(), sa.ForeignKey("sales_ticket.id", ondelete="CASCADE")),
        sa.Column("ticket_id", sa.String(36)),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("payment_method", sa.String(20)),
        sa.Column("currency_code", sa.String(3), nullable=False),
        sa.Column("locale_code", sa.String(16), nullable=False),
        sa.Column("reversal_type", sa.String(30), nullable=False),
        sa.Column("reversed_at", sa.DateTime(), nullable=False),
        sa.Column("performed_by_member_id", sa.Integer(), sa.ForeignKey("organization_member.id", ondelete="SET NULL")),
        sa.UniqueConstraint("organization_id", "original_sale_id", name="uq_reversed_sale_line_original"),
    )
    for column in ("organization_id", "sales_ticket_id", "ticket_id"):
        op.create_index("ix_reversed_sale_line_" + column, "reversed_sale_line", [column])


def downgrade():
    op.drop_table("reversed_sale_line")
