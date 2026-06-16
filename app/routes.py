# -*- coding: utf-8 -*-
import resend
from email_validator import validate_email, EmailNotValidError
import random
import string
from datetime import datetime, timedelta
from io import BytesIO
import stripe
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, send_file
from sqlalchemy import func
from . import db
from .models import Product, Sale, Supplier, User

main = Blueprint("main", __name__)


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return User.query.get(user_id)


def trial_expired(user):
    if not user:
        return True
    if user.plan == "pro":
        return False
    days_used = (datetime.utcnow() - user.created_at).days
    return days_used >= 14


def money(value):
    return f"${value:,.2f} MXN"


def send_email(to, subject, html):
    try:
        resend.api_key = current_app.config["RESEND_API_KEY"]
        resend.Emails.send({
            "from": current_app.config["RESEND_FROM"],
            "to": to,
            "subject": subject,
            "html": html
        })
    except Exception as e:
        print(f"Error enviando correo: {e}")


@main.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        try:
            validate_email(email, check_deliverability=True)
        except EmailNotValidError:
            flash("El correo no es válido o no existe.", "danger")
            return redirect(url_for("main.register"))

        password = request.form["password"]
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
            flash("Ese correo ya está registrado.", "danger")
            return redirect(url_for("main.register"))

        user = User(email=email, company_name=company_name)
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
        db.session.commit()

        session["user_id"] = user.id

        if request.args.get("plan") == "pro" or request.form.get("plan") == "pro":
            flash("Cuenta creada correctamente. Activa PATIA Pro para continuar.", "success")
            return redirect(url_for("main.subscribe"))

        code = ''.join(random.choices(string.digits, k=6))
        user.verification_code = code
        user.verification_code_expires = datetime.utcnow() + timedelta(minutes=30)
        db.session.commit()

        send_email(
            to=user.email,
            subject="Verifica tu correo en PATIA",
            html=f"""
            <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                <img src="https://patiaapp.com/static/img/logo-patia.png" style="width:160px;margin-bottom:24px;">
                <h1 style="color:#29d3a8;">Verifica tu correo</h1>
                <p style="color:#9aa8c7;font-size:16px;">Tu codigo de verificacion es:</p>
                <div style="font-size:48px;font-weight:900;letter-spacing:12px;color:#fff;margin:24px 0;">{code}</div>
                <p style="color:#9aa8c7;font-size:14px;">Este codigo expira en 30 minutos.</p>
            </div>
            """
        )

        flash("Te enviamos un codigo de verificacion a tu correo.", "success")
        return redirect(url_for("main.verify_email"))

    return render_template("auth.html", title="Crear cuenta", button="Crear cuenta", mode="register", plan=request.args.get("plan"))


@main.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    if user.email_verified:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        code = request.form.get("code", "").strip()

        if not user.verification_code or not user.verification_code_expires:
            flash("Código inválido.", "danger")
            return redirect(url_for("main.verify_email"))

        if datetime.utcnow() > user.verification_code_expires:
            flash("El código expiró. Solicita uno nuevo.", "danger")
            return redirect(url_for("main.verify_email"))

        if code != user.verification_code:
            flash("Código incorrecto.", "danger")
            return redirect(url_for("main.verify_email"))

        user.email_verified = True
        user.verification_code = None
        user.verification_code_expires = None
        db.session.commit()

        flash("¡Correo verificado! Bienvenido a PATIA.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("verify_email.html", user=user)


@main.route("/resend-verification", methods=["POST"])
def resend_verification():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))

    code = ''.join(random.choices(string.digits, k=6))
    user.verification_code = code
    user.verification_code_expires = datetime.utcnow() + timedelta(minutes=30)
    db.session.commit()

    send_email(
        to=user.email,
        subject="Nuevo codigo de verificacion PATIA",
        html=f"""
        <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
            <h1 style="color:#29d3a8;">Tu nuevo codigo</h1>
            <div style="font-size:48px;font-weight:900;letter-spacing:12px;color:#fff;margin:24px 0;">{code}</div>
            <p style="color:#9aa8c7;font-size:14px;">Este codigo expira en 30 minutos.</p>
        </div>
        """
    )

    flash("Te enviamos un nuevo codigo.", "success")
    return redirect(url_for("main.verify_email"))


@main.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = ''.join(random.choices(string.ascii_letters + string.digits, k=48))
            user.reset_token = token
            user.reset_token_expires = datetime.utcnow() + timedelta(minutes=30)
            db.session.commit()
            send_email(
                to=user.email,
                subject="Recupera tu contrasena PATIA",
                html=f"""
                <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
                    <h1 style="color:#29d3a8;">Recuperar contrasena</h1>
                    <p style="color:#9aa8c7;">Haz clic en el boton para crear una nueva contrasena. Expira en 30 minutos.</p>
                    <a href="https://patiaapp.com/reset-password/{token}" style="display:inline-block;margin-top:24px;padding:14px 28px;background:linear-gradient(135deg,#7c5cff,#29d3a8);color:white;text-decoration:none;border-radius:14px;font-weight:800;">Crear nueva contrasena</a>
                </div>
                """
            )
        flash("Si ese correo existe, te enviamos un enlace.", "success")
        return redirect(url_for("main.login"))
    return render_template("forgot_password.html")


@main.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = User.query.filter_by(reset_token=token).first()
    if not user or user.reset_token_expires < datetime.utcnow():
        flash("El enlace expiro o no es valido.", "danger")
        return redirect(url_for("main.forgot_password"))
    if request.method == "POST":
        password = request.form["password"]
        user.set_password(password)
        user.reset_token = None
        user.reset_token_expires = None
        db.session.commit()
        flash("Contrasena actualizada. Inicia sesion.", "success")
        return redirect(url_for("main.login"))
    return render_template("reset_password.html", token=token)


@main.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Correo o contraseña incorrectos.", "danger")
            return redirect(url_for("main.login"))

        session["user_id"] = user.id
        flash("Sesión iniciada correctamente.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth.html", title="Iniciar sesión", button="Entrar", mode="login")


@main.route("/logout")
def logout():
    session.clear()
    flash("Sesión cerrada.", "success")
    return redirect("/")


@main.app_template_filter("money")
def money_filter(value):
    return money(value or 0)


def analytics():
    today = datetime.utcnow().date()
    start = datetime.combine(today, datetime.min.time())
    week_start = datetime.utcnow() - timedelta(days=7)
    user_id = session.get("user_id")

    products = Product.query.filter_by(user_id=user_id).all()

    total_products = len(products)
    inventory_value = sum(p.inventory_value for p in products)
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
        .filter(Product.user_id == user_id)
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

    alerts = []
    for p in products:
        sold_7_days = (
            db.session.query(func.sum(Sale.quantity))
            .filter(Sale.user_id == user_id, Sale.product_id == p.id, Sale.created_at >= week_start)
            .scalar() or 0
        )
        avg_daily_sales = sold_7_days / 7
        days_left = round(p.stock / avg_daily_sales, 1) if avg_daily_sales > 0 else None

        if p.stock <= 0:
            alerts.append({"type": "critical", "title": f"{p.name} agotado", "text": "Stock actual: 0. Necesitas reabastecerlo inmediatamente."})
        elif p.stock <= p.min_stock:
            alerts.append({"type": "critical", "title": f"Reordenar {p.name}", "text": f"Stock actual: {p.stock}. Minimo recomendado: {p.min_stock}."})
        elif days_left is not None and days_left <= 3:
            alerts.append({"type": "critical", "title": f"{p.name} se agotara pronto", "text": f"Con el ritmo actual de ventas, se acabara en aproximadamente {days_left} dias."})
        elif days_left is not None and days_left <= 7:
            alerts.append({"type": "warning", "title": f"Vigilar {p.name}", "text": f"Inventario estimado para {days_left} dias."})

    recommendations = []
    if top_products:
        recommendations.append(f"{top_products[0].name} es tu producto mas vendido actualmente.")
    if week_sales > 0:
        recommendations.append(f"Las ventas de los ultimos 7 dias suman ${week_sales:,.0f} MXN.")
    if profit > 0:
        recommendations.append(f"La utilidad estimada de la semana fue de ${profit:,.0f} MXN.")
    if low_stock:
        recommendations.append(f"Tienes {low_stock} productos con inventario bajo. Reabastecellos pronto.")

    alerts = alerts[:5]
    return dict(
        total_products=total_products,
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
        session.clear()
        return render_template("landing.html")
    return render_template(
        "dashboard.html",
        company_name=user.company_name,
        user=user,
        trial_days_left=max(0, 14 - (datetime.utcnow() - user.created_at).days) if user.created_at else 14,
        **analytics()
    )


@main.route("/products")
def products():
    if not session.get("user_id"):
        return redirect(url_for("main.landing"))
    user = current_user()
    if trial_expired(user):
        return render_template("trial_expired.html")
    q = request.args.get("q", "").strip()
    query = Product.query.filter(Product.user_id == session["user_id"])
    if q:
        query = query.filter(
            Product.name.ilike(f"%{q}%") |
            Product.category.ilike(f"%{q}%") |
            Product.sku.ilike(f"%{q}%")
        )
    return render_template("products.html", products=query.order_by(Product.name).all(), q=q, user=user)


@main.route("/download-template")
def download_template():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    import pandas as pd

    columns = ["SKU", "Codigo de barras", "Nombre del producto", "Categoria", "Proveedor", "Costo", "Precio de venta", "Stock inicial", "Stock minimo"]
    df = pd.DataFrame(columns=columns)
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="PRODUCTOS", startrow=3)
        workbook = writer.book
        ws = writer.sheets["PRODUCTOS"]

        ws["A1"] = "PATIA - Plantilla oficial de productos"
        ws["A2"] = "Llena esta tabla con tus productos. No cambies los nombres de las columnas."
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

    file = request.files.get("catalog_file")
    if not file:
        flash("Selecciona un archivo.", "danger")
        return redirect(url_for("main.products") + "#catalogo")

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file, sheet_name="PRODUCTOS", header=3)

        df = df.rename(columns={
            "SKU": "sku", "Codigo de barras": "barcode", "Nombre del producto": "name",
            "Categoria": "category", "Proveedor": "supplier", "Costo": "cost_price",
            "Precio de venta": "sale_price", "Stock inicial": "stock", "Stock minimo": "min_stock"
        })

        for _, row in df.iterrows():
            sku = str(row.get("sku", "")).strip()
            raw_barcode = row.get("barcode", "")
            try:
                barcode = str(int(float(raw_barcode))) if raw_barcode and str(raw_barcode) != "nan" else ""
            except:
                barcode = str(raw_barcode).strip()

            existing = Product.query.filter_by(user_id=session["user_id"], sku=sku).first()
            if existing:
                existing.stock += int(row.get("stock", 0) or 0)
                existing.sale_price = float(row.get("sale_price", 0) or 0)
                existing.cost_price = float(row.get("cost_price", 0) or 0)
                existing.min_stock = int(row.get("min_stock", 5) or 5)
                existing.barcode = barcode
                continue

            if not existing and barcode:
                existing = Product.query.filter_by(user_id=session["user_id"], barcode=barcode).first()

            if existing:
                existing.stock = int(row.get("stock", 0) or 0)
                existing.sale_price = float(row.get("sale_price", 0) or 0)
                existing.cost_price = float(row.get("cost_price", 0) or 0)
                existing.min_stock = int(row.get("min_stock", 5) or 5)
            else:
                product = Product(
                    user_id=session["user_id"], sku=sku, barcode=barcode,
                    name=str(row.get("name", "")).strip(), category=str(row.get("category", "General")).strip(),
                    supplier=str(row.get("supplier", "")).strip(), cost_price=float(row.get("cost_price", 0) or 0),
                    sale_price=float(row.get("sale_price", 0) or 0), stock=int(row.get("stock", 0) or 0),
                    min_stock=int(row.get("min_stock", 5) or 5)
                )
                db.session.add(product)

        db.session.commit()
        flash("Catalogo importado correctamente.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"Error al importar: {e}", "danger")

    return redirect(url_for("main.products"))


@main.route("/products/new", methods=["POST"])
def add_product():
    p = Product(
        user_id=session["user_id"],
        sku=request.form["sku"],
        barcode=request.form.get("barcode") or None,
        name=request.form["name"],
        category=request.form.get("category") or "General",
        supplier=request.form.get("supplier"),
        cost_price=float(request.form.get("cost_price") or 0),
        sale_price=float(request.form.get("sale_price") or 0),
        stock=int(request.form.get("stock") or 0),
        min_stock=int(request.form.get("min_stock") or 5),
    )
    db.session.add(p)
    db.session.commit()
    flash("Producto creado correctamente.", "success")
    return redirect(url_for("main.products"))


@main.route("/sell", methods=["GET", "POST"])
def sell():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()
    if trial_expired(user):
        return render_template("trial_expired.html")

    if request.method == "POST":
        product = Product.query.filter_by(id=int(request.form["product_id"]), user_id=session["user_id"]).first_or_404()
        qty = int(request.form.get("quantity") or 1)
        if qty <= 0:
            flash("La cantidad debe ser mayor a cero.", "danger")
        elif product.stock < qty:
            flash("No hay suficiente inventario.", "danger")
        else:
            product.stock -= qty
            sale = Sale(user_id=session["user_id"], product_id=product.id, quantity=qty, unit_price=product.sale_price, total=qty * product.sale_price)
            db.session.add(sale)
            db.session.commit()
            flash(f"Venta registrada: {product.name} x{qty}.", "success")
        return redirect(url_for("main.sell"))

    sales = Sale.query.filter_by(user_id=session["user_id"]).order_by(Sale.created_at.desc()).limit(12).all()
    products = Product.query.filter_by(user_id=session["user_id"]).order_by(Product.name).all()
    return render_template("sell.html", products=products, sales=sales, user=user)


@main.route("/sell-cart", methods=["POST"])
def sell_cart():
    from flask import jsonify
    user = current_user()
    if not user:
        return jsonify({"ok": False, "error": "No autenticado"})

    data = request.get_json()
    items = data.get("items", [])

    try:
        for item in items:
            product = Product.query.filter_by(id=int(item["product_id"]), user_id=user.id).first()
            if not product:
                return jsonify({"ok": False, "error": "Producto no encontrado"})
            qty = int(item["quantity"])
            if product.stock < qty:
                return jsonify({"ok": False, "error": f"Stock insuficiente: {product.name}"})
            product.stock -= qty
            sale = Sale(user_id=user.id, product_id=product.id, quantity=qty, unit_price=product.sale_price, total=qty * product.sale_price)
            db.session.add(sale)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)})


@main.route("/reports")
def reports():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    if user.email == "albertonicopat@gmail.com" or (user.plan or "").lower().strip() == "pro":
        return render_template("reports.html", user=user, **analytics())
    return redirect(url_for("main.subscribe"))


@main.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user = current_user()

    if request.method == "POST":
        supplier_name = request.form["name"].strip()
        existing_supplier = Supplier.query.filter_by(user_id=session["user_id"], name=supplier_name).first()
        if existing_supplier:
            flash("Ese proveedor ya existe.", "danger")
            return redirect(url_for("main.suppliers"))
        s = Supplier(user_id=session["user_id"], name=supplier_name, contact=request.form.get("contact"), phone=request.form.get("phone"), notes=request.form.get("notes"))
        db.session.add(s)
        db.session.commit()
        flash("Proveedor guardado.", "success")
        return redirect(url_for("main.suppliers"))

    suppliers = Supplier.query.filter_by(user_id=session["user_id"]).order_by(Supplier.name).all()
    return render_template("suppliers.html", suppliers=suppliers, user=user)


@main.route("/subscribe")
def subscribe():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    return render_template("subscribe.html", user=user)


@main.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": current_app.config["STRIPE_PRICE_ID"], "quantity": 1}],
        success_url=url_for("main.stripe_success", _external=True),
        cancel_url=url_for("main.subscribe", _external=True),
        metadata={"user_id": user.id}
    )
    return redirect(checkout_session.url, code=303)


@main.route("/stripe-webhook", methods=["POST"])
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

    data = event["data"]["object"]

    if event["type"] == "checkout.session.completed":
        user_id = data.get("metadata", {}).get("user_id")
        if user_id:
            user = User.query.get(int(user_id))
            if user:
                user.plan = "pro"
                user.stripe_customer_id = data.get("customer")
                user.stripe_subscription_id = data.get("subscription")
                user.subscription_status = "active"
                db.session.commit()

    elif event["type"] == "invoice.payment_succeeded":
        sub_id = data.get("subscription")
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            import stripe as stripe_lib
            stripe_lib.api_key = current_app.config["STRIPE_SECRET_KEY"]
            sub = stripe_lib.Subscription.retrieve(sub_id)
            user.subscription_status = "active"
            user.plan = "pro"
            user.current_period_end = datetime.utcfromtimestamp(sub["current_period_end"])
            db.session.commit()

    elif event["type"] == "invoice.payment_failed":
        sub_id = data.get("subscription")
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            user.subscription_status = "past_due"
            db.session.commit()

    elif event["type"] in ["customer.subscription.deleted", "customer.subscription.updated"]:
        sub_id = data.get("id")
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
        if user:
            status = data.get("status")
            user.subscription_status = status
            user.cancel_at_period_end = data.get("cancel_at_period_end", False)
            if status in ["canceled", "unpaid"]:
                user.plan = "trial"
            db.session.commit()

    return "", 200


@main.route("/stripe-success")
def stripe_success():
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
    user.plan = "pro"
    db.session.commit()
    flash("Tu cuenta PATIA Pro ha sido activada.")
    send_email(
        to=user.email,
        subject="Tu cuenta PATIA Pro esta activa!",
        html=f"""
        <div style="font-family:Inter,Arial,sans-serif;max-width:600px;margin:0 auto;background:#0b1020;color:#eef3ff;padding:40px;border-radius:24px;">
            <img src="https://patiaapp.com/static/img/logo-patia.png" style="width:160px;margin-bottom:24px;">
            <h1 style="color:#29d3a8;">Ya eres PATIA Pro!</h1>
            <p style="color:#9aa8c7;font-size:16px;line-height:1.6;">Tu suscripcion ha sido activada correctamente. Ahora tienes acceso a <strong style="color:#fff;">Reportes IA</strong> y todas las funciones avanzadas.</p>
            <a href="https://patiaapp.com/reports" style="display:inline-block;margin-top:24px;padding:14px 28px;background:linear-gradient(135deg,#7c5cff,#29d3a8);color:white;text-decoration:none;border-radius:14px;font-weight:800;">Ver mis Reportes IA</a>
            <p style="margin-top:32px;color:#9aa8c7;font-size:13px;">Tu suscripcion se renueva automaticamente cada mes. Puedes cancelar cuando quieras desde Mi Suscripcion.</p>
        </div>
        """
    )
    return redirect(url_for("main.dashboard"))


@main.route("/sales/<int:sale_id>/cancel", methods=["POST"])
def cancel_sale(sale_id):
    user = current_user()
    if not user:
        return redirect(url_for("main.login"))
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
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    subscription_info = None
    if user.stripe_subscription_id:
        try:
            sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
            user.current_period_end = datetime.utcfromtimestamp(sub["current_period_end"])
            user.cancel_at_period_end = sub["cancel_at_period_end"]
            db.session.commit()
            subscription_info = sub
        except Exception:
            pass
    return render_template("subscription.html", user=user, subscription_info=subscription_info)


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
    except Exception as e:
        flash(f"Error al cancelar: {e}", "danger")
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
    except Exception as e:
        flash(f"Error al reactivar: {e}", "danger")
    return redirect(url_for("main.subscription"))


@main.route("/billing-portal", methods=["POST"])
def billing_portal():
    user = current_user()
    if not user or not user.stripe_customer_id:
        return redirect(url_for("main.subscribe"))
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]
    try:
        portal = stripe.billing_portal.Session.create(customer=user.stripe_customer_id, return_url=url_for("main.subscription", _external=True))
        return redirect(portal.url)
    except Exception as e:
        flash(f"Error: {e}", "danger")
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
        products_count = Product.query.filter_by(user_id=u.id).count()
        sales_count = Sale.query.filter_by(user_id=u.id).count()
        sales_money = db.session.query(func.sum(Sale.total)).filter_by(user_id=u.id).scalar() or 0
        days_in_patia = (today - u.created_at).days if u.created_at else 0
        trial_days_left = max(0, 14 - days_in_patia)

        if u.plan == "pro":
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


@main.route("/reset-demo")
def reset_demo():
    from seed import seed_data
    seed_data()
    flash("Datos demo cargados.", "success")
    return redirect(url_for("main.dashboard"))


@main.route("/products/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    product = Product.query.filter_by(id=product_id, user_id=session["user_id"]).first_or_404()
    Sale.query.filter_by(product_id=product.id, user_id=session["user_id"]).delete()
    db.session.delete(product)
    db.session.commit()
    return redirect(url_for("main.products") + "#catalogo")


@main.route("/products/delete-all", methods=["POST"])
def delete_all_products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user_id = session["user_id"]
    Sale.query.filter_by(user_id=user_id).delete()
    Product.query.filter_by(user_id=user_id).delete()
    db.session.commit()
    flash("Catalogo eliminado completamente.", "success")
    return redirect(url_for("main.products"))


@main.route("/products/delete-selected", methods=["POST"])
def delete_selected_products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
    user_id = session["user_id"]
    ids_raw = request.form.get("ids", "")
    ids = [int(i) for i in ids_raw.split(",") if i.strip().isdigit()]
    for product_id in ids:
        product = Product.query.filter_by(id=product_id, user_id=user_id).first()
        if product:
            Sale.query.filter_by(product_id=product.id, user_id=user_id).delete()
            db.session.delete(product)
    db.session.commit()
    flash(f"{len(ids)} productos eliminados.", "success")
    return redirect(url_for("main.products"))


@main.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    if not session.get("user_id"):
        return redirect(url_for("main.login"))
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
    user.plan = "pro"
    db.session.commit()
    flash("Cliente marcado como PRO.")
    return redirect(url_for("main.admin"))