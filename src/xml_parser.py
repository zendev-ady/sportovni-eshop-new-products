"""
xml_parser.py — B2B XML feed parser.

Input:  URL string or local file path pointing to the SPORTPROFIS B2B XML feed.
Output: List[Dict] — one dict per <product> node, fully parsed, no translation/pricing.

Each returned dict has:
  id            str
  name          str  (raw English CDATA)
  description   str  (raw English HTML CDATA)
  mpn           str
  model         str  (opaque — never translate)
  producer      str  (never translate)
  weight        str
  category      str  (B2B category path, e.g. "Footwear/Football")
  created_at    str | None
  wholesale_netto  float
  attrs         Dict[str, List[str]]   e.g. {"Colour": ["Black"], "Product Type": ["Shoes", "FG - Firm ground"]}
  images        List[str]              absolute URLs
  stock         Dict:
      attribute     str   (e.g. "Size", "Colour", or "")
      total_quantity int
      items         List[Dict]:
          uid           str   (variation SKU — sacred, never modify)
          ean           str
          quantity      int
          size_label    str   (e.g. "XL", "42", "N/A")
"""

import io
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from urllib.request import urlopen

logger = logging.getLogger(__name__)


def parse(source: str) -> List[Dict]:
    """
    Parse the B2B XML feed from a URL or local file path.

    Args:
        source: HTTP(S) URL or absolute/relative path to the XML file.

    Returns:
        List of product dicts as described in the module docstring.

    Raises:
        RuntimeError: if the source cannot be opened or is not valid XML.
    """
    stream = _open_source(source)
    products = _parse_stream(stream)
    logger.info("Parsed %d products from %s", len(products), source)
    return products


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _open_source(source: str):
    """Return a binary file-like object for the given URL or file path."""
    if source.startswith("http://") or source.startswith("https://"):
        try:
            logger.info("Fetching XML from URL: %s", source)
            response = urlopen(source)  # noqa: S310 — URL is from trusted config
            return io.BytesIO(response.read())
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch XML from {source}: {exc}") from exc
    else:
        try:
            return open(source, "rb")
        except OSError as exc:
            raise RuntimeError(f"Failed to open XML file {source}: {exc}") from exc


def _parse_stream(stream) -> List[Dict]:
    """
    Iterate over <product> elements using iterparse to avoid loading the full
    document into memory (feed is >50 MB).
    """
    products = []
    skipped = 0

    context = ET.iterparse(stream, events=("end",))
    for event, elem in context:
        if elem.tag != "product":
            continue

        product = _parse_product(elem)
        elem.clear()  # free memory

        if product is None:
            skipped += 1
            continue

        products.append(product)

    if skipped:
        logger.warning("Skipped %d products due to missing id or mpn", skipped)

    return products


def _parse_product(elem: ET.Element) -> Optional[Dict]:
    """
    Extract all fields from a single <product> element.

    Returns None if the product is missing id or mpn (unrecoverable data error).
    """
    product_id = _text(elem, "id")
    mpn = _text(elem, "mpn")

    if not product_id:
        logger.warning("Product missing <id>, skipping")
        return None
    if not mpn:
        logger.warning("Product id=%s missing <mpn>, skipping", product_id)
        return None

    wholesale_netto = _float(elem, "prices/wholesale_netto")
    if wholesale_netto == 0.0:
        logger.warning("Product id=%s mpn=%s has wholesale_netto=0 (will be skipped by grouper)", product_id, mpn)

    return {
        "id": product_id,
        "name": _text(elem, "name"),
        "description": _text(elem, "description"),
        "mpn": mpn,
        "model": _text(elem, "model"),
        "producer": _text(elem, "producer"),
        "weight": _text(elem, "weight"),
        "category": _text(elem, "category"),
        "created_at": _text(elem, "created_at"),  # None if absent
        "wholesale_netto": wholesale_netto,
        "attrs": _parse_attrs(elem),
        "images": _parse_images(elem),
        "stock": _parse_stock(elem),
    }


def _parse_attrs(elem: ET.Element) -> Dict[str, List[str]]:
    """
    Parse <attrs><attr name="...">value</attr></attrs>.

    Handles:
    - Same attr name appearing multiple times → merged into one list
    - Comma-separated values within a single attr → split and deduplicated
    """
    attrs: Dict[str, List[str]] = {}
    attrs_elem = elem.find("attrs")
    if attrs_elem is None:
        return attrs

    for attr in attrs_elem.findall("attr"):
        name = attr.get("name", "").strip()
        raw_value = (attr.text or "").strip()
        if not name or not raw_value:
            continue

        # Split comma-separated values and strip whitespace
        values = [v.strip() for v in raw_value.split(",") if v.strip()]

        existing = attrs.setdefault(name, [])
        for v in values:
            if v not in existing:
                existing.append(v)

    return attrs


def _parse_images(elem: ET.Element) -> List[str]:
    """
    Parse <images><image>url</image></images>.
    URLs in the feed are already absolute — use as-is.
    """
    images_elem = elem.find("images")
    if images_elem is None:
        return []
    return [
        img.text.strip()
        for img in images_elem.findall("image")
        if img.text and img.text.strip()
    ]


def _parse_stock(elem: ET.Element) -> Dict:
    """
    Parse <stock id="..." attribute="Size" quantity="195">
              <item uid="..." ean="..." quantity="10">XL</item>
           </stock>
    """
    stock_elem = elem.find("stock")
    if stock_elem is None:
        return {"attribute": "", "total_quantity": 0, "items": []}

    items = []
    for item in stock_elem.findall("item"):
        size_label = (item.text or "").strip()
        items.append({
            "uid": item.get("uid", ""),
            "ean": item.get("ean", ""),
            "quantity": int(item.get("quantity", 0)),
            "size_label": size_label if size_label else "N/A",
        })

    return {
        "attribute": stock_elem.get("attribute", ""),
        "total_quantity": int(stock_elem.get("quantity", 0)),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Tiny XML helpers
# ---------------------------------------------------------------------------

def _text(elem: ET.Element, path: str) -> Optional[str]:
    """Return stripped text of a sub-element, or None if absent/empty."""
    node = elem.find(path)
    if node is None or node.text is None:
        return None
    stripped = node.text.strip()
    return stripped if stripped else None


def _float(elem: ET.Element, path: str) -> float:
    """Return float value of a sub-element, or 0.0 if absent/invalid."""
    node = elem.find(path)
    if node is None or node.text is None:
        return 0.0
    try:
        return float(node.text.strip())
    except ValueError:
        return 0.0
