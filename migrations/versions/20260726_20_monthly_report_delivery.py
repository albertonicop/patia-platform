"""add monthly report delivery tracking

Revision ID: 20260726_20
Revises: 20260724_19
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_20"
down_revision = "20260724_19"
branch_labels = None
depends_on = None


def _columns(inspector):
    return {
        column["name"]
        for column in inspector.get_columns("monthly_owner_report")
    }


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "monthly_owner_report" not in inspector.get_table_names():
        raise RuntimeError(
            "Cannot add delivery tracking because monthly_owner_report is missing."
        )
    columns = _columns(inspector)
    with op.batch_alter_table("monthly_owner_report") as batch_op:
        if "last_attempt_at" not in columns:
            batch_op.add_column(
                sa.Column("last_attempt_at", sa.DateTime(), nullable=True)
            )
        if "next_retry_at" not in columns:
            batch_op.add_column(
                sa.Column("next_retry_at", sa.DateTime(), nullable=True)
            )
        if "attempt_count" not in columns:
            batch_op.add_column(
                sa.Column(
                    "attempt_count",
                    sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
            )
        if "failure_code" not in columns:
            batch_op.add_column(
                sa.Column(
                    "failure_code",
                    sa.String(length=40),
                    nullable=True,
                )
            )

    inspector = sa.inspect(op.get_bind())
    indexes = {
        index["name"]
        for index in inspector.get_indexes("monthly_owner_report")
    }
    if "ix_monthly_owner_report_retry" not in indexes:
        op.create_index(
            "ix_monthly_owner_report_retry",
            "monthly_owner_report",
            ["status", "next_retry_at"],
            unique=False,
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if "monthly_owner_report" not in inspector.get_table_names():
        return
    indexes = {
        index["name"]
        for index in inspector.get_indexes("monthly_owner_report")
    }
    if "ix_monthly_owner_report_retry" in indexes:
        op.drop_index(
            "ix_monthly_owner_report_retry",
            table_name="monthly_owner_report",
        )
    columns = _columns(inspector)
    with op.batch_alter_table("monthly_owner_report") as batch_op:
        for column_name in (
            "failure_code",
            "attempt_count",
            "next_retry_at",
            "last_attempt_at",
        ):
            if column_name in columns:
                batch_op.drop_column(column_name)
