from datetime import datetime, timedelta
from io import BytesIO
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, send_file
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

        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        company_name = request.form.get("company_name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        state = request.form.get("state", "").strip()
        business_type = request.form.get("business_type", "").strip()

        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash("Ese correo ya está registrado.", "danger")
            return redirect(url_for("main.register"))

        user = User(
            email=email,
            company_name=company_name
        )

        user.first_name = first_name
        user.last_name = last_name
        user.phone = phone
        user.address = address
        user.city = city
        user.state = state
        user.business_type = business_type

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

    for p in products:
        sold_7_days = (
            db.session.query(func.sum(Sale.quantity))
            .filter(
                Sale.user_id == user_id,
                Sale.product_id == p.id,
                Sale.created_at >= week_start
            )
            .scalar() or 0
        )

        avg_daily_sales = sold_7_days / 7

        if avg_daily_sales > 0:
            days_left = round(p.stock / avg_daily_sales, 1)
        else:
            days_left = None

        if p.stock <= 0:
            alerts.append({
                "type": "critical",
                "title": f"{p.name} agotado",
                "text": "Stock actual: 0. Necesitas reabastecerlo inmediatamente."
            })

        elif p.stock <= p.min_stock:
            alerts.append({
                "type": "critical",
                "title": f"Reordenar {p.name}",
                "text": f"Stock actual: {p.stock}. Mínimo recomendado: {p.min_stock}."
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


    recommendations = []

    if top_products:
        recommendations.append(f"{top_products[0].name} es tu producto más vendido actualmente.")

    if week_sales > 0:
        recommendations.append(f"Las ventas de los últimos 7 días suman ${week_sales:,.0f} MXN.")

    if profit > 0:
        recommendations.append(f"La utilidad estimada de la semana fue de ${profit:,.0f} MXN.")

    if low_stock:
        recommendations.append(f"Tienes {low_stock} productos con inventario bajo. Reabastécelos pronto.")
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
        **analytics()
    )

@main.route("/products")
def products():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

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

    return render_template("products.html", products=query.order_by(Product.name).all(), q=q)
@main.route("/download-template")
def download_template():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    import pandas as pd

    columns = [
        "SKU",
        "Código de barras",
        "Nombre del producto",
        "Categoría",
        "Proveedor",
        "Costo",
        "Precio de venta",
        "Stock inicial",
        "Stock mínimo"
    ]

    df = pd.DataFrame(columns=columns)

    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="PRODUCTOS", startrow=3)

        workbook = writer.book
        ws = writer.sheets["PRODUCTOS"]

        # Título
        ws["A1"] = "PATIA - Plantilla oficial de productos"
        ws["A2"] = "Llena esta tabla con tus productos. No cambies los nombres de las columnas."
        ws.merge_cells("A1:I1")
        ws.merge_cells("A2:I2")

        # Estilos
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.worksheet.table import Table, TableStyleInfo

        title_fill = PatternFill("solid", fgColor="0B1020")
        header_fill = PatternFill("solid", fgColor="00D4FF")
        white_font = Font(color="FFFFFF", bold=True)
        dark_font = Font(color="0B1020", bold=True)
        center = Alignment(horizontal="center", vertical="center")
        thin = Side(border_style="thin", color="D9E2F3")

        ws["A1"].fill = title_fill
        ws["A1"].font = Font(color="FFFFFF", bold=True, size=18)
        ws["A1"].alignment = center

        ws["A2"].font = Font(color="666666", italic=True)
        ws["A2"].alignment = center

        # Encabezados
        for cell in ws[4]:
            cell.fill = header_fill
            cell.font = dark_font
            cell.alignment = center
            cell.border = Border(top=thin, left=thin, right=thin, bottom=thin)

        # Crear filas vacías para que se vea como tabla
        for row in range(5, 105):
            for col in range(1, 10):
                ws.cell(row=row, column=col).border = Border(
                    top=thin, left=thin, right=thin, bottom=thin
                )

        # Tabla
        table = Table(displayName="TablaProductosPATIA", ref="A4:I104")
        style = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False
        )
        table.tableStyleInfo = style
        ws.add_table(table)

        # Anchos
        widths = {
            "A": 18, "B": 22, "C": 32, "D": 20, "E": 24,
            "F": 14, "G": 18, "H": 18, "I": 18
        }

        for col, width in widths.items():
            ws.column_dimensions[col].width = width

        ws.freeze_panes = "A5"

        # Hoja instrucciones
        instrucciones = workbook.create_sheet("INSTRUCCIONES")

        instrucciones["A1"] = "PATIA - Guía para llenar tu catálogo"
        instrucciones["A1"].font = Font(bold=True, size=18, color="FFFFFF")
        instrucciones["A1"].fill = title_fill

        instrucciones["A3"] = "1. No modifiques los nombres de las columnas."
        instrucciones["A4"] = "2. Cada fila debe representar un producto."
        instrucciones["A5"] = "3. SKU es el código interno del producto."
        instrucciones["A6"] = "4. Código de barras puede ser el código del empaque."
        instrucciones["A7"] = "5. Costo es lo que te cuesta comprar el producto."
        instrucciones["A8"] = "6. Precio de venta es el precio al público."
        instrucciones["A9"] = "7. Stock inicial es la cantidad actual disponible."
        instrucciones["A10"] = "8. Stock mínimo activa alertas de reabastecimiento."

        instrucciones.column_dimensions["A"].width = 90

        # Hoja nota preliminar
        nota = workbook.create_sheet("NOTA PRELIMINAR")

        nota["A1"] = "Bienvenido a PATIA"
        nota["A1"].font = Font(bold=True, size=20, color="FFFFFF")
        nota["A1"].fill = title_fill

        nota["A3"] = "Para comenzar, llena la hoja PRODUCTOS con la información actual de tu negocio."
        nota["A4"] = "Después sube este archivo en la sección Inventario dentro de PATIA."
        nota["A6"] = "PATIA utilizará esta información para configurar:"
        nota["A7"] = "• Inventario"
        nota["A8"] = "• Alertas de stock"
        nota["A9"] = "• Punto de venta"
        nota["A10"] = "• Reportes"
        nota["A11"] = "• Análisis inteligente de ventas"

        nota.column_dimensions["A"].width = 90

    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="plantilla_productos_PATIA.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
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
    "SKU": "sku",
    "Código de barras": "barcode",
    "Nombre del producto": "name",
    "Categoría": "category",
    "Proveedor": "supplier",
    "Costo": "cost_price",
    "Precio de venta": "sale_price",
    "Stock inicial": "stock",
    "Stock mínimo": "min_stock"
})
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

    user = current_user()

    if trial_expired(user):
        return render_template("trial_expired.html")

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
    user = current_user()

    if not user:
        return redirect(url_for("main.login"))

    if trial_expired(user):
        return render_template("trial_expired.html")

    return render_template("reports.html", **analytics())


@main.route("/suppliers", methods=["GET", "POST"])
def suppliers():
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    if request.method == "POST":
        supplier_name = request.form["name"].strip()

        existing_supplier = Supplier.query.filter_by(
            user_id=session["user_id"],
            name=supplier_name
        ).first()

        if existing_supplier:
            flash("Ese proveedor ya existe.", "danger")
            return redirect(url_for("main.suppliers"))

        s = Supplier(
            user_id=session["user_id"],
            name=supplier_name,
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
    total_products = 0
    total_sales_count = 0
    total_sales_money = 0
    trial_clients = 0
    expired_clients = 0
    expiring_soon = 0
    new_this_week = 0
    new_this_month = 0

    for u in users:
        products_count = Product.query.filter_by(user_id=u.id).count()
        sales_count = Sale.query.filter_by(user_id=u.id).count()
        sales_money = db.session.query(func.sum(Sale.total)).filter_by(user_id=u.id).scalar() or 0

        days_in_patia = (today - u.created_at).days if u.created_at else 0
        trial_days_left = max(0, 14 - days_in_patia)

        if u.plan == "pro":
            status = "Pro"
            trial_days_left = "∞"
        elif trial_days_left > 0:
            status = "Prueba"
            trial_clients += 1
        else:
            status = "Vencido"
            expired_clients += 1

        if trial_days_left != "∞" and 0 < trial_days_left <= 7:
            expiring_soon += 1

        if days_in_patia <= 7:
            new_this_week += 1

        if days_in_patia <= 30:
            new_this_month += 1

        total_products += products_count
        total_sales_count += sales_count
        total_sales_money += sales_money

        clients.append({
            "user": u,
            "products_count": products_count,
            "sales_count": sales_count,
            "sales_money": sales_money,
            "days_in_patia": days_in_patia,
            "trial_days_left": trial_days_left,
            "status": status
        })

    top_client = max(clients, key=lambda c: c["products_count"], default=None)
    latest_client = clients[0] if clients else None

    return render_template(
        "admin.html",
        clients=clients,
        total_clients=len(users),
        total_products=total_products,
        total_sales_count=total_sales_count,
        total_sales_money=total_sales_money,
        trial_clients=trial_clients,
        expired_clients=expired_clients,
        expiring_soon=expiring_soon,
        new_this_week=new_this_week,
        new_this_month=new_this_month,
        top_client=top_client,
        latest_client=latest_client
    )
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

    product = Product.query.filter_by(
        id=product_id,
        user_id=session["user_id"]
    ).first_or_404()

    Sale.query.filter_by(
        product_id=product.id,
        user_id=session["user_id"]
    ).delete()

    db.session.delete(product)
    db.session.commit()

    return redirect(url_for("main.products") + "#catalogo")

@main.route("/suppliers/<int:supplier_id>/delete", methods=["POST"])
def delete_supplier(supplier_id):
    if not session.get("user_id"):
        return redirect(url_for("main.login"))

    supplier = Supplier.query.filter_by(
        id=supplier_id,
        user_id=session["user_id"]
    ).first_or_404()

    db.session.delete(supplier)
    db.session.commit()

    flash("Proveedor eliminado correctamente.", "success")
    return redirect(url_for("main.suppliers"))