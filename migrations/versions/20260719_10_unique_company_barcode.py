"""Guarantee barcode uniqueness within each company.

Revision ID: 20260719_10
Revises: 20260718_09
Create Date: 2026-07-19
"""

from alembic import op
import sqlalchemy as sa


revision = "20260719_10"
down_revision = "20260718_09"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_product_user_barcode"
LEGACY_INDEX_NAME = "ix_product_user_barcode"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "product" not in inspector.get_table_names():
        raise RuntimeError(
            "Cannot enforce barcode uniqueness because the product table is missing."
        )

    product_columns = {
        column["name"] for column in inspector.get_columns("product")
    }
    if "barcode" not in product_columns:
        op.add_column(
            "product",
            sa.Column("barcode", sa.String(length=64), nullable=True),
        )

    duplicates = bind.execute(
        sa.text(
            """
            SELECT user_id, barcode, COUNT(*) AS duplicate_count
            FROM product
            WHERE barcode IS NOT NULL
            GROUP BY user_id, barcode
            HAVING COUNT(*) > 1
            """
        )
    ).all()
    if duplicates:
        details = "; ".join(
            "user_id={user_id}, barcode={barcode!r}, rows={count}".format(
                user_id=row.user_id,
                barcode=row.barcode,
                count=row.duplicate_count,
            )
            for row in duplicates
        )
        raise RuntimeError(
            "Cannot enforce barcode uniqueness: duplicate barcodes exist within "
            "at least one company. Review them manually before retrying the migration. "
            f"Blocking values: {details}"
        )

    indexes = {index["name"]: index for index in inspector.get_indexes("product")}
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "product",
            ["user_id", "barcode"],
            unique=True,
        )

    inspector = sa.inspect(bind)
    indexes = {index["name"]: index for index in inspector.get_indexes("product")}
    if LEGACY_INDEX_NAME in indexes:
        op.drop_index(LEGACY_INDEX_NAME, table_name="product")


def downgrade():
    # Non-destructive by policy: keep the integrity guarantee in place.
    pass
