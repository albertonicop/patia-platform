"""Add organization customers and optional ticket ownership.

Revision ID: 20260723_15
Revises: 20260722_14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_15"
down_revision = "20260722_14"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    required = {"organization", "organization_member", "sales_ticket"}
    missing = sorted(required - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(
            "Customer migration requires existing tables: "
            + ", ".join(missing)
        )

    op.create_table(
        "customer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("created_by_member_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=False),
        sa.Column("phone_normalized", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_customer_name_not_blank",
        ),
        sa.CheckConstraint(
            "length(trim(phone_normalized)) > 0",
            name="ck_customer_phone_not_blank",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_customer_organization_id",
        "customer",
        ["organization_id"],
    )
    op.create_index(
        "ix_customer_created_by_member_id",
        "customer",
        ["created_by_member_id"],
    )
    op.create_index(
        "ix_customer_org_active_name",
        "customer",
        ["organization_id", "is_active", "name"],
    )
    op.create_index(
        "ix_customer_org_phone",
        "customer",
        ["organization_id", "phone_normalized"],
    )

    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.add_column(
            sa.Column("customer_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_sales_ticket_customer",
            "customer",
            ["customer_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_sales_ticket_customer_id",
            ["customer_id"],
        )


def downgrade():
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.drop_index("ix_sales_ticket_customer_id")
        batch_op.drop_constraint(
            "fk_sales_ticket_customer",
            type_="foreignkey",
        )
        batch_op.drop_column("customer_id")
    op.drop_table("customer")
