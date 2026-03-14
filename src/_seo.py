"""
_seo.py — Rule-based SEO helpers for WooCommerce product payloads.

No AI calls, no I/O, no side effects. All functions are pure transformations.

Used by _payloads.py to generate:
  - Rank Math focus keyword
  - Rank Math SEO title
  - WooCommerce product tags
  - Czech product type (typ_cs) derived from B2B "Product Type" attribute
"""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from typing import TYPE_CHECKING, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Re-use the same Product Type → Czech mapping that category_mapper uses.
from category_mapper import _PRODUCT_TYPE_MAP
from product_grouper import ProductGroup

if TYPE_CHECKING:
    from translator import TranslatedGroup

logger = logging.getLogger(__name__)

# Store name suffix for SEO title — appended only when total length allows it.
_STORE_SUFFIX = " – SportEshop"
_SEO_TITLE_MAX = 65


# Unique Czech type words from _PRODUCT_TYPE_MAP, longest first so "kopačky"
# matches before "boty" when both could appear in a name.
_CS_TYP_WORDS: list[str] = sorted(
    set(_PRODUCT_TYPE_MAP.values()), key=lambda w: -len(w)
)


def get_typ_cs(group: ProductGroup, name_cs: str = "") -> str:
    """
    Derive Czech product type from B2B 'Product Type' attribute.

    Primary: looks up _PRODUCT_TYPE_MAP via group.attrs["Product Type"].
    Fallback: scans name_cs for any known Czech type word (longest match wins).
    This ensures the type is never missing when the AI already translated it
    into the product name (e.g. name_cs = "Dámská mikina adidas …").

    Args:
        group:   ProductGroup with raw B2B attrs.
        name_cs: Czech product name from TranslatedGroup (used as fallback).

    Returns:
        Czech product type string (e.g. "kopačky", "mikina"), or "" if not found.
    """
    raw_values = group.attrs.get("Product Type", [])
    for raw_pt in raw_values:
        cs_typ = _PRODUCT_TYPE_MAP.get(raw_pt)
        if cs_typ:
            return cs_typ
    if raw_values:
        logger.debug("[seo] No Czech mapping for Product Type %r — trying name_cs", raw_values)

    # Fallback: scan the Czech name for a known type word
    if name_cs:
        name_lower = name_cs.lower()
        for word in _CS_TYP_WORDS:
            if word in name_lower:
                logger.debug("[seo] Extracted typ %r from name_cs", word)
                return word

    return ""


def _effective_gender_lower(attrs_cs: dict) -> str:
    """
    Return lowercase Czech gender for SEO, or "" for Unisex / missing.

    Args:
        attrs_cs: Translated attribute dict from TranslatedGroup.

    Returns:
        Lowercase gender string (e.g. "pánské", "dámské", "dětské") or "".
    """
    gender_values = attrs_cs.get("pohlavi", [])
    if not gender_values:
        return ""
    if len(gender_values) > 1 or "Unisex" in gender_values:
        return ""
    return gender_values[0].lower()


def build_focus_keyword(group: ProductGroup, translated: TranslatedGroup) -> str:
    """
    Build Rank Math focus keyword by slicing name_cs up to and including the brand.

    The Czech name is generated in format "{pohlaví} {typ} {značka} {model} {barva}",
    so the first N words through the brand give the ideal focus keyword with correct
    Czech grammar (e.g. "dámská mikina adidas", not a puzzle-assembled "dámské mikina adidas").

    Falls back to assembling from parts if the brand is not found in the name.

    Args:
        group:      ProductGroup (for producer).
        translated: TranslatedGroup (for name_cs).

    Returns:
        Lowercase focus keyword string.
    """
    name_cs = translated.name_cs.strip()
    brand = group.producer.strip()

    if name_cs and brand:
        name_lower = name_cs.lower()
        brand_lower = brand.lower()
        idx = name_lower.find(brand_lower)
        if idx != -1:
            keyword = name_lower[: idx + len(brand_lower)].strip()
            if keyword:
                return keyword

    # Fallback: assemble from known parts
    parts: List[str] = []
    gender = _effective_gender_lower(translated.attrs_cs)
    if gender:
        parts.append(gender)
    typ = get_typ_cs(group, name_cs)
    if typ:
        parts.append(typ.lower())
    if brand:
        parts.append(brand.lower())
    keyword = " ".join(parts)
    if not keyword:
        logger.warning("[seo] Empty focus keyword for model=%r", group.model)
    return keyword


def trim_seo_title(name_cs: str) -> str:
    """
    Build Rank Math SEO title from Czech product name.

    Appends " – SportEshop" only if total length stays within 65 chars.
    Otherwise returns name_cs trimmed to 65 chars.

    Args:
        name_cs: Czech product name from TranslatedGroup.

    Returns:
        SEO title string, max 65 chars.
    """
    if not name_cs:
        return ""

    with_suffix = name_cs + _STORE_SUFFIX
    if len(with_suffix) <= _SEO_TITLE_MAX:
        return with_suffix

    # Name alone fits or needs trimming
    if len(name_cs) <= _SEO_TITLE_MAX:
        return name_cs

    return name_cs[:_SEO_TITLE_MAX]


def _model_short(model: str) -> str:
    """
    Extract short model identifier (first word(s), max 30 chars).

    Model codes like "X Speedportal" or "Copa Mundial" are good for long-tail SEO.

    Args:
        model: Raw model string from ProductGroup.

    Returns:
        Trimmed model string, max 30 chars, or "" if model is empty.
    """
    if not model:
        return ""
    # Take up to 30 chars, break at last space if truncated
    short = model[:30]
    if len(model) > 30 and " " in short:
        short = short[:short.rfind(" ")]
    return short.strip()


def build_tags(group: ProductGroup, translated: TranslatedGroup) -> list[dict]:
    """
    Build WooCommerce tags list from existing product data (no AI).

    Tags: [brand, sport_cs, typ_cs, gender_cs (if not Unisex), model_short]
    Duplicates and empty strings are excluded.

    Args:
        group:      ProductGroup (for producer, model, attrs).
        translated: TranslatedGroup (for attrs_cs).

    Returns:
        List of {"name": "..."} dicts for WooCommerce tags field.
    """
    tag_values: List[str] = []

    # Brand
    brand = group.producer.strip()
    if brand:
        tag_values.append(brand)

    # Sport (Czech)
    sport_values = translated.attrs_cs.get("sport", [])
    if sport_values:
        tag_values.append(sport_values[0])

    # Product type (Czech)
    typ = get_typ_cs(group, translated.name_cs)
    if typ:
        tag_values.append(typ)

    # Gender (Czech), skip Unisex
    gender = _effective_gender_lower(translated.attrs_cs)
    if gender:
        tag_values.append(gender)

    # Model short
    short = _model_short(group.model)
    if short:
        tag_values.append(short)

    # Deduplicate preserving order, case-insensitive
    seen: set[str] = set()
    unique: List[str] = []
    for v in tag_values:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)

    return [{"name": tag} for tag in unique]


def _to_ascii(text: str) -> str:
    """Convert Czech diacritics to ASCII equivalents for URL slugs."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def build_slug(group: ProductGroup, translated: TranslatedGroup) -> str:
    """
    Build a short, keyword-rich URL slug for the WooCommerce product.

    Format: {focus_keyword_ascii}-{model_short_ascii}
    Example: "damska-mikina-adidas-essentials-linear"

    This fixes two Rank Math errors at once:
      - Focus Keyword not found in URL
      - URL is too long (WordPress auto-generates from full name = 100+ chars)

    Args:
        group:      ProductGroup (for producer, model).
        translated: TranslatedGroup (for name_cs).

    Returns:
        URL-safe slug string. Falls back to parent_sku if inputs are empty.
    """
    # Focus keyword part (same slice logic as build_focus_keyword)
    name_cs = translated.name_cs.strip()
    brand = group.producer.strip()
    focus = ""
    if name_cs and brand:
        idx = name_cs.lower().find(brand.lower())
        if idx != -1:
            focus = name_cs[: idx + len(brand)].strip()
    if not focus:
        focus = name_cs.split()[0] if name_cs else group.parent_sku

    # Model short part — strip the brand prefix that's already in focus keyword
    model = group.model.strip()
    if brand and model.lower().startswith(brand.lower()):
        model = model[len(brand):].strip()
    short = _model_short(model)

    parts = [focus]
    if short:
        parts.append(short)

    raw = " ".join(parts)
    ascii_slug = _to_ascii(raw).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_slug).strip("-")
    return slug or group.parent_sku
