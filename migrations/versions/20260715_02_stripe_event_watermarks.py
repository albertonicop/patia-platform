"""Separate Stripe watermarks and webhook completion timestamps.

Revision ID: 20260715_02
Revises: 20260715_01
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_02"
down_revision = "20260715_01"
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
        if "stripe_subscription_updated_at" not in user_columns:
            batch_op.add_column(
                sa.Column("stripe_subscription_updated_at", sa.DateTime(), nullable=True)
            )
        if "stripe_invoice_updated_at" not in user_columns:
            batch_op.add_column(
                sa.Column("stripe_invoice_updated_at", sa.DateTime(), nullable=True)
            )

    if "stripe_state_updated_at" in user_columns:
        op.execute(
            sa.text(
                'UPDATE "user" SET '
                'stripe_subscription_updated_at = stripe_state_updated_at, '
                'stripe_invoice_updated_at = stripe_state_updated_at '
                'WHERE stripe_state_updated_at IS NOT NULL'
            )
        )
        with op.batch_alter_table("user") as batch_op:
            batch_op.drop_column("stripe_state_updated_at")

    event_columns = _columns("stripe_webhook_event")
    with op.batch_alter_table("stripe_webhook_event") as batch_op:
        if "completed_at" not in event_columns:
            batch_op.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
        if "failed_at" not in event_columns:
            batch_op.add_column(sa.Column("failed_at", sa.DateTime(), nullable=True))

    if "processed_at" in event_columns:
        op.execute(
            sa.text(
                "UPDATE stripe_webhook_event SET completed_at = processed_at "
                "WHERE status IN ('processed', 'ignored')"
            )
        )
        op.execute(
            sa.text(
                "UPDATE stripe_webhook_event SET failed_at = processed_at "
                "WHERE status = 'failed'"
            )
        )
        with op.batch_alter_table("stripe_webhook_event") as batch_op:
            batch_op.drop_column("processed_at")


def downgrade():
    event_columns = _columns("stripe_webhook_event")
    with op.batch_alter_table("stripe_webhook_event") as batch_op:
        if "processed_at" not in event_columns:
            batch_op.add_column(sa.Column("processed_at", sa.DateTime(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE stripe_webhook_event SET processed_at = "
            "CASE WHEN status = 'failed' THEN failed_at ELSE completed_at END"
        )
    )
    with op.batch_alter_table("stripe_webhook_event") as batch_op:
        if "failed_at" in event_columns:
            batch_op.drop_column("failed_at")
        if "completed_at" in event_columns:
            batch_op.drop_column("completed_at")

    user_columns = _columns("user")
    with op.batch_alter_table("user") as batch_op:
        if "stripe_state_updated_at" not in user_columns:
            batch_op.add_column(
                sa.Column("stripe_state_updated_at", sa.DateTime(), nullable=True)
            )

    op.execute(
        sa.text(
            'UPDATE "user" SET stripe_state_updated_at = '
            "COALESCE(stripe_subscription_updated_at, stripe_invoice_updated_at)"
        )
    )
    with op.batch_alter_table("user") as batch_op:
        if "stripe_invoice_updated_at" in user_columns:
            batch_op.drop_column("stripe_invoice_updated_at")
        if "stripe_subscription_updated_at" in user_columns:
            batch_op.drop_column("stripe_subscription_updated_at")
