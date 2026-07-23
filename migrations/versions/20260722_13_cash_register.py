"""Add cash register shifts and immutable cash movements.

Revision ID: 20260722_13
Revises: 20260722_12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_13"
down_revision = "20260722_12"
branch_labels = None
depends_on = None

MONEY = sa.Numeric(precision=14, scale=2)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {
        "organization",
        "organization_member",
        "sales_ticket",
    }
    missing = sorted(required - set(inspector.get_table_names()))
    if missing:
        raise RuntimeError(
            "Cash register migration requires existing tables: "
            + ", ".join(missing)
        )

    op.create_table(
        "cash_register_session",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column(
            "register_key",
            sa.String(length=40),
            nullable=False,
            server_default="MAIN",
        ),
        sa.Column("open_key", sa.String(length=40), nullable=True),
        sa.Column(
            "status",
            sa.String(length=10),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("opened_by_member_id", sa.Integer(), nullable=True),
        sa.Column("closed_by_member_id", sa.Integer(), nullable=True),
        sa.Column(
            "opening_cash", MONEY, nullable=False, server_default="0.00"
        ),
        sa.Column("expected_cash_at_close", MONEY, nullable=True),
        sa.Column("counted_cash", MONEY, nullable=True),
        sa.Column("difference", MONEY, nullable=True),
        sa.Column("closing_notes", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name="ck_cash_register_session_status",
        ),
        sa.CheckConstraint(
            "opening_cash >= 0",
            name="ck_cash_register_session_opening_cash_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["opened_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["closed_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "open_key",
            name="uq_cash_register_session_open_register",
        ),
    )
    op.create_index(
        "ix_cash_register_session_organization_id",
        "cash_register_session",
        ["organization_id"],
    )
    op.create_index(
        "ix_cash_register_session_organization_opened",
        "cash_register_session",
        ["organization_id", "opened_at"],
    )

    op.create_table(
        "cash_movement",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("cash_register_session_id", sa.Integer(), nullable=False),
        sa.Column("performed_by_member_id", sa.Integer(), nullable=True),
        sa.Column("sales_ticket_id", sa.Integer(), nullable=True),
        sa.Column("movement_type", sa.String(length=20), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "amount > 0", name="ck_cash_movement_amount_positive"
        ),
        sa.CheckConstraint(
            "movement_type IN "
            "('OPENING', 'SALE_CASH', 'CASH_IN', 'WITHDRAWAL', 'EXPENSE', 'REFUND')",
            name="ck_cash_movement_type",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cash_register_session_id"],
            ["cash_register_session.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["performed_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sales_ticket_id"],
            ["sales_ticket.id"],
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_cash_movement_organization_id",
        "cash_movement",
        ["organization_id"],
    )
    op.create_index(
        "ix_cash_movement_cash_register_session_id",
        "cash_movement",
        ["cash_register_session_id"],
    )
    op.create_index(
        "ix_cash_movement_sales_ticket_id",
        "cash_movement",
        ["sales_ticket_id"],
    )
    op.create_index(
        "ix_cash_movement_session_created",
        "cash_movement",
        ["cash_register_session_id", "created_at"],
    )
    op.create_index(
        "ix_cash_movement_organization_created",
        "cash_movement",
        ["organization_id", "created_at"],
    )

    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.add_column(
            sa.Column(
                "cash_register_session_id",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_sales_ticket_cash_register_session",
            "cash_register_session",
            ["cash_register_session_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_sales_ticket_cash_register_session_id",
            ["cash_register_session_id"],
        )


def downgrade():
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.drop_index("ix_sales_ticket_cash_register_session_id")
        batch_op.drop_constraint(
            "fk_sales_ticket_cash_register_session",
            type_="foreignkey",
        )
        batch_op.drop_column("cash_register_session_id")
    op.drop_table("cash_movement")
    op.drop_table("cash_register_session")
