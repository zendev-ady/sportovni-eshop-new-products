"""
product_grouper.py — Groups flat xml_parser output into WooCommerce-ready ProductGroups.

Input:  List[Dict] from xml_parser.parse()
Output: List[ProductGroup] — one per WooCommerce parent product

Grouping key: product["model"] if non-empty, else product["mpn"]

Classification matrix (per group):
  single B2B product  + all N/A sizes  → simple
  multiple B2B products + all N/A sizes → colour_only
  single B2B product  + real sizes     → size_only
  multiple B2B products + real sizes   → colour_size
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

ProductKind = Literal["simple", "colour_only", "size_only", "colour_size"]


@dataclass
class Variation:
    """
    One WooCommerce variation.

    Attributes:
        sku:             item@uid — sacred, never modified
        ean:             item@ean
        quantity:        stock count for this variation
        size_label:      EU size string, or "N/A" for colour-only / simple products
        colour:          value of attrs["Colour"][0] from the parent B2B product;
                         empty string for size-only and simple products
        images:          absolute URLs from the B2B product this item belongs to
        wholesale_netto: raw EUR net price, for pricing stage
    """
    sku: str
    ean: str
    quantity: int
    size_label: str
    colour: str
    images: List[str]
    wholesale_netto: float


@dataclass
class ProductGroup:
    """
    One WooCommerce parent product plus all its variations.

    Attributes:
        parent_sku:    mpn of the first B2B product in the group — used as WC parent SKU
        model:         grouping key (model field, or mpn if model was empty)
        kind:          simple | colour_only | size_only | colour_size
        name:          raw English product name — translator stage will localise this
        description:   raw English HTML description — translator stage will localise this
        producer:      brand name — never translate
        weight:        kg string from first product in group
        category:      B2B category path (e.g. "Footwear/Running/Men")
        created_at:    ISO datetime string or None
        attrs:         merged, deduplicated attribute dict from all B2B products in group
        images:        merged, deduplicated image URLs (parent gallery)
        variations:    list of Variation objects
    """
    parent_sku: str
    model: str
    kind: ProductKind
    name: str
    description: str
    producer: str
    weight: str
    category: str
    created_at: Optional[str]
    attrs: Dict[str, List[str]]
    images: List[str]
    variations: List[Variation] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def group(products: List[Dict]) -> List[ProductGroup]:
    """
    Group and classify flat xml_parser products into ProductGroup dataclasses.

    Args:
        products: List[Dict] as returned by xml_parser.parse().

    Returns:
        List[ProductGroup] — one entry per WooCommerce parent product.
        Products with wholesale_netto == 0 are filtered out before grouping.
        Groups that yield zero variations are skipped with a warning.
    """
    # Business rule: skip zero-price products (data errors in the feed)
    valid = [p for p in products if p["wholesale_netto"] > 0]
    skipped_price = len(products) - len(valid)
    if skipped_price:
        logger.warning("Filtered out %d products with wholesale_netto=0", skipped_price)

    # Bucket by grouping key
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for p in valid:
        key = (p["model"] or "").strip() or p["mpn"]
        buckets[key].append(p)

    logger.info("Grouping %d products into %d model buckets", len(valid), len(buckets))

    result: List[ProductGroup] = []
    skipped_empty = 0

    for key, bucket in buckets.items():
        kind = _determine_kind(bucket)
        variations = _build_variations(bucket, kind)

        if not variations:
            logger.warning(
                "Model '%s' produced 0 variations — skipping (check stock data)", key
            )
            skipped_empty += 1
            continue

        first = bucket[0]
        result.append(
            ProductGroup(
                parent_sku=first["mpn"],
                model=key,
                kind=kind,
                name=first["name"] or "",
                description=first["description"] or "",
                producer=first["producer"] or "",
                weight=first["weight"] or "",
                category=first["category"] or "",
                created_at=first["created_at"],
                attrs=_merge_attrs(bucket),
                images=_merge_images(bucket),
                variations=variations,
            )
        )

    if skipped_empty:
        logger.warning("Skipped %d groups with no variations", skipped_empty)

    logger.info(
        "Produced %d ProductGroups (%d simple, %d colour_only, %d size_only, %d colour_size)",
        len(result),
        sum(1 for g in result if g.kind == "simple"),
        sum(1 for g in result if g.kind == "colour_only"),
        sum(1 for g in result if g.kind == "size_only"),
        sum(1 for g in result if g.kind == "colour_size"),
    )

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _determine_kind(products: List[Dict]) -> ProductKind:
    """
    Classify a group of B2B products sharing the same model.

    Args:
        products: non-empty list of parsed product dicts for one model.

    Returns:
        One of "simple", "colour_only", "size_only", "colour_size".
    """
    has_multiple = len(products) > 1
    has_real_sizes = any(
        item["size_label"] not in ("N/A", "")
        for p in products
        for item in p["stock"]["items"]
    )

    if has_multiple and has_real_sizes:
        return "colour_size"
    if has_multiple:
        return "colour_only"
    if has_real_sizes:
        return "size_only"
    return "simple"


def _build_variations(products: List[Dict], kind: ProductKind) -> List[Variation]:
    """
    Build the flat Variation list for a group.

    Args:
        products: all B2B products belonging to one model group.
        kind:     classification result from _determine_kind.

    Returns:
        List of Variation objects. May be empty if source data has no stock items.
    """
    variations: List[Variation] = []

    if kind in ("simple", "size_only"):
        # Single B2B product — iterate its stock items
        p = products[0]
        colour = ""  # no colour axis on these kinds
        for item in p["stock"]["items"]:
            variations.append(
                Variation(
                    sku=item["uid"],
                    ean=item["ean"],
                    quantity=item["quantity"],
                    size_label=item["size_label"],
                    colour=colour,
                    images=list(p["images"]),
                    wholesale_netto=p["wholesale_netto"],
                )
            )

    elif kind == "colour_only":
        # One variation per B2B product — the single stock item is the variation
        for p in products:
            items = p["stock"]["items"]
            if not items:
                logger.warning(
                    "colour_only product id=%s (model='%s') has no stock items — skipping colour",
                    p["id"],
                    p.get("model") or p["mpn"],
                )
                continue
            colour = (p["attrs"].get("Colour") or [""])[0]
            item = items[0]
            variations.append(
                Variation(
                    sku=item["uid"],
                    ean=item["ean"],
                    quantity=item["quantity"],
                    size_label=item["size_label"],  # typically "N/A"
                    colour=colour,
                    images=list(p["images"]),
                    wholesale_netto=p["wholesale_netto"],
                )
            )

    else:  # colour_size
        # Nested: one colour per B2B product × one size per stock item
        for p in products:
            colour = (p["attrs"].get("Colour") or [""])[0]
            for item in p["stock"]["items"]:
                variations.append(
                    Variation(
                        sku=item["uid"],
                        ean=item["ean"],
                        quantity=item["quantity"],
                        size_label=item["size_label"],
                        colour=colour,
                        images=list(p["images"]),
                        wholesale_netto=p["wholesale_netto"],
                    )
                )

    return variations


def _merge_attrs(products: List[Dict]) -> Dict[str, List[str]]:
    """
    Union all attrs dicts from the group, deduplicating values while preserving order.

    Args:
        products: list of parsed product dicts for one model group.

    Returns:
        Single merged Dict[str, List[str]].
    """
    merged: Dict[str, List[str]] = {}
    for p in products:
        for name, values in p["attrs"].items():
            existing = merged.setdefault(name, [])
            for v in values:
                if v not in existing:
                    existing.append(v)
    return merged


def _merge_images(products: List[Dict]) -> List[str]:
    """
    Merge image URL lists from all products in the group, preserving order
    and deduplicating by URL.

    Args:
        products: list of parsed product dicts for one model group.

    Returns:
        Deduplicated list of absolute image URLs.
    """
    seen: set = set()
    result: List[str] = []
    for p in products:
        for url in p["images"]:
            if url not in seen:
                seen.add(url)
                result.append(url)
    return result
