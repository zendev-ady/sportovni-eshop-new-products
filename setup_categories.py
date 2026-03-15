"""
setup_categories.py — Automatické vytvoření chybějících WooCommerce kategorií.

Skript přečte stromovou strukturu kategorií z CategoryMapper, porovná ji s aktuálním
stavem WooCommerce (via REST API) a vytvoří vše co chybí.  Po dokončení přepíše
category_ids.json s reálnými ID z produkčního obchodu.

Idempotentní — bezpečné spustit vícekrát.  Přeskočí kategorie, které už v WC existují.

Input:
    Živý WooCommerce REST API (config.WOO_URL + api_keys.py)
    config/category_ids.json (aktuální stav, i s ID=0)

Output:
    config/category_ids.json (přepsaný reálnými ID)
    stdout — přehled: nalezeno / vytvořeno / stále chybí

Spuštění:
    cd b2b_to_woocommerce
    python setup_categories.py --dry-run   # zobrazí plán bez volání API
    python setup_categories.py             # vytvoří kategorie a přepíše JSON
"""

import argparse
import json
import logging
import os
import sys
import time
import unicodedata

# ---------------------------------------------------------------------------
# Path setup — allow running from b2b_to_woocommerce/ directly
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))

from woocommerce import API as WooAPI

from config.config import WOO_URL, _CATEGORY_IDS_PATH
from config.api_keys import WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET
from src.category_mapper import CategoryMapper

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("setup_categories")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    """
    Derive a plain ASCII slug from a Czech category name.

    Used as a fallback comparison when exact name.lower() match fails
    (e.g. encoding edge cases with diacritics on some systems).

    Args:
        name: Czech category name, e.g. "Házená".

    Returns:
        Lowercase ASCII string, e.g. "hazena".
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_bytes = nfkd.encode("ascii", "ignore")
    return ascii_bytes.decode("ascii").lower()


def fetch_all_wc_categories(api) -> dict[int, dict]:
    """
    Fetch all WooCommerce product categories, handling pagination.

    Args:
        api: WooAPI instance.

    Returns:
        dict mapping WC category ID → {id, parent_id, name, slug}.

    Raises:
        RuntimeError: if any API page request returns an unexpected response.
    """
    all_cats: dict[int, dict] = {}
    page = 1
    while True:
        resp = api.get("products/categories", params={"per_page": 100, "page": page})
        if resp.status_code != 200:
            raise RuntimeError(
                f"GET products/categories page {page} failed: "
                f"HTTP {resp.status_code} — {resp.text[:200]}"
            )
        data = resp.json()
        if not data:
            break
        for cat in data:
            all_cats[cat["id"]] = {
                "id":        cat["id"],
                "parent_id": cat["parent"],
                "name":      cat["name"],
                "slug":      cat["slug"],
            }
        if len(data) < 100:
            break
        page += 1
    logger.info("Fetched %d WC categories (pages: %d)", len(all_cats), page)
    return all_cats


def find_wc_id_by_name_and_parent(
    wc_cats: dict[int, dict],
    name: str,
    parent_id: int,
) -> int | None:
    """
    Find an existing WooCommerce category by name and parent ID.

    Matching strategy (in order):
    1. Exact name.lower() + parent_id match.
    2. Slug fallback — compare _slugify(name) against WC slug.  Handles
       diacritics edge cases (e.g. "Házená" → slug "hazena").

    Args:
        wc_cats:   Dict of WC categories (id → {id, parent_id, name, slug}).
        name:      Czech category name to look for.
        parent_id: Expected WC parent ID (0 for top-level).

    Returns:
        WC category ID if found, None otherwise.
    """
    name_lower = name.lower()
    name_slug  = _slugify(name)

    for cat in wc_cats.values():
        if cat["parent_id"] != parent_id:
            continue
        if cat["name"].lower() == name_lower:
            return cat["id"]
        if cat["slug"] == name_slug:
            return cat["id"]

    return None


def extract_desired_paths(structure: dict, parent_path: str = "") -> list[str]:
    """
    Recursively extract all category paths from a CategoryMapper structure dict.

    Returns paths in top-down order (parents before their children) so creation
    can proceed without forward-reference problems.

    Args:
        structure:   Nested dict from CategoryMapper._define_category_structure().
        parent_path: Accumulated path string (used in recursion, empty for top level).

    Returns:
        Ordered list of path strings, e.g.
        ["Sporty", "Sporty > Fotbal", "Sporty > Fotbal > Kopačky", ...].
    """
    paths: list[str] = []
    for name in structure:
        current = f"{parent_path} > {name}" if parent_path else name
        paths.append(current)
        sub = structure[name].get("subcategories", {})
        if sub:
            paths.extend(extract_desired_paths(sub, current))
    return paths


def create_wc_category(
    api,
    name: str,
    parent_id: int,
    dry_run: bool = False,
) -> int:
    """
    Create a new WooCommerce product category.

    Args:
        api:       WooAPI instance.
        name:      Czech category name (e.g. "Volejbal").
        parent_id: WC ID of the parent category (0 for top-level).
        dry_run:   If True, skip the actual API call and return 0.

    Returns:
        New WooCommerce category ID, or 0 in dry-run mode.

    Raises:
        RuntimeError: if WooCommerce returns an error response (non-dry-run only).
    """
    if dry_run:
        logger.info("[dry-run] WOULD CREATE: %r (parent_id=%d)", name, parent_id)
        return 0

    payload = {"name": name, "parent": parent_id}
    resp = api.post("products/categories", payload)
    data = resp.json()
    if "id" not in data:
        raise RuntimeError(
            f"WC create category failed for {name!r} (parent={parent_id}): {data}"
        )
    new_id = data["id"]
    logger.info("Created: %r → ID %d (parent_id=%d)", name, new_id, parent_id)
    return new_id


def main(dry_run: bool = False) -> None:
    """
    Orchestrate category sync: fetch WC state, compare with desired tree,
    create missing categories, and rewrite category_ids.json.

    Args:
        dry_run: Print plan but do not POST to WooCommerce or modify JSON.
    """
    # ------------------------------------------------------------------
    # 1. Init API
    # ------------------------------------------------------------------
    api = WooAPI(
        url=WOO_URL,
        consumer_key=WOO_CONSUMER_KEY,
        consumer_secret=WOO_CONSUMER_SECRET,
        wp_api=True,
        version="wc/v3",
        timeout=30,
    )

    # ------------------------------------------------------------------
    # 2. Load current category_ids.json → id_map (path → int)
    # ------------------------------------------------------------------
    ids_path = os.path.join(os.path.dirname(__file__), _CATEGORY_IDS_PATH)
    with open(ids_path, encoding="utf-8") as f:
        raw_json = json.load(f)
    # Strip _comment key — it's metadata, not a category path
    id_map: dict[str, int] = {k: v for k, v in raw_json.items() if not k.startswith("_")}
    logger.info("Loaded %d paths from category_ids.json", len(id_map))

    # ------------------------------------------------------------------
    # 3. Fetch all current WC categories
    # ------------------------------------------------------------------
    wc_cats = fetch_all_wc_categories(api)

    # ------------------------------------------------------------------
    # 4. Build desired path list from CategoryMapper
    # ------------------------------------------------------------------
    mapper = CategoryMapper()
    structure = mapper._define_category_structure()
    desired_paths = extract_desired_paths(structure)
    logger.info("Desired paths from CategoryMapper: %d", len(desired_paths))

    # ------------------------------------------------------------------
    # 5. Process each path in top-down order
    # ------------------------------------------------------------------
    n_found    = 0
    n_created  = 0
    n_skipped  = 0  # already had non-zero ID

    for path in desired_paths:
        parts     = path.split(" > ")
        leaf_name = parts[-1]
        parent_path = " > ".join(parts[:-1])

        # a) Already has a real ID in id_map — skip
        if id_map.get(path, 0) != 0:
            n_skipped += 1
            logger.debug("[=] already has ID %d: %r", id_map[path], path)
            continue

        # b) Determine expected parent WC ID
        if parent_path:
            parent_wc_id = id_map.get(parent_path, 0)
            if parent_wc_id == 0:
                logger.warning(
                    "[!] parent %r has ID=0 — cannot create %r, skipping",
                    parent_path, path,
                )
                continue
        else:
            parent_wc_id = 0  # top-level category

        # c) Check if WC already has this category (by name + parent)
        existing_id = find_wc_id_by_name_and_parent(wc_cats, leaf_name, parent_wc_id)
        if existing_id:
            id_map[path] = existing_id
            n_found += 1
            logger.info("[~] found in WC: %r → ID %d", path, existing_id)
            continue

        # d) Create
        new_id = create_wc_category(api, leaf_name, parent_wc_id, dry_run=dry_run)
        if new_id:
            id_map[path] = new_id
            # Add to local wc_cats so children can find their parent immediately
            wc_cats[new_id] = {
                "id": new_id, "parent_id": parent_wc_id,
                "name": leaf_name, "slug": _slugify(leaf_name),
            }
            n_created += 1
        # If dry_run, new_id=0 — do not update id_map so the JSON stays clean

        if not dry_run:
            time.sleep(0.3)  # avoid hammering the WC API

    # ------------------------------------------------------------------
    # 6. Rewrite category_ids.json (unless dry-run)
    # ------------------------------------------------------------------
    n_still_zero = sum(1 for v in id_map.values() if v == 0)

    if not dry_run:
        output = {"_comment": raw_json.get("_comment", "")}
        output.update(id_map)
        with open(ids_path, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        logger.info("category_ids.json updated.")
    else:
        logger.info("[dry-run] category_ids.json NOT modified.")

    # ------------------------------------------------------------------
    # 7. Summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 60)
    print(f"  Kategorie — přehled ({'' if not dry_run else 'DRY RUN'})")
    print("=" * 60)
    print(f"  Již měly ID (přeskočeny): {n_skipped}")
    print(f"  Nalezeny v WC:            {n_found}")
    print(f"  Vytvořeny:                {n_created}")
    print(f"  Stále ID=0:               {n_still_zero}")
    print("=" * 60)

    if n_still_zero > 0:
        print("\nKategorie stále bez ID (ID=0):")
        for path, wc_id in id_map.items():
            if wc_id == 0:
                print(f"  - {path}")


if __name__ == "__main__":
    # Fix Windows terminal encoding
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="Vytvoří chybějící WooCommerce kategorie a aktualizuje category_ids.json."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Zobrazí plán bez volání POST API a bez modifikace JSON.",
    )
    args = parser.parse_args()

    main(dry_run=args.dry_run)
