# B2B → WooCommerce Sync Pipeline

Automated daily sync from the SPORTPROFIS B2B XML feed to a WooCommerce store.
Downloads ~80 000 products, groups them into variable WooCommerce products, translates
names and descriptions to Czech via AI, maps categories, uploads images to GCS, and
pushes everything via the WooCommerce REST API.

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Fill in secrets
cp config/api_keys.py.example config/api_keys.py   # edit with your keys

# 3. Selective sync (recommended for testing)
python web_ui.py        # open http://localhost:8000, paste product URLs

# 4. Full sync
python run_sync.py

# 5. Dry run (no WooCommerce writes)
python run_sync.py --dry-run --limit 50
```

---

## Project structure

```
b2b_to_woocommerce/
├── run_sync.py              Full pipeline entry point
├── select_sync.py           Sync only specific products by URL
├── web_ui.py                FastAPI web UI (dark theme, SSE streaming log)
├── urls.txt                 Paste product URLs here for CLI selective sync
│
├── src/
│   ├── xml_parser.py        Downloads + parses B2B XML feed → List[Dict]
│   ├── product_grouper.py   Groups flat products into variable/simple ProductGroups
│   ├── price_calculator.py  EUR→CZK pricing (CNB rate + margin + VAT)
│   ├── translator.py        EN→CS translation via OpenAI + SQLite cache
│   ├── attribute_mapper.py  Builds WooCommerce attribute payloads
│   ├── category_mapper.py   Maps B2B categories to WooCommerce category IDs
│   ├── woo_client.py        WooCommerce REST API client (batched upserts)
│   ├── image_uploader.py    Uploads product images to Google Cloud Storage
│   ├── _payloads.py         Pure WooCommerce payload builders
│   └── _cache.py            SQLite SKU→WC ID cache helpers
│
├── config/
│   ├── config.py            All tunable settings (margins, model, URLs)
│   ├── api_keys.py          Secrets — gitignored
│   ├── attr_maps.py         Static EN→CS attribute translation dicts
│   ├── category_ids.json    WooCommerce category path → ID mapping
│   └── gcs-key.json         GCS service account — gitignored
│
├── templates/
│   └── index.html           Web UI template
│
├── tests/
│   ├── test_price_calculator.py
│   ├── test_category_mapper.py
│   └── test_xml_parser.py
│
└── cache/                   Auto-created at runtime
    ├── translations.db      SQLite translation cache (EN→CS)
    ├── sku_cache.db         SQLite SKU→WooCommerce ID cache
    └── image_cache.db       SQLite image URL→GCS URL cache
```

---

## Secrets — `config/api_keys.py`

```python
WOO_CONSUMER_KEY    = "ck_..."
WOO_CONSUMER_SECRET = "cs_..."
KILOCODE_API_KEY    = "..."     # Kilo AI gateway key (routes to OpenAI/Gemini)
GCS_SERVICE_ACCOUNT = {...}     # or use config/gcs-key.json
```

Also copy `.env.example` → `.env` for optional Telegram alerts.

---

## Selective sync — web UI

```bash
python web_ui.py
```

Open **http://localhost:8000**, paste one or more B2B product URLs (one per line), click **Spustit sync**.

- The full model group (all colours + sizes) is always synced for each URL
- Logs stream live to the browser via SSE
- **Stop** button terminates the sync mid-run
- **Přeskočit překlad při rate limitu** — skips AI translation on first 429 (returns English text)

CLI alternative:

```bash
python select_sync.py --urls urls.txt
python select_sync.py --dry-run
```

---

## Full sync

```bash
python run_sync.py                          # full live sync
python run_sync.py --dry-run                # no WooCommerce writes
python run_sync.py --source feed.xml        # use local XML instead of URL
python run_sync.py --dry-run --limit 20     # first 20 product groups only
```

Exit codes: `0` = success (per-product errors logged, not raised), `1` = fatal.

---

## Pricing formula

```
customer_czk = round(
    (wholesale_netto × PRICE_ADJUSTMENT + shipping_eur)
    × eur_czk_rate          ← fetched from CNB each run
    × margin                ← category override or weight-based tier
    × 1.21                  ← Czech VAT
    + margin_extra          ← flat CZK uplift for products < 30 kg
)
```

All knobs are in `config/config.py`: `PRICE_ADJUSTMENT`, `BASE_SHIPPING_EUR`,
`MARGINS`, `MARGIN_EXTRA_CZK`.

---

## Translation

- Text fields (name, short/long description) → `openai/gpt-4o` via Kilo AI gateway
- Attribute values (colour, gender, sport, …) → static dicts in `config/attr_maps.py`
- All results cached in `cache/translations.db` — AI is never called twice for the same text
- Set `SKIP_TRANSLATION = True` in `config/config.py` to bypass AI entirely (English text kept as-is)

---

## Category mapping

`config/category_ids.json` maps WooCommerce category paths to IDs:

```json
"Sporty > Fotbal > Kopačky > Lisovky": 45
```

Value `0` = category not yet created in WC Admin (logs a WARNING, product still synced).
Set `WOO_FALLBACK_CATEGORY_ID` in `config/config.py` to the ID of an "Ostatní" catch-all category.

---

## Business rules

| Rule | Detail |
|---|---|
| Never delete products | Disappeared products are set to `draft` |
| SKUs are sacred | `<item@uid>` values are never modified |
| `regular_price` is always a string | WooCommerce API requirement |
| Parent has no price | Price lives on variations only (variable products) |
| Parent upserted before variations | Required by WooCommerce batch API |
| Translation cache mandatory | Never call OpenAI without checking SQLite first |
| All customer-facing text in Czech | English in output is always a bug |

---

## Tests

```bash
python -m pytest tests/ -v      # 58 tests
```

---

## WooCommerce store

`https://darkblue-toad-760041.hostingersite.com/`

Notable category IDs: Sporty=42, Fotbal=43, Kopačky=44, Lisovky=45, Muži=429, Ženy=433, Děti=431.
