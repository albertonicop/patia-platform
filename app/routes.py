from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from sqlalchemy import func
from . import db
from .models import Product, Sale, Supplier, User
main = Blueprint("main", __name__)
def current_user():
    user_id = session.get("user_id")

    if not user_id:
        return None

    return User.query.get(user_id)


def login_required():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

def money(value):
    return f"${value:,.2f} MXN"

@main.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        company_name = request.form["company_name"].strip()

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Ese correo ya está registrado.", "danger")
            return redirect(url_for("main.register"))

        user = User(email=email, company_name=company_name)
        user.set_password(password)

        db.session.add(user)
        db.session.commit()

        session["user_id"] = user.id
        flash("Cuenta creada correctamente.", "success")
        return redirect(url_for("main.dashboard"))

    return render_template("auth.html", title="Crear cuenta", button="Crear cuenta", mode="register")


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
    return redirect(url_for("main.login"))
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
    db.session.query(
        func.sum((Sale.unit_price - Product.cost_price) * Sale.quantity)
    )
    .join(Product)
    .filter(Product.user_id == user_id)
    .scalar() or 0
)

    top_products = (
    db.session.query(
        Product.name,
        func.sum(Sale.quantity).label("qty"),
        func.sum(Sale.total).label("revenue")
    )
    .join(Sale)
    .filter(Product.user_id == user_id)
    .group_by(Product.id)
    .order_by(func.sum(Sale.quantity).desc())
    .limit(5)
    .all()
)
    

    category_sales = (
    db.session.query(
        Product.category,
        func.sum(Sale.total).label("revenue")
    )
    .join(Sale)
    .filter(Product.user_id == user_id)
    .group_by(Product.category)
    .order_by(func.sum(Sale.total).desc())
    .all()
)

    alerts = []

    for p in Product.query.filter_by(
    user_id=user_id
    ).order_by(Product.stock.asc()).limit(20).all():
        
            sold_7_days = (
                db.session.query(func.sum(Sale.quantity))
                .filter(
                    Sale.product_id == p.id,
                    Sale.created_at >= week_start,
                )
                .scalar()
                or 0
            )

            avg_daily_sales = sold_7_days / 7

            if avg_daily_sales > 0:
                days_left = round(p.stock / avg_daily_sales, 1)
            else:
                days_left = None

            if p.stock <= p.min_stock:
                alerts.append({
                    "type": "critical",
                    "title": f"Reordenar {p.name}",
                    "text": f"Stock actual: {p.stock}. Está en nivel crítico."
                })

            elif days_left is not None and days_left <= 3:
                alerts.append({
                    "type": "critical",
                    "title": f"{p.name} se agotará pronto",
                    "text": f"Con el ritmo actual de ventas, se acabará en aproximadamente {days_left} días."
                })

            elif days_left is not None and days_left <= 7:
                alerts.append({
                    "type": "warning",
                    "title": f"Vigilar {p.name}",
                    "text": f"Inventario estimado para {days_left} días."
                })

            elif p.margin < 18:
                alerts.append({
                    "type": "warning",
                    "title": f"Margen bajo en {p.name}",
                    "text": f"Margen actual: {p.margin}%."
                })

    recommendations = []


    if top_products:
        recommendations.append(
            f"{top_products[0].name} es tu producto más vendido actualmente."
        )

    if week_sales > 0:
        recommendations.append(
            f"Las ventas de los últimos 7 días suman ${week_sales:,.0f} MXN."
        )

    if profit > 0:
        recommendations.append(
            f"La utilidad estimada de la semana fue de ${profit:,.0f} MXN."
        )

    if low_stock:
        recommendations.append(
            f"Tienes {low_stock} productos con inventario bajo. Reabastécelos pronto."
        )

    if top_products and low_stock:
        recommendations.append(
            f"{top_products[0].name} tiene alta rotación. Mantén suficiente stock disponible."
        )

    return dict(
        total_products=total_products,
        inventory_value=inventory_value,
        low_stock=low_stock,
        today_sales=today_sales,
        week_sales=week_sales,
        profit=profit,
        top_products=top_products,
        category_sales=category_sales,
        alerts=alerts[:6],
        recommendations=recommendations,
    )


@main.route("/")
def dashboard():
   return render_template(
    "dashboard.html",
    company_name=current_user().company_name if current_user() else "PATIA",
    **analytics()
)


@main.route("/products")
def products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    q = request.args.get("q", "").strip()
    query = Product.query.filter(Product.user_id == session["user_id"])

    if q:
        query = query.filter(
            Product.name.ilike(f"%{q}%") |
            Product.category.ilike(f"%{q}%") |
            Product.sku.ilike(f"%{q}%")
        )

    return render_template("products.html", products=query.order_by(Product.name).all(), q=q)
@main.route("/import-products", methods=["POST"])
def import_products():
    import pandas as pd

    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    file = request.files.get("catalog_file")

    if not file:
        flash("Selecciona un archivo.", "danger")
        return redirect(url_for("main.products"))

    try:
        if file.filename.endswith(".csv"):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        for _, row in df.iterrows():

            product = Product(
                user_id=session["user_id"],
                sku=str(row.get("sku", "")).strip(),
                barcode=str(row.get("barcode", "")).strip(),
                name=str(row.get("name", "")).strip(),
                category=str(row.get("category", "General")).strip(),
                supplier=str(row.get("supplier", "")).strip(),
                cost_price=float(row.get("cost_price", 0) or 0),
                sale_price=float(row.get("sale_price", 0) or 0),
                stock=int(row.get("stock", 0) or 0),
                min_stock=int(row.get("min_stock", 5) or 5)
            )

            db.session.add(product)

        db.session.commit()

        flash("Catálogo importado correctamente.", "success")

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

    if request.method == "POST":
        product = Product.query.filter_by(
            id=int(request.form["product_id"]),
            user_id=session["user_id"]
        ).first_or_404()

        qty = int(request.form.get("quantity") or 1)

        if qty <= 0:
            flash("La cantidad debe ser mayor a cero.", "danger")

        elif product.stock < qty:
            flash("No hay suficiente inventario.", "danger")

        else:
            product.stock -= qty
            sale = Sale(
                user_id=session["user_id"],
                product_id=product.id,
                quantity=qty,
                unit_price=product.sale_price,
                total=qty * product.sale_price
            )
            db.session.add(sale)
            db.session.commit()
            flash(f"Venta registrada: {product.name} x{qty}.", "success")

        return redirect(url_for("main.sell"))

    sales = Sale.query.filter_by(
        user_id=session["user_id"]
    ).order_by(Sale.created_at.desc()).limit(12).all()

    products = Product.query.filter_by(
        user_id=session["user_id"]
    ).order_by(Product.name).all()

    return render_template("sell.html", products=products, sales=sales)


@main.route("/reports")
def reports():
    return render_template("reports.html", **analytics())


@main.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    if request.method == "POST":
        s = Supplier(
            user_id=session["user_id"],
            name=request.form["name"],
            contact=request.form.get("contact"),
            phone=request.form.get("phone"),
            notes=request.form.get("notes")
        )

        db.session.add(s)
        db.session.commit()
        flash("Proveedor guardado.", "success")
        return redirect(url_for("main.suppliers"))

    suppliers = Supplier.query.filter_by(
        user_id=session["user_id"]
    ).order_by(Supplier.name).all()

    return render_template("suppliers.html", suppliers=suppliers)


@main.route("/reset-demo")
def reset_demo():
    from seed import seed_data
    seed_data()
    flash("Datos demo cargados.", "success")
    return redirect(url_for("main.dashboard"))
