"""
category_mapper.py — Map product groups to WooCommerce category IDs and margin slugs.

Phase 3 of the b2b_to_woocommerce pipeline.

Public API:
    resolve(group, translated) -> tuple[list[int], str]
        Returns (category_ids, margin_slug) for use in woo_client.upsert_group().

Inputs:
    group:      ProductGroup from product_grouper.py
    translated: TranslatedGroup from translator.py

Outputs:
    category_ids: list of WooCommerce integer category IDs (max 2, complementary strategy)
    margin_slug:  key for config.MARGINS lookup (e.g. "fotbal", "default")

Category rules are defined in CategoryMapper._define_category_structure().
WooCommerce IDs are configured in config.WOO_CATEGORY_IDS — fill in after creating
categories in WC Admin > Products > Categories.

Ported from: fastcentrik-to-woocommerce/src/fastcentrik_woocommerce/mappers/category_mapper.py
"""

import hashlib
import re
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import (
    WOO_CATEGORY_IDS,
    WOO_FALLBACK_CATEGORY_ID,
    TRANSLATION_DB,
    TRANSLATION_MODEL,
    SKIP_TRANSLATION,
    SKIP_ON_RATE_LIMIT,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bad sport values — B2B supplier placeholder strings that carry no routing
# information.  Stripped from params before routing so that B2B category
# fallback (step 5 in _build_mapper_params) can fill in the correct value.
# ---------------------------------------------------------------------------
_BAD_SPORT_VALUES: frozenset[str] = frozenset({
    "to be categorized",
    "tbd",
})

# ---------------------------------------------------------------------------
# Sport routing — translates Czech sport values from attrs_cs into values
# that match CategoryMapper conditions.  Does NOT change what gets stored in
# WooCommerce as a product attribute (attrs_cs is untouched).
# ---------------------------------------------------------------------------
_SPORT_ROUTING_MAP: dict[str, str] = {
    "bojová umění":             "bojové sporty",   # Martial arts → Bojové sporty category
    "zimní sporty":             "lední hokej",     # Winter sports → Lední hokej (no ski cat yet)
    "trénink":                  "fitness",         # Training → Fitness category
    # Sports with no dedicated WC category — will fall through to parent or unmapped:
    "turistika/outdoor":        "turistika",
    "volnočasové aktivity":     "lifestyle",
    "cyklistika":               "cyklistika",
    "florbal":                  "florbal",
    "házená":                   "házená",
    "volejbal":                 "volejbal",
    "badminton":                "badminton",
    "squash":                   "squash",
    "stolní tenis":             "stolní tenis",
    "plavání":                  "plavání",
    "bruslení":                 "bruslení",
}

# ---------------------------------------------------------------------------
# B2B category path → sport routing.
# Maps the first segment of group.category (the B2B XML <category> element,
# e.g. "Football/Men" → first segment "football") to the Czech sport routing
# value used by CategoryMapper.  None means the segment is a generic container
# (no sport info) — try the second segment, or leave sport unset.
#
# NOTE: "lifestyle" maps to None intentionally — do not set sport for lifestyle
# products; gender + product type routing handles them correctly.
# ---------------------------------------------------------------------------
_B2B_CAT_TO_SPORT: dict[str, str | None] = {
    "football":      "fotbal",
    "soccer":        "fotbal",
    "tennis":        "tenis",
    "padel":         "padel",
    "basketball":    "basketbal",
    "martial arts":  "bojové sporty",
    "running":       "běh",
    "fitness":       "fitness",
    "training":      "fitness",
    "ice hockey":    "lední hokej",
    "volleyball":    "volejbal",
    "floorball":     "florbal",
    "handball":      "házená",
    "badminton":     "badminton",
    "squash":        "squash",
    "swimming":      "plavání",
    "cycling":       "cyklistika",
    "bike":          "cyklistika",
    "winter sports": "lední hokej",
    "skating":       "bruslení",
    # None = no sport signal — gender+type routing takes over
    "lifestyle":     None,
    "footwear":      None,
    "clothing":      None,
    "accessories":   None,
}

# ---------------------------------------------------------------------------
# B2B category path gender segment → Czech pohlavi value.
# Scans all slash-separated segments of group.category for a known gender term.
# ---------------------------------------------------------------------------
_B2B_GENDER_MAP: dict[str, str] = {
    "men":    "pánské",
    "man":    "pánské",
    "women":  "dámské",
    "woman":  "dámské",
    "kids":   "dětské",
    "junior": "dětské",
    "boys":   "dětské",
    "girls":  "dětské",
    "unisex": "unisex",
}

# ---------------------------------------------------------------------------
# Name-based keyword fallback — ordered lists used in step 7 of
# _build_mapper_params to extract gender and product type from the raw English
# B2B product name when attrs and group.category gave nothing.
#
# Order matters: longer / more-specific patterns must come BEFORE shorter ones
# to avoid "men" matching inside "women".
# ---------------------------------------------------------------------------
_NAME_GENDER_KEYWORDS: list[tuple[str, str]] = [
    ("for kids",   "dětské"),
    ("for junior", "dětské"),
    ("kids'",      "dětské"),
    ("kid's",      "dětské"),
    (" kids ",     "dětské"),
    ("youth",      "dětské"),
    ("junior",     "dětské"),
    (" jr ",       "dětské"),
    (" jr.",       "dětské"),
    ("women's",    "dámské"),
    ("woman's",    "dámské"),
    ("for women",  "dámské"),
    ("men's",      "pánské"),
    ("man's",      "pánské"),
    ("for men",    "pánské"),
    ("unisex",     "unisex"),
]

_NAME_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("tracksuit pants", "kalhoty"),
    ("zip hoodie",      "mikina"),
    ("sweatshirt",      "mikina"),
    ("hoodie",          "mikina"),
    ("winter jacket",   "bunda"),
    ("padded vest",     "vesta"),
    ("polo shirt",      "tričko"),
    ("t-shirt",         "tričko"),
    (" tee ",           "tričko"),
    (" jacket",         "bunda"),
    (" vest ",          "vesta"),
    (" pants",          "kalhoty"),
    (" shorts",         "kalhoty"),
    ("leggings",        "kalhoty"),
    ("backpack",        "batoh"),
    (" bag ",           "batoh"),
    ("sneakers",        "boty"),
    ("sandals",         "sandále"),
    (" shoes",          "boty"),
    (" boots",          "boty"),
    ("beanie",          "čepice"),
    (" cap",            "čepice"),
    (" hat ",           "čepice"),
    ("gloves",          "rukavice"),
    (" ball",           "míč"),
]

# ---------------------------------------------------------------------------
# Product Type routing — raw B2B English "Product Type" value → Czech "typ" param
# used by CategoryMapper conditions.
# ---------------------------------------------------------------------------
_PRODUCT_TYPE_MAP: dict[str, str] = {
    "T-shirt":              "tričko",
    "T-Shirt":              "tričko",
    "Polo shirt":           "tričko",
    "Polo Shirt":           "tričko",
    "Hoodie":               "mikina",
    "Sweatshirt":           "mikina",
    "Jacket":               "bunda",
    "Winter jacket":        "bunda",
    "Vest":                 "vesta",
    "Padded vest":          "vesta",
    "Pants":                "kalhoty",
    "Shorts":               "kalhoty",
    "Tracksuit pants":      "kalhoty",
    "Leggings":             "kalhoty",
    "Dress":                "šaty",
    "Skirt":                "sukně",
    "Shoes":                "boty",
    "Football boots":       "boty",
    "Running shoes":        "boty",
    "Sandals":              "sandále",
    "Slippers":             "pantofle",
    "Backpack":             "batoh",
    "Bag":                  "batoh",
    "Cap":                  "čepice",
    "Hat":                  "čepice",
    "Beanie":               "čepice",
    "Gloves":               "rukavice",
    "Ball":                 "míč",
    "Racket":               "raketa",
    "Hockey stick":         "hokejka",
    "Ice skates":           "brusle",
    "Shin guards":          "chrániče",
    "Goalkeeper gloves":    "rukavice",
}

# ---------------------------------------------------------------------------
# Category source routing — raw B2B "Category" value → Czech "kategorie" param.
# ---------------------------------------------------------------------------
_CATEGORY_SOURCE_MAP: dict[str, str] = {
    "Clothing":     "oblečení",
    "Shoes":        "boty",
    "Accessories":  "doplňky",
    "Equipment":    "vybavení",
    "Balls":        "míče",
    "Gloves":       "rukavice",
}

# ---------------------------------------------------------------------------
# Slug derivation — maps second-level category name (lowercased) to margin slug.
# ---------------------------------------------------------------------------
_SLUG_MAP: dict[str, str] = {
    "fotbal":           "fotbal",
    "tenis":            "tenis",
    "padel":            "padel",
    "basketbal":        "basketbal",
    "bojové sporty":    "bojove_sporty",
    "běh":              "beh",
    "lední hokej":      "hokej",
    "fitness":          "fitness",
    "volejbal":         "volejbal",
    "florbal":          "florbal",
    "házená":           "hazena",
    "cyklistika":       "cyklistika",
    "badminton":        "badminton",
}

# ---------------------------------------------------------------------------
# Path aliases — CategoryMapper paths that don't exist verbatim in WC, but have
# a close equivalent.  Applied before WOO_CATEGORY_IDS lookup.
# ---------------------------------------------------------------------------
_PATH_ALIASES: dict[str, str] = {
    # CategoryMapper calls it "Tenisové míče a doplňky"; WC only has "Tenisové doplňky"
    "Sporty > Tenis > Tenisové míče a doplňky": "Sporty > Tenis > Tenisové doplňky",
}

# ---------------------------------------------------------------------------
# attrs_cs key → CategoryMapper param key mapping
# attrs_cs keys come from attr_maps.ATTRIBUTE_NAME_MAP WC param names.
# ---------------------------------------------------------------------------
_ATTRS_CS_TO_MAPPER: dict[str, str] = {
    "pohlavi":  "pohlavi",   # Gender → e.g. "Pánské", "Dámské", "Dětské"
    "sport":    "sport",     # Sport  → e.g. "Fotbal", "Tenis", "Bojová umění"
    "barva":    "barva",     # Colour → Czech colour name
    "material": "material",  # Material → Czech material name
    "vyrobce":  "znacka",    # Producer → brand name (used by brand_contains conditions)
}


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _build_mapper_params(group, translated) -> dict[str, str]:
    """
    Build the params dict consumed by CategoryMapper.map_product_to_multiple_categories().

    Sources (in order):
    1. translated.attrs_cs — Czech-translated WC attrs (pohlavi, sport, barva, material, vyrobce)
    2. group.attrs["Product Type"] — English value → Czech typ via _PRODUCT_TYPE_MAP
    3. group.attrs["Category"] — English value → Czech kategorie via _CATEGORY_SOURCE_MAP
    [bad-sport strip] — removes placeholder B2B sport values like "to be categorized"
    4. Apply sport routing map — convert Czech sport values to CategoryMapper routing values
    5. group.category B2B path — fills in missing sport/pohlavi when attrs were absent/stripped
    6. Shoe-size heuristic — EU sizes 20–35 (≥3 numeric sizes) → pohlavi=dětské
    7. group.name English keyword scan — last resort for products with empty attrs AND
       bad group.category ("To be categorized"); sets pohlavi and/or typ from name keywords

    Args:
        group:      ProductGroup with raw B2B attrs in group.attrs and B2B category in
                    group.category (e.g. "Football/Men", "Lifestyle/Shoes/Women").
        translated: TranslatedGroup with Czech attrs in translated.attrs_cs.

    Returns:
        dict[str, str] — flat string values, ready for CategoryMapper.
    """
    params: dict[str, str] = {}

    # 1. From attrs_cs — already Czech-translated
    for cs_key, mapper_key in _ATTRS_CS_TO_MAPPER.items():
        values = translated.attrs_cs.get(cs_key, [])
        if values:
            params[mapper_key] = ", ".join(str(v) for v in values if v)

    # 2. Raw B2B "Product Type" → Czech "typ" param (routing-only, not in attrs_cs)
    raw_pt_values = group.attrs.get("Product Type", [])
    if raw_pt_values:
        raw_pt = raw_pt_values[0]
        cs_typ = _PRODUCT_TYPE_MAP.get(raw_pt)
        if cs_typ:
            params["typ"] = cs_typ
        else:
            logger.debug("[category] unknown Product Type %r — not routing on typ", raw_pt)

    # 3. Raw B2B "Category" → Czech "kategorie" hint param (routing-only, not in attrs_cs)
    raw_cat_values = group.attrs.get("Category", [])
    if raw_cat_values:
        raw_cat = raw_cat_values[0]
        cs_kat = _CATEGORY_SOURCE_MAP.get(raw_cat)
        if cs_kat:
            params["kategorie"] = cs_kat

    # Strip bad sport values BEFORE routing map and BEFORE B2B fallback (step 4 and 5)
    # so that the stripped slot can be filled by step 5 from group.category.
    if "sport" in params:
        sport_first = params["sport"].split(", ")[0].strip().lower()
        if sport_first in _BAD_SPORT_VALUES:
            logger.debug(
                "[category] bad sport value stripped: %r (model=%s)",
                params["sport"], group.model,
            )
            del params["sport"]

    # 4. Apply sport routing map — convert Czech sport values to CategoryMapper routing values
    if "sport" in params:
        sport_lower = params["sport"].split(", ")[0].lower()
        routed = _SPORT_ROUTING_MAP.get(sport_lower)
        if routed:
            params["sport"] = routed  # routing only — attrs_cs is unchanged

    # 5. Parse group.category B2B path — fill in missing sport/pohlavi.
    #    Only fires when steps 1–4 did not already provide these params.
    #    group.category format: "Football/Men", "Lifestyle/Shoes/Women", "Training/Kids"
    raw_b2b_category = (group.category or "").strip()
    if raw_b2b_category:
        segments = [s.strip().lower() for s in raw_b2b_category.split("/") if s.strip()]
        # Sport: try first segment, then second (for generic containers like "Footwear/Football")
        if "sport" not in params and segments:
            b2b_sport = _B2B_CAT_TO_SPORT.get(segments[0])
            if b2b_sport is None and len(segments) >= 2:
                b2b_sport = _B2B_CAT_TO_SPORT.get(segments[1])
            if b2b_sport is not None:
                params["sport"] = b2b_sport
                logger.debug(
                    "[category] sport from B2B category %r → %r",
                    raw_b2b_category, b2b_sport,
                )
        # Gender: scan all segments
        if "pohlavi" not in params:
            for seg in segments:
                cs_gender = _B2B_GENDER_MAP.get(seg)
                if cs_gender:
                    params["pohlavi"] = cs_gender
                    logger.debug(
                        "[category] pohlavi from B2B category %r → %r",
                        raw_b2b_category, cs_gender,
                    )
                    break

    # 6. Shoe-size heuristic — if ALL numeric size labels are in range 20–35,
    #    this is a children's product (EU adult shoes start at 36).
    #    Requires ≥3 sizes to avoid false positives (e.g. a single size-35 that
    #    could also be a women's shoe).
    if "pohlavi" not in params and hasattr(group, "variations") and group.variations:
        numeric_sizes = []
        for v in group.variations:
            try:
                numeric_sizes.append(float(v.size_label))
            except (ValueError, AttributeError):
                pass
        if (
            numeric_sizes
            and all(20 <= s <= 35 for s in numeric_sizes)
            and len(numeric_sizes) >= 3
        ):
            params["pohlavi"] = "dětské"
            logger.debug(
                "[category] pohlavi=dětské inferred from shoe sizes %s (model=%s)",
                sorted(numeric_sizes), group.model,
            )

    # 7. English name keyword scan — last resort when everything above gave nothing.
    #    group.name is the raw B2B English name, always present even when attrs={} and
    #    group.category="To be categorized".
    name_lower = (" " + (getattr(group, "name", "") or "").lower() + " ")
    if "pohlavi" not in params:
        for kw, gender in _NAME_GENDER_KEYWORDS:
            if kw in name_lower:
                params["pohlavi"] = gender
                logger.debug(
                    "[category] pohlavi=%r inferred from name keyword %r (model=%s)",
                    gender, kw.strip(), group.model,
                )
                break
    if "typ" not in params:
        for kw, typ in _NAME_TYPE_KEYWORDS:
            if kw in name_lower:
                params["typ"] = typ
                logger.debug(
                    "[category] typ=%r inferred from name keyword %r (model=%s)",
                    typ, kw.strip(), group.model,
                )
                break

    return params


def _resolve_ids(paths: list[str]) -> list[int]:
    """
    Resolve CategoryMapper path strings to WooCommerce integer IDs.

    Looks each path up in config.WOO_CATEGORY_IDS (with alias fallback).
    When a path has ID=0, walks up the hierarchy until a non-zero parent is found
    (e.g. "Muži > Pánské oblečení > Pánské mikiny" → "Muži > Pánské oblečení").
    Falls back to WOO_FALLBACK_CATEGORY_ID when nothing resolves.

    Args:
        paths: List of category path strings from CategoryMapper (max 2).

    Returns:
        List of non-zero WC integer IDs, deduplicated, max 2 entries.
    """
    ids: list[int] = []
    for path in paths:
        resolved_path = _PATH_ALIASES.get(path, path)
        parts = resolved_path.split(" > ")
        found = False
        for depth in range(len(parts), 0, -1):
            candidate = " > ".join(parts[:depth])
            wc_id = WOO_CATEGORY_IDS.get(candidate, 0)
            if wc_id:
                if depth < len(parts):
                    logger.warning(
                        "[category] %r has no WC ID — using parent %r (ID=%d)",
                        resolved_path, candidate, wc_id,
                    )
                ids.append(wc_id)
                found = True
                break
        if not found:
            logger.warning(
                "[category] no WC ID for %r or any parent — skipped",
                resolved_path,
            )

    # Deduplicate, preserve order
    seen: set[int] = set()
    unique: list[int] = []
    for wc_id in ids:
        if wc_id not in seen:
            seen.add(wc_id)
            unique.append(wc_id)

    if not unique:
        if WOO_FALLBACK_CATEGORY_ID:
            return [WOO_FALLBACK_CATEGORY_ID]
        logger.warning("[category] no IDs resolved and WOO_FALLBACK_CATEGORY_ID is 0 — product gets no category")
        return []

    return unique[:2]


def _derive_slug(paths: list[str]) -> str:
    """
    Derive a margin slug from the matched category paths.

    Checks the second level of the deepest path against _SLUG_MAP.
    Falls back to first level, then "default".

    Args:
        paths: List of category path strings (e.g. ["Sporty > Fotbal > Kopačky > Lisovky"]).

    Returns:
        Margin slug string (e.g. "fotbal", "default").
    """
    for path in paths:
        parts = path.split(" > ")
        if len(parts) >= 2:
            slug = _SLUG_MAP.get(parts[1].lower())
            if slug:
                return slug
        slug = _SLUG_MAP.get(parts[0].lower()) if parts else None
        if slug:
            return slug
    return "default"


# ---------------------------------------------------------------------------
# AI category fallback
# ---------------------------------------------------------------------------

# Lazy-opened connection to the translation DB (reuse existing translations table).
_cat_db_conn: sqlite3.Connection | None = None


def _cat_db() -> sqlite3.Connection:
    """Return (or lazily open) the translations SQLite connection."""
    global _cat_db_conn
    if _cat_db_conn is None:
        os.makedirs(os.path.dirname(TRANSLATION_DB), exist_ok=True)
        _cat_db_conn = sqlite3.connect(TRANSLATION_DB)
        _cat_db_conn.execute("PRAGMA journal_mode=WAL")
        _cat_db_conn.execute(
            "CREATE TABLE IF NOT EXISTS translations "
            "(hash TEXT, type TEXT, result TEXT, created_at TEXT, updated_at TEXT, "
            "PRIMARY KEY (hash, type))"
        )
        _cat_db_conn.commit()
    return _cat_db_conn


def _cat_cache_get(key: str) -> str | None:
    """Look up AI category result in the translations cache (type='category_fallback')."""
    row = _cat_db().execute(
        "SELECT result FROM translations WHERE hash = ? AND type = 'category_fallback'",
        (key,),
    ).fetchone()
    return row[0] if row else None


def _cat_cache_set(key: str, value: str) -> None:
    """Store AI category result in the translations cache."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _cat_db()
    row = conn.execute(
        "SELECT created_at FROM translations WHERE hash = ? AND type = 'category_fallback'",
        (key,),
    ).fetchone()
    created_at = row[0] if row and row[0] else now
    conn.execute(
        "INSERT OR REPLACE INTO translations (hash, type, result, created_at, updated_at) "
        "VALUES (?, 'category_fallback', ?, ?, ?)",
        (key, value, created_at, now),
    )
    conn.commit()


def _valid_category_paths() -> list[str]:
    """Return sorted list of category paths that have a non-zero WC ID."""
    return sorted(path for path, wc_id in WOO_CATEGORY_IDS.items() if wc_id > 0)


def _ai_fallback_category(
    name_cs: str,
    params: dict,
    name_en: str = "",
    b2b_category: str = "",
) -> str | None:
    """
    Ask the AI to pick the best WooCommerce category path for an unmapped product.

    Uses the same Kilo AI Gateway and translations SQLite cache as translator.py.
    Returns None when SKIP_TRANSLATION is True, on any error, or when the AI
    returns a path that is not in the known category list.

    Args:
        name_cs:      Czech product name (from translated.name_cs).
        params:       Mapper params dict built by _build_mapper_params().
        name_en:      Original English B2B product name (group.name) — extra context.
        b2b_category: B2B category path (group.category, e.g. "Volleyball/Unisex").

    Returns:
        A valid category path string from WOO_CATEGORY_IDS, or None.
    """
    if SKIP_TRANSLATION:
        return None

    cache_key = hashlib.sha256(
        (name_cs + "|" + str(sorted(params.items()))).encode("utf-8")
    ).hexdigest()

    cached = _cat_cache_get(cache_key)
    if cached is not None:
        logger.debug("[category] AI fallback cache hit: %r", cached)
        return cached if cached != "__none__" else None

    valid_paths = _valid_category_paths()
    if not valid_paths:
        logger.warning("[category] AI fallback: žádné platné kategorie v category_ids.json")
        return None

    params_str = ", ".join(f"{k}={v}" for k, v in sorted(params.items())) or "(žádné)"
    categories_str = "\n".join(f"- {p}" for p in valid_paths)
    extra_lines = []
    if name_en:
        extra_lines.append(f"Anglický název: {name_en}")
    if b2b_category:
        extra_lines.append(f"B2B kategorie: {b2b_category}")
    extra_block = ("\n" + "\n".join(extra_lines)) if extra_lines else ""

    system_msg = (
        "Jsi asistent pro český sportovní e-shop zaměřený na oblečení, obuv a sportovní vybavení. "
        "Vybereš jednu nejlepší WooCommerce kategorii pro daný produkt. "
        "Pokud produkt zjevně nepatří do sportovního e-shopu (např. elektronika, obaly na tablety, "
        "psací potřeby, samolepky, drogerie, hračky bez sportu apod.), odpověz přesně: none\n"
        "Jinak odpovíš POUZE přesným řetězcem cesty kategorie ze zadaného seznamu — "
        "nic jiného, žádné uvozovky, žádný komentář."
    )
    user_msg = (
        f"Produkt: {name_cs}{extra_block}\n"
        f"Parametry: {params_str}\n\n"
        f"Dostupné kategorie (vyber přesně jednu, nebo odpověz 'none' pokud produkt nepatří do sortimentu):\n"
        f"{categories_str}\n\n"
        f"Odpověz pouze přesným názvem kategorie, nebo slovem none."
    )

    try:
        from openai import OpenAI, RateLimitError

        client = OpenAI(
            base_url="https://api.kilo.ai/api/gateway",
            api_key=_get_api_key(),
            max_retries=0,
        )

        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=TRANSLATION_MODEL,
                    messages=[
                        {"role": "system", "content": system_msg},
                        {"role": "user", "content": user_msg},
                    ],
                    max_completion_tokens=80,
                )
                raw = response.choices[0].message.content.strip().strip('"').strip("'")
                if raw.lower() == "none":
                    logger.warning(
                        "[category] AI fallback: produkt %r vyhodnocen jako mimo sortiment — draft",
                        name_cs,
                    )
                    _cat_cache_set(cache_key, "__none__")
                    return None
                elif raw in WOO_CATEGORY_IDS and WOO_CATEGORY_IDS[raw] > 0:
                    logger.info("[category] AI fallback → %r", raw)
                    _cat_cache_set(cache_key, raw)
                    return raw
                else:
                    logger.warning(
                        "[category] AI fallback: odpověď %r není platná kategorie — přeskočeno",
                        raw,
                    )
                    _cat_cache_set(cache_key, "__none__")
                    return None
            except RateLimitError:
                if SKIP_ON_RATE_LIMIT:
                    logger.warning("[category] AI fallback: rate limit, přeskočeno (skip_on_rate_limit=True)")
                    return None
                wait = 4.1 * (2 ** attempt)
                logger.warning("[category] AI fallback: rate limit, čekám %.1fs", wait)
                time.sleep(wait)

        logger.warning("[category] AI fallback: všechny pokusy vyčerpány")
        return None

    except Exception as exc:
        logger.error("[category] AI fallback: chyba — %s", exc)
        return None


def _get_api_key() -> str:
    """Load KILOCODE_API_KEY lazily to avoid import errors when api_keys.py is absent."""
    from config.api_keys import KILOCODE_API_KEY  # type: ignore
    return KILOCODE_API_KEY


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve(group, translated) -> tuple[list[int], str]:
    """
    Map a product group to WooCommerce category IDs and a margin slug.

    Uses CategoryMapper with the "complementary" strategy (max 2 categories
    from different top-level branches).

    Args:
        group:      ProductGroup (for raw B2B attrs).
        translated: TranslatedGroup (for Czech name and Czech attrs).

    Returns:
        Tuple of:
        - list[int]: WooCommerce category IDs (empty list if nothing resolves
          and WOO_FALLBACK_CATEGORY_ID is 0).
        - str: margin slug for config.MARGINS lookup.
    """
    params = _build_mapper_params(group, translated)
    name = translated.name_cs or ""

    categories, mapping_type = _mapper.map_product_to_multiple_categories(
        name, params, max_categories=2, strategy="complementary"
    )

    if mapping_type == "unmapped":
        logger.warning(
            "[category] unmapped: model=%r  name=%r  params=%r",
            group.model, name, params,
        )
        ai_path = _ai_fallback_category(name, params, group.name, group.category)
        if ai_path:
            categories = [ai_path]

    ids = _resolve_ids(categories)
    slug = _derive_slug(categories)

    logger.info(
        "[category] %s  →  ids=%s  slug=%s",
        " | ".join(categories) if categories else "⚠ nenamapováno",
        ids if ids else "∅ (žádné ID)",
        slug,
    )

    return ids, slug


# ---------------------------------------------------------------------------
# CategoryMapper class — ported from fastcentrik project
# Defines the full WC category tree and condition-matching logic.
# ---------------------------------------------------------------------------

class CategoryMapper:
    """
    Mapuje produkty do WooCommerce kategorií na základě definovaných pravidel.
    """

    def __init__(self):
        """Inicializace mapperu s definicí kategoriální struktury."""
        self.category_structure = self._define_category_structure()
        self.mapping_stats = {
            'mapped': 0,
            'fallback': 0,
            'unmapped': 0,
            'category_counts': {}
        }

    def _define_category_structure(self) -> Dict:
        """
        Definuje kompletní strukturu WooCommerce kategorií s pravidly pro mapování.

        Struktura pravidel:
        - name_contains: seznam slov, která musí obsahovat název produktu
        - name_regex: regulární výraz pro název produktu
        - params: slovník parametrů a jejich hodnot
        - params_any: alespoň jeden z parametrů musí odpovídat
        - brand_contains: značka obsahuje
        - priority: priorita pravidla (vyšší = důležitější)
        """
        return {
            "Muži": {
                "conditions": [
                    {"params": {"pohlavi": ["pánské", "muž", "men", "unisex"]}},
                    {"name_contains": ["pánsk", "muž", "men's", "unisex"]},
                    {"params": {"kategorie": ["pánské", "unisex"]}}
                ],
                "subcategories": {
                    "Pánské oblečení": {
                        "conditions": [
                            {"name_contains": ["oblečení", "mikina", "kalhoty", "tričko", "bunda", "kabát", "vesta",
                                               "hoodie", "sweatshirt", "jacket", "pants", "shirt", "t-shirt"]},
                            {"params": {"typ": ["oblečení", "oděv"]}},
                            {"params": {"kategorie": ["oblečení"]}}
                        ],
                        "subcategories": {
                            "Pánské mikiny": {
                                "conditions": [
                                    {"name_contains": ["mikina", "hoodie", "sweatshirt"]},
                                    {"params": {"typ": ["mikina"]}}
                                ],
                                "priority": 10
                            },
                            "Pánské kalhoty": {
                                "conditions": [
                                    {"name_contains": ["kalhoty", "džíny", "jeans", "tepláky", "kraťasy", "shorts"]},
                                    {"params": {"typ": ["kalhoty", "džíny", "tepláky"]}}
                                ],
                                "priority": 10
                            },
                            "Pánská trička": {
                                "conditions": [
                                    {"name_contains": ["tričko", "triko", "t-shirt", "tshirt", "polo"]},
                                    {"params": {"typ": ["tričko", "triko"]}}
                                ],
                                "priority": 10
                            },
                            "Pánské zimní oblečení": {
                                "conditions": [
                                    {"name_contains": ["zimní", "bunda", "kabát", "parka", "péřov", "lyžařsk", "vesta"]},
                                    {"params": {"sezona": ["zima", "zimní"]}},
                                    {"params": {"typ": ["bunda", "kabát", "zimní oblečení", "vesta"]}}
                                ],
                                "priority": 9
                            }
                        }
                    },
                    "Pánské boty": {
                        "conditions": [
                            {"name_contains": ["boty", "tenisky", "obuv", "kopačky", "tretry", "pantofle", "sandále", "trekové", "turistická",
                                               "shoes", "boots", "sneakers", "sandals"]},
                            {"params": {"typ": ["obuv", "boty"]}},
                            {"params": {"kategorie": ["boty"]}}
                        ],
                        "subcategories": {
                            "Pánská outdoorová obuv": {
                                "conditions": [
                                    {"name_contains": ["outdoor", "trekk", "trek", "hory", "turistick", "trekové", "turistická"]},
                                    {"params": {"typ": ["outdoor obuv", "trekové boty", "turistická obuv"]}},
                                    {"brand_contains": ["palladium"]}
                                ],
                                "priority": 10
                            },
                            "Pánské tenisky": {
                                "conditions": [
                                    {"name_contains": ["tenisky", "sneaker", "lifestyle", "volnočas"]},
                                    {"params": {"typ": ["tenisky", "sneakers"]}}
                                ],
                                "priority": 10
                            },
                            "Pánské pantofle": {
                                "conditions": [
                                    {"name_contains": ["pantofle", "nazouváky", "přezůvky", "domácí obuv"]},
                                    {"params": {"typ": ["pantofle"]}}
                                ],
                                "priority": 10
                            },
                            "Pánské sandály": {
                                "conditions": [
                                    {"name_contains": ["sandále", "sandály", "žabky"]},
                                    {"params": {"typ": ["sandále"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Pánské doplňky": {
                        "conditions": [
                            {"name_contains": ["batoh", "čepice", "rukavice", "šála", "pásek", "peněženka", "kšiltovka"]},
                            {"params": {"typ": ["doplňky", "příslušenství"]}},
                            {"params": {"kategorie": ["doplňky"]}}
                        ],
                        "subcategories": {
                            "Pánské batohy": {
                                "conditions": [
                                    {"name_contains": ["batoh", "ruksak", "backpack"]},
                                    {"params": {"typ": ["batoh"]}}
                                ],
                                "priority": 10
                            },
                            "Pánské čepice": {
                                "conditions": [
                                    {"name_contains": ["čepice", "kšiltovka", "kulich", "cap", "beanie"]},
                                    {"params": {"typ": ["čepice", "pokrývka hlavy"]}}
                                ],
                                "priority": 10
                            }
                        }
                    }
                }
            },
            "Ženy": {
                "conditions": [
                    {"params": {"pohlavi": ["dámské", "žena", "women", "unisex"]}},
                    {"name_contains": ["dámsk", "žen", "women's", "unisex", "dívčí"]},
                    {"params": {"kategorie": ["dámské", "unisex"]}}
                ],
                "subcategories": {
                    "Dámské oblečení": {
                        "conditions": [
                            {"name_contains": ["oblečení", "mikina", "kalhoty", "tričko", "šaty", "sukně", "vesta",
                                               "hoodie", "sweatshirt", "jacket", "pants", "shirt", "t-shirt"]},
                            {"params": {"typ": ["oblečení", "oděv"]}},
                            {"params": {"kategorie": ["oblečení"]}}
                        ],
                        "subcategories": {
                            "Dámské mikiny": {
                                "conditions": [
                                    {"name_contains": ["mikina", "hoodie", "sweatshirt"]},
                                    {"params": {"typ": ["mikina"]}}
                                ],
                                "priority": 10
                            },
                            "Dámská trička": {
                                "conditions": [
                                    {"name_contains": ["tričko", "triko", "t-shirt", "tshirt", "top"]},
                                    {"params": {"typ": ["tričko", "triko", "top"]}}
                                ],
                                "priority": 10
                            },
                            "Dámské kalhoty": {
                                "conditions": [
                                    {"name_contains": ["kalhoty", "džíny", "jeans", "legíny", "leggings"]},
                                    {"params": {"typ": ["kalhoty", "džíny", "legíny"]}}
                                ],
                                "priority": 10
                            },
                            "Dámské zimní oblečení": {
                                "conditions": [
                                    {"name_contains": ["zimní", "bunda", "kabát", "parka", "péřov", "vesta"]},
                                    {"params": {"sezona": ["zima", "zimní"]}},
                                    {"params": {"typ": ["bunda", "kabát", "zimní oblečení", "vesta"]}}
                                ],
                                "priority": 9
                            }
                        }
                    },
                    "Dámské boty": {
                        "conditions": [
                            {"name_contains": ["boty", "tenisky", "obuv", "lodičky", "kozačky", "pantofle", "trekové", "turistická",
                                               "shoes", "boots", "sneakers", "sandals"]},
                            {"params": {"typ": ["obuv", "boty"]}},
                            {"params": {"kategorie": ["boty"]}}
                        ],
                        "subcategories": {
                            "Dámská outdoorová obuv": {
                                "conditions": [
                                    {"name_contains": ["outdoor", "trekk", "trek", "turistick", "trekové", "turistická"]},
                                    {"params": {"typ": ["outdoor obuv", "trekové boty", "turistická obuv"]}},
                                    {"brand_contains": ["palladium"]}
                                ],
                                "priority": 10
                            },
                            "Dámské tenisky": {
                                "conditions": [
                                    {"name_contains": ["tenisky", "sneaker", "lifestyle"]},
                                    {"params": {"typ": ["tenisky", "sneakers"]}}
                                ],
                                "priority": 10
                            },
                            "Dámské pantofle": {
                                "conditions": [
                                    {"name_contains": ["pantofle", "nazouváky", "přezůvky"]},
                                    {"params": {"typ": ["pantofle"]}}
                                ],
                                "priority": 10
                            },
                            "Dámské sandály": {
                                "conditions": [
                                    {"name_contains": ["sandále", "sandály", "žabky"]},
                                    {"params": {"typ": ["sandále"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Dámské doplňky": {
                        "conditions": [
                            {"name_contains": ["batoh", "čepice", "kabelka", "šála", "rukavice", "kšiltovka"]},
                            {"params": {"typ": ["doplňky", "příslušenství"]}},
                            {"params": {"kategorie": ["doplňky"]}}
                        ],
                        "subcategories": {
                            "Dámské batohy": {
                                "conditions": [
                                    {"name_contains": ["batoh", "ruksak", "backpack"]},
                                    {"params": {"typ": ["batoh"]}}
                                ],
                                "priority": 10
                            },
                            "Dámské čepice": {
                                "conditions": [
                                    {"name_contains": ["čepice", "kšiltovka", "kulich", "baret"]},
                                    {"params": {"typ": ["čepice", "pokrývka hlavy"]}}
                                ],
                                "priority": 10
                            }
                        }
                    }
                }
            },
            "Děti": {
                "conditions": [
                    {"params": {"pohlavi": ["dětské", "děti", "kids", "junior"]}},
                    {"name_contains": ["dětsk", "junior", "kid", "boy", "girl", " jr", "děti"]},
                    {"params": {"kategorie": ["dětské"]}}
                ],
                "subcategories": {
                    "Dětské oblečení": {
                        "conditions": [
                            {"name_contains": ["oblečení", "mikina", "kalhoty", "tričko",
                                               "hoodie", "sweatshirt", "jacket", "pants", "shirt", "t-shirt"]},
                            {"params": {"typ": ["oblečení", "oděv"]}},
                            {"params": {"kategorie": ["oblečení"]}}
                        ],
                        "subcategories": {
                            "Dětské mikiny": {
                                "conditions": [
                                    {"name_contains": ["mikina", "hoodie", "sweatshirt"]},
                                    {"params": {"typ": ["mikina"]}}
                                ],
                                "priority": 10
                            },
                            "Dětská trička": {
                                "conditions": [
                                    {"name_contains": ["tričko", "triko", "t-shirt"]},
                                    {"params": {"typ": ["tričko", "triko"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské kalhoty": {
                                "conditions": [
                                    {"name_contains": ["kalhoty", "džíny", "tepláky", "kraťasy"]},
                                    {"params": {"typ": ["kalhoty", "džíny", "tepláky"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské zimní oblečení": {
                                "conditions": [
                                    {"name_contains": ["zimní", "bunda", "kombinéza", "lyžařsk"]},
                                    {"params": {"sezona": ["zima", "zimní"]}},
                                    {"params": {"typ": ["bunda", "zimní oblečení"]}}
                                ],
                                "priority": 9
                            }
                        }
                    },
                    "Dětské boty": {
                        "conditions": [
                            {"name_contains": ["boty", "tenisky", "obuv", "sandále",
                                               "shoes", "boots", "sneakers", "sandals"]},
                            {"params": {"typ": ["obuv", "boty"]}},
                            {"params": {"kategorie": ["boty"]}}
                        ],
                        "subcategories": {
                            "Dětské outdoorové boty": {
                                "conditions": [
                                    {"name_contains": ["outdoor", "turistick", "trek"]},
                                    {"params": {"typ": ["outdoor obuv"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské tenisky": {
                                "conditions": [
                                    {"name_contains": ["tenisky", "sneaker"]},
                                    {"params": {"typ": ["tenisky", "sneakers"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské pantofle": {
                                "conditions": [
                                    {"name_contains": ["pantofle", "přezůvky"]},
                                    {"params": {"typ": ["pantofle"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské sandále": {
                                "conditions": [
                                    {"name_contains": ["sandále", "sandály"]},
                                    {"params": {"typ": ["sandále"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Dětské doplňky": {
                        "conditions": [
                            {"name_contains": ["batoh", "čepice", "rukavice"]},
                            {"params": {"typ": ["doplňky", "příslušenství"]}},
                            {"params": {"kategorie": ["doplňky"]}}
                        ],
                        "subcategories": {
                            "Dětské batohy": {
                                "conditions": [
                                    {"name_contains": ["batoh", "školní batoh"]},
                                    {"params": {"typ": ["batoh"]}}
                                ],
                                "priority": 10
                            },
                            "Dětské čepice": {
                                "conditions": [
                                    {"name_contains": ["čepice", "kulich", "kšiltovka"]},
                                    {"params": {"typ": ["čepice"]}}
                                ],
                                "priority": 10
                            }
                        }
                    }
                }
            },
            "Sporty": {
                "conditions": [
                    {"params": {"sport": ["fotbal", "tenis", "basketbal", "běh", "fitness", "hokej",
                                          "volejbal", "florbal", "házená", "cyklistika", "badminton",
                                          "squash", "plavání", "bruslení", "padel"]}},
                    {"name_contains": ["sport", "fotbal", "tenis", "basketbal", "běh", "fitness",
                                       "volejbal", "florbal", "házená", "cyklistika", "badminton"]},
                    {"params": {"kategorie": ["sport", "sporty"]}}
                ],
                "subcategories": {
                    "Fotbal": {
                        "conditions": [
                            {"params": {"sport": ["fotbal", "football", "soccer"]}},
                            {"name_contains": ["fotbal", "kopačky", "football", "soccer"]},
                            {"params": {"typ": ["kopačky", "fotbalové vybavení"]}}
                        ],
                        "subcategories": {
                            "Kopačky": {
                                "conditions": [
                                    {"name_contains": ["kopačky", "kopačka"]},
                                    {"params": {"typ": ["kopačky", "boty"]}},
                                    {"params": {"kategorie": ["boty"]}}
                                ],
                                "subcategories": {
                                    "Lisovky": {
                                        "conditions": [
                                            {"name_contains": ["lisovky", "FG", "AG"]},
                                            {"params": {"povrch": ["FG", "AG", "lisovky"]}}
                                        ],
                                        "priority": 12
                                    },
                                    "Kolíky a lisokolíky": {
                                        "conditions": [
                                            {"name_contains": ["kolíky", "SG", "lisokolíky"]},
                                            {"params": {"povrch": ["SG", "kolíky"]}}
                                        ],
                                        "priority": 12
                                    },
                                    "Sálovky": {
                                        "conditions": [
                                            {"name_contains": ["sálovky", "IC", "IN", "indoor"]},
                                            {"params": {"povrch": ["IC", "IN", "sálovky"]}}
                                        ],
                                        "priority": 12
                                    },
                                    "Turfy": {
                                        "conditions": [
                                            {"name_contains": ["turfy", "TF", "turf"]},
                                            {"params": {"povrch": ["TF", "turfy"]}}
                                        ],
                                        "priority": 12
                                    }
                                }
                            },
                            "Fotbalové míče": {
                                "conditions": [
                                    {"name_contains": ["míč", "ball", "fotbalový míč"]},
                                    {"params": {"typ": ["míč", "fotbalový míč"]}}
                                ],
                                "priority": 10
                            },
                            "Fotbalové oblečení": {
                                "conditions": [
                                    {"name_contains": ["dres", "trenýrky", "štulpny", "fotbalové oblečení"]},
                                    {"params": {"typ": ["dres", "fotbalové oblečení"]}},
                                    {"params": {"kategorie": ["oblečení"]}}
                                ],
                                "priority": 10
                            },
                            "Fotbalový brankář": {
                                "conditions": [
                                    {"name_contains": ["brankář", "goalkeeper"]},
                                    {"params": {"typ": ["brankářské vybavení", "rukavice"]}}
                                ],
                                "priority": 10
                            },
                            "Fotbalové chrániče": {
                                "conditions": [
                                    {"name_contains": ["chránič", "chrániče", "shin"]},
                                    {"params": {"typ": ["chrániče"]}}
                                ],
                                "priority": 10
                            },
                            "Fotbalové vybavení": {
                                "conditions": [
                                    {"name_contains": ["trénink", "kužel", "meta", "síť", "vybavení"]},
                                    {"params": {"typ": ["tréninkové vybavení", "vybavení"]}}
                                ],
                                "priority": 9
                            }
                        }
                    },
                    "Tenis": {
                        "conditions": [
                            {"params": {"sport": ["tenis", "tennis"]}},
                            {"name_contains": ["tenis", "tennis", "raketa"]},
                            {"params": {"typ": ["tenisové vybavení"]}}
                        ],
                        "subcategories": {
                            "Tenisové rakety": {
                                "conditions": [
                                    {"name_contains": ["raketa", "racket", "racquet"]},
                                    {"params": {"typ": ["raketa", "tenisová raketa"]}}
                                ],
                                "priority": 10
                            },
                            "Tenisové boty": {
                                "conditions": [
                                    {"name_contains": ["tenisové boty", "tennis shoes"]},
                                    {"params": {"typ": ["tenisové boty"]}},
                                    {"params": {"kategorie": ["boty"]}}
                                ],
                                "priority": 10
                            },
                            "Tenisové míče a doplňky": {
                                "conditions": [
                                    {"name_contains": ["tenisový míč", "tennis ball", "výplet", "grip"]},
                                    {"params": {"typ": ["tenisové míče", "tenisové doplňky"]}}
                                ],
                                "priority": 10
                            },
                            "Tenisové tašky": {
                                "conditions": [
                                    {"name_contains": ["tenisová taška", "tennis bag"]},
                                    {"params": {"typ": ["tenisová taška"]}}
                                ],
                                "priority": 10
                            },
                            "Tenisové oblečení": {
                                "conditions": [
                                    {"name_contains": ["tenisové oblečení", "tennis wear"]},
                                    {"params": {"typ": ["tenisové oblečení"]}},
                                    {"params": {"kategorie": ["oblečení"]}}
                                ],
                                "priority": 10
                            },
                            "Tenisové doplňky": {
                                "conditions": [
                                    {"name_contains": ["kšiltovka", "čepice", "cap", "aeroready", "training", "running", "baseball"]},
                                    {"params": {"typ": ["tenisové doplňky", "čepice"]}},
                                    {"params": {"kategorie": ["doplňky"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Padel": {
                        "conditions": [
                            {"params": {"sport": ["padel"]}},
                            {"name_contains": ["padel"]},
                            {"params": {"typ": ["padelové vybavení"]}}
                        ],
                        "subcategories": {
                            "Padelové rakety": {
                                "conditions": [
                                    {"name_contains": ["padelová raketa", "padel racket"]},
                                    {"params": {"typ": ["padelová raketa"]}}
                                ],
                                "priority": 10
                            },
                            "Padelové míče a doplňky": {
                                "conditions": [
                                    {"name_contains": ["padelový míč", "padel ball"]},
                                    {"params": {"typ": ["padelové míče", "padelové doplňky"]}}
                                ],
                                "priority": 10
                            },
                            "Padelové tašky": {
                                "conditions": [
                                    {"name_contains": ["padelová taška", "padel bag"]},
                                    {"params": {"typ": ["padelová taška"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Basketbal": {
                        "conditions": [
                            {"params": {"sport": ["basketbal", "basketball"]}},
                            {"name_contains": ["basketbal", "basketball"]},
                            {"params": {"typ": ["basketbalové vybavení"]}}
                        ],
                        "subcategories": {
                            "Basketbalové boty": {
                                "conditions": [
                                    {"name_contains": ["basketbalové boty", "basketball shoes"]},
                                    {"params": {"typ": ["basketbalové boty"]}},
                                    {"params": {"kategorie": ["boty"]}}
                                ],
                                "priority": 10
                            },
                            "Basketbalové míče": {
                                "conditions": [
                                    {"name_contains": ["basketbalový míč", "basketball"]},
                                    {"params": {"typ": ["basketbalový míč"]}}
                                ],
                                "priority": 10
                            },
                            "Basketbalové oblečení": {
                                "conditions": [
                                    {"name_contains": ["basketbalový dres", "basketball jersey"]},
                                    {"params": {"typ": ["basketbalové oblečení"]}},
                                    {"params": {"kategorie": ["oblečení"]}}
                                ],
                                "priority": 10
                            },
                            "Basketbalové desky a koše": {
                                "conditions": [
                                    {"name_contains": ["basketbalový koš", "deska", "hoop"]},
                                    {"params": {"typ": ["basketbalový koš", "basketbalová deska"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Bojové sporty": {
                        "conditions": [
                            {"params": {"sport": ["box", "mma", "karate", "judo", "bojové sporty"]}},
                            {"name_contains": ["box", "mma", "karate", "judo", "bojov", "rukavice", "helma", "figurína", "dummy", "kick-box", "boxersk"]},
                            {"params": {"typ": ["bojové vybavení", "rukavice", "helma"]}},
                            {"name_contains": ["boxersk"]}
                        ],
                        "subcategories": {
                            "Box": {
                                "conditions": [
                                    {"params": {"sport": ["box", "boxing"]}},
                                    {"name_contains": ["box", "boxing", "boxersk"]},
                                    {"params": {"typ": ["boxerské vybavení", "rukavice", "helma"]}},
                                    {"name_contains": ["boxersk"]}
                                ],
                                "priority": 10
                            },
                            "MMA": {
                                "conditions": [
                                    {"params": {"sport": ["mma"]}},
                                    {"name_contains": ["mma", "mixed martial", "figurína", "dummy", "kick-box"]},
                                    {"params": {"typ": ["mma vybavení"]}}
                                ],
                                "priority": 10
                            },
                            "Karate": {
                                "conditions": [
                                    {"params": {"sport": ["karate"]}},
                                    {"name_contains": ["karate"]},
                                    {"params": {"typ": ["karate vybavení"]}}
                                ],
                                "priority": 10
                            },
                            "Judo": {
                                "conditions": [
                                    {"params": {"sport": ["judo"]}},
                                    {"name_contains": ["judo", "judogi"]},
                                    {"params": {"typ": ["judo vybavení"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Běh": {
                        "conditions": [
                            {"params": {"sport": ["běh", "running"]}},
                            {"name_contains": ["běh", "běžeck", "running"]},
                            {"params": {"typ": ["běžecké vybavení"]}}
                        ],
                        "subcategories": {
                            "Běžecká obuv": {
                                "conditions": [
                                    {"name_contains": ["běžecké boty", "running shoes", "běžecká obuv"]},
                                    {"params": {"typ": ["běžecké boty", "běžecká obuv"]}},
                                    {"params": {"kategorie": ["boty"]}}
                                ],
                                "priority": 10
                            },
                            "Běžecké oblečení": {
                                "conditions": [
                                    {"name_contains": ["běžecké oblečení", "running wear"]},
                                    {"params": {"typ": ["běžecké oblečení"]}},
                                    {"params": {"kategorie": ["oblečení"]}}
                                ],
                                "priority": 10
                            },
                            "Běžecké batohy": {
                                "conditions": [
                                    {"name_contains": ["běžecký batoh", "running pack"]},
                                    {"params": {"typ": ["běžecký batoh"]}}
                                ],
                                "priority": 10
                            },
                            "Běžecké doplňky": {
                                "conditions": [
                                    {"name_contains": ["běžecké doplňky", "čelovka", "pás"]},
                                    {"params": {"typ": ["běžecké doplňky"]}},
                                    {"params": {"kategorie": ["doplňky"]}}
                                ],
                                "priority": 10
                            }
                        }
                    },
                    "Lední hokej": {
                        "conditions": [
                            {"params": {"sport": ["hokej", "lední hokej", "ice hockey"]}},
                            {"name_contains": ["hokej", "hockey", "brusle", "hokejka", "kalhoty", "brankář", "dres"]},
                            {"params": {"typ": ["hokejové vybavení"]}}
                        ],
                        "subcategories": {
                            "Hokejky": {
                                "conditions": [
                                    {"name_contains": ["hokejka", "hockey stick"]},
                                    {"params": {"typ": ["hokejka"]}}
                                ],
                                "priority": 10
                            },
                            "Hokejové brusle": {
                                "conditions": [
                                    {"name_contains": ["hokejové brusle", "brusle", "brankářské brusle"]},
                                    {"params": {"typ": ["hokejové brusle", "brusle"]}}
                                ],
                                "priority": 10
                            },
                            "Hokejové oblečení": {
                                "conditions": [
                                    {"name_contains": ["hokejové kalhoty", "dres", "rozhodčí"]},
                                    {"params": {"typ": ["hokejové oblečení", "dres"]}},
                                    {"params": {"kategorie": ["oblečení"]}}
                                ],
                                "priority": 10
                            }
                        },
                        "priority": 8
                    },
                    "Fitness": {
                        "conditions": [
                            {"params": {"sport": ["fitness", "posilování"]}},
                            {"name_contains": ["fitness", "posilov", "činka", "gym", "cyklotrenažér", "eliptick", "trenažér", "cyklotrenazer"]},
                            {"params": {"typ": ["fitness vybavení", "fitness stroj", "kardio stroj"]}},
                            {"brand_contains": ["nordictrack", "schwinn", "proform"]}
                        ],
                        "subcategories": {
                            "Fitness obuv": {
                                "conditions": [
                                    {"name_contains": ["fitness boty", "gym shoes"]},
                                    {"params": {"typ": ["fitness obuv"]}}
                                ],
                                "priority": 10
                            },
                            "Stroje": {
                                "conditions": [
                                    {"name_contains": ["stroj", "běžecký pás", "rotoped", "cyklotrenažér", "eliptick", "trenažér", "schwinn", "nordictrack", "proform"]},
                                    {"params": {"typ": ["fitness stroj", "posilovací stroj", "kardio stroj"]}},
                                    {"brand_contains": ["nordictrack", "schwinn", "proform"]}
                                ],
                                "subcategories": {
                                    "Kardio stroje": {
                                        "conditions": [
                                            {"name_contains": ["běžecký pás", "rotoped", "eliptick", "cyklotrenažér", "trenažér", "schwinn", "nordictrack", "proform"]},
                                            {"params": {"typ": ["kardio stroj", "fitness stroj"]}},
                                            {"brand_contains": ["nordictrack", "schwinn", "proform"]}
                                        ],
                                        "priority": 11
                                    },
                                    "Posilovací stroje": {
                                        "conditions": [
                                            {"name_contains": ["posilovací stroj", "bench", "stojan"]},
                                            {"params": {"typ": ["posilovací stroj"]}}
                                        ],
                                        "priority": 11
                                    }
                                }
                            },
                            "Jóga": {
                                "conditions": [
                                    {"name_contains": ["jóga", "yoga"]},
                                    {"params": {"typ": ["jóga vybavení"]}}
                                ],
                                "priority": 10
                            },
                            "Pilates": {
                                "conditions": [
                                    {"name_contains": ["pilates"]},
                                    {"params": {"typ": ["pilates vybavení"]}}
                                ],
                                "priority": 10
                            },
                            "Cvičící vybavení": {
                                "conditions": [
                                    {"name_contains": ["činka", "kettlebell", "guma", "expandér"]},
                                    {"params": {"typ": ["cvičící vybavení", "fitness doplňky"]}}
                                ],
                                "priority": 9
                            }
                        }
                    },
                    "Volejbal": {
                        "conditions": [
                            {"params": {"sport": ["volejbal", "volleyball"]}},
                            {"name_contains": ["volejbal", "volleyball"]},
                        ],
                        "priority": 8
                    },
                    "Florbal": {
                        "conditions": [
                            {"params": {"sport": ["florbal", "floorball"]}},
                            {"name_contains": ["florbal", "floorball"]},
                        ],
                        "priority": 8
                    },
                    "Házená": {
                        "conditions": [
                            {"params": {"sport": ["házená", "handball"]}},
                            {"name_contains": ["házená", "handball"]},
                        ],
                        "priority": 8
                    },
                    "Cyklistika": {
                        "conditions": [
                            {"params": {"sport": ["cyklistika", "cycling", "bike"]}},
                            {"name_contains": ["cyklo", "kolo", "cycling", "bike"]},
                        ],
                        "priority": 8
                    },
                    "Badminton": {
                        "conditions": [
                            {"params": {"sport": ["badminton"]}},
                            {"name_contains": ["badminton"]},
                        ],
                        "priority": 8
                    }
                }
            },
            "Zimní oblečení": {
                "conditions": [
                    {"name_contains": ["zimní", "péřov", "vesta", "bunda", "kabát", "lyžařsk", "dívčí"]},
                    {"params": {"sezona": ["zima", "zimní"]}},
                    {"params": {"typ": ["zimní oblečení", "bunda", "kabát", "vesta"]}}
                ],
                "subcategories": {
                    "Zimní bundy": {
                        "conditions": [
                            {"name_contains": ["bunda", "kabát", "parka"]},
                            {"params": {"typ": ["bunda", "kabát", "zimní oblečení"]}}
                        ],
                        "priority": 10
                    },
                    "Péřové vesty": {
                        "conditions": [
                            {"name_contains": ["vesta", "péřov"]},
                            {"params": {"typ": ["vesta", "péřová vesta"]}}
                        ],
                        "priority": 10
                    },
                    "Lyžařské oblečení": {
                        "conditions": [
                            {"name_contains": ["lyžařsk", "ski"]},
                            {"params": {"sport": ["lyžování", "ski"]}},
                            {"params": {"typ": ["lyžařské oblečení"]}}
                        ],
                        "priority": 10
                    }
                },
                "priority": 7
            }
        }

    def map_product_to_category(self, product_name: str, product_params: Dict[str, Any],
                                original_category: Optional[str] = None) -> Tuple[str, str]:
        """
        Mapuje produkt do správné WooCommerce kategorie.

        Args:
            product_name: Název produktu
            product_params: Slovník parametrů produktu
            original_category: Původní kategorie (fallback)

        Returns:
            Tuple[str, str]: (category_path, mapping_type)
        """
        product_name_lower = product_name.lower() if product_name else ""
        normalized_params = {}
        for key, value in product_params.items():
            if value:
                normalized_params[key.lower()] = str(value).lower()

        best_match = self._find_best_category_match(
            product_name_lower, normalized_params, self.category_structure
        )

        if best_match:
            self.mapping_stats['mapped'] += 1
            self.mapping_stats['category_counts'][best_match] = \
                self.mapping_stats['category_counts'].get(best_match, 0) + 1
            return best_match, "exact"

        if original_category:
            self.mapping_stats['fallback'] += 1
            logger.warning("Použit fallback pro produkt '%s' -> '%s'", product_name, original_category)
            return original_category, "fallback"

        self.mapping_stats['unmapped'] += 1
        logger.error("Produkt '%s' nemohl být namapován do žádné kategorie", product_name)
        return "", "unmapped"

    def map_product_to_multiple_categories(self, product_name: str, product_params: Dict[str, Any],
                                           original_category: Optional[str] = None,
                                           max_categories: int = 2,
                                           strategy: str = "complementary") -> Tuple[List[str], str]:
        """
        Mapuje produkt do více WooCommerce kategorií.

        Args:
            product_name: Název produktu
            product_params: Slovník parametrů produktu
            original_category: Původní kategorie (fallback)
            max_categories: Maximální počet kategorií (default 2)
            strategy: "complementary" (z různých hlavních větví) nebo "all_matches"

        Returns:
            Tuple[List[str], str]: (seznam kategorií, mapping_type)
        """
        product_name_lower = product_name.lower() if product_name else ""
        normalized_params = {}
        for key, value in product_params.items():
            if value:
                normalized_params[key.lower()] = str(value).lower()

        all_matches = self._find_all_category_matches(
            product_name_lower, normalized_params, self.category_structure
        )

        if strategy == "complementary":
            selected_categories = self._select_complementary_categories(all_matches, max_categories)
        else:
            selected_categories = self._select_best_matches(all_matches, max_categories)

        if selected_categories:
            self.mapping_stats['mapped'] += 1
            for category in selected_categories:
                self.mapping_stats['category_counts'][category] = \
                    self.mapping_stats['category_counts'].get(category, 0) + 1
            return selected_categories, "exact"

        if original_category:
            self.mapping_stats['fallback'] += 1
            logger.warning("Použit fallback pro produkt '%s' -> '%s'", product_name, original_category)
            return [original_category], "fallback"

        self.mapping_stats['unmapped'] += 1
        logger.error("Produkt '%s' nemohl být namapován do žádné kategorie", product_name)
        return [], "unmapped"

    def _find_best_category_match(self, product_name: str, params: Dict[str, str],
                                  category_tree: Dict, parent_path: str = "") -> Optional[str]:
        """Rekurzivně hledá nejlepší shodu v kategoriovém stromu."""
        best_match = None
        best_priority = -1

        for category_name, category_data in category_tree.items():
            current_path = f"{parent_path} > {category_name}" if parent_path else category_name

            if self._check_category_conditions(product_name, params, category_data.get('conditions', [])):
                if 'subcategories' in category_data:
                    sub_match = self._find_best_category_match(
                        product_name, params, category_data['subcategories'], current_path
                    )
                    if sub_match:
                        return sub_match

                priority = category_data.get('priority', 0)
                if priority > best_priority:
                    best_match = current_path
                    best_priority = priority

        return best_match

    def _find_all_category_matches(self, product_name: str, params: Dict[str, str],
                                   category_tree: Dict, parent_path: str = "") -> List[Tuple[str, int, int]]:
        """Rekurzivně hledá všechny odpovídající kategorie."""
        matches = []

        for category_name, category_data in category_tree.items():
            current_path = f"{parent_path} > {category_name}" if parent_path else category_name
            current_depth = current_path.count(' > ') + 1

            if self._check_category_conditions(product_name, params, category_data.get('conditions', [])):
                priority = category_data.get('priority', 0)

                if 'subcategories' in category_data:
                    sub_matches = self._find_all_category_matches(
                        product_name, params, category_data['subcategories'], current_path
                    )
                    matches.extend(sub_matches)
                    if not sub_matches:
                        matches.append((current_path, priority, current_depth))
                else:
                    matches.append((current_path, priority, current_depth))

        return matches

    def _select_complementary_categories(self, matches: List[Tuple[str, int, int]],
                                         max_categories: int) -> List[str]:
        """Vybere komplementární kategorie z různých hlavních větví."""
        if not matches:
            return []

        branches: dict = {}
        for category_path, priority, depth in matches:
            main_branch = category_path.split(' > ')[0]
            if main_branch not in branches:
                branches[main_branch] = []
            branches[main_branch].append((category_path, priority, depth))

        selected = []
        for branch, branch_matches in branches.items():
            branch_matches.sort(key=lambda x: (x[1], x[2]), reverse=True)
            if branch_matches:
                selected.append(branch_matches[0])

        selected.sort(key=lambda x: x[1], reverse=True)
        return [cat[0] for cat in selected[:max_categories]]

    def _select_best_matches(self, matches: List[Tuple[str, int, int]],
                             max_categories: int) -> List[str]:
        """Vybere nejlepší kategorie podle priority a specifičnosti."""
        if not matches:
            return []
        matches.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return [cat[0] for cat in matches[:max_categories]]

    def _check_category_conditions(self, product_name: str, params: Dict[str, str],
                                   conditions: List[Dict]) -> bool:
        """
        Kontroluje zda produkt splňuje alespoň jednu podmínku pro danou kategorii.

        Args:
            product_name: Normalizovaný (lowercase) název produktu.
            params: Normalizované (lowercase) parametry produktu.
            conditions: Seznam podmínek.

        Returns:
            True pokud alespoň jedna podmínka vyhovuje.
        """
        if not conditions:
            return False

        for condition in conditions:
            if 'name_contains' in condition:
                if any(word in product_name for word in condition['name_contains']):
                    return True

            if 'name_regex' in condition:
                if re.search(condition['name_regex'], product_name):
                    return True

            if 'params' in condition:
                params_match = True
                for param_name, param_values in condition['params'].items():
                    if param_name.lower() not in params:
                        params_match = False
                        break
                    param_value = params[param_name.lower()]
                    if isinstance(param_values, list):
                        if not any(v.lower() in param_value for v in param_values):
                            params_match = False
                            break
                    else:
                        if param_values.lower() not in param_value:
                            params_match = False
                            break
                if params_match:
                    return True

            if 'params_any' in condition:
                for param_name, param_values in condition['params_any'].items():
                    if param_name.lower() in params:
                        param_value = params[param_name.lower()]
                        if isinstance(param_values, list):
                            if param_value in [v.lower() for v in param_values]:
                                return True
                        else:
                            if param_value == param_values.lower():
                                return True

            if 'brand_contains' in condition:
                brand = params.get('znacka', '') or params.get('vyrobce', '')
                if any(word.lower() in brand.lower() for word in condition['brand_contains']):
                    return True

        return False

    def get_mapping_stats(self) -> Dict:
        """Vrací statistiky mapování."""
        return self.mapping_stats

    def reset_stats(self) -> None:
        """Resetuje statistiky mapování."""
        self.mapping_stats = {
            'mapped': 0,
            'fallback': 0,
            'unmapped': 0,
            'category_counts': {}
        }


# ---------------------------------------------------------------------------
# Module-level singleton — instantiated once on import
# ---------------------------------------------------------------------------
_mapper = CategoryMapper()
