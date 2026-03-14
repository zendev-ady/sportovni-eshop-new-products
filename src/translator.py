"""
translator.py — Translates a ProductGroup from English to Czech.

Input:  ProductGroup (from product_grouper.py)
Output: TranslatedGroup dataclass with Czech name, descriptions, and attrs.

Translation strategy:
  - name, short_description, long_description: gpt-4o-mini via OpenAI API
  - attribute values: static dicts only (attr_maps.py) — never AI
  - SQLite cache keyed by SHA-256 of input text — mandatory in production
  - config.SKIP_TRANSLATION=True bypasses AI calls entirely (attrs still translated)
"""

import hashlib
import logging
import re
import sqlite3
import sys
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import config, attr_maps
from config.api_keys import KILOCODE_API_KEY
from product_grouper import ProductGroup

logger = logging.getLogger(__name__)

# Circuit breaker — set True after all retries are exhausted.
# All subsequent AI calls in this process run fail immediately.
_rate_limit_tripped: bool = False


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass
class TranslatedGroup:
    """
    Czech-language content for a ProductGroup, ready for the WooCommerce builder.

    Attributes:
        name_cs:               Czech product name (50–70 chars target)
        short_description_cs:  Czech short description, HTML allowed
        long_description_cs:   Czech long description, HTML
        attrs_cs:              Attribute dict with Czech param names as keys and
                               Czech values as list items. Keys from ATTRIBUTE_NAME_MAP,
                               empty-string-mapped attrs excluded.
    """
    name_cs: str
    short_description_cs: str
    long_description_cs: str
    seo_description_cs: str
    attrs_cs: Dict[str, List[str]]


# ---------------------------------------------------------------------------
# SQLite cache
# ---------------------------------------------------------------------------

def _init_db(path: str) -> sqlite3.Connection:
    """Open (or create) the translation cache DB and return a connection."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS translations "
        "(hash TEXT, type TEXT, result TEXT, created_at TEXT, updated_at TEXT, PRIMARY KEY (hash, type))"
    )

    # Backward-compatible migration for existing DB files.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(translations)").fetchall()}
    if "created_at" not in cols:
        conn.execute("ALTER TABLE translations ADD COLUMN created_at TEXT")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE translations ADD COLUMN updated_at TEXT")

    conn.commit()
    return conn


def _cache_get(conn: sqlite3.Connection, text_hash: str, trans_type: str) -> Optional[str]:
    row = conn.execute(
        "SELECT result FROM translations WHERE hash = ? AND type = ?",
        (text_hash, trans_type),
    ).fetchone()
    return row[0] if row else None


def _cache_set(conn: sqlite3.Connection, text_hash: str, trans_type: str, result: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT created_at FROM translations WHERE hash = ? AND type = ?",
        (text_hash, trans_type),
    ).fetchone()
    created_at = row[0] if row and row[0] else now

    conn.execute(
        """
        INSERT OR REPLACE INTO translations (hash, type, result, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (text_hash, trans_type, result, created_at, now),
    )
    conn.commit()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Helpers for prompt context
# ---------------------------------------------------------------------------

def _effective_gender(gender_values: List[str]) -> str:
    """
    Determine gender label for prompts.

    Args:
        gender_values: Czech gender values from attrs_cs["pohlavi"].

    Returns:
        "Unisex" if multiple genders or "Unisex" is present, else the single value.
    """
    if not gender_values:
        return ""
    if len(gender_values) > 1 or "Unisex" in gender_values:
        return "Unisex"
    return gender_values[0]


# ---------------------------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------------------------

def _build_messages(text: str, trans_type: str, context: dict) -> list:
    """Build the messages list for a single OpenAI call."""
    producer = context.get("producer", "")
    category = context.get("category", "")
    sport    = context.get("attrs_cs", {}).get("sport", [""])[0]

    gender_values = context.get("attrs_cs", {}).get("pohlavi", [])
    gender        = _effective_gender(gender_values)

    colour_values = context.get("attrs_cs", {}).get("barva", [])
    colour        = colour_values[0] if len(colour_values) == 1 else ""

    if trans_type == "name":
        colour_instruction = (
            "Barvu uveď jako přídavné jméno ve shodě s typem produktu "
            "(např. 'černé', 'černá', 'černý') — nikdy ne jako 's černým designem' "
            "ani jiné opisné spojení."
            if colour else
            "Barvu do názvu neuváděj — produkt existuje ve více barvách."
        )
        # "Unisex" looks odd in product names (e.g. "Unisex míč", "Unisex taška").
        # Only include gender prefix when it is Pánské/Dámské/Dětské.
        gender_in_name = gender if gender not in ("Unisex", "") else ""
        system = (
            "Jsi SEO copywriter pro český sportovní e-shop. "
            "Píšeš názvy produktů v češtině. Nikdy nepřekládáš názvy značek ani modelové kódy."
        )
        user = (
            f"Vytvoř český název produktu (50–70 znaků) podle formátu: "
            f"{{pohlaví}} {{typ}} {{značka}} {{model}} {{barva}}.\n"
            f"Pohlaví použij jen pokud je uvedeno níže — pokud je prázdné, vynech ho.\n"
            f"Originál: {text}\n"
            f"Značka: {producer} | Kategorie: {category} | Sport: {sport} | "
            f"Pohlaví: {gender_in_name or '(vynechat)'} | Barva: {colour or '(více barev)'}\n"
            f"{colour_instruction}\n"
            f"Nepouštěj anglická slova (Hoodie, Full Zip, Sweatshirt, Half-Zip, Training, "
            f"Performance, Academy apod.) do výsledného názvu — piš výhradně česky.\n"
            f"Vrať pouze výsledný název, bez uvozovek."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if trans_type == "short_description":
        colours_str = ", ".join(colour_values) if colour_values else ""
        system = (
            "Jsi copywriter pro český sportovní e-shop. "
            "Píšeš krátké HTML popisy produktů (300–500 znaků). "
            "Používej <strong> pro 2–3 klíčová slova."
        )
        user = (
            f"Napiš krátký popis produktu v češtině.\n"
            f"Originální popis: {text}\n"
            f"Kontext — Název: {context.get('name_cs', '')} | Značka: {producer} | "
            f"Sport: {sport} | Pohlaví: {gender or '(neuvedeno)'} | Barva: {colours_str or '(neuvedena)'}\n"
            f"Modelový kód zmiň nejvýše jednou (SEO). Vrať pouze HTML text, bez obalujících tagů."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if trans_type == "long_description":
        system = (
            "Jsi copywriter pro český sportovní e-shop. "
            "Píšeš detailní HTML popisy produktů (1000–1500 znaků). "
            "Používej <p>, <strong>, <ul>, <li>. Nepřidávej sekce s výzvou k akci explicitně."
        )
        user = (
            f"Napiš detailní popis produktu v češtině.\n"
            f"Originální popis: {text}\n"
            f"Kontext — Název: {context.get('name_cs', '')} | Krátký popis: {context.get('short_cs', '')} | "
            f"Značka: {producer} | Sport: {sport} | Pohlaví: {gender or '(neuvedeno)'}\n"
            f"Modelový kód zmiň nejvýše jednou (SEO). Doplňuj krátký popis, neopakuj ho. Vrať pouze HTML text."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    if trans_type == "seo_description":
        focus_keyword = context.get("focus_keyword", "")
        system = (
            "Jsi SEO copywriter pro český sportovní e-shop. "
            "Píšeš meta description pro Google — přesně 1 věta, 130–155 znaků, česky. "
            "Klíčové slovo musí být v textu doslova. Přidej měkkou výzvu k akci."
        )
        user = (
            f"Napiš meta description (1 věta, 130–155 znaků) pro tento produkt:\n"
            f"Název: {context.get('name_cs', '')}\n"
            f"Klíčové slovo (musí být v textu): {focus_keyword}\n"
            f"Značka: {producer} | Sport: {sport} | Pohlaví: {gender or '(neuvedeno)'}\n"
            f"Vrať pouze text meta description, bez uvozovek."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    raise ValueError(f"Unknown trans_type: {trans_type!r}")


def _strip_code_fence(text: str) -> str:
    """Remove markdown code fences (```html ... ``` or ``` ... ```) from AI response.

    Input:  raw string from AI, possibly wrapped in ```html\\n...\\n```
    Output: clean string with fences removed
    """
    text = text.strip()
    text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
    text = re.sub(r'\n?```$', '', text)
    return text.strip()


def _call_ai(text: str, trans_type: str, context: dict) -> str:
    """Call Kilo AI Gateway (OpenAI-compatible). Raises on failure — caller handles logging."""
    global _rate_limit_tripped
    from openai import OpenAI, RateLimitError

    if _rate_limit_tripped:
        raise RuntimeError("Rate limit: circuit open — přeskočeno")

    client = OpenAI(
        base_url="https://api.kilo.ai/api/gateway",
        api_key=KILOCODE_API_KEY,
        max_retries=0,  # we handle retries manually below
    )

    for attempt in range(5):
        try:
            response = client.chat.completions.create(
                model=config.TRANSLATION_MODEL,
                messages=_build_messages(text, trans_type, context),
                max_completion_tokens={"name": 100, "short_description": 600, "long_description": 1400, "seo_description": 200}[trans_type],
            )
            return _strip_code_fence(response.choices[0].message.content)
        except RateLimitError:
            if config.SKIP_ON_RATE_LIMIT:
                raise RuntimeError("Rate limit: skip_on_rate_limit=True — přeskočeno")
            wait = 4.1 * (2 ** attempt)
            logger.warning("Rate limit hit, waiting %.1fs (attempt %d/5)", wait, attempt + 1)
            time.sleep(wait)

    _rate_limit_tripped = True
    logger.warning("Rate limit: všechny pokusy vyčerpány — překlady přeskočeny pro zbytek tohoto běhu")
    raise RuntimeError("Rate limit: all 5 retry attempts exhausted")


# ---------------------------------------------------------------------------
# Translate one text field
# ---------------------------------------------------------------------------

def _translate_text(
    conn: sqlite3.Connection,
    text: str,
    trans_type: str,
    context: dict,
    cache_key_suffix: str = "",
) -> str:
    """
    Return Czech translation of *text*.

    Cache lookup → AI call → cache store.
    If SKIP_TRANSLATION is True, returns *text* unchanged (no DB access).

    Args:
        conn:             Open SQLite connection to the translation cache.
        text:             Raw English text to translate.
        trans_type:       "name" | "short_description" | "long_description"
        context:          Dict passed to prompt builder (producer, category, attrs_cs, etc.)
        cache_key_suffix: Extra string appended to hash input to differentiate context
                          variants of the same source text (e.g. "|Unisex|Černá").

    Returns:
        Translated Czech string, or original text if SKIP_TRANSLATION is set.

    Raises:
        Does not raise — logs errors and returns original text as fallback.
    """
    if not text:
        return ""

    if config.SKIP_TRANSLATION:
        return text

    h = _sha256(text + cache_key_suffix)
    cached = _cache_get(conn, h, trans_type)
    if cached is not None:
        logger.debug("Cache hit: %s %s", trans_type, h[:8])
        return cached

    try:
        result = _call_ai(text, trans_type, context)
    except Exception as exc:
        logger.error("OpenAI error [%s]: %s — returning original text", trans_type, exc)
        return text

    _cache_set(conn, h, trans_type, result)
    return result


# ---------------------------------------------------------------------------
# Attribute translation (static dicts, never AI)
# ---------------------------------------------------------------------------

def _map_attrs(attrs: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """
    Translate B2B attribute dict to Czech WooCommerce param dict.

    Key rename: via ATTRIBUTE_NAME_MAP (e.g. "Colour" → "barva").
    Value translate: via per-attr static dict. Unknown values pass through with WARNING.
    Attrs mapping to empty string in ATTRIBUTE_NAME_MAP are excluded.

    Args:
        attrs: Raw group.attrs — {English attr name: [English values]}

    Returns:
        {Czech param name: [Czech values]} — only attrs with non-empty mapped names.
    """
    result: Dict[str, List[str]] = {}
    for en_key, values in attrs.items():
        cs_key = attr_maps.ATTRIBUTE_NAME_MAP.get(en_key)
        if cs_key is None:
            logger.warning("Unknown attr name %r — dropped from attrs_cs", en_key)
            continue
        if cs_key == "":
            continue  # routing-only attr, not for WooCommerce

        val_map = attr_maps.get_value_map(en_key)
        translated: List[str] = []
        for v in values:
            cs_v = val_map.get(v)
            if cs_v is None:
                if val_map:  # map exists but value not found
                    logger.warning("Unknown value %r for attr %r — using as-is", v, en_key)
                cs_v = v
            translated.append(cs_v)

        # Merge into existing key (e.g. multiple "kolekce" attrs)
        if cs_key in result:
            result[cs_key].extend(translated)
        else:
            result[cs_key] = translated

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def translate(group: ProductGroup) -> TranslatedGroup:
    """
    Translate a ProductGroup into Czech content for WooCommerce.

    Attribute values are translated via static dicts (always, even in dev mode).
    Text fields (name, short/long description) use gpt-4o-mini with SQLite caching.
    Set config.SKIP_TRANSLATION=True to skip AI calls and return English text as-is.

    Cache keys include effective gender and colour list so that context changes
    (e.g. mono-colour vs multi-colour variant of same source text) produce
    separate cache entries with the correct translation.

    Args:
        group: A ProductGroup from product_grouper.group()

    Returns:
        TranslatedGroup with Czech fields populated.
    """
    attrs_cs = _map_attrs(group.attrs)

    # Compute cache-key suffix from context that influences the prompt.
    # Cache version is config-driven so prompt strategy changes can be rolled out safely.
    effective_gender = _effective_gender(attrs_cs.get("pohlavi", []))
    colours_key = ",".join(sorted(attrs_cs.get("barva", [])))
    cache_suffix = f"|{config.TRANSLATION_CACHE_VERSION}|{effective_gender}|{colours_key}"

    conn = _init_db(config.TRANSLATION_DB)
    try:
        context_base = {
            "producer":  group.producer,
            "category":  group.category,
            "attrs_cs":  attrs_cs,
        }

        name_cs = _translate_text(conn, group.name, "name", context_base, cache_suffix)

        short_cs = _translate_text(
            conn, group.description, "short_description",
            {**context_base, "name_cs": name_cs},
            cache_suffix,
        )

        long_cs = _translate_text(
            conn, group.description, "long_description",
            {**context_base, "name_cs": name_cs, "short_cs": short_cs},
            cache_suffix,
        )

        # SEO meta description — keyed on name_cs (same product → same description)
        # Focus keyword = name_cs sliced up to and including the brand (same logic as _seo.py)
        _brand_lower = group.producer.strip().lower()
        _name_lower = name_cs.lower()
        _idx = _name_lower.find(_brand_lower) if _brand_lower else -1
        focus_keyword = (
            name_cs[: _idx + len(_brand_lower)].strip().lower()
            if _idx != -1 else name_cs.split()[0].lower() if name_cs else ""
        )
        seo_cs = _translate_text(
            conn, name_cs, "seo_description",
            {**context_base, "name_cs": name_cs, "focus_keyword": focus_keyword},
            cache_suffix,
        )
    finally:
        conn.close()

    return TranslatedGroup(
        name_cs=name_cs,
        short_description_cs=short_cs,
        long_description_cs=long_cs,
        seo_description_cs=seo_cs,
        attrs_cs=attrs_cs,
    )
