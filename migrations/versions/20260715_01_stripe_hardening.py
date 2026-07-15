"""Stripe hardening and persistent webhook idempotency.

Revision ID: 20260715_01
Revises: 20260715_00
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_01"
down_revision = "20260715_00"
branch_labels = None
depends_on = None


def _columns(table_name):
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade():
    user_columns = _columns("user")
    with op.batch_alter_table("user") as batch_op:
        if "manual_pro_access" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "manual_pro_access",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "next_payment_attempt" not in user_columns:
            batch_op.add_column(
                sa.Column("next_payment_attempt", sa.DateTime(), nullable=True)
            )
        if not {
            "stripe_state_updated_at",
            "stripe_subscription_updated_at",
            "stripe_invoice_updated_at",
        }.intersection(user_columns):
            batch_op.add_column(
                sa.Column("stripe_state_updated_at", sa.DateTime(), nullable=True)
            )

    if "stripe_webhook_event" not in sa.inspect(op.get_bind()).get_table_names():
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
            sa.UniqueConstraint(
                "stripe_event_id",
                name="uq_stripe_webhook_event_event_id",
            ),
        )
        op.create_index(
            "ix_stripe_webhook_event_object_id",
            "stripe_webhook_event",
            ["object_id"],
            unique=False,
        )

    # No se concede acceso manual automáticamente. Los candidatos se auditan
    # con el comando: flask audit-manual-pro-candidates


def downgrade():
    user_columns = _columns("user")
    with op.batch_alter_table("user") as batch_op:
        if "stripe_state_updated_at" in user_columns:
            batch_op.drop_column("stripe_state_updated_at")
        if "next_payment_attempt" in user_columns:
            batch_op.drop_column("next_payment_attempt")
        if "manual_pro_access" in user_columns:
            batch_op.drop_column("manual_pro_access")
