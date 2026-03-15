#!/usr/bin/env python3
"""
select_sync.py — Selective B2B → WooCommerce sync.

Reads a list of b2bsportswholesale.net product URLs and syncs only those products.
The XML ID embedded in each URL is used to look up the product; the full model group
(all colours / sizes) is then included so the WooCommerce product is complete.

Usage (CLI):
    python select_sync.py                        # reads urls.txt
    python select_sync.py --dry-run              # no WooCommerce API calls
    python select_sync.py --urls my_urls.txt     # custom URL file
    python select_sync.py --source feed.xml      # override XML source

Importable API:
    from select_sync import run_select
    run_select(["https://b2bsportswholesale.net/..."], dry_run=True)
"""

import argparse
import logging
import os
import re
import sys
import time
from typing import Optional

# Ensure Czech characters print correctly on Windows terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "src"))

import xml_parser
import product_grouper
import translator
import category_mapper
from price_calculator import calculate_price, get_eur_czk_rate
from config.config import XML_SOURCE_URL, LOG_DIR
from config import config as _config

logger = logging.getLogger(__name__)

DEFAULT_URLS_FILE = os.path.join(_HERE, "urls.txt")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def load_urls(filepath: str) -> list[str]:
    """
    Load product URLs from a text file.

    Args:
        filepath: Path to the file. One URL per line.
                  Lines starting with '#' and blank lines are ignored.

    Returns:
        List of URL strings.

    Raises:
        RuntimeError: if the file cannot be opened.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        raise RuntimeError(f"Nelze otevřít soubor s URL: {filepath}: {exc}") from exc

    urls = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        urls.append(stripped)
    return urls


def extract_id(url: str) -> Optional[str]:
    """
    Extract the B2B product ID from a b2bsportswholesale.net URL.

    The ID is the trailing numeric segment after the last '-', e.g.:
        https://b2bsportswholesale.net/adidas-copa-mundial-fg-015110-football-boots-1359
        → "1359"

    Args:
        url: Full product URL string.

    Returns:
        ID string, or None if no numeric ID is found.
    """
    match = re.search(r"-(\d+)(?:/)?$", url.rstrip("/"))
    if match:
        return match.group(1)
    logger.warning("Nelze extrahovat ID produktu z URL: %s", url)
    return None


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------

def filter_products(products: list, ids: set[str]) -> list:
    """
    Filter products to those matching the given IDs, then expand to full model groups.

    Step 1: find all products whose 'id' is in ids.
    Step 2: collect their model keys (model field if non-empty, else mpn).
    Step 3: return all products sharing those model keys — so every colour/size
            of a selected product is included and the WooCommerce product is complete.

    Args:
        products: Full list of parsed product dicts from xml_parser.
        ids:      Set of product ID strings extracted from user-provided URLs.

    Returns:
        Filtered list of product dicts (may be larger than len(ids) due to model expansion).
    """
    model_keys: set[str] = set()
    matched_ids: set[str] = set()

    for p in products:
        if p["id"] in ids:
            matched_ids.add(p["id"])
            key = p["model"] or p["mpn"]
            if key:
                model_keys.add(key)

    missing = ids - matched_ids
    if missing:
        logger.warning("Žádný XML produkt nenalezen pro ID: %s", ", ".join(sorted(missing)))

    if not model_keys:
        return []

    result = [p for p in products if (p["model"] or p["mpn"]) in model_keys]
    logger.info(
        "Výběr: %d URL → %d nalezených ID → %d model klíčů → %d B2B produktů",
        len(ids), len(matched_ids), len(model_keys), len(result),
    )
    return result


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_select(
    urls: list[str],
    *,
    dry_run: bool = False,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    skip_on_rate_limit: bool = False,
) -> None:
    """
    Run the sync pipeline for a specific list of B2B product URLs.

    Args:
        urls:                List of b2bsportswholesale.net product URL strings.
        dry_run:             If True, skip all WooCommerce API calls and print a summary.
        source:              XML URL or file path override; uses config default if None.
        limit:               If set, process only the first N product groups (smoke-test helper).
        skip_on_rate_limit:  If True, skip translation immediately on Gemini 429 (returns English).
    """
    if skip_on_rate_limit:
        _config.SKIP_ON_RATE_LIMIT = True
    src = source or XML_SOURCE_URL
    logger.info(
        "Spouštím selektivní sync — %d URL(s)%s",
        len(urls),
        "  [DRY RUN]" if dry_run else "",
    )

    # 1. Extract IDs from URLs
    ids: set[str] = set()
    for url in urls:
        pid = extract_id(url)
        if pid:
            ids.add(pid)

    if not ids:
        logger.error("Z poskytnutých URL nelze extrahovat žádná platná ID.")
        return

    # 2. Parse full XML feed
    all_products = xml_parser.parse(src)
    logger.info("Načteno %d produktů z feedu", len(all_products))

    # 3. Filter to selected products + expand to full model groups
    selected = filter_products(all_products, ids)
    if not selected:
        logger.error("Žádný produkt neodpovídá ID: %s", ", ".join(sorted(ids)))
        return

    # 4. Group
    groups = product_grouper.group(selected)
    logger.info("Seskupeno do %d produktových skupin", len(groups))

    if limit:
        groups = groups[:limit]
        logger.info("Omezeno na prvních %d skupin (--limit)", limit)

    # 5. Dry-run or live upsert
    if dry_run:
        _dry_run_summary(groups)
        return

    from woo_client import WooClient
    from image_uploader import resolve_images

    logger.info("Překládám názvy produktů...")
    translations = {}
    for g in groups:
        logger.info("  Překlad [%s] SKU=%s  %r", g.kind, g.parent_sku, g.name)
        translations[g.parent_sku] = translator.translate(g)

    logger.info("Nahrávám obrázky do GCS...")
    resolve_images(groups, translations)

    _sync_start = time.monotonic()
    with WooClient() as woo:
        for idx, group in enumerate(groups):
            translated = translations[group.parent_sku]
            done = idx + 1
            elapsed = time.monotonic() - _sync_start
            eta_s = (elapsed / done) * (len(groups) - done)
            logger.info(
                "── %d/%d  [%s]  SKU=%s  ETA %dm%02ds",
                done, len(groups), group.kind, group.parent_sku,
                int(eta_s // 60), int(eta_s % 60),
            )
            logger.info("   %r  →  %r", group.name[:65], (translated.name_cs or "–")[:65])
            cat_ids, cat_slug = category_mapper.resolve(group, translated)
            woo.upsert_group(group, translated, category_ids=cat_ids, category_slug=cat_slug,
                             status="draft" if not cat_ids else "publish")
        woo.flush()

    logger.info("Selektivní sync dokončen — %d skupin(a) uložena do WooCommerce", len(groups))


def _dry_run_summary(groups: list) -> None:
    """Print a dry-run summary for selected groups."""
    rate = get_eur_czk_rate()
    print("\n=== DRY RUN — NÁHLED ===")
    print(f"  Skupiny  : {len(groups)}")
    print(f"  EUR/CZK  : {rate:.4f}")

    for g in groups:
        t = translator.translate(g)
        cat_ids, cat_slug = category_mapper.resolve(g, t)
        v = g.variations[0] if g.variations else None
        try:
            weight = float(g.weight) if g.weight else 0.0
        except ValueError:
            weight = 0.0
        czk = calculate_price(v.wholesale_netto, weight) if v else "?"

        print(f"\n  [{g.kind:<12}] {g.name[:60]!r}")
        print(f"    CS   : {(t.name_cs or '—')[:60]!r}")
        print(f"    SKU  : {g.parent_sku}  |  cena: {czk} CZK")
        print(f"    Kat. : {cat_ids}  ({cat_slug!r})")
        print(f"    Var. : {len(g.variations)}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Selektivní B2B → WooCommerce sync")
    p.add_argument(
        "--urls",
        default=DEFAULT_URLS_FILE,
        metavar="SOUBOR",
        help="Cesta k souboru se seznamem URL (výchozí: urls.txt)",
    )
    p.add_argument(
        "--source",
        default=None,
        metavar="URL_NEBO_CESTA",
        help="Přepis XML zdroje; jinak se použije XML_SOURCE_URL z konfigurace",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Zpracuj pouze prvních N skupin (užitečné pro testování)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Přeložit, nacenit, zobrazit náhled — bez volání WooCommerce API",
    )
    p.add_argument(
        "--skip-on-rate-limit",
        action="store_true",
        help="Přeskočit překlad okamžitě při Gemini 429 místo čekání (vrátí anglický text)",
    )
    return p.parse_args()


def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(LOG_DIR, "select_sync.log"), mode="a", encoding="utf-8"
            ),
        ],
    )


if __name__ == "__main__":
    args = _parse_args()
    _setup_logging()
    try:
        urls = load_urls(args.urls)
        if not urls:
            print(
                f"V souboru {args.urls} nejsou žádné URL.\n"
                "Vlož URL produktů (jeden na řádek) a spusť znovu."
            )
            sys.exit(0)
        logger.info("Načteno %d URL z %s", len(urls), args.urls)
        run_select(urls, dry_run=args.dry_run, source=args.source, limit=args.limit,
                   skip_on_rate_limit=args.skip_on_rate_limit)
    except Exception as exc:
        logging.getLogger(__name__).critical("Fatální chyba: %s", exc, exc_info=True)
        sys.exit(1)
