"""
attribute_mapper.py — Build WooCommerce attribute lists from translated attrs_cs.

Used by _payloads.py to populate 'attributes' on parent products and individual
variations. Replaces the private helpers _parent_attributes() and
_variation_attributes() that were in _payloads.py (Phase 1).

Phase 1 helpers covered only variation axes (Barva, Velikost).
This module adds non-variation attributes (material, sport, brand, etc.)
and context-aware Product Type mapping (ported from legacy map_params.py).

Input (build_parent_attributes):
    group      — ProductGroup (for kind, variations, raw attrs for context)
    attrs_cs   — Dict[str, List[str]] from TranslatedGroup.attrs_cs

Input (build_variation_attributes):
    colour_en  — English colour string from Variation.colour
    size_label — Size label string (e.g. 'XL', '42')
    kind       — ProductGroup kind string

Output:
    List of WooCommerce attribute dicts ready for the REST API payload.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import attr_maps
from config.config import WOO_ATTR_COLOUR, WOO_ATTR_SIZE

logger = logging.getLogger(__name__)

# B2B attr names used as Product Type context signals — never added to WC as-is
_SPORT_KEY    = "Sport"
_CATEGORY_KEY = "Category"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_parent_attributes(group, attrs_cs: dict) -> list:
    """
    Build the full 'attributes' list for a WooCommerce parent (or simple) product.

    Variation axes (Barva, Velikost) are included with variation=True so
    WooCommerce can render the variation selector.
    All other attrs_cs entries become non-variation, visible product attributes.
    'Product Type' is resolved via context-aware mapping (see _product_type_name()).

    Args:
        group:    ProductGroup (provides kind, variations for sizes, raw attrs
                  for Product Type context).
        attrs_cs: Czech attribute dict from TranslatedGroup.attrs_cs.
                  Keys are WC param names (e.g. 'barva', 'material', 'sport').

    Returns:
        List of WooCommerce attribute dicts.
    """
    result = []

    # ---- 1. Variation axes -----------------------------------------------

    if group.kind in ("colour_only", "colour_size"):
        # Prefer Czech colour values from attrs_cs; fall back to translating
        # variation.colour directly when attrs_cs has no 'barva' entry.
        if attrs_cs.get("barva"):
            colours = list(dict.fromkeys(c for c in attrs_cs["barva"] if c))
        else:
            colours = list(dict.fromkeys(
                attr_maps.COLOUR.get(v.colour, v.colour)
                for v in group.variations if v.colour
            ))
        if colours:
            result.append({
                "name":      WOO_ATTR_COLOUR,
                "options":   colours,
                "variation": True,
                "visible":   True,
            })

    if group.kind in ("size_only", "colour_size"):
        sizes = list(dict.fromkeys(
            v.size_label for v in group.variations
            if v.size_label not in ("", "N/A")
        ))
        if sizes:
            result.append({
                "name":      WOO_ATTR_SIZE,
                "options":   sizes,
                "variation": True,
                "visible":   True,
            })

    # ---- 2. Context-aware Product Type -----------------------------------

    raw_sport    = (group.attrs.get(_SPORT_KEY)    or [""])[0]
    raw_category = (group.attrs.get(_CATEGORY_KEY) or [""])[0]
    product_types = group.attrs.get("Product Type", [])
    if product_types:
        pt_name = _product_type_name(raw_sport, raw_category)
        if pt_name:
            result.append({
                "name":      pt_name,
                "options":   list(dict.fromkeys(v for v in product_types if v)),
                "variation": False,
                "visible":   True,
            })
        else:
            logger.debug(
                "Product Type skipped — no context match (sport=%r, category=%r)",
                raw_sport, raw_category,
            )

    # ---- 3. All other attrs_cs entries (non-variation) -------------------

    skip_keys = {"barva", "velikost"}
    for cs_key, values in attrs_cs.items():
        if cs_key in skip_keys or not values:
            continue
        cleaned = list(dict.fromkeys(v for v in values if v))
        if not cleaned:
            continue
        result.append({
            "name":      cs_key,
            "options":   cleaned,
            "variation": False,
            "visible":   True,
        })

    return result


def build_variation_attributes(colour_en: str, size_label: str, kind: str) -> list:
    """
    Build the attribute pinning list for a single WooCommerce variation.

    Colour is translated English→Czech via static dict (attr_maps.COLOUR).
    Size labels are never translated — they are supplier-assigned identifiers.

    Args:
        colour_en:  English colour string from Variation.colour (e.g. 'Black').
        size_label: Size label (e.g. 'XL', '42', 'N/A').
        kind:       ProductGroup kind string.

    Returns:
        List of attribute option dicts (one per variation axis).
    """
    attrs = []

    if kind in ("colour_only", "colour_size") and colour_en:
        colour_cs = attr_maps.COLOUR.get(colour_en, colour_en)
        if colour_en not in attr_maps.COLOUR:
            logger.warning("Unknown colour %r — passing through as-is", colour_en)
        attrs.append({"name": WOO_ATTR_COLOUR, "option": colour_cs})

    if kind in ("size_only", "colour_size") and size_label not in ("", "N/A"):
        attrs.append({"name": WOO_ATTR_SIZE, "option": size_label})

    return attrs


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _product_type_name(sport: str, category: str) -> str | None:
    """
    Map 'Product Type' to a WooCommerce param name based on sport+category context.

    Ported from legacy map_params.py:
        Football + Shoes  → 'typ_kopacek'
        Tennis/Badminton/Squash → 'typ_produktu'
        All others        → None (attribute is skipped)

    Args:
        sport:    English sport value from raw B2B attrs.
        category: English category value from raw B2B attrs.

    Returns:
        WooCommerce param name string, or None if no match.
    """
    if sport == "Football" and category == "Shoes":
        return "typ_kopacek"
    if sport in ("Tennis", "Badminton", "Squash"):
        return "typ_produktu"
    return None
