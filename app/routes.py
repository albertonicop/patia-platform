# -*- coding: utf-8 -*-
import resend
from email_validator import validate_email, EmailNotValidError
import math
import string
from datetime import datetime, timedelta
from io import BytesIO
import stripe
import secrets
import uuid
import re
from flask import Blueprint, render_template, request, redirect, url_for, flash as flask_flash, session, current_app, send_file, jsonify, abort
from flask_babel import force_locale, gettext
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from urllib.parse import urljoin, urlparse
from . import csrf, db, limiter
from .models import Product, Sale, StripeWebhookEvent, Supplier, User

main = Blueprint("main", __name__)
SUPPORTED_LANGUAGES = {"es", "en"}


def flash(message, category="message"):
    """Translate application-owned notices without altering user-provided data."""
    return flask_flash(gettext(message), category)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = User.query.get(user_id)
    if not user:
        return None
    if user.session_token and user.session_token != session.get("session_token"):
        session.clear()
        session["kicked_out"] = True
        return None
    sync_user_plan(user)
    return user


def _safe_next_url(candidate):
    if not candidate:
        return url_for("main.dashboard")
    base = urlparse(request.host_url)
    target = urlparse(urljoin(request.host_url, candidate))
    if target.scheme in {"http", "https"} and target.netloc == base.netloc:
        return target.path + (f"?{target.query}" if target.query else "")
    return url_for("main.dashboard")


@main.route("/language", methods=["POST"])
def set_language():
    language = request.form.get("language", "").lower()
    if language not in SUPPORTED_LANGUAGES:
        language = "es"
    session["language"] = language
    user = current_user()
    if user and user.preferred_language != language:
        user.preferred_language = language
        db.session.commit()
    return redirect(_safe_next_url(request.form.get("next") or request.referrer))


def trial_expired(user):
    if not user:
        return True
    if has_pro_access(user):
        return False
    days_used = (datetime.utcnow() - user.created_at).days
    return days_used >= 14


def _trial_access_response(user, *, json_response=False):
    if not trial_expired(user):
        return None
    if json_response:
        return jsonify({
            "ok": False,
            "error": gettext("Tu periodo de prueba terminó. Activa PATIA Pro para continuar."),
        }), 403
    flash(
        "Tu periodo de prueba terminó. Activa PATIA Pro para continuar.",
        "danger",
    )
    return render_template("trial_expired.html"), 403


def money(value):
    return f"${value:,.2f} MXN"


def _sale_ticket_key(sale):
    """Identificador interno estable, incluso para ventas históricas sin UUID."""
    return sale.ticket_id or f"sale-{sale.id}"


def _short_sale_folio(sales):
    """Folio legible y estable sin reemplazar el identificador interno."""
    ticket_id = sales[0].ticket_id
    if ticket_id:
        try:
            folio_number = uuid.UUID(ticket_id).int % 1_000_000
            return f"V-{folio_number:06d}"
        except (ValueError, TypeError, AttributeError):
            pass
    return f"V-{min(sale.id for sale in sales):06d}"


PAYMENT_METHOD_LABELS = {
    "cash": "Efectivo",
    "card": "Tarjeta",
    "transfer": "Transferencia",
    "other": "Otro",
}


def _payment_method_label(value):
    return gettext(PAYMENT_METHOD_LABELS.get(value, "No especificado"))


def _subscription_status_label(value):
    labels = {
        "active": gettext("Activa"),
        "trialing": gettext("En prueba"),
        "past_due": gettext("Pago pendiente"),
        "unpaid": gettext("Sin pagar"),
        "canceled": gettext("Cancelada"),
        "incomplete": gettext("Incompleta"),
        "incomplete_expired": gettext("Incompleta y vencida"),
        "paused": gettext("Pausada"),
    }
    return labels.get((value or "").lower(), gettext("Sin suscripción"))


def _translated_payment_method_labels():
    return {key: gettext(label) for key, label in PAYMENT_METHOD_LABELS.items()}


def _group_sales_by_ticket(sales, *, limit=None):
    grouped = {}
    for sale in sales:
        key = _sale_ticket_key(sale)
        group = grouped.setdefault(key, {
            "ticket_id": key,
            "sales": [],
            "created_at": sale.created_at,
            "total": 0,
            "item_count": 0,
            "payment_method": sale.payment_method,
        })
        group["sales"].append(sale)
        group["total"] += sale.total
        group["item_count"] += sale.quantity
        if sale.created_at < group["created_at"]:
            group["created_at"] = sale.created_at
    result = list(grouped.values())
    for group in result:
        group["folio"] = _short_sale_folio(group["sales"])
    result.sort(key=lambda group: group["created_at"], reverse=True)
    return result[:limit] if limit else result


MANAGED_SUBSCRIPTION_STATUSES = {"active", "trialing", "past_due", "unpaid"}
KNOWN_SUBSCRIPTION_STATUSES = {
    "active", "trialing", "past_due", "unpaid", "canceled",
    "incomplete", "incomplete_expired", "paused",
}


class StripeEventIgnored(Exception):
    """Evento válido de Stripe que no pertenece a esta integración."""


def _as_utc_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.utcfromtimestamp(int(value))


def has_pro_access(user, now=None):
    if not user:
        return False
    if user.manual_pro_access:
        return True

    now = now or datetime.utcnow()
    status = (user.subscription_status or "").lower()
    period_end = user.current_period_end
    if status in {"active", "trialing"}:
        return bool(period_end and period_end >= now)
    if status == "past_due" and period_end:
        grace_days = current_app.config.get("STRIPE_PAST_DUE_GRACE_DAYS", 3)
        return period_end + timedelta(days=grace_days) >= now
    return False


def sync_user_plan(user, now=None):
    user.plan = "pro" if has_pro_access(user, now=now) else "trial"


def _public_url(path):
    return f"{current_app.config['PUBLIC_BASE_URL']}{path}"


def _subscription_has_configured_price(subscription):
    expected = current_app.config["STRIPE_PRICE_ID"]
    items = (subscription.get("items") or {}).get("data") or []
    return any((item.get("price") or {}).get("id") == expected for item in items)


def _subscription_period_end(subscription):
    value = subscription.get("current_period_end")
    if value:
        return _as_utc_datetime(value)
    periods = [
        item.get("current_period_end")
        for item in ((subscription.get("items") or {}).get("data") or [])
        if item.get("current_period_end")
    ]
    return _as_utc_datetime(max(periods)) if periods else None


def _invoice_subscription_id(invoice):
    subscription_id = invoice.get("subscription")
    if subscription_id:
        return subscription_id
    details = (invoice.get("parent") or {}).get("subscription_details") or {}
    return details.get("subscription")


def _event_is_newer(user, stripe_created_at, event_family):
    if event_family == "invoice":
        watermark = user.stripe_invoice_updated_at
    elif event_family == "subscription":
        watermark = user.stripe_subscription_updated_at
    else:
        raise ValueError("Familia de evento Stripe no soportada.")
    return not watermark or stripe_created_at >= watermark


def _validate_subscription(subscription, user=None, customer_id=None):
    if not _subscription_has_configured_price(subscription):
        raise StripeEventIgnored("La suscripción no usa STRIPE_PRICE_ID.")
    subscription_customer = subscription.get("customer")
    if customer_id and subscription_customer != customer_id:
        raise StripeEventIgnored("El cliente de Stripe no coincide.")
    if user and user.stripe_customer_id and subscription_customer != user.stripe_customer_id:
        raise StripeEventIgnored("La suscripción no pertenece al cliente guardado.")


def _find_subscription_user(subscription):
    subscription_id = subscription.get("id")
    user = User.query.filter_by(stripe_subscription_id=subscription_id).first()
    if user:
        return user
    user_id = (subscription.get("metadata") or {}).get("user_id")
    if user_id and str(user_id).isdigit():
        return db.session.get(User, int(user_id))
    return None


def _ensure_stripe_ids_available(user, customer_id, subscription_id):
    customer_owner = User.query.filter(
        User.stripe_customer_id == customer_id,
        User.id != user.id,
    ).first()
    subscription_owner = User.query.filter(
        User.stripe_subscription_id == subscription_id,
        User.id != user.id,
    ).first()
    if customer_owner or subscription_owner:
        raise StripeEventIgnored("Identificador Stripe ya vinculado a otro usuario.")


def _sync_subscription_state(user, subscription, stripe_created_at, deleted=False):
    if not _event_is_newer(user, stripe_created_at, "subscription"):
        return False
    status = "canceled" if deleted else (subscription.get("status") or "").lower()
    if status not in KNOWN_SUBSCRIPTION_STATUSES:
        raise StripeEventIgnored(f"Estado de suscripción no soportado: {status}")
    user.stripe_customer_id = subscription.get("customer") or user.stripe_customer_id
    user.stripe_subscription_id = subscription.get("id") or user.stripe_subscription_id
    user.subscription_status = status
    user.current_period_end = _subscription_period_end(subscription) or user.current_period_end
    user.cancel_at_period_end = bool(subscription.get("cancel_at_period_end", False))
    user.stripe_subscription_updated_at = stripe_created_at
    if status != "past_due":
        user.next_payment_attempt = None
    sync_user_plan(user)
    return True


def _has_managed_stripe_subscription(user):
    if not user.stripe_subscription_id:
        return False
    if user.subscription_status in MANAGED_SUBSCRIPTION_STATUSES:
        return True
    if current_app.config["STRIPE_DISABLED"]:
        return True
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    try:
        subscription = stripe.Subscription.retrieve(user.stripe_subscription_id)
        _validate_subscription(subscription, user=user)
        _sync_subscription_state(user, subscription, datetime.utcnow())
        db.session.commit()
        return subscription.get("status") in MANAGED_SUBSCRIPTION_STATUSES
    except StripeEventIgnored:
        db.session.rollback()
        return False
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "No se pudo comprobar la suscripción antes de eliminar al usuario"
        )
        return True


def send_email(to, subject, html, language="es"):
    api_key = current_app.config.get("RESEND_API_KEY")
    sender = current_app.config.get("RESEND_FROM")
    if not api_key or not sender:
        current_app.logger.error("Resend no está configurado; correo no enviado.")
        return False
    try:
        with force_locale(language if language in SUPPORTED_LANGUAGES else "es"):
            resend.api_key = api_key
            resend.Emails.send({
                "from": sender,
                "to": to,
                "subject": gettext(subject),
                "html": html,
            })
        return True
    except Exception:
        current_app.logger.exception("No se pudo enviar un correo con Resend.")
        return False


@main.route("/register", methods=["GET", "POST"])
@limiter.limit("3 per hour", methods=["POST"])
def register():
    if request.method == "POST":
        selected_language = session.get("language", "es")
        if selected_language not in SUPPORTED_LANGUAGES:
            selected_language = "es"
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        required_fields = {
            "first_name": "nombre",
            "last_name": "apellido",
            "company_name": "nombre de la empresa",
            "phone": "teléfono",
            "address": "dirección",
            "city": "ciudad",
            "state": "estado",
            "business_type": "giro del negocio",
            "postal_code": "código postal",
            "email": "correo",
            "password": "contraseña",
        }
        missing = [
            label
            for field, label in required_fields.items()
            if not request.form.get(field, "").strip()
        ]
        if missing:
            flash(gettext("Completa todos los campos obligatorios."), "danger")
            return render_template(
                "auth.html",
                title=gettext("Crear cuenta"),
                button=gettext("Crear cuenta"),
                mode="register",
                plan=request.form.get("plan"),
                form_data=request.form,
            ), 400
        try:
            validate_email(email, check_deliverability=True)
        except EmailNotValidError:
            flash(gettext("El correo no es válido o no existe."), "danger")
            return render_template(
                "auth.html",
                title=gettext("Crear cuenta"),
                button=gettext("Crear cuenta"),
                mode="register",
                plan=request.form.get("plan"),
                form_data=request.form,
            ), 400

        if len(password) < 8:
            flash(gettext("La contraseña debe tener al menos 8 caracteres."), "danger")
            return render_template(
                "auth.html",
                title=gettext("Crear cuenta"),
                button=gettext("Crear cuenta"),
                mode="register",
                plan=request.form.get("plan"),
                form_data=request.form,
            ), 400
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip()
        business_type = request.form.get("business_type", "").strip()
        postal_code = request.form.get("postal_code", "").strip()

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash(gettext("Ese correo ya está registrado."), "danger")
            return render_template(
                "auth.html",
                title=gettext("Crear cuenta"),
                button=gettext("Crear cuenta"),
                mode="register",
                plan=request.form.get("plan"),
                form_data=request.form,
            ), 409

        user = User(email=email, company_name=company_name)
        user.preferred_language = selected_language
        user.first_name = first_name
        user.last_name = last_name
        user.phone = phone
        user.address = address
        user.city = city
        user.state = state
        user.business_type = business_type
        user.postal_code = postal_code
        user.set_password(password)

        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash(gettext("Ese correo ya está registrado."), "danger")
            return render_template(
                "auth.html",
                title=gettext("Crear cuenta"),
                button=gettext("Crear cuenta"),
                mode="register",
                plan=request.form.get("plan"),
                form_data=request.form,
            ), 409

        session.clear()
        session["language"] = user.preferred_language
        session["user_id"] = user.id
        session["post_verify_destination"] = (
            "subscribe"
            if request.args.get("plan") == "pro" or request.form.get("plan") == "pro"
            else "dashboard"
        )

        code = "".join(secrets.choice(string.digits) for _ in range(6))
        user.verification_code = code
        user.verification_code_expires = datetime.utcnow() + timedelta(minutes=30)
        db.session.commit()

        with force_locale(user.preferred_language):
            email_sent = send_email(
                to=user.email,
                subject=gettext("Verifica tu correo en PATIA"),
                html=f"""
            <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                <img src="{_public_url('/static/img/logo-patia.png')}" style="width:160px;margin-bottom:24px;">
                <h1 style="color:#29d3a8;">{gettext("Verifica tu correo")}</h1>
                <p style="color:#9aa8c7;font-size:16px;">{gettext("Tu código de verificación es:")}</p>
                <div style="font-size:48px;font-weight:900;letter-spacing:12px;color:#fff;margin:24px 0;">{code}</div>
                <p style="color:#9aa8c7;font-size:14px;">{gettext("Este código expira en 30 minutos.")}</p>
            </div>
            """,
                language=user.preferred_language,
            )
        if not email_sent:
            flash(
                "No pudimos enviar el código. Usa Reenviar código para intentarlo nuevamente.",
                "danger",
            )

        return redirect(url_for("main.verify_email"))

    return render_template("auth.html", title=gettext("Crear cuenta"), button=gettext("Crear cuenta"), mode="register", plan=request.args.get("plan"), form_data={})


@main.route("/verify-email", methods=["GET", "POST"])
@limiter.limit("10 per 10 minutes", methods=["POST"])
def verify_email():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    if user.email_verified:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()

        if not user.verification_code or not user.verification_code_expires:
            flash(gettext("Código inválido."), "danger")
            return redirect(url_for("main.verify_email"))

        if datetime.utcnow() > user.verification_code_expires:
            flash(gettext("El código expiró. Solicita uno nuevo."), "danger")
            return redirect(url_for("main.verify_email"))

        if code != user.verification_code:
            flash(gettext("Código incorrecto."), "danger")
            return redirect(url_for("main.verify_email"))

        user.email_verified = True
        user.verification_code = None
        user.verification_code_expires = None
        db.session.commit()
        with force_locale(user.preferred_language):
            send_email(
                to=user.email,
                subject=gettext("¡Bienvenido a PATIA!"),
                html=f"""
            <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                <img src="{_public_url('/static/img/logo-patia.png')}" style="width:160px;margin-bottom:24px;">
                <h1 style="color:#29d3a8;">{gettext("Bienvenido a PATIA, %(name)s!", name=user.first_name or user.company_name)}</h1>
                <p style="color:#9aa8c7;font-size:16px;line-height:1.6;">{gettext("Tu cuenta está lista. Tienes 14 días gratis para explorar todo.")}</p>
                <a href="{_public_url('/products')}" style="display:inline-block;margin-top:24px;padding:14px 28px;background:linear-gradient(135deg,#7c5cff,#29d3a8);color:white;text-decoration:none;border-radius:14px;font-weight:800;">{gettext("Ir a mi inventario")}</a>
            </div>
            """,
                language=user.preferred_language,
            )

        destination = session.pop("post_verify_destination", "dashboard")
        flash(gettext("¡Correo verificado! Bienvenido a PATIA."), "success")
        if destination == "subscribe":
            return redirect(url_for("main.subscribe"))
        return redirect(url_for("main.dashboard"))

    return render_template("verify_email.html", user=user)


@main.route("/resend-verification", methods=["POST"])
@limiter.limit("3 per 15 minutes")
def resend_verification():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    code = "".join(secrets.choice(string.digits) for _ in range(6))
    user.verification_code = code
    user.verification_code_expires = datetime.utcnow() + timedelta(minutes=30)
    db.session.commit()

    with force_locale(user.preferred_language):
        email_sent = send_email(
            to=user.email,
            subject=gettext("Nuevo código de verificación PATIA"),
            html=f"""
        <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
            <h1 style="color:#29d3a8;">{gettext("Tu nuevo código")}</h1>
            <div style="font-size:48px;font-weight:900;letter-spacing:12px;color:#fff;margin:24px 0;">{code}</div>
            <p style="color:#9aa8c7;font-size:14px;">{gettext("Este código expira en 30 minutos.")}</p>
        </div>
        """,
            language=user.preferred_language,
        )
    if email_sent:
        flash(gettext("Te enviamos un nuevo código de verificación."), "success")
    else:
        flash(gettext("No pudimos enviar el código. Inténtalo nuevamente en unos minutos."), "danger")

    return redirect(url_for("main.verify_email"))


@main.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = secrets.token_urlsafe(36)
            user.reset_token = token
            user.reset_token_expires = datetime.utcnow() + timedelta(minutes=30)
            db.session.commit()
            with force_locale(user.preferred_language):
                email_sent = send_email(
                    to=user.email,
                    subject=gettext("Recupera tu contraseña PATIA"),
                    html=f"""
                <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                    <h1 style="color:#29d3a8;">{gettext("Recuperar contraseña")}</h1>
                    <p style="color:#9aa8c7;">{gettext("Haz clic en el botón para crear una nueva contraseña. Expira en 30 minutos.")}</p>
                    <a href="{_public_url(f'/reset-password/{token}')}" style="display:inline-block;margin-top:24px;padding:14px 28px;background:linear-gradient(135deg,#7c5cff,#29d3a8);color:white;text-decoration:none;border-radius:14px;font-weight:800;">{gettext("Crear nueva contraseña")}</a>
                </div>
                """,
                    language=user.preferred_language,
                )
            if not email_sent:
                flash("No pudimos enviar el enlace. Inténtalo nuevamente en unos minutos.", "danger")
                return redirect(url_for("main.forgot_password"))
        flash(gettext("Si ese correo existe, te enviamos un enlace."), "success")
        return redirect(url_for("main.login"))
    return render_template("forgot_password.html")


@main.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("5 per hour", methods=["POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()
    if (
        not user
        or not user.reset_token_expires
        or user.reset_token_expires < datetime.utcnow()
    ):
        return render_template("reset_password.html", token=None, expired=True)
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash(gettext("La contraseña debe tener al menos 8 caracteres."), "danger")
            return render_template("reset_password.html", token=token, expired=False)
        user.set_password(password)
        user.reset_token = None
        user.reset_token_expires = None
        db.session.commit()
        flash(gettext("Contraseña actualizada. Inicia sesión."), "success")
        return redirect(url_for("main.login"))
    return render_template("reset_password.html", token=token, expired=False)


@main.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash(gettext("Correo o contraseña incorrectos."), "danger")
            return redirect(url_for("main.login"))

        token = secrets.token_hex(32)
        user.session_token = token
        db.session.commit()
        session.clear()
        session["language"] = (
            user.preferred_language
            if user.preferred_language in SUPPORTED_LANGUAGES
            else "es"
        )
        session["user_id"] = user.id
        session["session_token"] = token
        days_used = (datetime.utcnow() - user.created_at).days
        if days_used >= 12 and not user.trial_warning_sent and not has_pro_access(user):
            with force_locale(user.preferred_language):
                warning_sent = send_email(
                    to=user.email,
                    subject=gettext("Tu prueba gratuita de PATIA termina en 2 días"),
                    html=f"""
                <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                    <img src="{_public_url('/static/img/logo-patia.png')}" style="width:160px;margin-bottom:24px;">
                    <h1 style="color:#ff5c7a;">{gettext("Tu prueba termina en 2 días")}</h1>
                    <p style="color:#9aa8c7;font-size:16px;line-height:1.6;">{gettext("Hola %(name)s, tu periodo de prueba gratuita de PATIA termina pronto. No pierdas el acceso a tu inventario y ventas.", name=user.first_name or user.company_name)}</p>
                    <a href="{_public_url('/subscribe')}" style="display:inline-block;margin-top:24px;padding:14px 28px;background:linear-gradient(135deg,#7c5cff,#29d3a8);color:white;text-decoration:none;border-radius:14px;font-weight:800;">{gettext("Activar PATIA Pro")}</a>
                </div>
                """,
                    language=user.preferred_language,
                )
            if warning_sent:
                user.trial_warning_sent = True
                db.session.commit()
        flash(gettext("Sesión iniciada correctamente."), "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth.html", title=gettext("Iniciar sesión"), button=gettext("Entrar"), mode="login")


@main.route("/logout", methods=["POST"])
def logout():
    language = session.get("language", "es")
    session.clear()
    session["language"] = language if language in SUPPORTED_LANGUAGES else "es"
    return redirect(url_for("main.dashboard"))


@main.app_template_filter("money")
def money_filter(value):
    return money(value or 0)


def analytics():
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    week_start = datetime.utcnow() - timedelta(days=7)
    user_id = session.get("user_id")

    products = Product.query.filter_by(user_id=user_id, is_active=True).all()

    total_products = len(products)
    total_sales = Sale.query.filter_by(user_id=user_id).count()
    inventory_value = (
        db.session.query(func.sum(Product.stock * Product.cost_price))
        .filter(Product.user_id == user_id, Product.is_active.is_(True))
        .scalar()
        or 0
    )
    low_stock = sum(1 for p in products if p.stock <= p.min_stock)

    today_sales = db.session.query(func.sum(Sale.total)).filter(
        Sale.user_id == user_id,
        Sale.created_at >= start
    ).scalar() or 0

    week_sales = db.session.query(func.sum(Sale.total)).filter(
        Sale.user_id == user_id,
        Sale.created_at >= week_start
    ).scalar() or 0

    profit = (
        db.session.query(func.sum((Sale.unit_price - Product.cost_price) * Sale.quantity))
        .join(Product)
        .filter(
            Product.user_id == user_id,
            Sale.user_id == user_id,
            Sale.created_at >= week_start,
        )
        .scalar() or 0
    )

    top_products = (
        db.session.query(Product.name, func.sum(Sale.quantity).label("qty"), func.sum(Sale.total).label("revenue"))
        .join(Sale)
        .filter(Product.user_id == user_id)
        .group_by(Product.id)
        .order_by(func.sum(Sale.quantity).desc())
        .limit(5)
        .all()
    )

    category_sales = (
        db.session.query(Product.category, func.sum(Sale.total).label("revenue"))
        .join(Sale)
        .filter(Product.user_id == user_id)
        .group_by(Product.category)
        .order_by(func.sum(Sale.total).desc())
        .all()
    )

    sold_by_product = dict(
        db.session.query(Sale.product_id, func.sum(Sale.quantity))
        .filter(
            Sale.user_id == user_id,
            Sale.created_at >= week_start,
        )
        .group_by(Sale.product_id)
        .all()
    )

    alerts = []
    for p in products:
        sold_7_days = sold_by_product.get(p.id, 0) or 0
        avg_daily_sales = sold_7_days / 7
        days_left = round(p.stock / avg_daily_sales, 1) if avg_daily_sales > 0 else None

        if p.stock <= 0:
            alerts.append({
                "type": "critical",
                "title": gettext("%(product)s agotado", product=p.name),
                "text": gettext("Stock actual: 0. Necesitas reabastecerlo inmediatamente."),
            })
        elif p.stock <= p.min_stock:
            alerts.append({
                "type": "critical",
                "title": gettext("Reordenar %(product)s", product=p.name),
                "text": gettext(
                    "Stock actual: %(stock)s. Mínimo recomendado: %(minimum)s.",
                    stock=p.stock,
                    minimum=p.min_stock,
                ),
            })
        elif days_left is not None and days_left <= 3:
            alerts.append({
                "type": "critical",
                "title": gettext("%(product)s se agotará pronto", product=p.name),
                "text": gettext(
                    "Con el ritmo actual de ventas, se acabará en aproximadamente %(days)s días.",
                    days=days_left,
                ),
            })
        elif days_left is not None and days_left <= 7:
            alerts.append({
                "type": "warning",
                "title": gettext("Vigilar %(product)s", product=p.name),
                "text": gettext("Inventario estimado para %(days)s días.", days=days_left),
            })

    recommendations = []
    if top_products:
        recommendations.append(gettext(
            "%(product)s es el producto con más movimiento. Conviene revisar sus existencias antes de tu próxima compra.",
            product=top_products[0].name,
        ))
    if week_sales > 0:
        recommendations.append(gettext(
            "En los últimos 7 días registraste $%(amount)s MXN en ventas.",
            amount=f"{week_sales:,.0f}",
        ))
    if profit > 0:
        recommendations.append(gettext(
            "Tu utilidad estimada de los últimos 7 días es $%(amount)s MXN, considerando el costo registrado de los productos vendidos.",
            amount=f"{profit:,.0f}",
        ))
    if low_stock:
        recommendations.append(
            gettext(
                "Tienes %(count)s producto con inventario bajo. Revísalo antes de que afecte una venta.",
                count=low_stock,
            )
            if low_stock == 1
            else gettext(
                "Tienes %(count)s productos con inventario bajo. Revísalos antes de que afecten una venta.",
                count=low_stock,
            )
        )

    alerts = alerts[:5]
    return dict(
        total_products=total_products,
        total_sales=total_sales,
        inventory_value=inventory_value,
        low_stock=low_stock,
        today_sales=today_sales,
        week_sales=week_sales,
        profit=profit,
        top_products=top_products,
        category_sales=category_sales,
        alerts=alerts,
        recommendations=recommendations,
    )


@main.route("/")
def dashboard():
    user = current_user()
    if not user:
        language = session.get("language", "es")
        session.clear()
        session["language"] = language if language in SUPPORTED_LANGUAGES else "es"
        return render_template("landing.html")
    dashboard_data = analytics()
    has_basic_data = all(
        (user.company_name, user.phone, user.city, user.state)
    )
    has_products = dashboard_data["total_products"] > 0
    has_sales = dashboard_data["total_sales"] > 0
    onboarding_steps = [
        {
            "title": gettext("Completa los datos básicos"),
            "text": gettext("Confirma la información principal de tu negocio."),
            "completed": has_basic_data,
            "action_label": gettext("Completar datos"),
            "action_url": url_for("main.settings"),
        },
        {
            "title": gettext("Agrega tu primer producto"),
            "text": gettext("Captura un producto o importa tu catálogo desde Excel."),
            "completed": has_products,
            "action_label": gettext("Agregar primer producto"),
            "action_url": url_for("main.products"),
        },
        {
            "title": gettext("Registra tu primera venta"),
            "text": gettext("Prueba el punto de venta con un producto de tu catálogo."),
            "completed": has_sales,
            "action_label": gettext("Registrar primera venta") if has_products else None,
            "action_url": url_for("main.sell") if has_products else None,
        },
        {
            "title": gettext("Revisa tus resultados"),
            "text": gettext("Consulta ventas, inventario y alertas en este panel."),
            "completed": has_sales,
            "action_label": None,
            "action_url": None,
        },
    ]
    completed_steps = sum(step["completed"] for step in onboarding_steps)
    onboarding_progress = round(completed_steps / len(onboarding_steps) * 100)
    onboarding_completed = completed_steps == len(onboarding_steps)

    return render_template(
        "dashboard.html",
        company_name=user.company_name,
        user=user,
        trial_days_left=max(0, 14 - (datetime.utcnow() - user.created_at).days) if user.created_at else 14,
        onboarding_steps=onboarding_steps,
        onboarding_completed=onboarding_completed,
        onboarding_progress=onboarding_progress,
        show_onboarding=not has_products or not has_sales,
        **dashboard_data,
    )


@main.route("/products")
def products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()
    if trial_expired(user):
        return render_template("trial_expired.html")
    q = request.args.get("q", "").strip()
    catalog_query = Product.query.filter(
        Product.user_id == session["user_id"],
        Product.is_active.is_(True),
    )
    catalog_count = catalog_query.count()
    query = catalog_query
    if q:
        query = query.filter(
            Product.name.ilike(f"%{q}%") |
            Product.category.ilike(f"%{q}%") |
            Product.sku.ilike(f"%{q}%")
        )
    return render_template(
        "products.html",
        products=query.order_by(Product.name).all(),
        catalog_count=catalog_count,
        q=q,
        user=user,
    )


@main.route("/download-template")
def download_template():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    import pandas as pd

    columns = [
        gettext("SKU"),
        gettext("Código de barras"),
        gettext("Nombre del producto"),
        gettext("Categoría"),
        gettext("Proveedor"),
        gettext("Costo"),
        gettext("Precio de venta"),
        gettext("Stock inicial"),
        gettext("Stock mínimo"),
    ]
    df = pd.DataFrame(columns=columns)
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sheet_name = gettext("PRODUCTOS")
        df.to_excel(writer, index=False, sheet_name=sheet_name, startrow=3)
        workbook = writer.book
        ws = writer.sheets[sheet_name]

        ws["A1"] = gettext("PATIA - Plantilla oficial de productos")
        ws["A2"] = gettext(
            "Llena esta tabla con tus productos. No cambies los nombres de las columnas."
        )
        ws.merge_cells("A1:I1")
        ws.merge_cells("A2:I2")

        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.worksheet.table import Table, TableStyleInfo

        title_fill = PatternFill("solid", fgColor="0B1020")
        header_fill = PatternFill("solid", fgColor="00D4FF")
        dark_font = Font(color="0B1020", bold=True)
        center = Alignment(horizontal="center", vertical="center")
        thin = Side(border_style="thin", color="D9E2F3")

        ws["A1"].fill = title_fill
        ws["A1"].font = Font(color="FFFFFF", bold=True, size=18)
        ws["A1"].alignment = center
        ws["A2"].font = Font(color="666666", italic=True)
        ws["A2"].alignment = center

        for cell in ws[4]:
            cell.fill = header_fill
            cell.font = dark_font
            cell.alignment = center
            cell.border = Border(top=thin, left=thin, right=thin, bottom=thin)

        for row in range(5, 105):
            for col in range(1, 10):
                ws.cell(row=row, column=col).border = Border(top=thin, left=thin, right=thin, bottom=thin)

        table = Table(displayName="TablaProductosPATIA", ref="A4:I104")
        style = TableStyleInfo(name="TableStyleMedium2", showFirstColumn=False, showLastColumn=False, showRowStripes=True, showColumnStripes=False)
        table.tableStyleInfo = style
        ws.add_table(table)

        widths = {"A": 18, "B": 22, "C": 32, "D": 20, "E": 24, "F": 14, "G": 18, "H": 18, "I": 18}
        for col, width in widths.items():
            ws.column_dimensions[col].width = width
        ws.freeze_panes = "A5"

    output.seek(0)
    return send_file(output, as_attachment=True, download_name="plantilla_productos_PATIA.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@main.route("/import-products", methods=["POST"])
def import_products():
    import pandas as pd
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()
    access_block = _trial_access_response(user)
    if access_block:
        return access_block

    file = request.files.get("catalog_file")
    if not file:
        flash("Selecciona un archivo.", "danger")
        return redirect(url_for("main.products") + "#importar-catalogo")

    try:
        filename = (file.filename or "").lower()
        if filename.endswith(".csv"):
            df = pd.read_csv(file)
            first_data_row = 2
        elif filename.endswith(".xlsx"):
            workbook = pd.ExcelFile(file)
            sheet_name = next(
                (
                    name
                    for name in workbook.sheet_names
                    if name.upper() in {"PRODUCTOS", "PRODUCTS"}
                ),
                None,
            )
            if not sheet_name:
                raise ValueError("missing product sheet")
            df = pd.read_excel(workbook, sheet_name=sheet_name, header=3)
            first_data_row = 5
        else:
            flash("Usa un archivo CSV o Excel .xlsx.", "danger")
            return redirect(url_for("main.products") + "#importar-catalogo")

        column_aliases = {
            "SKU": "sku",
            "Codigo de barras": "barcode",
            "Código de barras": "barcode",
            "Barcode": "barcode",
            "Nombre del producto": "name",
            "Product name": "name",
            "Categoria": "category",
            "Categoría": "category",
            "Category": "category",
            "Proveedor": "supplier",
            "Supplier": "supplier",
            "Costo": "cost_price",
            "Cost": "cost_price",
            "Precio de venta": "sale_price",
            "Sale price": "sale_price",
            "Stock inicial": "stock",
            "Initial stock": "stock",
            "Stock minimo": "min_stock",
            "Stock mínimo": "min_stock",
            "Minimum stock": "min_stock",
        }
        df = df.rename(columns=column_aliases)
        essential_columns = {"sku", "name", "cost_price", "sale_price", "stock"}
        missing_columns = sorted(essential_columns - set(df.columns))
        if missing_columns:
            flash(
                gettext(
                    "El archivo no contiene todas las columnas obligatorias. Descarga la plantilla PATIA e inténtalo de nuevo."
                ),
                "danger",
            )
            return redirect(url_for("main.products") + "#importar-catalogo")

        summary = {"created": 0, "updated": 0, "omitted": 0, "errors": 0}

        def text_value(value, default=""):
            if pd.isna(value):
                return default
            return str(value).strip()

        def number_value(value, default=0, integer=False):
            if pd.isna(value) or str(value).strip() == "":
                number = default
            else:
                number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("negative value")
            if integer and not number.is_integer():
                raise ValueError("fractional integer value")
            return int(number) if integer else number

        for row_index, row in df.iterrows():
            if all(pd.isna(value) or str(value).strip() == "" for value in row.values):
                summary["omitted"] += 1
                continue

            try:
                sku = text_value(row.get("sku"))
                name = text_value(row.get("name"))
                raw_barcode = row.get("barcode", "")
                try:
                    barcode = (
                        str(int(float(raw_barcode)))
                        if text_value(raw_barcode)
                        else ""
                    )
                except (TypeError, ValueError):
                    barcode = text_value(raw_barcode)

                stock = number_value(row.get("stock"), integer=True)
                cost_price = number_value(row.get("cost_price"))
                sale_price = number_value(row.get("sale_price"))
                min_stock = number_value(row.get("min_stock"), default=5, integer=True)

                existing = None
                matched_by_sku = False
                if sku:
                    existing = Product.query.filter_by(
                        user_id=session["user_id"], sku=sku
                    ).first()
                    matched_by_sku = existing is not None

                if not existing and barcode:
                    existing = Product.query.filter_by(
                        user_id=session["user_id"], barcode=barcode
                    ).first()

                if existing:
                    existing.is_active = True
                    # Política existente: SKU suma stock; código lo reemplaza.
                    existing.stock = existing.stock + stock if matched_by_sku else stock
                    existing.sale_price = sale_price
                    existing.cost_price = cost_price
                    existing.min_stock = min_stock
                    if matched_by_sku:
                        existing.barcode = barcode
                    summary["updated"] += 1
                    continue

                if not sku or not name:
                    raise ValueError("missing product identity")

                db.session.add(Product(
                    user_id=session["user_id"],
                    sku=sku,
                    barcode=barcode or None,
                    name=name,
                    category=text_value(row.get("category"), "General") or "General",
                    supplier=text_value(row.get("supplier")) or None,
                    cost_price=cost_price,
                    sale_price=sale_price,
                    stock=stock,
                    min_stock=min_stock,
                ))
                summary["created"] += 1
            except (TypeError, ValueError, OverflowError):
                summary["errors"] += 1
                current_app.logger.warning(
                    "Fila inválida en importación de catálogo (fila %s)",
                    row_index + first_data_row,
                    exc_info=True,
                )

        db.session.commit()
        flash(gettext(
            "Importación terminada: %(created)s creados, %(updated)s actualizados, %(omitted)s omitidos y %(errors)s errores.",
            **summary,
        ), "success")

    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo importar el catálogo")
        flash(
            "No pudimos procesar el archivo. Verifica que use la plantilla PATIA e inténtalo de nuevo.",
            "danger",
        )

    return redirect(url_for("main.products") + "#importar-catalogo")


@main.route("/products/new", methods=["POST"])
def add_product():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(user)
    if access_block:
        return access_block

    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    try:
        cost_price = float(request.form.get("cost_price") or 0)
        sale_price = float(request.form.get("sale_price") or 0)
        stock = int(request.form.get("stock") or 0)
        min_stock = int(request.form.get("min_stock") or 5)
    except (TypeError, ValueError):
        flash("Revisa precios y existencias e inténtalo nuevamente.", "danger")
        return redirect(url_for("main.products"))
    if not name or not sku:
        flash("Nombre y SKU son obligatorios.", "danger")
        return redirect(url_for("main.products"))
    existing_sku = Product.query.filter_by(user_id=user.id, sku=sku).first()
    if existing_sku and existing_sku.is_active:
        flash("Ya existe un producto con ese SKU. Usa un SKU diferente.", "danger")
        return redirect(url_for("main.products") + "#agregar-producto")
    barcode = request.form.get("barcode", "").strip() or None
    existing_barcode = (
        Product.query.filter_by(user_id=user.id, barcode=barcode).first()
        if barcode else None
    )
    if existing_barcode and existing_barcode.is_active:
        flash("Ya existe un producto con ese código de barras.", "danger")
        return redirect(url_for("main.products") + "#agregar-producto")
    if existing_sku and existing_barcode and existing_sku.id != existing_barcode.id:
        flash(
            "El SKU y el código de barras pertenecen a productos históricos diferentes.",
            "danger",
        )
        return redirect(url_for("main.products") + "#agregar-producto")
    if (
        not math.isfinite(cost_price)
        or not math.isfinite(sale_price)
        or min(cost_price, sale_price, stock, min_stock) < 0
    ):
        flash("Precios y existencias no pueden ser negativos.", "danger")
        return redirect(url_for("main.products"))

    p = existing_sku or existing_barcode
    if p:
        p.sku = sku
        p.barcode = barcode
        p.name = name
        p.category = request.form.get("category") or "General"
        p.supplier = request.form.get("supplier")
        p.cost_price = cost_price
        p.sale_price = sale_price
        p.stock = stock
        p.min_stock = min_stock
        p.is_active = True
    else:
        p = Product(
            user_id=user.id,
            sku=sku,
            barcode=barcode,
            name=name,
            category=request.form.get("category") or "General",
            supplier=request.form.get("supplier"),
            cost_price=cost_price,
            sale_price=sale_price,
            stock=stock,
            min_stock=min_stock,
        )
        db.session.add(p)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.info(
            "Alta de producto rechazada por identificador duplicado para user_id=%s",
            user.id,
        )
        flash("No pudimos guardar el producto porque el SKU ya está en uso.", "danger")
        return redirect(url_for("main.products") + "#agregar-producto")
    flash("Producto creado correctamente.", "success")
    return redirect(url_for("main.products"))


@main.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
def edit_product(product_id):
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(user)
    if access_block:
        return access_block

    product = Product.query.filter_by(
        id=product_id,
        user_id=user.id,
        is_active=True,
    ).first_or_404()
    if request.method == "GET":
        return render_template("edit_product.html", product=product, user=user)

    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    barcode = request.form.get("barcode", "").strip() or None
    try:
        cost_price = float(request.form.get("cost_price") or 0)
        sale_price = float(request.form.get("sale_price") or 0)
        stock = int(request.form.get("stock") or 0)
        min_stock = int(request.form.get("min_stock") or 0)
    except (TypeError, ValueError):
        flash("Revisa precios y existencias e inténtalo nuevamente.", "danger")
        return render_template("edit_product.html", product=product, user=user), 400

    if not name or not sku:
        flash("Nombre y SKU son obligatorios.", "danger")
        return render_template("edit_product.html", product=product, user=user), 400
    if (
        not math.isfinite(cost_price)
        or not math.isfinite(sale_price)
        or min(cost_price, sale_price, stock, min_stock) < 0
    ):
        flash("Precios y existencias no pueden ser negativos.", "danger")
        return render_template("edit_product.html", product=product, user=user), 400

    duplicate_sku = Product.query.filter(
        Product.user_id == user.id,
        Product.sku == sku,
        Product.id != product.id,
    ).first()
    if duplicate_sku:
        flash("Ya existe otro producto con ese SKU.", "danger")
        return render_template("edit_product.html", product=product, user=user), 409

    if barcode:
        duplicate_barcode = Product.query.filter(
            Product.user_id == user.id,
            Product.barcode == barcode,
            Product.id != product.id,
        ).first()
        if duplicate_barcode:
            flash("Ya existe otro producto con ese código de barras.", "danger")
            return render_template("edit_product.html", product=product, user=user), 409

    product.name = name
    product.sku = sku
    product.barcode = barcode
    product.category = request.form.get("category", "").strip() or "General"
    product.supplier = request.form.get("supplier", "").strip() or None
    product.cost_price = cost_price
    product.sale_price = sale_price
    product.stock = stock
    product.min_stock = min_stock
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        current_app.logger.info(
            "Edición de producto rechazada por identificador duplicado para user_id=%s",
            user.id,
        )
        flash("No pudimos guardar el producto porque el SKU ya está en uso.", "danger")
        return render_template("edit_product.html", product=product, user=user), 409

    flash("Producto actualizado correctamente. Las ventas anteriores conservaron sus importes originales.", "success")
    return redirect(url_for("main.products") + "#catalogo")


@main.route("/sell", methods=["GET", "POST"])
def sell():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()
    if trial_expired(user):
        return render_template("trial_expired.html")

    if request.method == "POST":
        try:
            product_id = int(request.form.get("product_id", ""))
            qty = int(request.form.get("quantity") or 1)
        except (TypeError, ValueError):
            flash("Selecciona un producto y una cantidad válida.", "danger")
            return redirect(url_for("main.sell"))
        product = (
            Product.query.filter_by(
                id=product_id,
                user_id=session["user_id"],
                is_active=True,
            )
            .with_for_update()
            .first_or_404()
        )
        if qty <= 0:
            flash("La cantidad debe ser mayor a cero.", "danger")
        elif product.stock < qty:
            flash("No hay suficiente inventario.", "danger")
        else:
            payment_method = request.form.get("payment_method", "cash")
            if payment_method not in PAYMENT_METHOD_LABELS:
                flash("Selecciona un método de pago válido.", "danger")
                return redirect(url_for("main.sell"))
            product.stock -= qty
            sale = Sale(user_id=session["user_id"], product_id=product.id, quantity=qty, unit_price=product.sale_price, total=qty * product.sale_price, ticket_id=str(uuid.uuid4()), payment_method=payment_method)
            db.session.add(sale)
            db.session.commit()
            flash(
                gettext(
                    "Venta registrada: %(product)s x%(quantity)s.",
                    product=product.name,
                    quantity=qty,
                ),
                "success",
            )
        return redirect(url_for("main.sell"))

    sales = Sale.query.filter_by(user_id=session["user_id"]).order_by(Sale.created_at.desc(), Sale.id.desc()).all()
    sale_groups = _group_sales_by_ticket(sales, limit=12)
    products = Product.query.filter_by(
        user_id=session["user_id"],
        is_active=True,
    ).order_by(Product.name).all()
    return render_template(
        "sell.html",
        products=products,
        sales=sales,
        sale_groups=sale_groups,
        user=user,
        payment_method_labels=_translated_payment_method_labels(),
    )


@main.route("/sell-cart", methods=["POST"])
def sell_cart():
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": gettext("No autenticado")}), 401
    access_block = _trial_access_response(user, json_response=True)
    if access_block:
        return access_block

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": gettext("Solicitud inválida")}), 400

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": gettext("El carrito está vacío")}), 400

    payment_method = data.get("payment_method", "cash")
    if payment_method not in PAYMENT_METHOD_LABELS:
        return jsonify({"ok": False, "error": gettext("Selecciona un método de pago válido")}), 400

    request_id = data.get("request_id")
    if request_id:
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"ok": False, "error": gettext("Solicitud inválida")}), 400

    try:
        requested_items = {}
        for item in items:
            if not isinstance(item, dict):
                return jsonify({"ok": False, "error": gettext("Artículo inválido")}), 400

            try:
                product_id = int(item["product_id"])
                quantity = int(item["quantity"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"ok": False, "error": gettext("Artículo inválido")}), 400

            if quantity <= 0:
                return jsonify({"ok": False, "error": gettext("La cantidad debe ser mayor a cero")}), 400

            requested_items[product_id] = requested_items.get(product_id, 0) + quantity

        if request_id:
            previous_sales = Sale.query.filter_by(
                user_id=user.id,
                ticket_id=request_id,
            ).order_by(Sale.id).all()
            if previous_sales:
                folio = _short_sale_folio(previous_sales)
                return jsonify({
                    "ok": True,
                    "duplicate": True,
                    "ticket_id": request_id,
                    "folio": folio,
                    "ticket_url": url_for("main.ticket", ticket_ref=request_id),
                    "total": sum(sale.total for sale in previous_sales),
                    "payment_method": _payment_method_label(previous_sales[0].payment_method),
                    "single_sale_id": (
                        previous_sales[0].id if len(previous_sales) == 1 else None
                    ),
                })

        products = {
            product.id: product
            for product in Product.query.filter(
                Product.user_id == user.id,
                Product.is_active.is_(True),
                Product.id.in_(requested_items.keys()),
            )
            .order_by(Product.id)
            .with_for_update()
            .all()
        }

        if len(products) != len(requested_items):
            return jsonify({"ok": False, "error": gettext("Producto no encontrado")}), 404

        # Repetir la verificación tras bloquear inventario evita que dos workers
        # procesen simultáneamente el mismo request_id.
        if request_id:
            previous_sales = Sale.query.filter_by(
                user_id=user.id,
                ticket_id=request_id,
            ).order_by(Sale.id).all()
            if previous_sales:
                return jsonify({
                    "ok": True,
                    "duplicate": True,
                    "ticket_id": request_id,
                    "folio": _short_sale_folio(previous_sales),
                    "ticket_url": url_for("main.ticket", ticket_ref=request_id),
                    "total": sum(sale.total for sale in previous_sales),
                    "payment_method": _payment_method_label(previous_sales[0].payment_method),
                    "single_sale_id": (
                        previous_sales[0].id if len(previous_sales) == 1 else None
                    ),
                })

        for product_id, quantity in requested_items.items():
            product = products[product_id]
            if product.stock < quantity:
                return jsonify({
                    "ok": False,
                    "error": gettext("Stock insuficiente: %(product)s", product=product.name),
                }), 409

        ticket_id = request_id or str(uuid.uuid4())
        sales = []
        for product_id, quantity in requested_items.items():
            product = products[product_id]
            product.stock -= quantity
            sale = Sale(user_id=user.id, product_id=product.id, quantity=quantity, unit_price=product.sale_price, total=quantity * product.sale_price, payment_method=payment_method)
            sale.ticket_id = ticket_id
            db.session.add(sale)
            sales.append(sale)
        db.session.commit()
        folio = _short_sale_folio(sales)
        return jsonify({
            "ok": True,
            "ticket_id": ticket_id,
            "folio": folio,
            "ticket_url": url_for("main.ticket", ticket_ref=ticket_id),
            "total": sum(sale.total for sale in sales),
            "single_sale_id": sales[0].id if len(sales) == 1 else None,
            "payment_method": _payment_method_label(payment_method),
        })
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error al procesar el carrito")
        return jsonify({"ok": False, "error": gettext("No se pudo procesar la venta")}), 500


@main.route("/reports")
def reports():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if user.email == "albertonicopat@gmail.com" or has_pro_access(user):
        return render_template("reports.html", user=user, **analytics())
    return redirect(url_for("main.subscribe"))


@main.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()

    if request.method == "POST":
        access_block = _trial_access_response(user)
        if access_block:
            return access_block
        supplier_name = request.form.get("name", "").strip()
        if not supplier_name:
            flash("Escribe el nombre del proveedor.", "danger")
            return redirect(url_for("main.suppliers"))
        existing_supplier = Supplier.query.filter_by(user_id=session["user_id"], name=supplier_name).first()
        if existing_supplier:
            flash("Ese proveedor ya existe.", "danger")
            return redirect(url_for("main.suppliers"))
        s = Supplier(user_id=session["user_id"], name=supplier_name, contact=request.form.get("contact"), phone=request.form.get("phone"), notes=request.form.get("notes"))
        db.session.add(s)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Ese proveedor ya existe.", "danger")
            return redirect(url_for("main.suppliers"))
        flash("Proveedor guardado.", "success")
        return redirect(url_for("main.suppliers"))

    suppliers = Supplier.query.filter_by(user_id=session["user_id"]).order_by(Supplier.name).all()
    return render_template("suppliers.html", suppliers=suppliers, user=user)


@main.route("/subscribe")
def subscribe():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if not user.email_verified:
        session["post_verify_destination"] = "subscribe"
        flash("Verifica tu correo antes de activar PATIA Pro.", "info")
        return redirect(url_for("main.verify_email"))
    if request.args.get("checkout") == "cancelled":
        flash("El pago fue cancelado. No se realizó ningún cargo.", "info")
    return render_template("subscribe.html", user=user)


def _safe_stripe_error(error):
    """Return diagnostic fields without leaking Stripe identifiers or URLs."""
    message = str(error or "")[:1000]
    message = re.sub(r"https?://\S+", "[redacted-url]", message)
    message = re.sub(
        r"\b(?:sk|rk|pk|whsec|cs|sess|pi|sub|cus|price|prod|req)_[A-Za-z0-9_]+\b",
        "[redacted-stripe-id]",
        message,
    )


    code = getattr(error, "code", None) or getattr(error, "http_status", None)
    return type(error).__name__, code or "none", message or "none"


@main.route("/terminos")
def terms():
    return render_template("legal.html", document="terms")


@main.route("/privacidad")
def privacy():
    return render_template("legal.html", document="privacy")


@main.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if not user.email_verified:
        session["post_verify_destination"] = "subscribe"
        flash("Verifica tu correo antes de activar PATIA Pro.", "info")
        return redirect(url_for("main.verify_email"))
    if current_app.config["STRIPE_DISABLED"]:
        flash("La facturación no está disponible en este entorno.", "danger")
        return redirect(url_for("main.subscribe"))

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    existing_subscription = None

    if user.stripe_subscription_id and user.subscription_status in MANAGED_SUBSCRIPTION_STATUSES:
        return _redirect_to_billing_portal(user)

    if user.stripe_customer_id:
        try:
            subscriptions = stripe.Subscription.list(
                customer=user.stripe_customer_id,
                status="all",
                limit=10,
            )
            for candidate in subscriptions.get("data", []):
                if (
                    candidate.get("status") in MANAGED_SUBSCRIPTION_STATUSES
                    and _subscription_has_configured_price(candidate)
                ):
                    existing_subscription = candidate
                    break
        except Exception as error:
            error_type, error_code, error_message = _safe_stripe_error(error)
            current_app.logger.exception(
                "No se pudo comprobar suscripciones existentes: type=%s code=%s message=%s",
                error_type,
                error_code,
                error_message,
            )
            flash("No pudimos validar tu suscripción. Intenta nuevamente.", "danger")
            return redirect(url_for("main.subscribe"))

    if existing_subscription:
        user.stripe_subscription_id = existing_subscription.get("id")
        _sync_subscription_state(user, existing_subscription, datetime.utcnow())
        db.session.commit()
        return _redirect_to_billing_portal(user)

    checkout_params = {
        "payment_method_types": ["card"],
        "mode": "subscription",
        "line_items": [{"price": current_app.config["STRIPE_PRICE_ID"], "quantity": 1}],
        "success_url": _public_url("/stripe-success") + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": _public_url("/subscribe?checkout=cancelled"),
        "client_reference_id": str(user.id),
        "metadata": {"user_id": str(user.id)},
        "subscription_data": {"metadata": {"user_id": str(user.id)}},
    }
    if user.stripe_customer_id:
        checkout_params["customer"] = user.stripe_customer_id
    else:
        checkout_params["customer_email"] = user.email

    idempotency_window = int(datetime.utcnow().timestamp() // 1800)
    try:
        checkout_session = stripe.checkout.Session.create(
            **checkout_params,
            idempotency_key=f"patia-checkout-{user.id}-{idempotency_window}",
        )
        return redirect(checkout_session.url, code=303)
    except Exception as error:
        error_type, error_code, error_message = _safe_stripe_error(error)
        current_app.logger.exception(
            "No se pudo crear la sesión de Checkout: type=%s code=%s message=%s",
            error_type,
            error_code,
            error_message,
        )
        flash("No pudimos iniciar el pago. Intenta nuevamente.", "danger")
        return redirect(url_for("main.subscribe"))


def _process_stripe_event(event):
    event_type = event["type"]
    data = event["data"]["object"]
    stripe_created_at = _as_utc_datetime(event["created"])

    if event_type == "checkout.session.completed":
        if data.get("mode") != "subscription":
            raise StripeEventIgnored("Checkout no es de suscripción.")
        user_id = str(
            data.get("client_reference_id")
            or (data.get("metadata") or {}).get("user_id")
            or ""
        )
        if not user_id.isdigit():
            raise StripeEventIgnored("Checkout sin usuario válido.")
        user = db.session.get(User, int(user_id))
        if not user:
            raise StripeEventIgnored("Usuario de Checkout inexistente.")
        customer_id = data.get("customer")
        subscription_id = data.get("subscription")
        if not customer_id or not subscription_id:
            raise StripeEventIgnored("Checkout sin cliente o suscripción.")
        if user.stripe_customer_id and user.stripe_customer_id != customer_id:
            raise StripeEventIgnored("Checkout pertenece a otro cliente.")
        _ensure_stripe_ids_available(user, customer_id, subscription_id)
        subscription = stripe.Subscription.retrieve(subscription_id)
        _validate_subscription(subscription, user=user, customer_id=customer_id)
        metadata_user_id = str((subscription.get("metadata") or {}).get("user_id") or "")
        if metadata_user_id and metadata_user_id != str(user.id):
            raise StripeEventIgnored("Metadatos de suscripción no coinciden.")
        user.stripe_customer_id = customer_id
        user.stripe_subscription_id = subscription_id
        return

    if event_type in {"invoice.paid", "invoice.payment_failed"}:
        subscription_id = _invoice_subscription_id(data)
        if not subscription_id:
            raise StripeEventIgnored("Factura sin suscripción.")
        subscription = stripe.Subscription.retrieve(subscription_id)
        _validate_subscription(subscription, customer_id=data.get("customer"))
        user = _find_subscription_user(subscription)
        if not user:
            raise StripeEventIgnored("Suscripción sin usuario local.")
        _validate_subscription(subscription, user=user, customer_id=data.get("customer"))
        if not _event_is_newer(user, stripe_created_at, "invoice"):
            return
        subscription_event_is_newer = bool(
            user.stripe_subscription_updated_at
            and stripe_created_at < user.stripe_subscription_updated_at
        )
        user.stripe_customer_id = subscription.get("customer")
        user.stripe_subscription_id = subscription_id
        user.current_period_end = _subscription_period_end(subscription)
        user.cancel_at_period_end = bool(subscription.get("cancel_at_period_end", False))
        user.stripe_invoice_updated_at = stripe_created_at
        if event_type == "invoice.paid":
            status = (subscription.get("status") or "").lower()
            if status not in {"active", "trialing"}:
                raise StripeEventIgnored("Factura pagada con suscripción no activa.")
            if not subscription_event_is_newer:
                user.subscription_status = status
                user.next_payment_attempt = None
        else:
            if not subscription_event_is_newer:
                user.subscription_status = "past_due"
                user.next_payment_attempt = _as_utc_datetime(
                    data.get("next_payment_attempt")
                )
        sync_user_plan(user)
        return

    if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        _validate_subscription(data)
        user = _find_subscription_user(data)
        if not user:
            raise StripeEventIgnored("Suscripción sin usuario local.")
        _validate_subscription(data, user=user)
        _sync_subscription_state(
            user,
            data,
            stripe_created_at,
            deleted=event_type == "customer.subscription.deleted",
        )


def _record_failed_webhook(event, error):
    event_id = str(event.get("id") or "")
    if not event_id:
        return
    record = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
    if not record:
        data = (event.get("data") or {}).get("object") or {}
        record = StripeWebhookEvent(
            stripe_event_id=event_id,
            event_type=str(event.get("type") or "unknown"),
            object_id=data.get("id"),
            stripe_created_at=_as_utc_datetime(event.get("created")) or datetime.utcnow(),
        )
        db.session.add(record)
    record.status = "failed"
    record.completed_at = None
    record.failed_at = datetime.utcnow()
    record.error_message = str(error)[:1000]
    db.session.commit()


@main.route("/stripe-webhook", methods=["POST"])
@csrf.exempt
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = current_app.config["STRIPE_WEBHOOK_SECRET"]

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except ValueError:
        return "", 400
    except stripe.error.SignatureVerificationError:
        return "", 400

    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    event_id = str(event.get("id") or "")
    if not event_id:
        return "", 400

    existing = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
    if existing and existing.status in {"processed", "ignored"}:
        return "", 200

    data = event["data"]["object"]
    try:
        if not existing:
            existing = StripeWebhookEvent(
                stripe_event_id=event_id,
                event_type=event["type"],
                object_id=data.get("id"),
                stripe_created_at=_as_utc_datetime(event["created"]),
                status="pending",
            )
            db.session.add(existing)
            db.session.flush()
        else:
            existing.status = "pending"
            existing.error_message = None
            existing.completed_at = None
            existing.failed_at = None

        _process_stripe_event(event)
        existing.status = "processed"
        existing.completed_at = datetime.utcnow()
        existing.failed_at = None
        db.session.commit()
        return "", 200
    except StripeEventIgnored as error:
        existing.status = "ignored"
        existing.error_message = str(error)[:1000]
        existing.completed_at = datetime.utcnow()
        existing.failed_at = None
        db.session.commit()
        current_app.logger.warning("Evento Stripe ignorado: %s", error)
        return "", 200
    except IntegrityError:
        db.session.rollback()
        duplicate = StripeWebhookEvent.query.filter_by(stripe_event_id=event_id).first()
        if duplicate and duplicate.status in {"processed", "ignored"}:
            return "", 200
        current_app.logger.exception("Conflicto procesando webhook Stripe")
        return "", 500
    except Exception as error:
        db.session.rollback()
        current_app.logger.exception("Error procesando webhook Stripe %s", event_id)
        try:
            _record_failed_webhook(event, error)
        except Exception:
            db.session.rollback()
            current_app.logger.exception("No se pudo registrar el fallo del webhook")
        return "", 500


@main.route("/stripe-success")
def stripe_success():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    checkout_session_id = request.args.get("session_id", "").strip()
    if not checkout_session_id:
        flash("No se pudo validar la sesión de pago.", "danger")
        return redirect(url_for("main.subscription"))

    try:
        stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
        checkout_session = stripe.checkout.Session.retrieve(checkout_session_id)
    except Exception:
        current_app.logger.exception("No se pudo consultar la sesión de Checkout")
        flash("No se pudo validar la sesión de pago.", "danger")
        return redirect(url_for("main.subscription"))

    checkout_user_id = str(
        checkout_session.get("client_reference_id")
        or checkout_session.get("metadata", {}).get("user_id")
        or ""
    )
    if checkout_user_id != str(user.id):
        flash("La sesión de pago no pertenece a este usuario.", "danger")
        return redirect(url_for("main.subscription"))

    if has_pro_access(user):
        flash("Tu cuenta PATIA Pro está activa.", "success")
    else:
        flash("Pago recibido. Estamos confirmando tu suscripción con Stripe.", "success")
    return redirect(url_for("main.dashboard"))


@main.route("/sales/<int:sale_id>/cancel", methods=["POST"])
def cancel_sale(sale_id):
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(user)
    if access_block:
        return access_block
    sale = Sale.query.filter_by(id=sale_id, user_id=user.id).first_or_404()
    product = Product.query.get(sale.product_id)
    if product:
        product.stock += sale.quantity
    db.session.delete(sale)
    db.session.commit()
    flash("Venta cancelada. Stock devuelto al inventario.", "success")
    return redirect(url_for("main.sell"))


@main.route("/subscription")
def subscription():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    subscription_info = None
    if user.stripe_subscription_id and not current_app.config["STRIPE_DISABLED"]:
        stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
        try:
            sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
            _validate_subscription(sub, user=user)
            _sync_subscription_state(user, sub, datetime.utcnow())
            db.session.commit()
            subscription_info = sub
        except Exception:
            db.session.rollback()
            current_app.logger.exception("No se pudo sincronizar la suscripción")
            flash("No pudimos actualizar el estado de tu suscripción.", "danger")
    return render_template(
        "subscription.html",
        user=user,
        subscription_info=subscription_info,
        has_paid_access=has_pro_access(user),
        subscription_status_label=_subscription_status_label(user.subscription_status),
    )


@main.route("/cancel-subscription", methods=["POST"])
def cancel_subscription():
    user = current_user()
    if not user or not user.stripe_subscription_id:
        return redirect(url_for("main.dashboard"))
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    try:
        stripe.Subscription.modify(user.stripe_subscription_id, cancel_at_period_end=True)
        user.cancel_at_period_end = True
        db.session.commit()
        flash("Tu suscripcion se cancelara al final del periodo pagado.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo programar la cancelación")
        flash("No pudimos cancelar la suscripción. Intenta nuevamente.", "danger")
    return redirect(url_for("main.subscription"))


@main.route("/reactivate-subscription", methods=["POST"])
def reactivate_subscription():
    user = current_user()
    if not user or not user.stripe_subscription_id:
        return redirect(url_for("main.dashboard"))
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    try:
        stripe.Subscription.modify(user.stripe_subscription_id, cancel_at_period_end=False)
        user.cancel_at_period_end = False
        db.session.commit()
        flash("Tu suscripcion ha sido reactivada.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("No se pudo reactivar la suscripción")
        flash("No pudimos reactivar la suscripción. Intenta nuevamente.", "danger")
    return redirect(url_for("main.subscription"))


@main.route("/billing-portal", methods=["POST"])
def billing_portal():
    user = current_user()
    if not user or not user.stripe_customer_id:
        return redirect(url_for("main.subscribe"))
    return _redirect_to_billing_portal(user)


def _redirect_to_billing_portal(user):
    if current_app.config["STRIPE_DISABLED"]:
        flash("La facturación no está disponible en este entorno.", "danger")
        return redirect(url_for("main.subscription"))
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    try:
        portal = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=_public_url("/subscription"),
        )
        return redirect(portal.url, code=303)
    except Exception as error:
        error_type, error_code, error_message = _safe_stripe_error(error)
        current_app.logger.exception(
            "No se pudo abrir el portal de facturación: type=%s code=%s message=%s",
            error_type,
            error_code,
            error_message,
        )
        flash("No pudimos abrir el portal de facturación.", "danger")
        return redirect(url_for("main.subscription"))


@main.route("/admin")
def admin():
    user = current_user()
    if not user:
        session.clear()
        return redirect(url_for("main.login"))
    if user.email != "albertonicopat@gmail.com":
        flash("No autorizado.", "danger")
        return redirect(url_for("main.dashboard"))

    users = User.query.order_by(User.created_at.desc()).all()
    today = datetime.utcnow()
    clients = []
    total_products = total_sales_count = total_sales_money = trial_clients = expired_clients = expiring_soon = new_this_week = new_this_month = 0

    for u in users:
        products_count = Product.query.filter_by(user_id=u.id, is_active=True).count()
        sales_count = Sale.query.filter_by(user_id=u.id).count()
        sales_money = db.session.query(func.sum(Sale.total)).filter_by(user_id=u.id).scalar() or 0
        days_in_patia = (today - u.created_at).days if u.created_at else 0
        trial_days_left = max(0, 14 - days_in_patia)

        if has_pro_access(u):
            status = "Pro"
            trial_days_left = "inf"
        elif trial_days_left > 0:
            status = "Prueba"
            trial_clients += 1
        else:
            status = "Vencido"
            expired_clients += 1

        if trial_days_left != "inf" and 0 < trial_days_left <= 7:
            expiring_soon += 1
        if days_in_patia <= 7:
            new_this_week += 1
        if days_in_patia <= 30:
            new_this_month += 1

        total_products += products_count
        total_sales_count += sales_count
        total_sales_money += sales_money
        clients.append({"user": u, "products_count": products_count, "sales_count": sales_count, "sales_money": sales_money, "days_in_patia": days_in_patia, "trial_days_left": trial_days_left, "status": status})

    top_client = max(clients, key=lambda c: c["products_count"], default=None)
    latest_client = clients[0] if clients else None

    return render_template("admin.html", clients=clients, total_clients=len(users), total_products=total_products,
        total_sales_count=total_sales_count, total_sales_money=total_sales_money, trial_clients=trial_clients,
        expired_clients=expired_clients, expiring_soon=expiring_soon, new_this_week=new_this_week,
        new_this_month=new_this_month, top_client=top_client, latest_client=latest_client)


@main.route("/products/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(current_user())
    if access_block:
        return access_block
    product = Product.query.filter_by(
        id=product_id,
        user_id=session["user_id"],
        is_active=True,
    ).first_or_404()
    if Sale.query.filter_by(product_id=product.id, user_id=session["user_id"]).first():
        product.is_active = False
        db.session.commit()
        flash("Producto retirado del catálogo. Su historial de ventas se conserva.", "success")
        return redirect(url_for("main.products") + "#catalogo")
    db.session.delete(product)
    db.session.commit()
    flash("Producto eliminado correctamente.", "success")
    return redirect(url_for("main.products") + "#catalogo")


@main.route("/products/delete-all", methods=["POST"])
def delete_all_products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(current_user())
    if access_block:
        return access_block
    user_id = session["user_id"]
    products = Product.query.filter_by(user_id=user_id, is_active=True).all()
    deleted = 0
    protected = 0
    for product in products:
        if Sale.query.filter_by(user_id=user_id, product_id=product.id).first():
            product.is_active = False
            protected += 1
            continue
        db.session.delete(product)
        deleted += 1
    db.session.commit()
    if protected:
        flash(
            gettext(
                "Eliminamos %(deleted)s productos sin ventas y retiramos %(protected)s del catálogo conservando su historial.",
                deleted=deleted,
                protected=protected,
            ),
            "info",
        )
    else:
        flash(
            gettext("Eliminamos %(count)s productos del catálogo.", count=deleted),
            "success",
        )
    return redirect(url_for("main.products"))


@main.route("/products/delete-selected", methods=["POST"])
def delete_selected_products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(current_user())
    if access_block:
        return access_block
    user_id = session["user_id"]
    ids_raw = request.form.get("ids", "")
    ids = [int(i) for i in ids_raw.split(",") if i.strip().isdigit()]
    deleted = 0
    protected = 0
    for product_id in ids:
        product = Product.query.filter_by(
            id=product_id,
            user_id=user_id,
            is_active=True,
        ).first()
        if product:
            if Sale.query.filter_by(product_id=product.id, user_id=user_id).first():
                product.is_active = False
                protected += 1
                continue
            db.session.delete(product)
            deleted += 1
    db.session.commit()
    if protected:
        flash(
            gettext(
                "Eliminamos %(deleted)s seleccionados y retiramos %(protected)s conservando su historial.",
                deleted=deleted,
                protected=protected,
            ),
            "info",
        )
    else:
        flash(gettext("%(count)s productos eliminados.", count=deleted), "success")
    return redirect(url_for("main.products"))


@main.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    access_block = _trial_access_response(current_user())
    if access_block:
        return access_block
    supplier = Supplier.query.filter_by(id=supplier_id, user_id=session["user_id"]).first_or_404()
    db.session.delete(supplier)
    db.session.commit()
    flash("Proveedor eliminado correctamente.", "success")
    return redirect(url_for("main.suppliers"))


@main.route("/admin/delete-user/<int:user_id>", methods=["POST"])
def admin_delete_user(user_id):
    admin_user = current_user()
    if not admin_user or admin_user.email != "albertonicopat@gmail.com":
        return redirect(url_for("main.dashboard"))
    user = User.query.get_or_404(user_id)
    if user.email == "albertonicopat@gmail.com":
        flash("No puedes eliminar tu cuenta de administrador.")
        return redirect(url_for("main.admin"))
    if _has_managed_stripe_subscription(user):
        flash(
            "No puedes eliminar este usuario mientras tenga una suscripción Stripe gestionable.",
            "danger",
        )
        return redirect(url_for("main.admin"))
    db.session.delete(user)
    db.session.commit()
    flash("Cliente eliminado correctamente.")
    return redirect(url_for("main.admin"))


@main.route("/admin/make-pro/<int:user_id>", methods=["POST"])
def admin_make_pro(user_id):
    admin_user = current_user()
    if not admin_user or admin_user.email != "albertonicopat@gmail.com":
        return redirect(url_for("main.dashboard"))
    user = User.query.get_or_404(user_id)
    user.manual_pro_access = True
    sync_user_plan(user)
    db.session.commit()
    flash("Cliente marcado como PRO.")
    return redirect(url_for("main.admin"))

@main.route("/settings", methods=["GET", "POST"])
def settings():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if request.method == "POST":
        access_block = _trial_access_response(user)
        if access_block:
            return access_block
        company_name = request.form.get("company_name", "").strip()
        if not company_name:
            flash("El nombre del negocio es obligatorio.", "danger")
            return redirect(url_for("main.settings"))
        user.company_name = company_name
        user.rfc = request.form.get("rfc", "").strip().upper()
        user.tax_regime = request.form.get("tax_regime", "").strip()
        user.address = request.form.get("address", "").strip()
        user.city = request.form.get("city", "").strip()
        user.state = request.form.get("state", "").strip()
        user.postal_code = request.form.get("postal_code", "").strip()
        user.phone = request.form.get("phone", "").strip()
        db.session.commit()
        flash("Configuración guardada.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html", user=user)


@main.route("/receipt/<int:sale_id>")
def receipt(sale_id):
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    sale = Sale.query.filter_by(id=sale_id, user_id=user.id).first_or_404()
    return redirect(url_for("main.ticket", ticket_ref=_sale_ticket_key(sale)))


@main.route("/ticket/<ticket_ref>")
def ticket(ticket_ref):
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    if ticket_ref.startswith("sale-") and ticket_ref[5:].isdigit():
        sale = Sale.query.filter_by(id=int(ticket_ref[5:]), user_id=user.id).first_or_404()
        sales = [sale]
    else:
        sales = Sale.query.filter_by(
            ticket_id=ticket_ref,
            user_id=user.id,
        ).order_by(Sale.id).all()
        if not sales:
            abort(404)

    address_parts = [part for part in (
        user.address,
        user.city,
        user.state,
        user.postal_code,
    ) if part]
    return render_template(
        "ticket.html",
        user=user,
        sales=sales,
        ticket_id=_sale_ticket_key(sales[0]),
        folio=_short_sale_folio(sales),
        ticket_total=sum(sale.total for sale in sales),
        ticket_subtotal=sum(sale.total for sale in sales),
        item_count=sum(sale.quantity for sale in sales),
        ticket_created_at=min(sale.created_at for sale in sales),
        payment_method=_payment_method_label(sales[0].payment_method),
        business_address=", ".join(address_parts),
        auto_print=request.args.get("print") == "1",
    )
