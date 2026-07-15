"""Initial PATIA schema baseline.

Revision ID: 20260715_00
Revises:
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_00"
down_revision = None
branch_labels = None
depends_on = None


def _tables():
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade():
    tables = _tables()

    if "user" not in tables:
        op.create_table(
            "user",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("first_name", sa.String(80), nullable=True),
            sa.Column("last_name", sa.String(80), nullable=True),
            sa.Column("email", sa.String(120), nullable=False),
            sa.Column("password", sa.String(255), nullable=False),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("company_name", sa.String(120), nullable=False),
            sa.Column("address", sa.String(200), nullable=True),
            sa.Column("city", sa.String(80), nullable=True),
            sa.Column("state", sa.String(80), nullable=True),
            sa.Column("business_type", sa.String(80), nullable=True),
            sa.Column("postal_code", sa.String(10), nullable=True),
            sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("verification_code", sa.String(6), nullable=True),
            sa.Column("verification_code_expires", sa.DateTime(), nullable=True),
            sa.Column("reset_token", sa.String(100), nullable=True),
            sa.Column("reset_token_expires", sa.DateTime(), nullable=True),
            sa.Column("session_token", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("plan", sa.String(20), nullable=False, server_default="trial"),
            sa.Column("stripe_customer_id", sa.String(120), nullable=True),
            sa.Column("stripe_subscription_id", sa.String(120), nullable=True),
            sa.Column("subscription_status", sa.String(30), nullable=True),
            sa.Column("current_period_end", sa.DateTime(), nullable=True),
            sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("trial_warning_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("rfc", sa.String(20), nullable=True),
            sa.Column("tax_regime", sa.String(120), nullable=True),
        )
        op.create_index("ix_user_email", "user", ["email"], unique=True)
        op.create_index("ix_user_reset_token", "user", ["reset_token"], unique=False)
        op.create_index("ix_user_stripe_customer_id", "user", ["stripe_customer_id"], unique=False)
        op.create_index("ix_user_stripe_subscription_id", "user", ["stripe_subscription_id"], unique=False)

    if "product" not in tables:
        op.create_table(
            "product",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
            sa.Column("sku", sa.String(64), nullable=False),
            sa.Column("barcode", sa.String(64), nullable=True),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("category", sa.String(80), nullable=False, server_default="General"),
            sa.Column("supplier", sa.String(120), nullable=True),
            sa.Column("cost_price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("sale_price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("min_stock", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "sku", name="uq_product_user_sku"),
        )
        op.create_index("ix_product_user_id", "product", ["user_id"], unique=False)
        op.create_index("ix_product_user_name", "product", ["user_id", "name"], unique=False)
        op.create_index("ix_product_user_barcode", "product", ["user_id", "barcode"], unique=False)

    if "sale" not in tables:
        op.create_table(
            "sale",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
            sa.Column("product_id", sa.Integer(), sa.ForeignKey("product.id", ondelete="CASCADE"), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("unit_price", sa.Float(), nullable=False, server_default="0"),
            sa.Column("total", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("ticket_id", sa.String(36), nullable=True),
        )
        op.create_index("ix_sale_user_id", "sale", ["user_id"], unique=False)
        op.create_index("ix_sale_product_id", "sale", ["product_id"], unique=False)
        op.create_index("ix_sale_user_created_at", "sale", ["user_id", "created_at"], unique=False)
        op.create_index("ix_sale_user_ticket", "sale", ["user_id", "ticket_id"], unique=False)

    if "supplier" not in tables:
        op.create_table(
            "supplier",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("contact", sa.String(120), nullable=True),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.UniqueConstraint("user_id", "name", name="uq_supplier_user_name"),
        )
        op.create_index("ix_supplier_user_id", "supplier", ["user_id"], unique=False)
        op.create_index("ix_supplier_user_name", "supplier", ["user_id", "name"], unique=False)

    if "stripe_webhook_event" not in tables:
        op.create_table(
            "stripe_webhook_event",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("stripe_event_id", sa.String(255), nullable=False),
            sa.Column("event_type", sa.String(120), nullable=False),
            sa.Column("object_id", sa.String(255), nullable=True),
            sa.Column("stripe_created_at", sa.DateTime(), nullable=False),
            sa.Column("processed_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.UniqueConstraint("stripe_event_id", name="uq_stripe_webhook_event_event_id"),
        )
        op.create_index("ix_stripe_webhook_event_object_id", "stripe_webhook_event", ["object_id"], unique=False)


def downgrade():
    tables = _tables()
    for table_name in ("stripe_webhook_event", "sale", "supplier", "product", "user"):
        if table_name in tables:
            op.drop_table(table_name)
