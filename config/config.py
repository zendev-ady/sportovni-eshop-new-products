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
TRANSLATION_MODEL = "openai/gpt-4o-mini"
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
# IDs populated from product_categories_export_2026-03-12.csv.
# Paths with value 0 = category exists in CategoryMapper but not yet in WC.
# Every 0 logs a WARNING during sync — create the category in WC Admin and fill in the ID.
# ---------------------------------------------------------------------------
WOO_CATEGORY_IDS: dict[str, int] = {
    # ── Muži (429) ────────────────────────────────────────────────────────────
    "Muži":                                                         429,
    "Muži > Pánské oblečení":                                       444,
    "Muži > Pánské oblečení > Pánské mikiny":                         0,  # missing in WC
    "Muži > Pánské oblečení > Pánské kalhoty":                      469,
    "Muži > Pánské oblečení > Pánská trička":                       445,
    "Muži > Pánské oblečení > Pánské zimní oblečení":               457,
    "Muži > Pánské boty":                                           430,
    "Muži > Pánské boty > Pánská outdoorová obuv":                  451,
    "Muži > Pánské boty > Pánské tenisky":                          452,
    "Muži > Pánské boty > Pánské pantofle":                           0,  # missing in WC
    "Muži > Pánské boty > Pánské sandály":                            0,  # missing in WC
    "Muži > Pánské doplňky":                                        435,
    "Muži > Pánské doplňky > Pánské batohy":                        464,
    "Muži > Pánské doplňky > Pánské čepice":                        436,
    # ── Ženy (433) ────────────────────────────────────────────────────────────
    "Ženy":                                                         433,
    "Ženy > Dámské oblečení":                                       438,
    "Ženy > Dámské oblečení > Dámské mikiny":                       461,
    "Ženy > Dámské oblečení > Dámská trička":                       441,
    "Ženy > Dámské oblečení > Dámské kalhoty":                      448,
    "Ženy > Dámské oblečení > Dámské zimní oblečení":               463,
    "Ženy > Dámské boty":                                           434,
    "Ženy > Dámské boty > Dámská outdoorová obuv":                  466,
    "Ženy > Dámské boty > Dámské tenisky":                            0,  # missing in WC
    "Ženy > Dámské boty > Dámské pantofle":                           0,  # missing in WC
    "Ženy > Dámské boty > Dámské sandály":                            0,  # missing in WC
    "Ženy > Dámské doplňky":                                        439,
    "Ženy > Dámské doplňky > Dámské batohy":                        465,
    "Ženy > Dámské doplňky > Dámské čepice":                        440,
    # ── Děti (431) ────────────────────────────────────────────────────────────
    "Děti":                                                         431,
    "Děti > Dětské oblečení":                                       446,
    "Děti > Dětské oblečení > Dětské mikiny":                         0,  # missing in WC
    "Děti > Dětské oblečení > Dětská trička":                       447,
    "Děti > Dětské oblečení > Dětské kalhoty":                      449,
    "Děti > Dětské oblečení > Dětské zimní oblečení":                 0,  # missing in WC
    "Děti > Dětské boty":                                           432,
    "Děti > Dětské boty > Dětské outdoorové boty":                    0,  # missing in WC
    "Děti > Dětské boty > Dětské tenisky":                          460,
    "Děti > Dětské boty > Dětské pantofle":                           0,  # missing in WC
    "Děti > Dětské boty > Dětské sandále":                            0,  # missing in WC
    "Děti > Dětské doplňky":                                          0,  # missing in WC
    "Děti > Dětské doplňky > Dětské batohy":                          0,  # missing in WC
    "Děti > Dětské doplňky > Dětské čepice":                          0,  # missing in WC
    # ── Sporty (42) ───────────────────────────────────────────────────────────
    "Sporty":                                                        42,
    "Sporty > Fotbal":                                               43,
    "Sporty > Fotbal > Kopačky":                                     44,
    "Sporty > Fotbal > Kopačky > Lisovky":                           45,
    "Sporty > Fotbal > Kopačky > Kolíky a lisokolíky":               46,
    "Sporty > Fotbal > Kopačky > Sálovky":                           47,
    "Sporty > Fotbal > Kopačky > Turfy":                             48,
    "Sporty > Fotbal > Fotbalové míče":                              49,
    "Sporty > Fotbal > Fotbalové oblečení":                          50,
    "Sporty > Fotbal > Fotbalový brankář":                          450,
    "Sporty > Fotbal > Fotbalové chrániče":                          51,
    "Sporty > Fotbal > Fotbalové vybavení":                         455,
    "Sporty > Tenis":                                                53,
    "Sporty > Tenis > Tenisové rakety":                              54,
    "Sporty > Tenis > Tenisové boty":                                55,
    "Sporty > Tenis > Tenisové míče a doplňky":                       0,  # aliased → Tenisové doplňky via _PATH_ALIASES
    "Sporty > Tenis > Tenisové tašky":                               56,
    "Sporty > Tenis > Tenisové oblečení":                            57,
    "Sporty > Tenis > Tenisové doplňky":                             58,
    "Sporty > Padel":                                                 0,  # missing in WC
    "Sporty > Padel > Padelové rakety":                               0,  # missing in WC
    "Sporty > Padel > Padelové míče a doplňky":                       0,  # missing in WC
    "Sporty > Padel > Padelové tašky":                                0,  # missing in WC
    "Sporty > Basketbal":                                            59,
    "Sporty > Basketbal > Basketbalové boty":                        60,
    "Sporty > Basketbal > Basketbalové míče":                        61,
    "Sporty > Basketbal > Basketbalové oblečení":                    62,
    "Sporty > Basketbal > Basketbalové desky a koše":                63,
    "Sporty > Bojové sporty":                                        64,
    "Sporty > Bojové sporty > Box":                                  65,
    "Sporty > Bojové sporty > MMA":                                  66,
    "Sporty > Bojové sporty > Karate":                                0,  # missing in WC
    "Sporty > Bojové sporty > Judo":                                  0,  # missing in WC
    "Sporty > Běh":                                                  67,
    "Sporty > Běh > Běžecká obuv":                                   68,
    "Sporty > Běh > Běžecké oblečení":                               69,
    "Sporty > Běh > Běžecké batohy":                                 70,
    "Sporty > Běh > Běžecké doplňky":                               454,
    "Sporty > Lední hokej":                                         442,
    "Sporty > Lední hokej > Hokejky":                               467,
    "Sporty > Lední hokej > Hokejové brusle":                       468,
    "Sporty > Lední hokej > Hokejové oblečení":                     443,
    "Sporty > Fitness":                                             453,
    "Sporty > Fitness > Fitness obuv":                                0,  # missing in WC
    "Sporty > Fitness > Stroje":                                    458,
    "Sporty > Fitness > Stroje > Kardio stroje":                    459,
    "Sporty > Fitness > Stroje > Posilovací stroje":                  0,  # missing in WC
    "Sporty > Fitness > Jóga":                                        0,  # missing in WC
    "Sporty > Fitness > Pilates":                                     0,  # missing in WC
    "Sporty > Fitness > Cvičící vybavení":                            0,  # missing in WC
    # ── Zimní oblečení (437) ──────────────────────────────────────────────────
    "Zimní oblečení":                                               437,
    "Zimní oblečení > Zimní bundy":                                 456,
    "Zimní oblečení > Péřové vesty":                                462,
    "Zimní oblečení > Lyžařské oblečení":                             0,  # missing in WC
}

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
