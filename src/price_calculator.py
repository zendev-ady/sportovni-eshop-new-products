"""
price_calculator.py — EUR→CZK pricing for WooCommerce variations.

Input:  wholesale_netto (EUR float), weight (kg float), category_slug (str)
Output: regular_price as a string (integer CZK, incl. VAT)

Formula:
    customer_czk = round(
        (wholesale_netto × PRICE_ADJUSTMENT + shipping_eur)
        × eur_czk_rate
        × margin
        × 1.21          ← Czech VAT
        + margin_extra
    )

Where:
    PRICE_ADJUSTMENT  — true cost basis factor; see config.py for derivation
    shipping_eur      — weight-based flat shipping in EUR (see _shipping_eur)
    eur_czk_rate      — fetched once per run from CNB; falls back to EUR_CZK_FALLBACK
    margin            — category override if slug in MARGINS, else weight-based tier
    margin_extra      — MARGIN_EXTRA_CZK when weight < 30 kg, else 0
"""

import logging
import sys
import os
from datetime import date

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import (
    CNB_API_URL,
    EUR_CZK_FALLBACK,
    MARGINS,
    PRICE_ADJUSTMENT,
    BASE_SHIPPING_EUR,
    MARGIN_EXTRA_CZK,
)

logger = logging.getLogger(__name__)

# Module-level cache: one CNB fetch per process run
_rate_cache: float | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_eur_czk_rate() -> float:
    """
    Return the EUR/CZK exchange rate for today from the CNB API.

    Caches the result for the lifetime of the process so the API is hit
    only once per pipeline run. Falls back to EUR_CZK_FALLBACK from config
    on any network or parse error — never raises.

    Returns:
        float: EUR/CZK rate (e.g. 25.085).
    """
    global _rate_cache
    if _rate_cache is not None:
        return _rate_cache

    today = date.today().isoformat()
    url = f"{CNB_API_URL}?date={today}&lang=EN"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for entry in data["rates"]:
            if entry["currencyCode"] == "EUR":
                _rate_cache = entry["rate"] / entry["amount"]
                logger.info("EUR/CZK rate from CNB: %.4f (date: %s)", _rate_cache, today)
                return _rate_cache
        raise ValueError("EUR entry not found in CNB response")
    except Exception as exc:
        logger.warning(
            "CNB rate fetch failed (%s) — using fallback %.4f", exc, EUR_CZK_FALLBACK
        )
        _rate_cache = EUR_CZK_FALLBACK
        return _rate_cache


def calculate_price(
    wholesale_netto: float,
    weight: float,
    category_slug: str = "default",
) -> str:
    """
    Calculate the customer-facing CZK retail price for a single variation.

    Args:
        wholesale_netto: Supplier EUR net price (from B2B feed).
        weight:          Product weight in kg (float(ProductGroup.weight)).
        category_slug:   WooCommerce category slug for margin lookup.
                         Pass "default" when category is not yet known.

    Returns:
        Integer CZK price as a string (e.g. "1249"), as required by the
        WooCommerce REST API. Returns "0" with a warning for zero/negative input.
    """
    if wholesale_netto <= 0:
        logger.warning(
            "calculate_price called with wholesale_netto=%.4f — returning '0'",
            wholesale_netto,
        )
        return "0"

    rate = get_eur_czk_rate()
    shipping = _shipping_eur(weight)
    margin = _margin(weight, category_slug)
    margin_extra = MARGIN_EXTRA_CZK if weight < 30 else 0.0

    price = (
        (wholesale_netto * PRICE_ADJUSTMENT + shipping)
        * rate
        * margin
        * 1.21
        + margin_extra
    )
    return str(int(round(price)))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _shipping_eur(weight: float) -> float:
    """
    Weight-based shipping cost in EUR.

    Under 20 kg: flat BASE_SHIPPING_EUR.
    20 kg and above: multiplier = floor(weight / 10) + 1, capped at 4.
    The cap prevents unrealistic charges on very heavy catalogue items.

    Args:
        weight: Product weight in kg.

    Returns:
        Shipping cost in EUR.
    """
    if weight < 20:
        return BASE_SHIPPING_EUR
    multiplier = min(max(1, int(weight // 10) + 1), 4)
    return BASE_SHIPPING_EUR * multiplier


def _margin(weight: float, category_slug: str) -> float:
    """
    Determine profit margin multiplier.

    Category slug takes priority if explicitly listed in MARGINS (config.py).
    Otherwise falls back to weight-based tiers:
        weight < 30 kg → 1.05  (margin_extra compensates for the lower rate)
        weight ≥ 30 kg → 1.15  (heavier goods, no margin_extra)

    Args:
        weight:        Product weight in kg.
        category_slug: WooCommerce category slug.

    Returns:
        Margin multiplier (e.g. 1.05).
    """
    if category_slug in MARGINS:
        return MARGINS[category_slug]
    return 1.05 if weight < 30 else 1.15
