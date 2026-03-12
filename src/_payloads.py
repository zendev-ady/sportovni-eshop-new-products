"""
_payloads.py — Pure WooCommerce REST API payload builders. No I/O, no side effects.

Each function takes structured data from product_grouper and price_calculator
and returns a plain dict ready to be serialised to JSON and sent to the API.

Attribute strategy (Phase 1):
    Custom product-level attributes — no global pa_ registration needed.
    The 'variation' flag tells WooCommerce which attributes drive variations.
    Phase 4: migrate to global attributes (pa_barva, pa_velikost) for filters.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import WOO_ATTR_COLOUR, WOO_ATTR_SIZE
from price_calculator import calculate_price

# Re-export for callers that import from this module
from product_grouper import ProductGroup, Variation


def build_parent_payload(
    group: ProductGroup,
    category_ids: list,
    wc_id: int | None = None,
) -> dict:
    """
    Build the WooCommerce product payload for a parent (or simple) product.

    For variable products: no price, no stock — both live on variations.
    For simple products: price and stock are set here directly.

    Args:
        group:        ProductGroup from product_grouper.
        category_ids: List of WooCommerce category IDs (empty list = no category).
        wc_id:        Existing WooCommerce ID if updating; None if creating.

    Returns:
        dict — WooCommerce product payload.
    """
    is_simple = group.kind == "simple"

    payload: dict = {
        "sku":         group.parent_sku,
        "name":        group.name,
        "description": group.description,
        "type":        "simple" if is_simple else "variable",
        "status":      "publish",
        "categories":  [{"id": cid} for cid in category_ids],
        "images":      _image_list(group.images),
        "meta_data":   _parent_meta(group),
    }

    if is_simple:
        v = group.variations[0]
        payload["regular_price"] = calculate_price(
            v.wholesale_netto, float(group.weight)
        )
        payload["stock_quantity"] = v.quantity
        payload["manage_stock"]   = True
        payload["stock_status"]   = "instock" if v.quantity > 0 else "outofstock"
    else:
        payload["attributes"] = _parent_attributes(group)

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
        "attributes":     _variation_attributes(v, group),
        "images":         [{"src": v.images[0]}] if v.images else [],
        "meta_data":      [{"key": "_ean", "value": v.ean}],
    }

    if wc_id is not None:
        payload["id"] = wc_id

    return payload


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parent_attributes(group: ProductGroup) -> list:
    """
    Build the attributes list for a variable parent product.

    Declares all possible values for each variation axis so WooCommerce
    can construct the variation matrix. The 'variation' flag is True for
    axes that drive variations; 'visible' is True for all so they show
    in the product page.

    Args:
        group: ProductGroup with kind and variation list.

    Returns:
        List of attribute dicts.
    """
    attrs = []

    if group.kind in ("colour_only", "colour_size"):
        colours = list(dict.fromkeys(
            v.colour for v in group.variations if v.colour
        ))
        attrs.append({
            "name":      WOO_ATTR_COLOUR,
            "options":   colours,
            "variation": True,
            "visible":   True,
        })

    if group.kind in ("size_only", "colour_size"):
        sizes = list(dict.fromkeys(
            v.size_label for v in group.variations if v.size_label not in ("", "N/A")
        ))
        attrs.append({
            "name":      WOO_ATTR_SIZE,
            "options":   sizes,
            "variation": True,
            "visible":   True,
        })

    return attrs


def _variation_attributes(v: Variation, group: ProductGroup) -> list:
    """
    Build the attributes list for a single variation.

    Each entry pins the variation to one specific attribute value.

    Args:
        v:     The variation.
        group: Parent group (provides kind).

    Returns:
        List of attribute dicts with single-value options.
    """
    attrs = []

    if group.kind in ("colour_only", "colour_size") and v.colour:
        attrs.append({"name": WOO_ATTR_COLOUR, "option": v.colour})

    if group.kind in ("size_only", "colour_size") and v.size_label not in ("", "N/A"):
        attrs.append({"name": WOO_ATTR_SIZE, "option": v.size_label})

    return attrs


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
