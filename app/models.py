from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .money import MONEY_ZERO, money_decimal


class Organization(db.Model):
    __tablename__ = "organization"
    __table_args__ = (
        db.UniqueConstraint(
            "owner_user_id", name="uq_organization_owner_user_id"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(160), nullable=False, unique=True)
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    timezone = db.Column(
        db.String(64), nullable=False, default="America/Mexico_City"
    )
    currency = db.Column(db.String(3), nullable=False, default="MXN")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    members = db.relationship(
        "OrganizationMember",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    owner = db.relationship("User", foreign_keys=[owner_user_id])


class OrganizationMember(db.Model):
    __tablename__ = "organization_member"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "user_id", name="uq_organization_member_user"
        ),
        db.UniqueConstraint(
            "user_id", name="uq_organization_member_single_tenant"
        ),
        db.CheckConstraint(
            "role IN ('OWNER', 'MANAGER', 'CASHIER')",
            name="ck_organization_member_role",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = db.Column(db.String(20), nullable=False, default="CASHIER")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    pin_hash = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    organization = db.relationship("Organization", back_populates="members")
    user = db.relationship("User", back_populates="organization_memberships")

    def set_pin(self, pin):
        self.pin_hash = generate_password_hash(str(pin))

    def check_pin(self, pin):
        return bool(self.pin_hash) and check_password_hash(self.pin_hash, str(pin))


class OrganizationInvitation(db.Model):
    __tablename__ = "organization_invitation"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "email",
            name="uq_organization_invitation_email",
        ),
        db.CheckConstraint(
            "role IN ('MANAGER', 'CASHIER')",
            name="ck_organization_invitation_role",
        ),
        db.Index(
            "ix_organization_invitation_pending",
            "organization_id",
            "accepted_at",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
    )
    invited_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    email = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    accepted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    organization = db.relationship("Organization")
    invited_by_member = db.relationship("OrganizationMember")


class Product(db.Model):
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "sku", name="uq_product_organization_sku"
        ),
        db.UniqueConstraint(
            "organization_id", "barcode", name="uq_product_organization_barcode"
        ),
        db.UniqueConstraint(
            "user_id",
            "sku",
            name="uq_product_user_sku",
        ),
        db.Index(
            "uq_product_user_barcode",
            "user_id",
            "barcode",
            unique=True,
        ),
        db.Index("ix_product_user_name", "user_id", "name"),
        db.Index("ix_product_organization_name", "organization_id", "name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True,
        index=True,
    )

    sku = db.Column(db.String(64), nullable=False)
    barcode = db.Column(db.String(64), nullable=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(
        db.String(80),
        nullable=False,
        default="General",
    )
    supplier = db.Column(db.String(120), nullable=True)

    cost_price = db.Column(
        db.Numeric(14, 2),
        nullable=False,
        default=MONEY_ZERO,
    )
    sale_price = db.Column(
        db.Numeric(14, 2),
        nullable=False,
        default=MONEY_ZERO,
    )
    stock = db.Column(
        db.Integer,
        nullable=False,
        default=0,
    )
    min_stock = db.Column(
        db.Integer,
        nullable=False,
        default=5,
    )

    is_active = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    sales = db.relationship(
        "Sale",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    restock_events = db.relationship(
        "InventoryRestockEvent",
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @property
    def margin(self):
        if not self.sale_price:
            return 0

        return round(
            (
                (self.sale_price - self.cost_price)
                / self.sale_price
            )
            * 100,
            1,
        )

    @property
    def inventory_value(self):
        return money_decimal(self.stock * self.cost_price)


class InventoryRestockEvent(db.Model):
    __tablename__ = "inventory_restock_event"
    __table_args__ = (
        db.CheckConstraint("quantity > 0", name="ck_restock_quantity_positive"),
        db.CheckConstraint(
            "stock_before >= 0",
            name="ck_restock_stock_before_nonnegative",
        ),
        db.CheckConstraint(
            "stock_after >= stock_before",
            name="ck_restock_stock_after_valid",
        ),
        db.Index(
            "ix_restock_user_created_at",
            "user_id",
            "created_at",
        ),
        db.Index(
            "ix_restock_product_created_at",
            "product_id",
            "created_at",
        ),
        db.Index(
            "ix_restock_organization_created_at",
            "organization_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    quantity = db.Column(db.Integer, nullable=False)
    stock_before = db.Column(db.Integer, nullable=False)
    stock_after = db.Column(db.Integer, nullable=False)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    product = db.relationship(
        "Product",
        back_populates="restock_events",
    )


class SalesTicket(db.Model):
    __tablename__ = "sales_ticket"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "number",
            name="uq_sales_ticket_user_number",
        ),
        db.UniqueConstraint(
            "organization_id",
            "number",
            name="uq_sales_ticket_organization_number",
        ),
        db.UniqueConstraint(
            "organization_id",
            "public_id",
            name="uq_sales_ticket_organization_public_id",
        ),
        db.UniqueConstraint(
            "user_id",
            "public_id",
            name="uq_sales_ticket_user_public_id",
        ),
        db.Index(
            "ix_sales_ticket_user_created_at",
            "user_id",
            "created_at",
        ),
        db.Index(
            "ix_sales_ticket_organization_created_at",
            "organization_id",
            "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    number = db.Column(db.Integer, nullable=False)
    public_id = db.Column(db.String(36), nullable=False)
    payment_method = db.Column(db.String(20), nullable=True)
    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    sales = db.relationship(
        "Sale",
        back_populates="sales_ticket",
        passive_deletes=True,
    )

    @property
    def folio(self):
        return f"TKT-{self.number:06d}"


class Sale(db.Model):
    __table_args__ = (
        db.Index(
            "ix_sale_user_created_at",
            "user_id",
            "created_at",
        ),
        db.Index(
            "ix_sale_user_ticket",
            "user_id",
            "ticket_id",
        ),
        db.Index(
            "ix_sale_organization_created_at",
            "organization_id",
            "created_at",
        ),
        db.Index(
            "ix_sale_organization_ticket",
            "organization_id",
            "ticket_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True,
        index=True,
    )

    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    quantity = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )
    unit_price = db.Column(
        db.Numeric(14, 2),
        nullable=False,
        default=MONEY_ZERO,
    )
    total = db.Column(
        db.Numeric(14, 2),
        nullable=False,
        default=MONEY_ZERO,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    ticket_id = db.Column(db.String(36), nullable=True)
    payment_method = db.Column(db.String(20), nullable=True)
    sales_ticket_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_ticket.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    unit_cost = db.Column(db.Numeric(14, 2), nullable=True)
    cost_is_estimated = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    product = db.relationship(
        "Product",
        back_populates="sales",
    )
    sales_ticket = db.relationship(
        "SalesTicket",
        back_populates="sales",
    )


class Supplier(db.Model):
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "name", name="uq_supplier_organization_name"
        ),
        db.UniqueConstraint(
            "user_id",
            "name",
            name="uq_supplier_user_name",
        ),
        db.Index(
            "ix_supplier_user_name",
            "user_id",
            "name",
        ),
        db.Index(
            "ix_supplier_organization_name", "organization_id", "name"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True,
        index=True,
    )

    # Ya no es unique=True de forma global.
    name = db.Column(
        db.String(120),
        nullable=False,
    )

    contact = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    notes = db.Column(db.Text, nullable=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    first_name = db.Column(db.String(80), nullable=True)
    last_name = db.Column(db.String(80), nullable=True)

    email = db.Column(
        db.String(120),
        unique=True,
        nullable=False,
        index=True,
    )
    password = db.Column(
        db.String(255),
        nullable=False,
    )

    phone = db.Column(db.String(50), nullable=True)
    company_name = db.Column(
        db.String(120),
        nullable=False,
    )
    address = db.Column(db.String(200), nullable=True)
    city = db.Column(db.String(80), nullable=True)
    state = db.Column(db.String(80), nullable=True)
    business_type = db.Column(db.String(80), nullable=True)
    postal_code = db.Column(db.String(10), nullable=True)

    email_verified = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )
    verification_code = db.Column(
        db.String(6),
        nullable=True,
    )
    verification_code_expires = db.Column(
        db.DateTime,
        nullable=True,
    )

    reset_token = db.Column(
        db.String(100),
        nullable=True,
        index=True,
    )
    reset_token_expires = db.Column(
        db.DateTime,
        nullable=True,
    )

    session_token = db.Column(
        db.String(64),
        nullable=True,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    plan = db.Column(
        db.String(20),
        nullable=False,
        default="trial",
    )

    manual_pro_access = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    stripe_customer_id = db.Column(
        db.String(120),
        nullable=True,
        index=True,
    )
    stripe_subscription_id = db.Column(
        db.String(120),
        nullable=True,
        index=True,
    )
    subscription_status = db.Column(
        db.String(30),
        nullable=True,
    )
    current_period_end = db.Column(
        db.DateTime,
        nullable=True,
    )
    next_payment_attempt = db.Column(
        db.DateTime,
        nullable=True,
    )
    stripe_subscription_updated_at = db.Column(
        db.DateTime,
        nullable=True,
    )
    stripe_invoice_updated_at = db.Column(
        db.DateTime,
        nullable=True,
    )
    cancel_at_period_end = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    trial_warning_sent = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
    )

    rfc = db.Column(db.String(20), nullable=True)
    tax_regime = db.Column(db.String(120), nullable=True)
    preferred_language = db.Column(
        db.String(5),
        nullable=False,
        default="es",
    )
    timezone = db.Column(
        db.String(64),
        nullable=False,
        default="America/Mexico_City",
    )
    next_ticket_number = db.Column(
        db.Integer,
        nullable=False,
        default=1,
    )

    products = db.relationship(
        "Product",
        backref="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sales = db.relationship(
        "Sale",
        backref="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sales_tickets = db.relationship(
        "SalesTicket",
        backref="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    suppliers = db.relationship(
        "Supplier",
        backref="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    organization_memberships = db.relationship(
        "OrganizationMember",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def set_password(self, password):
        self.password = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(
            self.password,
            password,
        )


class StripeWebhookEvent(db.Model):
    __tablename__ = "stripe_webhook_event"

    id = db.Column(db.Integer, primary_key=True)
    stripe_event_id = db.Column(
        db.String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    event_type = db.Column(db.String(120), nullable=False)
    object_id = db.Column(db.String(255), nullable=True, index=True)
    stripe_created_at = db.Column(db.DateTime, nullable=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    failed_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="pending")
    error_message = db.Column(db.Text, nullable=True)
