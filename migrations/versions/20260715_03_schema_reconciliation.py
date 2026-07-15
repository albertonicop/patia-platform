"""Reconcile legacy production schema with the current PATIA models.

Revision ID: 20260715_03
Revises: 20260715_02
Create Date: 2026-07-15

This revision is intentionally additive and idempotent.  Some databases were
stamped with the baseline revision after their tables already existed, so the
baseline's conditional create_table calls did not reconcile missing columns.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260715_03"
down_revision = "20260715_02"
branch_labels = None
depends_on = None


def _inspector():
    # Return a fresh inspector after every DDL operation. PostgreSQL inspectors
    # cache schema metadata for the lifetime of the inspector instance.
    return sa.inspect(op.get_bind())


def _tables():
    return set(_inspector().get_table_names())


def _columns(table_name):
    if table_name not in _tables():
        return set()
    return {
        column["name"]
        for column in _inspector().get_columns(table_name)
    }


def _add_missing_columns(table_name, columns):
    existing = _columns(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)
            existing.add(column.name)


def _require_columns(table_name, required):
    missing = set(required) - _columns(table_name)
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(
            f"Cannot safely reconcile {table_name}: missing essential columns: {names}"
        )


def _reconcile_user():
    if "user" not in _tables():
        raise RuntimeError("Cannot reconcile schema: user table does not exist")

    # These fields identify existing accounts and cannot be reconstructed with
    # a safe generic value if a legacy database does not have them.
    _require_columns(
        "user",
        {"id", "email", "password", "company_name", "created_at"},
    )

    _add_missing_columns(
        "user",
        [
            sa.Column("first_name", sa.String(80), nullable=True),
            sa.Column("last_name", sa.String(80), nullable=True),
            sa.Column("phone", sa.String(50), nullable=True),
            sa.Column("address", sa.String(200), nullable=True),
            sa.Column("city", sa.String(80), nullable=True),
            sa.Column("state", sa.String(80), nullable=True),
            sa.Column("business_type", sa.String(80), nullable=True),
            sa.Column("postal_code", sa.String(10), nullable=True),
            sa.Column(
                "email_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("verification_code", sa.String(6), nullable=True),
            sa.Column("verification_code_expires", sa.DateTime(), nullable=True),
            sa.Column("reset_token", sa.String(100), nullable=True),
            sa.Column("reset_token_expires", sa.DateTime(), nullable=True),
            sa.Column("session_token", sa.String(64), nullable=True),
            sa.Column(
                "plan",
                sa.String(20),
                nullable=False,
                server_default="trial",
            ),
            sa.Column(
                "manual_pro_access",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("stripe_customer_id", sa.String(120), nullable=True),
            sa.Column("stripe_subscription_id", sa.String(120), nullable=True),
            sa.Column("subscription_status", sa.String(30), nullable=True),
            sa.Column("current_period_end", sa.DateTime(), nullable=True),
            sa.Column("next_payment_attempt", sa.DateTime(), nullable=True),
            sa.Column(
                "stripe_subscription_updated_at",
                sa.DateTime(),
                nullable=True,
            ),
            sa.Column("stripe_invoice_updated_at", sa.DateTime(), nullable=True),
            sa.Column(
                "cancel_at_period_end",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column(
                "trial_warning_sent",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("rfc", sa.String(20), nullable=True),
            sa.Column("tax_regime", sa.String(120), nullable=True),
        ],
    )


def _reconcile_sale():
    if "sale" not in _tables():
        raise RuntimeError("Cannot reconcile schema: sale table does not exist")

    _require_columns(
        "sale",
        {"id", "user_id", "product_id", "quantity", "unit_price", "total"},
    )
    _add_missing_columns(
        "sale",
        [sa.Column("ticket_id", sa.String(36), nullable=True)],
    )


def _create_stripe_webhook_event():
    op.create_table(
        "stripe_webhook_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stripe_event_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(120), nullable=False),
        sa.Column("object_id", sa.String(255), nullable=True),
        sa.Column("stripe_created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("failed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
            server_default="pending",
        ),
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


def _reconcile_stripe_webhook_event():
    if "stripe_webhook_event" not in _tables():
        _create_stripe_webhook_event()
        return

    _require_columns(
        "stripe_webhook_event",
        {"id", "stripe_event_id", "event_type", "stripe_created_at"},
    )
    _add_missing_columns(
        "stripe_webhook_event",
        [
            sa.Column("object_id", sa.String(255), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("failed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "status",
                sa.String(30),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
        ],
    )


def upgrade():
    _reconcile_user()
    _reconcile_sale()
    _reconcile_stripe_webhook_event()


def downgrade():
    # Deliberately non-destructive. These columns are part of the current model,
    # and dropping them could destroy production account and billing state.
    pass
