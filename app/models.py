from datetime import datetime

from sqlalchemy import event, inspect
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
    monthly_report_enabled = db.Column(
        db.Boolean, nullable=False, default=False
    )
    monthly_report_recipient = db.Column(db.String(120), nullable=True)
    monthly_sales_goal = db.Column(db.Numeric(14, 2), nullable=True)
    next_purchase_order_number = db.Column(
        db.Integer, nullable=False, default=1
    )
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
    monthly_reports = db.relationship(
        "MonthlyOwnerReport",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    purchase_orders = db.relationship(
        "PurchaseOrder",
        back_populates="organization",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


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
    inventory_movements = db.relationship(
        "InventoryMovement",
        back_populates="product",
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


class InventoryMovement(db.Model):
    """Immutable stock ledger entry scoped to one organization."""

    __tablename__ = "inventory_movement"
    __table_args__ = (
        db.CheckConstraint(
            "movement_type IN "
            "('OPENING_BALANCE', 'SALE', 'SALE_CANCELLATION', 'RETURN', "
            "'RESTOCK', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT', 'WASTE', "
            "'DAMAGE', 'INTERNAL_USE', 'PHYSICAL_COUNT', 'IMPORT')",
            name="ck_inventory_movement_type",
        ),
        db.CheckConstraint(
            "stock_before >= 0 AND stock_after >= 0",
            name="ck_inventory_movement_stock_nonnegative",
        ),
        db.CheckConstraint(
            "quantity_delta = stock_after - stock_before",
            name="ck_inventory_movement_delta_matches_stock",
        ),
        db.Index(
            "ix_inventory_movement_org_created",
            "organization_id",
            "created_at",
        ),
        db.Index(
            "ix_inventory_movement_product_created",
            "product_id",
            "created_at",
            "id",
        ),
        db.Index(
            "ix_inventory_movement_org_type_created",
            "organization_id",
            "movement_type",
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
    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    performed_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sale_id = db.Column(
        db.Integer,
        db.ForeignKey("sale.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    sales_ticket_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_ticket.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    restock_event_id = db.Column(
        db.Integer,
        db.ForeignKey("inventory_restock_event.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    movement_type = db.Column(db.String(30), nullable=False)
    quantity_delta = db.Column(db.Integer, nullable=False)
    stock_before = db.Column(db.Integer, nullable=False)
    stock_after = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=True)
    product_name = db.Column(db.String(160), nullable=False)
    product_sku = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    organization = db.relationship("Organization")
    product = db.relationship("Product", back_populates="inventory_movements")
    performed_by_member = db.relationship("OrganizationMember")
    sale = db.relationship("Sale")
    sales_ticket = db.relationship("SalesTicket")
    restock_event = db.relationship("InventoryRestockEvent")

    @property
    def direction_class(self):
        if self.quantity_delta > 0:
            return "in"
        if self.quantity_delta < 0:
            return "out"
        return "neutral"

    @property
    def quantity_class(self):
        if self.quantity_delta > 0:
            return "kardex-v1__positive"
        if self.quantity_delta < 0:
            return "kardex-v1__negative"
        return ""

    @property
    def signed_quantity(self):
        return f"{self.quantity_delta:+d}"


def _prevent_inventory_movement_mutation(mapper, connection, target):
    raise ValueError("Inventory movements are immutable.")


event.listen(InventoryMovement, "before_update", _prevent_inventory_movement_mutation)
event.listen(InventoryMovement, "before_delete", _prevent_inventory_movement_mutation)


class Customer(db.Model):
    """A lightweight customer record owned by one organization."""

    __tablename__ = "customer"
    __table_args__ = (
        db.Index(
            "ix_customer_org_active_name",
            "organization_id",
            "is_active",
            "name",
        ),
        db.Index(
            "ix_customer_org_phone",
            "organization_id",
            "phone_normalized",
        ),
        db.CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_customer_name_not_blank",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    phone_normalized = db.Column(db.String(20), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    credit_enabled = db.Column(db.Boolean, nullable=False, default=False)
    credit_limit = db.Column(
        db.Numeric(14, 2), nullable=False, default=MONEY_ZERO
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    organization = db.relationship("Organization")
    created_by_member = db.relationship("OrganizationMember")
    sales_tickets = db.relationship(
        "SalesTicket",
        back_populates="customer",
        passive_deletes=True,
    )
    credit_movements = db.relationship(
        "CustomerCreditMovement",
        back_populates="customer",
        passive_deletes=True,
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
    amount_received = db.Column(db.Numeric(14, 2), nullable=True)
    change_amount = db.Column(db.Numeric(14, 2), nullable=True)
    cashier_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customer.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    cash_register_session_id = db.Column(
        db.Integer,
        db.ForeignKey("cash_register_session.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    cash_register_session = db.relationship(
        "CashRegisterSession",
        back_populates="sales_tickets",
    )
    customer = db.relationship("Customer", back_populates="sales_tickets")
    cashier_member = db.relationship(
        "OrganizationMember",
        foreign_keys=[cashier_member_id],
    )

    @property
    def folio(self):
        return f"TKT-{self.number:06d}"


class CashRegisterSession(db.Model):
    __tablename__ = "cash_register_session"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id", "open_key",
            name="uq_cash_register_session_open_register",
        ),
        db.CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name="ck_cash_register_session_status",
        ),
        db.CheckConstraint(
            "opening_cash >= 0",
            name="ck_cash_register_session_opening_cash_nonnegative",
        ),
        db.Index(
            "ix_cash_register_session_organization_opened",
            "organization_id", "opened_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    register_key = db.Column(db.String(40), nullable=False, default="MAIN")
    open_key = db.Column(db.String(40), nullable=True)
    status = db.Column(db.String(10), nullable=False, default="OPEN")
    opened_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    closed_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    opening_cash = db.Column(
        db.Numeric(14, 2), nullable=False, default=MONEY_ZERO
    )
    expected_cash_at_close = db.Column(db.Numeric(14, 2), nullable=True)
    counted_cash = db.Column(db.Numeric(14, 2), nullable=True)
    difference = db.Column(db.Numeric(14, 2), nullable=True)
    closing_notes = db.Column(db.Text, nullable=True)
    opened_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    closed_at = db.Column(db.DateTime, nullable=True)

    organization = db.relationship("Organization")
    opened_by_member = db.relationship(
        "OrganizationMember", foreign_keys=[opened_by_member_id]
    )
    closed_by_member = db.relationship(
        "OrganizationMember", foreign_keys=[closed_by_member_id]
    )
    movements = db.relationship(
        "CashMovement",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CashMovement.created_at",
    )
    sales_tickets = db.relationship(
        "SalesTicket",
        back_populates="cash_register_session",
    )


class CashMovement(db.Model):
    __tablename__ = "cash_movement"
    __table_args__ = (
        db.CheckConstraint("amount > 0", name="ck_cash_movement_amount_positive"),
        db.CheckConstraint(
            "movement_type IN "
            "('OPENING', 'SALE_CASH', 'CREDIT_PAYMENT', 'CASH_IN', "
            "'WITHDRAWAL', 'EXPENSE', 'REFUND')",
            name="ck_cash_movement_type",
        ),
        db.Index(
            "ix_cash_movement_session_created",
            "cash_register_session_id", "created_at",
        ),
        db.Index(
            "ix_cash_movement_organization_created",
            "organization_id", "created_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cash_register_session_id = db.Column(
        db.Integer,
        db.ForeignKey("cash_register_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    performed_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    sales_ticket_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_ticket.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    movement_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    session = db.relationship("CashRegisterSession", back_populates="movements")
    performed_by_member = db.relationship("OrganizationMember")
    sales_ticket = db.relationship("SalesTicket")


class CustomerCreditMovement(db.Model):
    """Immutable account receivable movement for one customer."""

    __tablename__ = "customer_credit_movement"
    __table_args__ = (
        db.CheckConstraint(
            "movement_type IN ('CHARGE', 'PAYMENT', 'REVERSAL')",
            name="ck_customer_credit_movement_type",
        ),
        db.CheckConstraint(
            "amount > 0 AND balance_before >= 0 AND balance_after >= 0",
            name="ck_customer_credit_movement_amounts",
        ),
        db.CheckConstraint(
            "(movement_type = 'CHARGE' AND balance_after = balance_before + amount) "
            "OR (movement_type IN ('PAYMENT', 'REVERSAL') "
            "AND balance_after = balance_before - amount)",
            name="ck_customer_credit_movement_balance",
        ),
        db.Index(
            "ix_customer_credit_org_created",
            "organization_id",
            "created_at",
        ),
        db.Index(
            "ix_customer_credit_customer_created",
            "customer_id",
            "created_at",
            "id",
        ),
        db.UniqueConstraint(
            "organization_id",
            "request_id",
            name="uq_customer_credit_org_request",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    customer_id = db.Column(
        db.Integer,
        db.ForeignKey("customer.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    performed_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    authorized_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
    )
    sales_ticket_id = db.Column(
        db.Integer,
        db.ForeignKey("sales_ticket.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    cash_register_session_id = db.Column(
        db.Integer,
        db.ForeignKey("cash_register_session.id", ondelete="SET NULL"),
        nullable=True,
    )
    movement_type = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Numeric(14, 2), nullable=False)
    balance_before = db.Column(db.Numeric(14, 2), nullable=False)
    balance_after = db.Column(db.Numeric(14, 2), nullable=False)
    payment_method = db.Column(db.String(20), nullable=True)
    request_id = db.Column(db.String(36), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    organization = db.relationship("Organization")
    customer = db.relationship("Customer", back_populates="credit_movements")
    performed_by_member = db.relationship(
        "OrganizationMember", foreign_keys=[performed_by_member_id]
    )
    authorized_by_member = db.relationship(
        "OrganizationMember", foreign_keys=[authorized_by_member_id]
    )
    sales_ticket = db.relationship("SalesTicket")
    cash_register_session = db.relationship("CashRegisterSession")


def _prevent_credit_movement_mutation(mapper, connection, target):
    raise ValueError("Customer credit movements are immutable.")


event.listen(
    CustomerCreditMovement, "before_update", _prevent_credit_movement_mutation
)
event.listen(
    CustomerCreditMovement, "before_delete", _prevent_credit_movement_mutation
)


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
    subscription_plan_code = db.Column(db.String(20), nullable=True)
    trial_plan_code = db.Column(
        db.String(20), nullable=False, default="STARTER"
    )
    pending_plan_code = db.Column(db.String(20), nullable=True)
    pending_plan_effective_at = db.Column(db.DateTime, nullable=True)
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


class MonthlyOwnerReport(db.Model):
    __tablename__ = "monthly_owner_report"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "report_year",
            "report_month",
            name="uq_monthly_owner_report_period",
        ),
        db.CheckConstraint(
            "report_month >= 1 AND report_month <= 12",
            name="ck_monthly_owner_report_month",
        ),
        db.CheckConstraint(
            "status IN ('pending', 'generated', 'sending', 'sent', 'failed')",
            name="ck_monthly_owner_report_status",
        ),
        db.Index(
            "ix_monthly_owner_report_status_period",
            "status",
            "report_year",
            "report_month",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_year = db.Column(db.Integer, nullable=False)
    report_month = db.Column(db.Integer, nullable=False)
    recipient = db.Column(db.String(120), nullable=False)
    generated_at = db.Column(db.DateTime, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    last_attempt_at = db.Column(db.DateTime, nullable=True)
    next_retry_at = db.Column(db.DateTime, nullable=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="pending")
    failure_code = db.Column(db.String(40), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    snapshot_json = db.Column(db.Text, nullable=True)
    snapshot_hash = db.Column(db.String(64), nullable=True)
    snapshot_version = db.Column(db.Integer, nullable=False, default=1)
    generated_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    manual_generation = db.Column(
        db.Boolean, nullable=False, default=False
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    organization = db.relationship(
        "Organization", back_populates="monthly_reports"
    )
    generated_by_member = db.relationship("OrganizationMember")


def _prevent_monthly_snapshot_mutation(mapper, connection, target):
    state = inspect(target)
    snapshot_history = state.attrs.snapshot_json.history
    if (
        snapshot_history.has_changes()
        and snapshot_history.added
        and snapshot_history.added[0] is not None
        and (
            not snapshot_history.deleted
            or snapshot_history.deleted[0] is None
        )
    ):
        # A report row is claimed before its snapshot is generated. Allow
        # that one-time initialization, but never replacement afterwards.
        return
    for attribute_name in (
        "snapshot_json",
        "snapshot_hash",
        "snapshot_version",
    ):
        history = state.attrs[attribute_name].history
        if history.deleted and history.deleted[0] is not None:
            raise ValueError("Monthly report snapshots are immutable.")


event.listen(
    MonthlyOwnerReport,
    "before_update",
    _prevent_monthly_snapshot_mutation,
)


class PurchaseOrder(db.Model):
    """A supplier order scoped to one organization."""

    __tablename__ = "purchase_order"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "number",
            name="uq_purchase_order_organization_number",
        ),
        db.CheckConstraint(
            "status IN "
            "('DRAFT', 'ORDERED', 'PARTIALLY_RECEIVED', "
            "'RECEIVED', 'CANCELLED')",
            name="ck_purchase_order_status",
        ),
        db.Index(
            "ix_purchase_order_org_status_created",
            "organization_id",
            "status",
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
    number = db.Column(db.String(24), nullable=False)
    supplier_id = db.Column(
        db.Integer,
        db.ForeignKey("supplier.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    supplier_name = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="DRAFT")
    notes = db.Column(db.Text, nullable=True)
    created_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    ordered_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    organization = db.relationship(
        "Organization", back_populates="purchase_orders"
    )
    supplier = db.relationship("Supplier")
    created_by_member = db.relationship("OrganizationMember")
    items = db.relationship(
        "PurchaseOrderItem",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PurchaseOrderItem.id",
    )
    receipts = db.relationship(
        "PurchaseReceipt",
        back_populates="purchase_order",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PurchaseReceipt.created_at",
    )


class PurchaseOrderItem(db.Model):
    __tablename__ = "purchase_order_item"
    __table_args__ = (
        db.UniqueConstraint(
            "purchase_order_id",
            "product_id",
            name="uq_purchase_order_item_product",
        ),
        db.CheckConstraint(
            "ordered_quantity > 0",
            name="ck_purchase_order_item_ordered_positive",
        ),
        db.CheckConstraint(
            "received_quantity >= 0 "
            "AND received_quantity <= ordered_quantity",
            name="ck_purchase_order_item_received_valid",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_order.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id = db.Column(
        db.Integer,
        db.ForeignKey("product.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    product_name = db.Column(db.String(160), nullable=False)
    product_sku = db.Column(db.String(64), nullable=False)
    ordered_quantity = db.Column(db.Integer, nullable=False)
    received_quantity = db.Column(
        db.Integer, nullable=False, default=0
    )
    unit_cost = db.Column(db.Numeric(14, 2), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )

    purchase_order = db.relationship(
        "PurchaseOrder", back_populates="items"
    )
    product = db.relationship("Product")
    receipt_items = db.relationship(
        "PurchaseReceiptItem",
        back_populates="order_item",
        passive_deletes=True,
    )

    @property
    def pending_quantity(self):
        return max(
            int(self.ordered_quantity) - int(self.received_quantity),
            0,
        )


class PurchaseReceipt(db.Model):
    __tablename__ = "purchase_receipt"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "request_id",
            name="uq_purchase_receipt_organization_request",
        ),
        db.Index(
            "ix_purchase_receipt_org_created",
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
    purchase_order_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_order.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    received_by_member_id = db.Column(
        db.Integer,
        db.ForeignKey("organization_member.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    request_id = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )

    purchase_order = db.relationship(
        "PurchaseOrder", back_populates="receipts"
    )
    received_by_member = db.relationship("OrganizationMember")
    items = db.relationship(
        "PurchaseReceiptItem",
        back_populates="receipt",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class PurchaseReceiptItem(db.Model):
    __tablename__ = "purchase_receipt_item"
    __table_args__ = (
        db.UniqueConstraint(
            "purchase_receipt_id",
            "purchase_order_item_id",
            name="uq_purchase_receipt_item_order_item",
        ),
        db.CheckConstraint(
            "quantity > 0",
            name="ck_purchase_receipt_item_quantity_positive",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    purchase_receipt_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_receipt.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    purchase_order_item_id = db.Column(
        db.Integer,
        db.ForeignKey("purchase_order_item.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    quantity = db.Column(db.Integer, nullable=False)
    unit_cost = db.Column(db.Numeric(14, 2), nullable=True)
    restock_event_id = db.Column(
        db.Integer,
        db.ForeignKey("inventory_restock_event.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    receipt = db.relationship(
        "PurchaseReceipt", back_populates="items"
    )
    order_item = db.relationship(
        "PurchaseOrderItem", back_populates="receipt_items"
    )
    restock_event = db.relationship("InventoryRestockEvent")


class AiNarrativeRun(db.Model):
    """Tenant-scoped audit, cache and cost record for controlled AI copy."""

    __tablename__ = "ai_narrative_run"
    __table_args__ = (
        db.UniqueConstraint(
            "organization_id",
            "feature_name",
            "language",
            "data_hash",
            name="uq_ai_narrative_cache",
        ),
        db.CheckConstraint(
            "status IN ('SUCCESS', 'FAILED', 'FALLBACK', 'LIMITED')",
            name="ck_ai_narrative_status",
        ),
        db.Index(
            "ix_ai_narrative_org_feature_created",
            "organization_id",
            "feature_name",
            "created_at",
        ),
        db.Index(
            "ix_ai_narrative_created_status",
            "created_at",
            "status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    organization_id = db.Column(
        db.Integer,
        db.ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    feature_name = db.Column(db.String(40), nullable=False)
    language = db.Column(db.String(5), nullable=False, default="es")
    data_hash = db.Column(db.String(64), nullable=False)
    data_period = db.Column(db.String(80), nullable=False)
    model = db.Column(db.String(80), nullable=True)
    status = db.Column(db.String(16), nullable=False)
    output_json = db.Column(db.Text, nullable=False)
    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    estimated_cost_microusd = db.Column(
        db.BigInteger, nullable=False, default=0
    )
    latency_ms = db.Column(db.Integer, nullable=False, default=0)
    error_code = db.Column(db.String(80), nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    expires_at = db.Column(db.DateTime, nullable=False)
