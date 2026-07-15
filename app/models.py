from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

from . import db


class Product(db.Model):
    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "sku",
            name="uq_product_user_sku",
        ),
        db.Index("ix_product_user_name", "user_id", "name"),
        db.Index("ix_product_user_barcode", "user_id", "barcode"),
    )

    id = db.Column(db.Integer, primary_key=True)
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
        db.Float,
        nullable=False,
        default=0,
    )
    sale_price = db.Column(
        db.Float,
        nullable=False,
        default=0,
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
        return round(self.stock * self.cost_price, 2)


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
    )

    id = db.Column(db.Integer, primary_key=True)

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
        db.Float,
        nullable=False,
        default=0,
    )
    total = db.Column(
        db.Float,
        nullable=False,
        default=0,
    )

    created_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    ticket_id = db.Column(db.String(36), nullable=True)

    product = db.relationship(
        "Product",
        back_populates="sales",
    )


class Supplier(db.Model):
    __table_args__ = (
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
    )

    id = db.Column(db.Integer, primary_key=True)

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
    suppliers = db.relationship(
        "Supplier",
        backref="owner",
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
