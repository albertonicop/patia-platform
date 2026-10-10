"""Remember the owner workflow update without changing existing preferences.

Revision ID: 20261010_29
Revises: 20261010_28
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_29"
down_revision = "20261010_28"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "owner_work_update_notice",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.CheckConstraint("action IN ('seen','personalize','later')", name="ck_owner_work_update_notice_action"),
    )
    # No backfill or updates to users, businesses, subscriptions or preferences.


def downgrade():
    op.drop_table("owner_work_update_notice")
