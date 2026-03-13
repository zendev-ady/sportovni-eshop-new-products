"""
tests/test_price_calculator.py — Unit tests for price_calculator.py

Run:
    cd b2b_to_woocommerce
    python -m pytest tests/test_price_calculator.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import patch

import price_calculator
from price_calculator import calculate_price, _shipping_eur, _margin, get_eur_czk_rate
from config.config import BASE_SHIPPING_EUR, EUR_CZK_FALLBACK, MARGINS

# Fixed rate used for deterministic price assertions.
_FIXED_RATE = 25.0


# ---------------------------------------------------------------------------
# _shipping_eur
# ---------------------------------------------------------------------------

class TestShippingEur:
    def test_below_20kg_returns_flat_rate(self):
        assert _shipping_eur(0.0) == BASE_SHIPPING_EUR
        assert _shipping_eur(0.5) == BASE_SHIPPING_EUR
        assert _shipping_eur(19.9) == BASE_SHIPPING_EUR

    def test_20kg_uses_3x_multiplier(self):
        # floor(20/10) + 1 = 3
        assert _shipping_eur(20.0) == BASE_SHIPPING_EUR * 3

    def test_30kg_uses_4x_multiplier(self):
        # floor(30/10) + 1 = 4 (also the cap)
        assert _shipping_eur(30.0) == BASE_SHIPPING_EUR * 4

    def test_very_heavy_capped_at_4x(self):
        # floor(100/10) + 1 = 11, capped at 4
        assert _shipping_eur(100.0) == BASE_SHIPPING_EUR * 4
        assert _shipping_eur(999.0) == BASE_SHIPPING_EUR * 4


# ---------------------------------------------------------------------------
# _margin
# ---------------------------------------------------------------------------

class TestMargin:
    def test_known_slug_returns_config_value(self):
        for slug, expected in MARGINS.items():
            assert _margin(1.0, slug) == expected

    def test_unknown_slug_light_uses_1_05(self):
        assert _margin(0.0, "unknown") == 1.05
        assert _margin(29.9, "unknown") == 1.05

    def test_unknown_slug_heavy_uses_1_15(self):
        assert _margin(30.0, "unknown") == 1.15
        assert _margin(100.0, "unknown") == 1.15

    def test_category_slug_overrides_weight(self):
        # Even a very light product uses category margin when slug is known.
        for slug, expected in MARGINS.items():
            assert _margin(0.5, slug) == expected


# ---------------------------------------------------------------------------
# calculate_price
# ---------------------------------------------------------------------------

class TestCalculatePrice:
    def test_zero_wholesale_returns_string_zero(self):
        assert calculate_price(0.0, 1.0) == "0"

    def test_negative_wholesale_returns_string_zero(self):
        assert calculate_price(-10.0, 1.0) == "0"

    def test_result_is_string(self):
        price_calculator._rate_cache = _FIXED_RATE
        result = calculate_price(50.0, 1.0)
        assert isinstance(result, str)
        assert result.isdigit()

    def test_result_is_positive(self):
        price_calculator._rate_cache = _FIXED_RATE
        result = calculate_price(50.0, 1.0)
        assert int(result) > 0

    def test_heavier_product_costs_more(self):
        """Higher shipping weight → higher shipping EUR → higher CZK price."""
        price_calculator._rate_cache = _FIXED_RATE
        light = int(calculate_price(50.0, 1.0))
        heavy = int(calculate_price(50.0, 25.0))
        assert heavy > light

    def test_higher_wholesale_costs_more(self):
        price_calculator._rate_cache = _FIXED_RATE
        cheap = int(calculate_price(20.0, 1.0))
        expensive = int(calculate_price(100.0, 1.0))
        assert expensive > cheap

    def test_deterministic(self):
        price_calculator._rate_cache = _FIXED_RATE
        a = calculate_price(49.99, 0.8)
        b = calculate_price(49.99, 0.8)
        assert a == b

    def test_known_price_spot_check(self):
        """
        Spot-check: 50 EUR wholesale, 1 kg weight, default category slug.

        Formula (with FIXED_RATE=25, PRICE_ADJUSTMENT=1.107, BASE_SHIPPING_EUR=8.12,
                 MARGINS['default']=1.45, MARGIN_EXTRA_CZK=150, VAT=1.21):
            (50 * 1.107 + 8.12) * 25 * 1.45 * 1.21 + 150 = 2934
        """
        price_calculator._rate_cache = _FIXED_RATE
        result = calculate_price(50.0, 1.0, "default")
        assert abs(int(result) - 2934) <= 1


# ---------------------------------------------------------------------------
# get_eur_czk_rate — network & caching
# ---------------------------------------------------------------------------

class TestGetEurCzkRate:
    def setup_method(self):
        price_calculator._rate_cache = None  # reset cache before each test

    def test_returns_fallback_on_network_error(self):
        with patch("price_calculator.requests.get", side_effect=Exception("down")):
            rate = get_eur_czk_rate()
        assert rate == EUR_CZK_FALLBACK

    def test_parses_cnb_response(self):
        mock_resp = {"rates": [{"currencyCode": "EUR", "rate": 25.5, "amount": 1}]}
        with patch("price_calculator.requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_resp
            mock_get.return_value.raise_for_status = lambda: None
            rate = get_eur_czk_rate()
        assert rate == 25.5

    def test_result_is_cached(self):
        mock_resp = {"rates": [{"currencyCode": "EUR", "rate": 25.5, "amount": 1}]}
        with patch("price_calculator.requests.get") as mock_get:
            mock_get.return_value.json.return_value = mock_resp
            mock_get.return_value.raise_for_status = lambda: None
            get_eur_czk_rate()
            get_eur_czk_rate()
        assert mock_get.call_count == 1
