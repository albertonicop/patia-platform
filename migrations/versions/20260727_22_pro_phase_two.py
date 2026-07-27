"""add Pro monthly snapshots and intelligent purchasing

Revision ID: 20260727_22
Revises: 20260726_21
"""

from alembic import op
import sqlalchemy as sa


revision = "20260727_22"
down_revision = "20260726_21"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    organization_columns = {
        item["name"] for item in inspector.get_columns("organization")
    }
    if "next_purchase_order_number" not in organization_columns:
        with op.batch_alter_table("organization") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "next_purchase_order_number",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )

    report_columns = {
        item["name"]
        for item in inspector.get_columns("monthly_owner_report")
    }
    with op.batch_alter_table("monthly_owner_report") as batch_op:
        if "snapshot_json" not in report_columns:
            batch_op.add_column(
                sa.Column("snapshot_json", sa.Text(), nullable=True)
            )
        if "snapshot_hash" not in report_columns:
            batch_op.add_column(
                sa.Column(
                    "snapshot_hash", sa.String(length=64), nullable=True
                )
            )
        if "snapshot_version" not in report_columns:
            batch_op.add_column(
                sa.Column(
                    "snapshot_version",
                    sa.Integer(),
                    nullable=False,
                    server_default="1",
                )
            )
        if "generated_by_member_id" not in report_columns:
            batch_op.add_column(
                sa.Column(
                    "generated_by_member_id",
                    sa.Integer(),
                    nullable=True,
                )
            )
            batch_op.create_foreign_key(
                "fk_monthly_report_generated_by_member",
                "organization_member",
                ["generated_by_member_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.create_index(
                "ix_monthly_owner_report_generated_by_member_id",
                ["generated_by_member_id"],
            )
        if "manual_generation" not in report_columns:
            batch_op.add_column(
                sa.Column(
                    "manual_generation",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    tables = set(inspector.get_table_names())
    if "purchase_order" not in tables:
        op.create_table(
            "purchase_order",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("number", sa.String(length=24), nullable=False),
            sa.Column("supplier_id", sa.Integer(), nullable=True),
            sa.Column("supplier_name", sa.String(length=120), nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                nullable=False,
                server_default="DRAFT",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by_member_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("ordered_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["organization_id"],
                ["organization.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["supplier_id"], ["supplier.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["created_by_member_id"],
                ["organization_member.id"],
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint(
                "organization_id",
                "number",
                name="uq_purchase_order_organization_number",
            ),
            sa.CheckConstraint(
                "status IN ('DRAFT', 'ORDERED', "
                "'PARTIALLY_RECEIVED', 'RECEIVED', 'CANCELLED')",
                name="ck_purchase_order_status",
            ),
        )
        op.create_index(
            "ix_purchase_order_organization_id",
            "purchase_order",
            ["organization_id"],
        )
        op.create_index(
            "ix_purchase_order_supplier_id",
            "purchase_order",
            ["supplier_id"],
        )
        op.create_index(
            "ix_purchase_order_created_by_member_id",
            "purchase_order",
            ["created_by_member_id"],
        )
        op.create_index(
            "ix_purchase_order_org_status_created",
            "purchase_order",
            ["organization_id", "status", "created_at"],
        )

    if "purchase_order_item" not in tables:
        op.create_table(
            "purchase_order_item",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("purchase_order_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=True),
            sa.Column("product_name", sa.String(length=160), nullable=False),
            sa.Column("product_sku", sa.String(length=64), nullable=False),
            sa.Column("ordered_quantity", sa.Integer(), nullable=False),
            sa.Column(
                "received_quantity",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("unit_cost", sa.Numeric(14, 2), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["purchase_order_id"],
                ["purchase_order.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["product_id"], ["product.id"], ondelete="SET NULL"
            ),
            sa.UniqueConstraint(
                "purchase_order_id",
                "product_id",
                name="uq_purchase_order_item_product",
            ),
            sa.CheckConstraint(
                "ordered_quantity > 0",
                name="ck_purchase_order_item_ordered_positive",
            ),
            sa.CheckConstraint(
                "received_quantity >= 0 "
                "AND received_quantity <= ordered_quantity",
                name="ck_purchase_order_item_received_valid",
            ),
        )
        op.create_index(
            "ix_purchase_order_item_purchase_order_id",
            "purchase_order_item",
            ["purchase_order_id"],
        )
        op.create_index(
            "ix_purchase_order_item_product_id",
            "purchase_order_item",
            ["product_id"],
        )

    if "purchase_receipt" not in tables:
        op.create_table(
            "purchase_receipt",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("organization_id", sa.Integer(), nullable=False),
            sa.Column("purchase_order_id", sa.Integer(), nullable=False),
            sa.Column("received_by_member_id", sa.Integer(), nullable=True),
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["organization_id"],
                ["organization.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["purchase_order_id"],
                ["purchase_order.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["received_by_member_id"],
                ["organization_member.id"],
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint(
                "organization_id",
                "request_id",
                name="uq_purchase_receipt_organization_request",
            ),
        )
        op.create_index(
            "ix_purchase_receipt_organization_id",
            "purchase_receipt",
            ["organization_id"],
        )
        op.create_index(
            "ix_purchase_receipt_purchase_order_id",
            "purchase_receipt",
            ["purchase_order_id"],
        )
        op.create_index(
            "ix_purchase_receipt_received_by_member_id",
            "purchase_receipt",
            ["received_by_member_id"],
        )
        op.create_index(
            "ix_purchase_receipt_org_created",
            "purchase_receipt",
            ["organization_id", "created_at"],
        )

    if "purchase_receipt_item" not in tables:
        op.create_table(
            "purchase_receipt_item",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("purchase_receipt_id", sa.Integer(), nullable=False),
            sa.Column("purchase_order_item_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_cost", sa.Numeric(14, 2), nullable=True),
            sa.Column("restock_event_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["purchase_receipt_id"],
                ["purchase_receipt.id"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["purchase_order_item_id"],
                ["purchase_order_item.id"],
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["restock_event_id"],
                ["inventory_restock_event.id"],
                ondelete="SET NULL",
            ),
            sa.UniqueConstraint(
                "purchase_receipt_id",
                "purchase_order_item_id",
                name="uq_purchase_receipt_item_order_item",
            ),
            sa.CheckConstraint(
                "quantity > 0",
                name="ck_purchase_receipt_item_quantity_positive",
            ),
        )
        op.create_index(
            "ix_purchase_receipt_item_purchase_receipt_id",
            "purchase_receipt_item",
            ["purchase_receipt_id"],
        )
        op.create_index(
            "ix_purchase_receipt_item_purchase_order_item_id",
            "purchase_receipt_item",
            ["purchase_order_item_id"],
        )
        op.create_index(
            "ix_purchase_receipt_item_restock_event_id",
            "purchase_receipt_item",
            ["restock_event_id"],
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table_name in (
        "purchase_receipt_item",
        "purchase_receipt",
        "purchase_order_item",
        "purchase_order",
    ):
        if table_name in tables:
            op.drop_table(table_name)

    report_columns = {
        item["name"]
        for item in inspector.get_columns("monthly_owner_report")
    }
    with op.batch_alter_table("monthly_owner_report") as batch_op:
        if "generated_by_member_id" in report_columns:
            indexes = {
                item["name"]
                for item in inspector.get_indexes("monthly_owner_report")
            }
            if (
                "ix_monthly_owner_report_generated_by_member_id"
                in indexes
            ):
                batch_op.drop_index(
                    "ix_monthly_owner_report_generated_by_member_id"
                )
            batch_op.drop_constraint(
                "fk_monthly_report_generated_by_member",
                type_="foreignkey",
            )
        for column_name in (
            "manual_generation",
            "generated_by_member_id",
            "snapshot_version",
            "snapshot_hash",
            "snapshot_json",
        ):
            if column_name in report_columns:
                batch_op.drop_column(column_name)

    organization_columns = {
        item["name"] for item in inspector.get_columns("organization")
    }
    if "next_purchase_order_number" in organization_columns:
        with op.batch_alter_table("organization") as batch_op:
            batch_op.drop_column("next_purchase_order_number")
