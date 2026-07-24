"""centralize commercial plans and monthly owner reports

Revision ID: 20260724_19
Revises: 20260724_18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_19"
down_revision = "20260724_18"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())

    organization_columns = {
        column["name"] for column in inspector.get_columns("organization")
    }
    with op.batch_alter_table("organization") as batch_op:
        if "monthly_report_enabled" not in organization_columns:
            batch_op.add_column(
                sa.Column(
                    "monthly_report_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )
        if "monthly_report_recipient" not in organization_columns:
            batch_op.add_column(
                sa.Column(
                    "monthly_report_recipient",
                    sa.String(length=120),
                    nullable=True,
                )
            )

    user_columns = {
        column["name"] for column in inspector.get_columns("user")
    }
    with op.batch_alter_table("user") as batch_op:
        if "subscription_plan_code" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "subscription_plan_code",
                    sa.String(length=20),
                    nullable=True,
                )
            )
        if "trial_plan_code" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "trial_plan_code",
                    sa.String(length=20),
                    nullable=False,
                    server_default="STARTER",
                )
            )
        if "pending_plan_code" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "pending_plan_code",
                    sa.String(length=20),
                    nullable=True,
                )
            )
        if "pending_plan_effective_at" not in user_columns:
            batch_op.add_column(
                sa.Column(
                    "pending_plan_effective_at",
                    sa.DateTime(),
                    nullable=True,
                )
            )

    inspector = sa.inspect(op.get_bind())
    if "monthly_owner_report" not in inspector.get_table_names():
        op.create_table(
            "monthly_owner_report",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("report_year", sa.Integer(), nullable=False),
            sa.Column("report_month", sa.Integer(), nullable=False),
            sa.Column("recipient", sa.String(length=120), nullable=False),
            sa.Column("generated_at", sa.DateTime(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "report_month >= 1 AND report_month <= 12",
                name="ck_monthly_owner_report_month",
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'generated', 'sending', 'sent', 'failed')",
                name="ck_monthly_owner_report_status",
            ),
            sa.ForeignKeyConstraint(
                ["organization_id"],
                ["organization.id"],
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "organization_id",
                "report_year",
                "report_month",
                name="uq_monthly_owner_report_period",
            ),
        )
        op.create_index(
            "ix_monthly_owner_report_organization_id",
            "monthly_owner_report",
            ["organization_id"],
            unique=False,
        )
        op.create_index(
            "ix_monthly_owner_report_status_period",
            "monthly_owner_report",
            ["status", "report_year", "report_month"],
            unique=False,
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "monthly_owner_report" in inspector.get_table_names():
        op.drop_table("monthly_owner_report")

    user_columns = {
        column["name"] for column in inspector.get_columns("user")
    }
    with op.batch_alter_table("user") as batch_op:
        for column_name in (
            "pending_plan_effective_at",
            "pending_plan_code",
            "trial_plan_code",
            "subscription_plan_code",
        ):
            if column_name in user_columns:
                batch_op.drop_column(column_name)

    inspector = sa.inspect(op.get_bind())
    organization_columns = {
        column["name"] for column in inspector.get_columns("organization")
    }
    with op.batch_alter_table("organization") as batch_op:
        for column_name in (
            "monthly_report_recipient",
            "monthly_report_enabled",
        ):
            if column_name in organization_columns:
                batch_op.drop_column(column_name)
