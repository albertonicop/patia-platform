"""add restaurant recipes and decimal inventory quantities

Revision ID: 20260818_26
Revises: 20260810_25
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_26"
down_revision = "20260810_25"
branch_labels = None
depends_on = None


QUANTITY = sa.Numeric(14, 3)


def _quantity_columns():
    return {
        "product": ("stock", "min_stock"),
        "inventory_restock_event": ("quantity", "stock_before", "stock_after"),
        "inventory_movement": ("quantity_delta", "stock_before", "stock_after"),
        "purchase_order_item": ("ordered_quantity", "received_quantity"),
        "purchase_receipt_item": ("quantity",),
    }


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    added_quantity_columns = set()

    with op.batch_alter_table("organization") as batch_op:
        batch_op.add_column(
            sa.Column(
                "business_type", sa.String(30), nullable=False,
                server_default="general",
            )
        )
        batch_op.create_check_constraint(
            "ck_organization_business_type",
            "business_type IN ('general','restaurant')",
        )

    product_columns = {
        column["name"] for column in inspector.get_columns("product")
    }
    with op.batch_alter_table("product") as batch_op:
        # The reconciliation migration supports a deliberately minimal legacy
        # product table. Bring those installations to the same inventory
        # contract before converting quantities to fixed precision.
        for column in ("stock", "min_stock"):
            if column not in product_columns:
                batch_op.add_column(
                    sa.Column(
                        column, QUANTITY, nullable=False, server_default="0"
                    )
                )
                added_quantity_columns.add(("product", column))
        batch_op.add_column(
            sa.Column("unit_code", sa.String(20), nullable=False, server_default="piece")
        )
        batch_op.add_column(
            sa.Column("item_type", sa.String(20), nullable=False, server_default="inventory")
        )
        batch_op.create_check_constraint(
            "ck_product_unit_code",
            "unit_code IN ('kg','g','L','ml','piece','dozen','portion')",
        )
        batch_op.create_check_constraint(
            "ck_product_item_type", "item_type IN ('inventory','recipe')"
        )

    for table, columns in _quantity_columns().items():
        existing_columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns(table)
        }
        with op.batch_alter_table(table) as batch_op:
            if table == "inventory_movement":
                batch_op.drop_constraint(
                    "ck_inventory_movement_delta_matches_stock", type_="check"
                )
            for column in columns:
                if (table, column) in added_quantity_columns:
                    continue
                if column not in existing_columns:
                    batch_op.add_column(
                        sa.Column(
                            column, QUANTITY, nullable=False,
                            server_default="0",
                        )
                    )
                    continue
                batch_op.alter_column(
                    column,
                    existing_type=sa.Integer(),
                    type_=QUANTITY,
                    existing_nullable=False,
                )
            if table == "inventory_movement":
                batch_op.create_check_constraint(
                    "ck_inventory_movement_delta_matches_stock",
                    "ABS(quantity_delta - (stock_after - stock_before)) < 0.0005",
                )

    op.create_table(
        "recipe",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "organization_id", sa.Integer(),
            sa.ForeignKey("organization.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sale_product_id", sa.Integer(),
            sa.ForeignKey("product.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(80), nullable=False, server_default="Platillos"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("recipe_type", sa.String(20), nullable=False, server_default="dish"),
        sa.Column("yield_quantity", QUANTITY, nullable=False, server_default="1"),
        sa.Column("yield_unit_code", sa.String(20), nullable=False, server_default="portion"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_recipe_organization_name"),
        sa.UniqueConstraint("sale_product_id", name="uq_recipe_sale_product"),
        sa.CheckConstraint("recipe_type IN ('dish','preparation')", name="ck_recipe_type"),
        sa.CheckConstraint("yield_quantity > 0", name="ck_recipe_yield_positive"),
    )
    op.create_index("ix_recipe_organization_id", "recipe", ["organization_id"])
    op.create_index("ix_recipe_sale_product_id", "recipe", ["sale_product_id"])
    op.create_index(
        "ix_recipe_org_active_name", "recipe", ["organization_id", "is_active", "name"]
    )

    op.create_table(
        "recipe_component",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "recipe_id", sa.Integer(), sa.ForeignKey("recipe.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("product.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "source_recipe_id", sa.Integer(),
            sa.ForeignKey("recipe.id", ondelete="RESTRICT"), nullable=True,
        ),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("unit_code", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("quantity > 0", name="ck_recipe_component_quantity_positive"),
        sa.CheckConstraint(
            "(product_id IS NOT NULL AND source_recipe_id IS NULL) OR "
            "(product_id IS NULL AND source_recipe_id IS NOT NULL)",
            name="ck_recipe_component_one_source",
        ),
        sa.UniqueConstraint("recipe_id", "product_id", name="uq_recipe_component_product"),
        sa.UniqueConstraint("recipe_id", "source_recipe_id", name="uq_recipe_component_recipe"),
    )
    op.create_index("ix_recipe_component_recipe", "recipe_component", ["recipe_id", "position"])
    op.create_index("ix_recipe_component_product_id", "recipe_component", ["product_id"])
    op.create_index("ix_recipe_component_source_recipe_id", "recipe_component", ["source_recipe_id"])

    with op.batch_alter_table("sale") as batch_op:
        batch_op.add_column(sa.Column("recipe_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_sale_recipe_id_recipe", "recipe", ["recipe_id"], ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_sale_recipe_id", ["recipe_id"])

    op.create_table(
        "recipe_sale_consumption",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organization.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sale_id", sa.Integer(), sa.ForeignKey("sale.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sales_ticket_id", sa.Integer(), sa.ForeignKey("sales_ticket.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipe_id", sa.Integer(), sa.ForeignKey("recipe.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ingredient_product_id", sa.Integer(), sa.ForeignKey("product.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ingredient_name", sa.String(160), nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("unit_code", sa.String(20), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("total_cost", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_recipe_sale_consumption_quantity"),
    )
    for column in ("organization_id", "sale_id", "sales_ticket_id", "recipe_id", "ingredient_product_id"):
        op.create_index(f"ix_recipe_sale_consumption_{column}", "recipe_sale_consumption", [column])
    op.create_index("ix_recipe_consumption_sale", "recipe_sale_consumption", ["sale_id", "id"])
    op.create_index("ix_recipe_consumption_ticket", "recipe_sale_consumption", ["sales_ticket_id", "id"])


def downgrade():
    op.drop_table("recipe_sale_consumption")
    with op.batch_alter_table("sale") as batch_op:
        batch_op.drop_index("ix_sale_recipe_id")
        batch_op.drop_constraint("fk_sale_recipe_id_recipe", type_="foreignkey")
        batch_op.drop_column("recipe_id")
    op.drop_table("recipe_component")
    op.drop_table("recipe")

    for table, columns in reversed(tuple(_quantity_columns().items())):
        with op.batch_alter_table(table) as batch_op:
            if table == "inventory_movement":
                batch_op.drop_constraint(
                    "ck_inventory_movement_delta_matches_stock", type_="check"
                )
            for column in columns:
                batch_op.alter_column(
                    column, existing_type=QUANTITY, type_=sa.Integer(),
                    existing_nullable=False,
                )
            if table == "inventory_movement":
                batch_op.create_check_constraint(
                    "ck_inventory_movement_delta_matches_stock",
                    "quantity_delta = stock_after - stock_before",
                )
    with op.batch_alter_table("product") as batch_op:
        batch_op.drop_constraint("ck_product_item_type", type_="check")
        batch_op.drop_constraint("ck_product_unit_code", type_="check")
        batch_op.drop_column("item_type")
        batch_op.drop_column("unit_code")
    with op.batch_alter_table("organization") as batch_op:
        batch_op.drop_constraint("ck_organization_business_type", type_="check")
        batch_op.drop_column("business_type")
