"""Allow customers without a phone number.

Revision ID: 20260724_18
Revises: 20260723_17
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_18"
down_revision = "20260723_17"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "customer" not in inspector.get_table_names():
        raise RuntimeError("Customer phone migration requires the customer table.")

    constraints = {
        item.get("name")
        for item in inspector.get_check_constraints("customer")
    }
    with op.batch_alter_table("customer") as batch:
        if "ck_customer_phone_not_blank" in constraints:
            batch.drop_constraint(
                "ck_customer_phone_not_blank",
                type_="check",
            )
        batch.alter_column(
            "phone",
            existing_type=sa.String(length=30),
            nullable=True,
        )
        batch.alter_column(
            "phone_normalized",
            existing_type=sa.String(length=20),
            nullable=True,
        )


def downgrade():
    connection = op.get_bind()
    missing_phone = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM customer "
            "WHERE phone IS NULL OR phone_normalized IS NULL "
            "OR length(trim(phone_normalized)) = 0"
        )
    ).scalar_one()
    if missing_phone:
        raise RuntimeError(
            "Cannot restore required customer phones while customers "
            "without a phone number exist."
        )

    with op.batch_alter_table("customer") as batch:
        batch.alter_column(
            "phone",
            existing_type=sa.String(length=30),
            nullable=False,
        )
        batch.alter_column(
            "phone_normalized",
            existing_type=sa.String(length=20),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_customer_phone_not_blank",
            "length(trim(phone_normalized)) > 0",
        )
