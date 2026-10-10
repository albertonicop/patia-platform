"""Store owner presentation preferences without changing legacy accounts.

Revision ID: 20261010_28
Revises: 20261010_27
"""
from alembic import op
import sqlalchemy as sa

revision = "20261010_28"
down_revision = "20261010_27"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "owner_work_preference",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("mode IN ('pending','deferred','solo','team_operations','team_supervision')",
                           name="ck_owner_work_preference_mode"),
    )
    # Deliberately no backfill: existing owners keep the complete current menu.


def downgrade():
    op.drop_table("owner_work_preference")
