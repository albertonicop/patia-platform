"""add organization currency context and freeze it on sales

Revision ID: 20260810_25
Revises: 20260730_24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260810_25"
down_revision = "20260730_24"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("organization") as batch_op:
        batch_op.add_column(sa.Column("country_code", sa.String(2), nullable=False, server_default="MX"))
        batch_op.add_column(sa.Column("currency_code", sa.String(3), nullable=False, server_default="MXN"))
        batch_op.add_column(sa.Column("locale_code", sa.String(16), nullable=False, server_default="es_MX"))
    op.execute("UPDATE organization SET currency_code = currency WHERE currency IN ('MXN','USD','EUR','COP','CLP','PEN')")
    with op.batch_alter_table("organization") as batch_op:
        batch_op.create_check_constraint("ck_organization_country_code", "country_code IN ('MX','US','ES','CO','CL','PE')")
        batch_op.create_check_constraint("ck_organization_currency_code", "currency_code IN ('MXN','USD','EUR','COP','CLP','PEN')")
        batch_op.create_check_constraint("ck_organization_locale_code", "locale_code IN ('es_MX','en_US','es_ES','es_CO','es_CL','es_PE')")

    for table in ("sales_ticket", "sale"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("currency_code", sa.String(3), nullable=False, server_default="MXN"))
            batch_op.add_column(sa.Column("locale_code", sa.String(16), nullable=False, server_default="es_MX"))
        op.execute(
            f"UPDATE {table} SET currency_code = COALESCE((SELECT currency_code FROM organization WHERE organization.id = {table}.organization_id), 'MXN'), "
            f"locale_code = COALESCE((SELECT locale_code FROM organization WHERE organization.id = {table}.organization_id), 'es_MX')"
        )
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_check_constraint(
                f"ck_{table}_currency_code",
                "currency_code IN ('MXN','USD','EUR','COP','CLP','PEN')",
            )


def downgrade():
    for table in ("sale", "sales_ticket"):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"ck_{table}_currency_code", type_="check")
            batch_op.drop_column("locale_code")
            batch_op.drop_column("currency_code")
    with op.batch_alter_table("organization") as batch_op:
        batch_op.drop_constraint("ck_organization_locale_code", type_="check")
        batch_op.drop_constraint("ck_organization_currency_code", type_="check")
        batch_op.drop_constraint("ck_organization_country_code", type_="check")
        batch_op.drop_column("locale_code")
        batch_op.drop_column("currency_code")
        batch_op.drop_column("country_code")
