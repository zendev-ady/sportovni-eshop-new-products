"""
_payloads.py — Pure WooCommerce REST API payload builders. No I/O, no side effects.

Each function takes structured data from product_grouper and price_calculator
and returns a plain dict ready to be serialised to JSON and sent to the API.

Attribute strategy (Phase 1):
    Custom product-level attributes — no global pa_ registration needed.
    The 'variation' flag tells WooCommerce which attributes drive variations.
    Phase 4: migrate to global attributes (pa_barva, pa_velikost) for filters.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import WOO_ATTR_COLOUR, WOO_ATTR_SIZE
from price_calculator import calculate_price
import attribute_mapper

from typing import TYPE_CHECKING

# Re-export for callers that import from this module
from product_grouper import ProductGroup, Variation
if TYPE_CHECKING:
    from translator import TranslatedGroup


def build_parent_payload(
    group: ProductGroup,
    category_ids: list,
    translated: TranslatedGroup,
    wc_id: int | None = None,
) -> dict:
    """
    Build the WooCommerce product payload for a parent (or simple) product.

    For variable products: no price, no stock — both live on variations.
    For simple products: price and stock are set here directly.
    All products receive Czech name, descriptions, and full attribute set.

    Args:
        group:        ProductGroup from product_grouper.
        category_ids: List of WooCommerce category IDs (empty list = no category).
        translated:   TranslatedGroup with Czech name, descriptions, and attrs_cs.
        wc_id:        Existing WooCommerce ID if updating; None if creating.

    Returns:
        dict — WooCommerce product payload.
    """
    is_simple = group.kind == "simple"

    payload: dict = {
        "sku":               group.parent_sku,
        "name":              translated.name_cs,
        "description":       translated.long_description_cs,
        "short_description": translated.short_description_cs,
        "type":              "simple" if is_simple else "variable",
        "status":            "publish",
        "categories":        [{"id": cid} for cid in category_ids],
        "images":            _image_list(group.images),
        "meta_data":         _parent_meta(group),
        "attributes":        attribute_mapper.build_parent_attributes(group, translated.attrs_cs),
    }

    if is_simple:
        v = group.variations[0]
        payload["regular_price"] = calculate_price(
            v.wholesale_netto, float(group.weight)
        )
        payload["stock_quantity"] = v.quantity
        payload["manage_stock"]   = True
        payload["stock_status"]   = "instock" if v.quantity > 0 else "outofstock"

    if wc_id is not None:
        payload["id"] = wc_id

    return payload


def build_variation_payload(
    v: Variation,
    group: ProductGroup,
    category_slug: str = "default",
    wc_id: int | None = None,
) -> dict:
    """
    Build the WooCommerce variation payload for a single Variation.

    Args:
        v:             Variation from product_grouper.
        group:         Parent ProductGroup (needed for kind + weight).
        category_slug: WooCommerce category slug for margin lookup.
        wc_id:         Existing WooCommerce variation ID if updating; None if creating.

    Returns:
        dict — WooCommerce variation payload.
    """
    payload: dict = {
        "sku":           v.sku,
        "regular_price": calculate_price(
            v.wholesale_netto, float(group.weight), category_slug
        ),
        "stock_quantity": v.quantity,
        "manage_stock":   True,
        "stock_status":   "instock" if v.quantity > 0 else "outofstock",
        "attributes":     attribute_mapper.build_variation_attributes(v.colour, v.size_label, group.kind),
        "images":         [{"src": v.images[0]}] if v.images else [],
        "meta_data":      [{"key": "_ean", "value": v.ean}],
    }

    if wc_id is not None:
        payload["id"] = wc_id

    return payload


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _image_list(urls: list) -> list:
    """Convert a list of image URL strings to WooCommerce image dicts."""
    return [{"src": url} for url in urls]


def _parent_meta(group: ProductGroup) -> list:
    """Build meta_data list for parent product."""
    meta = [
        {"key": "_b2b_model",    "value": group.model},
        {"key": "_b2b_producer", "value": group.producer},
    ]
    if group.created_at:
        meta.append({"key": "_b2b_created_at", "value": group.created_at})
    return meta
