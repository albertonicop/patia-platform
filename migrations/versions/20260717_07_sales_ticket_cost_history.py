"""Add transactional sales tickets and historical line costs.

Revision ID: 20260717_07
Revises: 20260717_06
Create Date: 2026-07-17
"""

from collections import OrderedDict
from datetime import datetime
import uuid

from alembic import op
import sqlalchemy as sa


revision = "20260717_07"
down_revision = "20260717_06"
branch_labels = None
depends_on = None


def _columns(table_name):
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name):
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _foreign_keys(table_name):
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {
        foreign_key.get("name")
        for foreign_key in inspector.get_foreign_keys(table_name)
        if foreign_key.get("name")
    }


def _has_sale_ticket_foreign_key():
    inspector = sa.inspect(op.get_bind())
    return any(
        foreign_key.get("constrained_columns") == ["sales_ticket_id"]
        and foreign_key.get("referred_table") == "sales_ticket"
        for foreign_key in inspector.get_foreign_keys("sale")
    )


def _create_schema():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "sales_ticket" not in tables:
        op.create_table(
            "sales_ticket",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("user.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("public_id", sa.String(length=36), nullable=False),
            sa.Column("payment_method", sa.String(length=20), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "user_id",
                "number",
                name="uq_sales_ticket_user_number",
            ),
            sa.UniqueConstraint(
                "user_id",
                "public_id",
                name="uq_sales_ticket_user_public_id",
            ),
        )
        op.create_index(
            "ix_sales_ticket_user_id",
            "sales_ticket",
            ["user_id"],
            unique=False,
        )
        op.create_index(
            "ix_sales_ticket_user_created_at",
            "sales_ticket",
            ["user_id", "created_at"],
            unique=False,
        )

    if "next_ticket_number" not in _columns("user"):
        op.add_column(
            "user",
            sa.Column(
                "next_ticket_number",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
        )

    sale_columns = _columns("sale")
    if "sales_ticket_id" not in sale_columns:
        op.add_column(
            "sale",
            sa.Column("sales_ticket_id", sa.Integer(), nullable=True),
        )
    if "unit_cost" not in sale_columns:
        op.add_column(
            "sale",
            sa.Column("unit_cost", sa.Float(), nullable=True),
        )
    if "cost_is_estimated" not in sale_columns:
        op.add_column(
            "sale",
            sa.Column(
                "cost_is_estimated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    if "ix_sale_sales_ticket_id" not in _indexes("sale"):
        op.create_index(
            "ix_sale_sales_ticket_id",
            "sale",
            ["sales_ticket_id"],
            unique=False,
        )

    if not _has_sale_ticket_foreign_key():
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("sale") as batch_op:
                batch_op.create_foreign_key(
                    "fk_sale_sales_ticket_id",
                    "sales_ticket",
                    ["sales_ticket_id"],
                    ["id"],
                    ondelete="SET NULL",
                )
        else:
            op.create_foreign_key(
                "fk_sale_sales_ticket_id",
                "sale",
                "sales_ticket",
                ["sales_ticket_id"],
                ["id"],
                ondelete="SET NULL",
            )


def _backfill():
    bind = op.get_bind()
    metadata = sa.MetaData()
    user = sa.Table("user", metadata, autoload_with=bind)
    product = sa.Table("product", metadata, autoload_with=bind)
    sale = sa.Table("sale", metadata, autoload_with=bind)
    sales_ticket = sa.Table("sales_ticket", metadata, autoload_with=bind)

    product_costs = (
        {
            row.id: row.cost_price
            for row in bind.execute(
                sa.select(product.c.id, product.c.cost_price)
            )
        }
        if "cost_price" in product.c
        else {}
    )

    for user_id in bind.execute(sa.select(user.c.id).order_by(user.c.id)).scalars():
        existing_numbers = list(
            bind.execute(
                sa.select(sales_ticket.c.number)
                .where(sales_ticket.c.user_id == user_id)
                .order_by(sales_ticket.c.number)
            ).scalars()
        )
        next_number = max(existing_numbers, default=0) + 1
        rows = bind.execute(
            sa.select(
                sale.c.id,
                sale.c.ticket_id,
                sale.c.product_id,
                (
                    sale.c.created_at
                    if "created_at" in sale.c
                    else sa.literal(None).label("created_at")
                ),
                sale.c.payment_method,
                sale.c.sales_ticket_id,
            )
            .where(sale.c.user_id == user_id)
            .order_by(
                sale.c.created_at if "created_at" in sale.c else sale.c.id,
                sale.c.id,
            )
        ).all()

        groups = OrderedDict()
        for row in rows:
            if row.sales_ticket_id is not None:
                continue
            key = row.ticket_id or f"legacy-sale-{row.id}"
            groups.setdefault(key, []).append(row)

        for legacy_key, group in groups.items():
            public_id = (
                legacy_key
                if group[0].ticket_id and len(legacy_key) <= 36
                else str(uuid.uuid4())
            )
            created_at = min(
                (row.created_at for row in group if row.created_at),
                default=datetime.utcnow(),
            )
            payment_method = next(
                (row.payment_method for row in group if row.payment_method),
                None,
            )
            result = bind.execute(
                sales_ticket.insert().values(
                    user_id=user_id,
                    number=next_number,
                    public_id=public_id,
                    payment_method=payment_method,
                    created_at=created_at,
                )
            )
            sales_ticket_id = result.inserted_primary_key[0]

            for row in group:
                current_cost = product_costs.get(row.product_id)
                reliable_cost = (
                    float(current_cost)
                    if current_cost is not None and float(current_cost) > 0
                    else None
                )
                bind.execute(
                    sale.update()
                    .where(sale.c.id == row.id)
                    .values(
                        sales_ticket_id=sales_ticket_id,
                        ticket_id=public_id,
                        unit_cost=reliable_cost,
                        cost_is_estimated=True,
                    )
                )
            next_number += 1

        bind.execute(
            user.update()
            .where(user.c.id == user_id)
            .values(next_ticket_number=next_number)
        )


def upgrade():
    _create_schema()
    _backfill()


def downgrade():
    bind = op.get_bind()
    if _has_sale_ticket_foreign_key():
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("sale") as batch_op:
                batch_op.drop_constraint(
                    "fk_sale_sales_ticket_id",
                    type_="foreignkey",
                )
        else:
            op.drop_constraint(
                "fk_sale_sales_ticket_id",
                "sale",
                type_="foreignkey",
            )
    if "ix_sale_sales_ticket_id" in _indexes("sale"):
        op.drop_index("ix_sale_sales_ticket_id", table_name="sale")
    sale_columns = _columns("sale")
    for column_name in ("cost_is_estimated", "unit_cost", "sales_ticket_id"):
        if column_name in sale_columns:
            op.drop_column("sale", column_name)
            sale_columns.remove(column_name)
    if "next_ticket_number" in _columns("user"):
        op.drop_column("user", "next_ticket_number")
    if "sales_ticket" in sa.inspect(bind).get_table_names():
        op.drop_table("sales_ticket")
