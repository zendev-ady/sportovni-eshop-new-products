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
        "meta_data":      _variation_meta(v),
    }

    if wc_id is not None:
        payload["id"] = wc_id

    return payload


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _fifu_meta(images: list) -> list:
    """
    Build FIFU (Featured Image From URL) meta entries for a product.

    FIFU serves images directly from external URLs without downloading to WP media.
    Slot layout:
        fifu_image_url   = main featured image (images[0])
        fifu_image_url_0 = gallery slot 1     (images[1])
        fifu_image_url_1 = gallery slot 2     (images[2])
        ...up to fifu_image_url_14             (images[15])

    Args:
        images: Ordered list of absolute image URLs (GCS or original).

    Returns:
        List of meta dicts ready for WooCommerce meta_data field.
    """
    meta = [{"key": "fifu_image_url", "value": images[0] if images else ""}]
    for i in range(15):
        url = images[i + 1] if i + 1 < len(images) else ""
        meta.append({"key": f"fifu_image_url_{i}", "value": url})
    return meta


def _parent_meta(group: ProductGroup) -> list:
    """Build meta_data list for parent product, including FIFU image URLs."""
    meta = [
        {"key": "_b2b_model",    "value": group.model},
        {"key": "_b2b_producer", "value": group.producer},
    ]
    if group.created_at:
        meta.append({"key": "_b2b_created_at", "value": group.created_at})
    meta.extend(_fifu_meta(group.images))
    return meta


def _variation_meta(v: Variation) -> list:
    """
    Build meta_data list for a variation.

    Includes:
        _ean                      — EAN barcode
        fifu_image_url            — FIFU featured image; Blocksy "Use Variation Image"
                                    reads this to auto-populate the colour swatch thumbnail
                                    without manual WP Admin term configuration.
        fifu_image_url_0..N       — FIFU gallery slots (same pattern as parent).
        blocksy_post_meta_options — Blocksy per-variation gallery; switches the displayed
                                    gallery when this colour variant is selected.

    Args:
        v: Variation dataclass from product_grouper.

    Returns:
        List of meta dicts ready for WooCommerce meta_data field.
    """
    meta = [{"key": "_ean", "value": v.ean}]
    if v.images:
        meta.append({"key": "fifu_image_url", "value": v.images[0]})
        for i in range(min(len(v.images) - 1, 15)):
            meta.append({"key": f"fifu_image_url_{i}", "value": v.images[i + 1]})
        meta.append({
            "key": "blocksy_post_meta_options",
            "value": {
                "gallery_source": "custom",
                "images": [{"url": url} for url in v.images],
            },
        })
    return meta
