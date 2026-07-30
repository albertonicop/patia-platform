"""add controlled AI narrative audit and cache

Revision ID: 20260730_23
Revises: 20260727_22
Create Date: 2026-07-30
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_23"
down_revision = "20260727_22"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ai_narrative_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("feature_name", sa.String(length=40), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        sa.Column("data_hash", sa.String(length=64), nullable=False),
        sa.Column("data_period", sa.String(length=80), nullable=False),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "estimated_cost_microusd", sa.BigInteger(), nullable=False
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('SUCCESS', 'FAILED', 'FALLBACK', 'LIMITED')",
            name="ck_ai_narrative_status",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name="fk_ai_narrative_run_organization",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id",
            "feature_name",
            "language",
            "data_hash",
            name="uq_ai_narrative_cache",
        ),
    )
    op.create_index(
        "ix_ai_narrative_run_organization_id",
        "ai_narrative_run",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_narrative_org_feature_created",
        "ai_narrative_run",
        ["organization_id", "feature_name", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_narrative_created_status",
        "ai_narrative_run",
        ["created_at", "status"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_ai_narrative_created_status",
        table_name="ai_narrative_run",
    )
    op.drop_index(
        "ix_ai_narrative_org_feature_created",
        table_name="ai_narrative_run",
    )
    op.drop_index(
        "ix_ai_narrative_run_organization_id",
        table_name="ai_narrative_run",
    )
    op.drop_table("ai_narrative_run")
