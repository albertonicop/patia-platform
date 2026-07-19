"""Barcode helpers kept independent from inventory and external catalogs."""

import re
from dataclasses import dataclass

from .models import Product


MAX_BARCODE_LENGTH = 64
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_SKU_CHARACTERS = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True)
class ProductMetadata:
    """Public catalog metadata that a future provider may return."""

    name: str
    category: str | None = None


def normalize_barcode(value):
    """Preserve barcode text while rejecting scanner control characters."""
    barcode = str(value or "").strip()
    if not barcode or len(barcode) > MAX_BARCODE_LENGTH:
        raise ValueError("invalid barcode length")
    if _CONTROL_CHARACTERS.search(barcode):
        raise ValueError("invalid barcode characters")
    return barcode


def find_company_product_by_barcode(user_id, barcode, *, lock=False):
    """Find active or archived inventory belonging only to one company."""
    query = Product.query.filter_by(user_id=user_id, barcode=barcode)
    if lock:
        query = query.with_for_update()
    return query.first()


def automatic_sku(user_id, barcode):
    """Generate an editable SKU without exposing identifiers from other users."""
    normalized = _SKU_CHARACTERS.sub("-", barcode.upper()).strip("-")
    base = f"BC-{normalized or 'PRODUCTO'}"[:64]
    candidate = base
    suffix = 2
    while Product.query.filter_by(user_id=user_id, sku=candidate).first():
        marker = f"-{suffix}"
        candidate = f"{base[:64 - len(marker)]}{marker}"
        suffix += 1
    return candidate


def lookup_barcode(barcode) -> ProductMetadata | None:
    """Future external catalog adapter; intentionally performs no lookup yet."""
    return None
