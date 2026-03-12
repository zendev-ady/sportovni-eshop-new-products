"""
config.py — central configuration for b2b_to_woocommerce pipeline.
Secrets (WooCommerce keys, OpenAI key) go in api_keys.py, which is gitignored.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ---------------------------------------------------------------------------
# B2B data source
# ---------------------------------------------------------------------------
XML_SOURCE_URL = (
    "https://b2bsportswholesale.net/v2/xml/download/format/partner_b2b_full"
    "/key/a89c8346b85f143de7acb31923319263/lang/en"
)

# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------
CNB_API_URL = "https://api.cnb.cz/cnbapi/exrates/daily"
EUR_CZK_FALLBACK = 25.0  # used when CNB API is unreachable

# ---------------------------------------------------------------------------
# Pricing margins — keyed by WooCommerce category slug, fallback = "default"
# ---------------------------------------------------------------------------
MARGINS = {
    "default": 1.45,
}

# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
TRANSLATION_MODEL = "gpt-4o-mini"
SKIP_TRANSLATION = True   # set False in production to enable AI calls
TRANSLATION_DB = os.path.join(CACHE_DIR, "translations.db")

# ---------------------------------------------------------------------------
# WooCommerce
# ---------------------------------------------------------------------------
WOO_URL = ""          # e.g. "https://mujeshop.cz"
WOO_BATCH_SIZE = 100
