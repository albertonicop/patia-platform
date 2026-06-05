from app import create_app, db
from app.models import Product, Sale, Supplier
from datetime import datetime, timedelta
import random


def seed_data():
    app = create_app()
    with app.app_context():
        db.drop_all(); db.create_all()
        suppliers = [
            Supplier(name="Coca-Cola FEMSA", contact="Ventas ruta", phone="222-000-1000"),
            Supplier(name="PepsiCo / Sabritas", contact="Ejecutivo zona", phone="222-000-2000"),
            Supplier(name="Grupo Bimbo", contact="Reparto", phone="222-000-3000"),
            Supplier(name="Mayorista Centro", contact="Compras", phone="222-000-4000"),
        ]
        db.session.add_all(suppliers)
        products = [
            ("COCA600", "7501055300075", "Coca-Cola 600ml", "Bebidas", "Coca-Cola FEMSA", 12, 18, 42, 18),
            ("AGUA1L", "7501055300105", "Agua Ciel 1L", "Bebidas", "Coca-Cola FEMSA", 8, 14, 28, 15),
            ("SAB45", "7501011130001", "Sabritas Original 45g", "Botanas", "PepsiCo / Sabritas", 10, 17, 11, 20),
            ("DOR45", "7501011130002", "Doritos Nacho 45g", "Botanas", "PepsiCo / Sabritas", 11, 18, 9, 18),
            ("BIMBOL", "7501000110001", "Pan Bimbo Grande", "Panadería", "Grupo Bimbo", 32, 48, 14, 10),
            ("MARIN", "7501000110002", "Mantecadas Bimbo", "Panadería", "Grupo Bimbo", 18, 28, 22, 12),
            ("LECHE1", "7501020510001", "Leche Alpura 1L", "Lácteos", "Mayorista Centro", 22, 31, 6, 12),
            ("HUEVO", "7500000000012", "Huevo blanco 12 pzas", "Básicos", "Mayorista Centro", 34, 48, 7, 8),
            ("ATUN", "7501032390001", "Atún Dolores lata", "Abarrotes", "Mayorista Centro", 16, 25, 30, 10),
            ("ARROZ", "7501071300001", "Arroz 1kg", "Abarrotes", "Mayorista Centro", 20, 32, 18, 10),
        ]
        objs = []
        for row in products:
            objs.append(Product(sku=row[0], barcode=row[1], name=row[2], category=row[3], supplier=row[4], cost_price=row[5], sale_price=row[6], stock=row[7], min_stock=row[8]))
        db.session.add_all(objs); db.session.commit()
        for i in range(120):
            p = random.choice(objs)
            qty = random.choice([1,1,1,2,2,3])
            sale = Sale(product_id=p.id, quantity=qty, unit_price=p.sale_price, total=qty*p.sale_price, created_at=datetime.utcnow()-timedelta(days=random.randint(0,13), hours=random.randint(0,23)))
            db.session.add(sale)
        db.session.commit()
        print("Datos demo cargados.")

if __name__ == "__main__":
    seed_data()
