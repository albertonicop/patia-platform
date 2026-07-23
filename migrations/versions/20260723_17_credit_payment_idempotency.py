"""Add persistent idempotency keys to customer credit payments.

Revision ID: 20260723_17
Revises: 20260723_16
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_17"
down_revision = "20260723_16"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "customer_credit_movement" not in inspector.get_table_names():
        raise RuntimeError(
            "Credit payment idempotency requires customer_credit_movement."
        )
    columns = {
        column["name"]
        for column in inspector.get_columns("customer_credit_movement")
    }
    if "request_id" not in columns:
        with op.batch_alter_table("customer_credit_movement") as batch:
            batch.add_column(sa.Column("request_id", sa.String(36), nullable=True))

    inspector = sa.inspect(op.get_bind())
    unique_names = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints(
            "customer_credit_movement"
        )
    }
    if "uq_customer_credit_org_request" not in unique_names:
        with op.batch_alter_table("customer_credit_movement") as batch:
            batch.create_unique_constraint(
                "uq_customer_credit_org_request",
                ["organization_id", "request_id"],
            )


def downgrade():
    # Deliberately non-destructive: idempotency evidence must not be removed.
    pass
