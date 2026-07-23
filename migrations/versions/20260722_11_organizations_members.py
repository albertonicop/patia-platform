"""Introduce organizations and role memberships.

Revision ID: 20260722_11
Revises: 20260719_10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260722_11"
down_revision = "20260719_10"
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "product",
    "inventory_restock_event",
    "sales_ticket",
    "sale",
    "supplier",
)


def _table_names(bind):
    return set(sa.inspect(bind).get_table_names())


def _backfill_organizations(bind):
    metadata = sa.MetaData()
    user = sa.Table("user", metadata, autoload_with=bind)
    organization = sa.Table("organization", metadata, autoload_with=bind)
    membership = sa.Table("organization_member", metadata, autoload_with=bind)

    users = bind.execute(
        sa.select(
            user.c.id,
            user.c.company_name,
            user.c.timezone,
            user.c.created_at,
        ).order_by(user.c.id)
    ).mappings()
    for row in users:
        slug = f"org-{row['id']}"
        bind.execute(
            organization.insert().values(
                name=row["company_name"],
                slug=slug,
                owner_user_id=row["id"],
                timezone=row["timezone"] or "America/Mexico_City",
                currency="MXN",
                is_active=True,
                created_at=row["created_at"],
                updated_at=row["created_at"],
            )
        )
        organization_id = bind.execute(
            sa.select(organization.c.id).where(organization.c.slug == slug)
        ).scalar_one()
        bind.execute(
            membership.insert().values(
                organization_id=organization_id,
                user_id=row["id"],
                role="OWNER",
                is_active=True,
                pin_hash=None,
                created_at=row["created_at"],
                updated_at=row["created_at"],
            )
        )

        for table_name in TENANT_TABLES:
            table = sa.Table(table_name, metadata, autoload_with=bind)
            bind.execute(
                table.update()
                .where(table.c.user_id == row["id"])
                .values(organization_id=organization_id)
            )


def _assert_no_orphaned_business_rows(bind):
    metadata = sa.MetaData()
    for table_name in TENANT_TABLES:
        table = sa.Table(table_name, metadata, autoload_with=bind)
        count = bind.execute(
            sa.select(sa.func.count())
            .select_from(table)
            .where(table.c.organization_id.is_(None))
        ).scalar_one()
        if count:
            raise RuntimeError(
                f"Organization migration stopped: {table_name} contains "
                f"{count} row(s) without an owning user. No data was changed."
            )


def upgrade():
    bind = op.get_bind()
    tables = _table_names(bind)
    required = {"user", *TENANT_TABLES}
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(
            "Organization migration requires existing tables: " + ", ".join(missing)
        )

    op.create_table(
        "organization",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=160), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "timezone",
            sa.String(length=64),
            nullable=False,
            server_default="America/Mexico_City",
        ),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="MXN"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("slug", name="uq_organization_slug"),
        sa.UniqueConstraint(
            "owner_user_id", name="uq_organization_owner_user_id"
        ),
    )
    op.create_table(
        "organization_member",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("pin_hash", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('OWNER', 'MANAGER', 'CASHIER')",
            name="ck_organization_member_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_organization_member_user"
        ),
        sa.UniqueConstraint(
            "user_id", name="uq_organization_member_single_tenant"
        ),
    )
    op.create_table(
        "organization_invitation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("invited_by_member_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "role IN ('MANAGER', 'CASHIER')",
            name="ck_organization_invitation_role",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_member_id"],
            ["organization_member.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "organization_id", "email", name="uq_organization_invitation_email"
        ),
        sa.UniqueConstraint("token_hash", name="uq_organization_invitation_token"),
    )
    op.create_index(
        "ix_organization_invitation_pending",
        "organization_invitation",
        ["organization_id", "accepted_at", "created_at"],
    )

    for table_name in TENANT_TABLES:
        op.add_column(table_name, sa.Column("organization_id", sa.Integer(), nullable=True))
        op.create_index(
            f"ix_{table_name}_organization_id",
            table_name,
            ["organization_id"],
        )

    _backfill_organizations(bind)
    _assert_no_orphaned_business_rows(bind)

    for table_name in TENANT_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.alter_column("organization_id", nullable=False)
            batch_op.create_foreign_key(
                f"fk_{table_name}_organization_id",
                "organization",
                ["organization_id"],
                ["id"],
                ondelete="CASCADE",
            )

    with op.batch_alter_table("product") as batch_op:
        batch_op.create_unique_constraint(
            "uq_product_organization_sku", ["organization_id", "sku"]
        )
        batch_op.create_unique_constraint(
            "uq_product_organization_barcode", ["organization_id", "barcode"]
        )
        batch_op.create_index(
            "ix_product_organization_name", ["organization_id", "name"]
        )
    with op.batch_alter_table("inventory_restock_event") as batch_op:
        batch_op.create_index(
            "ix_restock_organization_created_at",
            ["organization_id", "created_at"],
        )
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.create_unique_constraint(
            "uq_sales_ticket_organization_number", ["organization_id", "number"]
        )
        batch_op.create_unique_constraint(
            "uq_sales_ticket_organization_public_id", ["organization_id", "public_id"]
        )
        batch_op.create_index(
            "ix_sales_ticket_organization_created_at",
            ["organization_id", "created_at"],
        )
    with op.batch_alter_table("sale") as batch_op:
        if "created_at" in {
            column["name"] for column in sa.inspect(bind).get_columns("sale")
        }:
            batch_op.create_index(
                "ix_sale_organization_created_at", ["organization_id", "created_at"]
            )
        batch_op.create_index(
            "ix_sale_organization_ticket", ["organization_id", "ticket_id"]
        )
    with op.batch_alter_table("supplier") as batch_op:
        batch_op.create_unique_constraint(
            "uq_supplier_organization_name", ["organization_id", "name"]
        )
        batch_op.create_index(
            "ix_supplier_organization_name", ["organization_id", "name"]
        )


def downgrade():
    bind = op.get_bind()
    member_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM organization_member WHERE role <> 'OWNER'")
    ).scalar_one()
    if member_count:
        raise RuntimeError(
            "Downgrade refused: non-owner memberships exist and would be lost."
        )
    invitation_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM organization_invitation")
    ).scalar_one()
    if invitation_count:
        raise RuntimeError(
            "Downgrade refused: organization invitations exist and would be lost."
        )

    with op.batch_alter_table("supplier") as batch_op:
        batch_op.drop_index("ix_supplier_organization_name")
        batch_op.drop_constraint("uq_supplier_organization_name", type_="unique")
    with op.batch_alter_table("sale") as batch_op:
        batch_op.drop_index("ix_sale_organization_ticket")
        if "ix_sale_organization_created_at" in {
            index["name"] for index in sa.inspect(bind).get_indexes("sale")
        }:
            batch_op.drop_index("ix_sale_organization_created_at")
    with op.batch_alter_table("sales_ticket") as batch_op:
        batch_op.drop_index("ix_sales_ticket_organization_created_at")
        batch_op.drop_constraint(
            "uq_sales_ticket_organization_public_id", type_="unique"
        )
        batch_op.drop_constraint(
            "uq_sales_ticket_organization_number", type_="unique"
        )
    with op.batch_alter_table("inventory_restock_event") as batch_op:
        batch_op.drop_index("ix_restock_organization_created_at")
    with op.batch_alter_table("product") as batch_op:
        batch_op.drop_index("ix_product_organization_name")
        batch_op.drop_constraint("uq_product_organization_barcode", type_="unique")
        batch_op.drop_constraint("uq_product_organization_sku", type_="unique")

    op.drop_index(
        "ix_organization_invitation_pending",
        table_name="organization_invitation",
    )
    op.drop_table("organization_invitation")

    for table_name in reversed(TENANT_TABLES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_constraint(
                f"fk_{table_name}_organization_id", type_="foreignkey"
            )
            batch_op.drop_index(f"ix_{table_name}_organization_id")
            batch_op.drop_column("organization_id")

    op.drop_table("organization_member")
    op.drop_table("organization")
