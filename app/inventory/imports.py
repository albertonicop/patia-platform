"""Professional, tenant-safe catalog import parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from hashlib import sha256
from io import BytesIO
import csv
import re
from io import StringIO
import unicodedata

import pandas as pd

from app import db
from app.inventory.services import record_inventory_movement
from app.models import Product
from app.money import money_decimal
from app.currencies import parse_localized_decimal
from app.units import normalize_unit, quantity_decimal


MAX_IMPORT_ROWS = 5_000
FIELDS = (
    "sku", "barcode", "name", "category", "supplier",
    "cost_price", "sale_price", "stock", "min_stock", "unit_code",
)
REQUIRED_FIELDS = frozenset({"name", "sale_price", "stock"})
ALIASES = {
    "sku": {
        "sku", "clave", "clave producto", "codigo interno", "id producto",
        "product sku", "item number", "no articulo", "numero articulo",
        "numero de articulo",
    },
    "barcode": {
        "codigo de barras", "barcode", "ean", "upc", "gtin",
        "codigo universal", "gtin ean", "ean upc",
    },
    "name": {
        "nombre", "nombre del producto", "producto", "articulo", "item",
        "product name", "description", "descripcion",
        "descripcion comercial",
    },
    "category": {
        "categoria", "category", "departamento", "familia", "linea",
        "familia de articulos",
    },
    "supplier": {"proveedor", "supplier", "vendor", "marca proveedor"},
    "cost_price": {"costo", "precio de compra", "costo unitario", "cost", "cost price", "purchase price"},
    "sale_price": {"precio", "precio de venta", "precio publico", "p venta", "sale price", "retail price", "unit price"},
    "stock": {"stock", "stock inicial", "existencias", "existencia", "cantidad", "inventario", "initial stock", "on hand"},
    "min_stock": {
        "stock minimo", "minimo", "existencia minima", "minimum stock",
        "reorder point", "reorder level", "punto de reorden",
    },
    "unit_code": {
        "unidad", "unidad base", "unidad de inventario", "unit", "base unit",
        "uom", "unidad medida",
    },
}


def _normalized(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _header_score(values) -> float:
    known = {alias for values in ALIASES.values() for alias in values}
    normalized = [_normalized(value) for value in values]
    return sum(
        1
        for header in normalized
        if header in known
        or any(SequenceMatcher(None, header, alias).ratio() >= 0.82 for alias in known)
    )


def _frame_with_detected_header(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return raw
    header_row = max(
        range(min(15, len(raw.index))),
        key=lambda index: _header_score(raw.iloc[index].tolist()),
    )
    frame = raw.iloc[header_row + 1:].copy()
    frame.columns = [
        str(value).strip() or f"column_{position + 1}"
        for position, value in enumerate(raw.iloc[header_row].tolist())
    ]
    frame = frame.reset_index(drop=True)
    frame.attrs["source_header_row"] = header_row + 1
    return frame


def _read_frame(filename: str, content: bytes) -> pd.DataFrame:
    lower = (filename or "").casefold()
    if lower.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = content.decode("latin-1")
        sample = text[:8192]
        lines = [line for line in sample.splitlines()[:20] if line.strip()]
        delimiter = max(
            (",", ";", "\t", "|"),
            key=lambda candidate: sum(
                line.count(candidate) for line in lines
            ),
        )
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=delimiter)
        except csv.Error:
            reader = csv.reader(StringIO(text), delimiter=delimiter)
        else:
            reader = csv.reader(StringIO(text), dialect)
        records = list(reader)
        width = max((len(row) for row in records), default=0)
        raw = pd.DataFrame(
            [row + [""] * (width - len(row)) for row in records],
            dtype=str,
        )
        return _frame_with_detected_header(raw)
    if not lower.endswith(".xlsx"):
        raise ValueError("unsupported_file")
    raw = pd.read_excel(BytesIO(content), dtype=str, header=None, keep_default_na=False)
    return _frame_with_detected_header(raw)


def auto_mapping_details(headers) -> tuple[dict[str, str], dict[str, float]]:
    normalized_headers = {
        _normalized(header): str(header)
        for header in headers
        if str(header).strip()
    }
    mapping, confidence = {}, {}
    used_headers = set()

    # Reserve exact matches first. A weak fuzzy match such as "Artículo" ->
    # "número de artículo" must never steal the header from the exact
    # product-name alias that follows it.
    for field, aliases in ALIASES.items():
        exact_headers = [
            original_header
            for normalized_header, original_header in normalized_headers.items()
            if normalized_header in aliases and original_header not in used_headers
        ]
        if exact_headers:
            mapping[field] = exact_headers[0]
            confidence[field] = 1.0
            used_headers.add(exact_headers[0])

    for field, aliases in ALIASES.items():
        if field in mapping:
            continue
        candidates = []
        for normalized_header, original_header in normalized_headers.items():
            if original_header in used_headers:
                continue
            score = max(
                SequenceMatcher(None, normalized_header, alias).ratio()
                for alias in aliases
            )
            candidates.append((score, original_header))
        if not candidates:
            continue
        score, original_header = max(candidates)
        if score >= 0.72 and original_header not in used_headers:
            mapping[field] = original_header
            confidence[field] = round(score, 2)
            used_headers.add(original_header)
    return mapping, confidence


def auto_mapping(headers) -> dict[str, str]:
    return auto_mapping_details(headers)[0]


def _text(value, limit=255):
    return str(value or "").strip()[:limit]


def _barcode(value):
    text = _text(value, 64)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _decimal(value, *, default="0.00", currency_code="MXN", locale_code="es_MX"):
    text = _text(value)
    if not text:
        return money_decimal(default)
    try:
        return parse_localized_decimal(text, currency_code, locale_code)
    except ValueError as exc:
        if str(exc) == "ambiguous_number":
            raise
        raise ValueError("invalid_number")
    except InvalidOperation:
        raise ValueError("invalid_number")


def _quantity(value, *, default=0):
    text = _text(value)
    if not text:
        return default
    try:
        number = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError("invalid_quantity")
    if number < 0:
        raise ValueError("invalid_quantity")
    return quantity_decimal(number)


def _row_value(row, mapping, field):
    header = mapping.get(field)
    return row.get(header, "") if header else ""


@dataclass
class CatalogImport:
    filename: str
    digest: str
    headers: list[str]
    mapping: dict[str, str]
    mapping_confidence: dict[str, float]
    rows: list[dict]
    errors: list[dict]
    summary: dict


def inspect_catalog(
    filename, content, mapping=None, existing_products=(),
    currency_code="MXN", locale_code="es_MX",
):
    if not content:
        raise ValueError("empty_file")
    frame = _read_frame(filename, content)
    if len(frame.index) > MAX_IMPORT_ROWS:
        raise ValueError("too_many_rows")
    headers = [str(column).strip() for column in frame.columns if str(column).strip()]
    suggested, confidence = auto_mapping_details(headers)
    chosen = {
        key: value
        for key, value in (mapping or suggested).items()
        if key in FIELDS and value in headers
    }
    if mapping:
        confidence = {
            field: (1.0 if suggested.get(field) == header else 0.0)
            for field, header in chosen.items()
        }
    digest = sha256(content).hexdigest()
    existing_sku = {item.sku: item for item in existing_products if item.sku}
    existing_barcode = {item.barcode: item for item in existing_products if item.barcode}
    seen_sku, seen_barcode = set(), set()
    rows, errors = [], []
    summary = {"total": 0, "valid": 0, "invalid": 0, "new": 0, "updated": 0, "duplicates": 0, "blank": 0}
    if not REQUIRED_FIELDS.issubset(chosen):
        return CatalogImport(
            filename, digest, headers, chosen, confidence, [], [], summary
        )

    first_data_row = int(frame.attrs.get("source_header_row", 1)) + 1
    for offset, (_, source) in enumerate(frame.iterrows(), first_data_row):
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
                "cost_price": _decimal(_row_value(source, chosen, "cost_price"), currency_code=currency_code, locale_code=locale_code),
                "sale_price": _decimal(_row_value(source, chosen, "sale_price"), currency_code=currency_code, locale_code=locale_code),
                "stock": _quantity(_row_value(source, chosen, "stock")),
                "min_stock": _quantity(_row_value(source, chosen, "min_stock"), default=5),
                "unit_code": normalize_unit(_row_value(source, chosen, "unit_code")),
            }
            if not item["name"]:
                raise ValueError("missing_identity")
            if not item["sku"]:
                item["sku"] = f"IMP-{digest[:8].upper()}-{offset:05d}"
                item["sku_generated"] = True
            else:
                item["sku_generated"] = False
            if item["sku"] in seen_sku or (
                item["barcode"] and item["barcode"] in seen_barcode
            ):
                summary["duplicates"] += 1
                raise ValueError("duplicate_in_file")
            seen_sku.add(item["sku"])
            if item["barcode"]:
                seen_barcode.add(item["barcode"])
            by_sku = existing_sku.get(item["sku"])
            by_barcode = existing_barcode.get(item["barcode"]) if item["barcode"] else None
            if by_sku and by_barcode and by_sku.id != by_barcode.id:
                raise ValueError("conflicting_identity")
            matched = by_sku or by_barcode
            if matched and item["sku_generated"]:
                item["sku"] = matched.sku
            item["action"] = "update" if matched else "create"
            item["product_id"] = (by_sku or by_barcode).id if (by_sku or by_barcode) else None
            summary["updated" if item["action"] == "update" else "new"] += 1
            summary["valid"] += 1
            rows.append(item)
        except ValueError as exc:
            summary["invalid"] += 1
            errors.append({"row": offset, "code": str(exc), "sku": _text(_row_value(source, chosen, "sku"), 64), "name": _text(_row_value(source, chosen, "name"), 160)})
    return CatalogImport(
        filename, digest, headers, chosen, confidence, rows, errors, summary
    )


def apply_catalog(imported, organization_id, owner_id, membership):
    existing = Product.query.filter_by(
        organization_id=organization_id, item_type="inventory"
    ).with_for_update().all()
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
            product.unit_code = row["unit_code"]
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
                unit_code=row["unit_code"],
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
                product.stock,
                reason="Alta mediante importación verificada",
            )
    return {"created": created, "updated": updated, "errors": len(imported.errors), "omitted": imported.summary["blank"]}
