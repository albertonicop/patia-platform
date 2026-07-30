"""Professional, tenant-safe catalog import parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import re
import unicodedata

import pandas as pd

from app import db
from app.inventory.services import record_inventory_movement
from app.models import Product
from app.money import money_decimal


MAX_IMPORT_ROWS = 5_000
FIELDS = (
    "sku", "barcode", "name", "category", "supplier",
    "cost_price", "sale_price", "stock", "min_stock",
)
REQUIRED_FIELDS = frozenset({"sku", "name", "sale_price", "stock"})
ALIASES = {
    "sku": {"sku", "clave", "codigo interno", "id producto", "product sku"},
    "barcode": {"codigo de barras", "barcode", "ean", "upc", "gtin"},
    "name": {"nombre", "nombre del producto", "producto", "product name", "description", "descripcion"},
    "category": {"categoria", "category", "departamento", "familia"},
    "supplier": {"proveedor", "supplier", "vendor"},
    "cost_price": {"costo", "precio de compra", "cost", "cost price"},
    "sale_price": {"precio", "precio de venta", "sale price", "retail price"},
    "stock": {"stock", "stock inicial", "existencias", "cantidad", "initial stock"},
    "min_stock": {"stock minimo", "minimo", "minimum stock", "reorder point"},
}


def _normalized(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _read_frame(filename: str, content: bytes) -> pd.DataFrame:
    lower = (filename or "").casefold()
    if lower.endswith(".csv"):
        try:
            return pd.read_csv(
                BytesIO(content), dtype=str, keep_default_na=False,
                encoding="utf-8-sig", sep=None, engine="python",
            )
        except UnicodeDecodeError:
            return pd.read_csv(
                BytesIO(content), dtype=str, keep_default_na=False,
                encoding="latin-1", sep=None, engine="python",
            )
    if not lower.endswith(".xlsx"):
        raise ValueError("unsupported_file")
    raw = pd.read_excel(BytesIO(content), dtype=str, header=None, keep_default_na=False)
    header_row = 0
    best_matches = -1
    known = {alias for values in ALIASES.values() for alias in values}
    for index in range(min(12, len(raw.index))):
        matches = sum(_normalized(value) in known for value in raw.iloc[index].tolist())
        if matches > best_matches:
            header_row, best_matches = index, matches
    frame = raw.iloc[header_row + 1:].copy()
    frame.columns = [str(value).strip() for value in raw.iloc[header_row].tolist()]
    return frame.reset_index(drop=True)


def auto_mapping(headers) -> dict[str, str]:
    normalized_headers = {_normalized(header): str(header) for header in headers if str(header).strip()}
    mapping = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in normalized_headers:
                mapping[field] = normalized_headers[alias]
                break
    return mapping


def _text(value, limit=255):
    return str(value or "").strip()[:limit]


def _barcode(value):
    text = _text(value, 64)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _decimal(value, *, default="0.00"):
    text = _text(value)
    if not text:
        text = default
    text = text.replace("\u00a0", "").replace("$", "").replace("MXN", "").strip()
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[-1]
        text = text.replace(",", ".") if len(tail) <= 2 else text.replace(",", "")
    try:
        return money_decimal(Decimal(text))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid_number")


def _integer(value, *, default=0):
    text = _text(value)
    if not text:
        return default
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError("invalid_integer")
    if number < 0 or number != number.to_integral_value():
        raise ValueError("invalid_integer")
    return int(number)


def _row_value(row, mapping, field):
    header = mapping.get(field)
    return row.get(header, "") if header else ""


@dataclass
class CatalogImport:
    filename: str
    digest: str
    headers: list[str]
    mapping: dict[str, str]
    rows: list[dict]
    errors: list[dict]
    summary: dict


def inspect_catalog(filename, content, mapping=None, existing_products=()):
    if not content:
        raise ValueError("empty_file")
    frame = _read_frame(filename, content)
    if len(frame.index) > MAX_IMPORT_ROWS:
        raise ValueError("too_many_rows")
    headers = [str(column).strip() for column in frame.columns if str(column).strip()]
    chosen = {key: value for key, value in (mapping or auto_mapping(headers)).items() if key in FIELDS and value in headers}
    existing_sku = {item.sku: item for item in existing_products if item.sku}
    existing_barcode = {item.barcode: item for item in existing_products if item.barcode}
    seen_sku, seen_barcode = set(), set()
    rows, errors = [], []
    summary = {"total": 0, "valid": 0, "invalid": 0, "new": 0, "updated": 0, "duplicates": 0, "blank": 0}
    if not REQUIRED_FIELDS.issubset(chosen):
        return CatalogImport(filename, sha256(content).hexdigest(), headers, chosen, [], [], summary)

    for offset, (_, source) in enumerate(frame.iterrows(), 2):
        values = [str(value or "").strip() for value in source.tolist()]
        if not any(values):
            summary["blank"] += 1
            continue
        summary["total"] += 1
        try:
            item = {
                "row": offset,
                "sku": _text(_row_value(source, chosen, "sku"), 64),
                "barcode": _barcode(_row_value(source, chosen, "barcode")) or None,
                "name": _text(_row_value(source, chosen, "name"), 160),
                "category": _text(_row_value(source, chosen, "category"), 80) or "General",
                "supplier": _text(_row_value(source, chosen, "supplier"), 120) or None,
                "cost_price": _decimal(_row_value(source, chosen, "cost_price")),
                "sale_price": _decimal(_row_value(source, chosen, "sale_price")),
                "stock": _integer(_row_value(source, chosen, "stock")),
                "min_stock": _integer(_row_value(source, chosen, "min_stock"), default=5),
            }
            if not item["sku"] or not item["name"]:
                raise ValueError("missing_identity")
            if item["sku"] in seen_sku or (item["barcode"] and item["barcode"] in seen_barcode):
                summary["duplicates"] += 1
                raise ValueError("duplicate_in_file")
            seen_sku.add(item["sku"])
            if item["barcode"]:
                seen_barcode.add(item["barcode"])
            by_sku = existing_sku.get(item["sku"])
            by_barcode = existing_barcode.get(item["barcode"]) if item["barcode"] else None
            if by_sku and by_barcode and by_sku.id != by_barcode.id:
                raise ValueError("conflicting_identity")
            item["action"] = "update" if by_sku or by_barcode else "create"
            item["product_id"] = (by_sku or by_barcode).id if (by_sku or by_barcode) else None
            summary["updated" if item["action"] == "update" else "new"] += 1
            summary["valid"] += 1
            rows.append(item)
        except ValueError as exc:
            summary["invalid"] += 1
            errors.append({"row": offset, "code": str(exc), "sku": _text(_row_value(source, chosen, "sku"), 64), "name": _text(_row_value(source, chosen, "name"), 160)})
    return CatalogImport(filename, sha256(content).hexdigest(), headers, chosen, rows, errors, summary)


def apply_catalog(imported, organization_id, owner_id, membership):
    existing = Product.query.filter_by(organization_id=organization_id).with_for_update().all()
    by_id = {item.id: item for item in existing}
    created = updated = 0
    pending_opening_movements = []
    for row in imported.rows:
        product = by_id.get(row["product_id"])
        if product:
            before = product.stock
            product.sku = row["sku"]
            product.barcode = row["barcode"]
            product.name = row["name"]
            product.category = row["category"]
            product.supplier = row["supplier"]
            product.cost_price = row["cost_price"]
            product.sale_price = row["sale_price"]
            product.stock = row["stock"]
            product.min_stock = row["min_stock"]
            product.is_active = True
            if before != product.stock:
                record_inventory_movement(
                    product,
                    membership,
                    "IMPORT",
                    before,
                    product.stock,
                    reason="Importación de catálogo verificada",
                )
            updated += 1
        else:
            product = Product(
                organization_id=organization_id, user_id=owner_id,
                sku=row["sku"], barcode=row["barcode"], name=row["name"],
                category=row["category"], supplier=row["supplier"],
                cost_price=row["cost_price"], sale_price=row["sale_price"],
                stock=row["stock"], min_stock=row["min_stock"],
            )
            db.session.add(product)
            pending_opening_movements.append(product)
            created += 1
    # One flush assigns all product IDs. Flushing once per row made a large
    # catalog unnecessarily slow without adding transactional protection.
    if pending_opening_movements:
        db.session.flush()
        for product in pending_opening_movements:
            record_inventory_movement(
                product,
                membership,
                "OPENING_BALANCE",
                0,
                int(product.stock),
                reason="Alta mediante importación verificada",
            )
    return {"created": created, "updated": updated, "errors": len(imported.errors), "omitted": imported.summary["blank"]}
