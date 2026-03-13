"""
config.py — central configuration for b2b_to_woocommerce pipeline.
Secrets (WooCommerce keys, OpenAI key) go in api_keys.py, which is gitignored.
"""

import json
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
# Pricing
# ---------------------------------------------------------------------------

# PRICE_ADJUSTMENT: converts wholesale_netto to the true cost we pay.
# Accounts for supplier's effective VAT and the negotiated discount they give us.
# Formula: 1 + effective_supplier_vat_rate − negotiated_discount_rate
# Recalculate if the supplier discount or their VAT rate changes.
PRICE_ADJUSTMENT = 1.107

# Flat shipping cost in EUR charged by the supplier per shipment.
# Scales up for heavy products — see price_calculator._shipping_eur().
BASE_SHIPPING_EUR = 8.12

# Fixed CZK uplift added to the final price for products weighing < 30 kg.
# Compensates for lower margin tier on lightweight goods.
MARGIN_EXTRA_CZK = 150.0

# ---------------------------------------------------------------------------
# Pricing margins — category slug → multiplier; falls back to weight-based
# tiers (1.05 / 1.15) when slug is not listed here.
# "default" is used when the category is unknown at pricing time.
# ---------------------------------------------------------------------------
MARGINS = {
    "default": 1.45,
}

# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------
TRANSLATION_MODEL = "gemini-2.0-flash"
SKIP_TRANSLATION = False  # set True to bypass AI calls (uses English text as-is)
TRANSLATION_DB = os.path.join(CACHE_DIR, "translations.db")

# ---------------------------------------------------------------------------
# WooCommerce
# ---------------------------------------------------------------------------
WOO_URL = "https://darkblue-toad-760041.hostingersite.com/"          # e.g. "https://mujeshop.cz"
WOO_BATCH_SIZE = 100
WOO_SKU_CACHE_DB = os.path.join(CACHE_DIR, "sku_cache.db")

# Czech-language attribute names used in WooCommerce product payloads.
# Phase 1: custom product-level attributes (no global pa_ registration needed).
# Phase 4: migrate to global attributes (pa_barva, pa_velikost) for layered nav.
WOO_ATTR_COLOUR = "Barva"
WOO_ATTR_SIZE   = "Velikost"

# ---------------------------------------------------------------------------
# Category mapping
# IDs are stored in config/category_ids.json — edit that file to add/update IDs.
# Value 0 = category not yet created in WC Admin; logs a WARNING during sync.
# ---------------------------------------------------------------------------
_CATEGORY_IDS_PATH = os.path.join(os.path.dirname(__file__), "category_ids.json")
with open(_CATEGORY_IDS_PATH, encoding="utf-8") as _f:
    _raw = json.load(_f)
    # Strip documentation keys (prefixed with _) before exposing as config.
    WOO_CATEGORY_IDS: dict[str, int] = {k: v for k, v in _raw.items() if not k.startswith("_")}

# WooCommerce ID of the "Ostatní" fallback category.
# Set this after creating an "Ostatní" category in WC Admin > Products > Categories.
WOO_FALLBACK_CATEGORY_ID: int = 0

# ---------------------------------------------------------------------------
# Google Cloud Storage — image hosting
# ---------------------------------------------------------------------------
GCS_BUCKET_NAME          = "sportovni-eshop-produkty-fotky"
GCS_IMAGE_PREFIX         = "images/"
GCS_PUBLIC_BASE          = "https://storage.googleapis.com/sportovni-eshop-produkty-fotky/images/"
GCS_SERVICE_ACCOUNT_JSON = os.path.join(BASE_DIR, "config", "gcs-key.json")
GCS_IMAGE_CACHE_DB       = os.path.join(CACHE_DIR, "image_cache.db")
