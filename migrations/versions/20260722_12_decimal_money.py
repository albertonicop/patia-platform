"""Store POS money as fixed precision decimals.

Revision ID: 20260722_12
Revises: 20260722_11
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from alembic import op
import sqlalchemy as sa


revision = "20260722_12"
down_revision = "20260722_11"
branch_labels = None
depends_on = None

MONEY_TYPE = sa.Numeric(precision=14, scale=2)
MONEY_LIMIT = Decimal("999999999999.99")
CENT = Decimal("0.01")
COLUMNS = {
    "product": ("cost_price", "sale_price"),
    "sale": ("unit_price", "total", "unit_cost"),
}


def _validated_decimal(value, table_name, column_name, row_id):
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise RuntimeError(
            f"Decimal migration stopped: {table_name}.{column_name} "
            f"contains an invalid value at id={row_id}."
        ) from error
    if not amount.is_finite() or abs(amount) > MONEY_LIMIT:
        raise RuntimeError(
            f"Decimal migration stopped: {table_name}.{column_name} "
            f"contains a non-finite or out-of-range value at id={row_id}."
        )
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def _normalize_existing_values(bind):
    metadata = sa.MetaData()
    for table_name, column_names in COLUMNS.items():
        table = sa.Table(table_name, metadata, autoload_with=bind)
        present_names = tuple(
            name for name in column_names if name in table.c
        )
        if not present_names:
            continue
        rows = bind.execute(
            sa.select(table.c.id, *(table.c[name] for name in present_names))
        ).mappings()
        for row in rows:
            values = {
                name: _validated_decimal(row[name], table_name, name, row["id"])
                for name in present_names
            }
            bind.execute(
                table.update().where(table.c.id == row["id"]).values(**values)
            )


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    missing = sorted(set(COLUMNS) - tables)
    if missing:
        raise RuntimeError(
            "Decimal migration requires existing tables: " + ", ".join(missing)
        )
    _normalize_existing_values(bind)
    for table_name, column_names in COLUMNS.items():
        present_names = {
            column["name"]
            for column in sa.inspect(bind).get_columns(table_name)
        }
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in column_names:
                if column_name not in present_names:
                    continue
                batch_op.alter_column(
                    column_name,
                    existing_type=sa.Float(),
                    type_=MONEY_TYPE,
                    existing_nullable=column_name == "unit_cost",
                )


def downgrade():
    bind = op.get_bind()
    for table_name, column_names in reversed(tuple(COLUMNS.items())):
        present_names = {
            column["name"]
            for column in sa.inspect(bind).get_columns(table_name)
        }
        with op.batch_alter_table(table_name) as batch_op:
            for column_name in column_names:
                if column_name not in present_names:
                    continue
                batch_op.alter_column(
                    column_name,
                    existing_type=MONEY_TYPE,
                    type_=sa.Float(),
                    existing_nullable=column_name == "unit_cost",
                )
