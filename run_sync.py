#!/usr/bin/env python3
"""
run_sync.py — B2B → WooCommerce daily sync pipeline entry point.

Usage:
    python run_sync.py                             # full live sync (live WC calls)
    python run_sync.py --dry-run                   # parse + group + price, no WC calls
    python run_sync.py --source feed.xml           # override XML source (URL or file)
    python run_sync.py --dry-run --limit 20        # first 20 groups only

Exit codes:
    0 — completed (per-product errors are logged, not raised)
    1 — fatal (XML unreachable, WooClient misconfiguration, unexpected crash)
"""

import argparse
import logging
import os
import sys
from collections import Counter

# Ensure Czech characters print correctly on Windows terminals.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make src/ and config/ importable from this root directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                              # → config.config, config.api_keys
sys.path.insert(0, os.path.join(_HERE, "src"))         # → xml_parser, product_grouper, …

import xml_parser
import product_grouper
import translator
import category_mapper
from price_calculator import calculate_price, get_eur_czk_rate
from config.config import XML_SOURCE_URL, LOG_DIR

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="B2B → WooCommerce sync pipeline")
    p.add_argument(
        "--source",
        default=None,
        metavar="URL_OR_PATH",
        help="XML URL or local file path; overrides XML_SOURCE_URL from config",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N product groups (useful for smoke tests)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse, group and price without making any WooCommerce API calls",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                os.path.join(LOG_DIR, "sync.log"), mode="a", encoding="utf-8"
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Dry-run summary
# ---------------------------------------------------------------------------

def _dry_run_summary(groups: list, limit: int | None) -> None:
    counts = Counter(g.kind for g in groups)
    total_vars = sum(len(g.variations) for g in groups)

    rate = get_eur_czk_rate()   # CNB fetch or fallback — acceptable in dry-run

    print("\n=== DRY RUN SUMMARY ===")
    print(f"  Groups total   : {len(groups)}" + (f"  (--limit {limit})" if limit else ""))
    print(f"  Variations     : {total_vars}")
    print(f"  simple         : {counts['simple']}")
    print(f"  colour_only    : {counts['colour_only']}")
    print(f"  size_only      : {counts['size_only']}")
    print(f"  colour_size    : {counts['colour_size']}")
    print(f"\n  EUR/CZK rate   : {rate:.4f}")

    print("\n  Sample prices — first 3 groups, first variation each:")
    for g in groups[:3]:
        if not g.variations:
            continue
        v = g.variations[0]
        try:
            weight = float(g.weight) if g.weight else 0.0
        except ValueError:
            weight = 0.0
        czk = calculate_price(v.wholesale_netto, weight)
        print(
            f"    [{g.kind:<12}] {g.name[:45]!r:50s}"
            f"  {v.wholesale_netto:>7.2f} EUR  ->  {czk:>6} CZK"
        )
    print()
    print("\n  Translation preview — first 3 groups (Czech name vs original):")
    for g in groups[:3]:
        t = translator.translate(g)
        print(f"    EN: {g.name[:65]!r}")
        print(f"    CS: {t.name_cs[:65]!r}")
        if t.attrs_cs:
            attrs_preview = ", ".join(
                f"{k}={v[0]!r}" for k, v in list(t.attrs_cs.items())[:4]
            )
            print(f"    attrs_cs: {attrs_preview}")
        print()



def main() -> None:
    args = _parse_args()
    _setup_logging()

    source = args.source or XML_SOURCE_URL
    logger.info(
        "Starting sync — source: %s%s",
        source,
        "  [DRY RUN]" if args.dry_run else "",
    )

    # 1. Parse
    products = xml_parser.parse(source)
    logger.info("Parsed %d products from feed", len(products))

    # 2. Group
    groups = product_grouper.group(products)
    kind_counts = Counter(g.kind for g in groups)
    logger.info(
        "Grouped into %d product groups (%s)",
        len(groups),
        ", ".join(f"{k}: {v}" for k, v in sorted(kind_counts.items())),
    )

    # 3. Apply --limit
    if args.limit:
        groups = groups[: args.limit]
        logger.info("Limiting to first %d groups via --limit", args.limit)

    # 4a. Dry-run — print summary and stop
    if args.dry_run:
        _dry_run_summary(groups, limit=args.limit)
        logger.info("Dry run complete — no WooCommerce calls made")
        return

    # 4b. Live upsert
    from woo_client import WooClient  # deferred — requires woocommerce package + WOO_URL
    from image_uploader import resolve_images

    logger.info("Translating product names for image filenames...")
    translations = {}
    for g in groups:
        logger.info("  Translating [%s] SKU=%s  %r", g.kind, g.parent_sku, g.name)
        translations[g.parent_sku] = translator.translate(g)

    logger.info("Resolving images to GCS...")
    resolve_images(groups, translations)

    current_skus = {g.parent_sku for g in groups}

    with WooClient() as woo:
        for group in groups:
            translated = translations[group.parent_sku]
            logger.info(
                "  Upserting [%s] SKU=%s  %r → %r",
                group.kind, group.parent_sku, group.name, translated.name_cs,
            )
            # Phase 3: resolve WooCommerce category IDs and margin slug
            cat_ids, cat_slug = category_mapper.resolve(group, translated)
            woo.upsert_group(group, translated, category_ids=cat_ids, category_slug=cat_slug)
        woo.flush()
        drafted = woo.draft_disappeared(current_skus) if not args.limit else 0
        if args.limit:
            logger.info("Skipping draft_disappeared — --limit is active")

    logger.info(
        "Sync complete — %d groups queued for upsert, %d products drafted",
        len(groups),
        drafted,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logging.getLogger(__name__).critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
