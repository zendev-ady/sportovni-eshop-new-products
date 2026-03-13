"""
woo_client.py — WooCommerce REST API client with rolling batch upsert.

Usage:
    from woo_client import WooClient

    with WooClient() as woo:
        woo.get_eur_czk_rate()          # optional: fail-fast rate check
        for group in groups:
            woo.upsert_group(group)
        woo.flush()                      # send remaining batch
        woo.draft_disappeared(current_parent_skus)

Batching:
    upsert_group() accumulates groups in _pending until WOO_BATCH_SIZE is
    reached, then automatically calls _send_batch(). Call flush() after the
    loop to drain any remainder. Each batch POST handles both creates and
    updates in a single API round-trip.

Error isolation:
    Per-item API errors are logged with SKU and reason — they never raise.
    The run continues for all remaining products (Business Rule #7).
"""

import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import (
    WOO_URL,
    WOO_BATCH_SIZE,
    WOO_SKU_CACHE_DB,
)
from config.api_keys import WOO_CONSUMER_KEY, WOO_CONSUMER_SECRET

from woocommerce import API as WooAPI

from _cache import open_cache, get_id, set_id, get_all_parent_skus
from _payloads import build_parent_payload, build_variation_payload
from product_grouper import ProductGroup

logger = logging.getLogger(__name__)


class WooClient:
    """
    Stateful WooCommerce client for the B2B sync pipeline.

    Maintains:
        - A WooCommerce REST API connection (woocommerce library).
        - An SQLite SKU→ID cache to avoid GET calls on every run.
        - A rolling list of pending groups, flushed in batches.

    Thread safety: not thread-safe — designed for single-threaded pipeline use.
    """

    def __init__(self):
        """
        Initialise API connection and open the SKU cache.

        Raises:
            RuntimeError: if WOO_URL is empty (misconfiguration caught at startup).
        """
        if not WOO_URL:
            raise RuntimeError(
                "WOO_URL is empty — set it in config/config.py before running."
            )

        self._api = WooAPI(
            url=WOO_URL,
            consumer_key=WOO_CONSUMER_KEY,
            consumer_secret=WOO_CONSUMER_SECRET,
            version="wc/v3",
            timeout=30,
        )
        os.makedirs(os.path.dirname(WOO_SKU_CACHE_DB), exist_ok=True)
        self._conn = open_cache(WOO_SKU_CACHE_DB)
        # List of (group, parent_payload, [variation_payloads], category_slug)
        self._pending: list = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Commit the cache and close the SQLite connection."""
        self._conn.commit()
        self._conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_group(
        self,
        group: ProductGroup,
        translated,
        category_ids: list | None = None,
        category_slug: str = "default",
    ) -> None:
        """
        Queue one ProductGroup for upsert. Flushes automatically when the
        rolling buffer reaches WOO_BATCH_SIZE.

        Args:
            group:         ProductGroup from product_grouper.
            translated:    TranslatedGroup with Czech name, descriptions, attrs_cs.
            category_ids:  WooCommerce category IDs; [] until Phase 3.
            category_slug: Category slug for margin lookup in price_calculator.
        """
        if category_ids is None:
            category_ids = []

        parent_wc_id = get_id(self._conn, group.parent_sku)
        parent_payload = build_parent_payload(group, category_ids, translated, wc_id=parent_wc_id)

        variation_payloads = []
        if group.kind != "simple":
            for v in group.variations:
                v_wc_id = get_id(self._conn, v.sku)
                variation_payloads.append(
                    build_variation_payload(v, group, category_slug, wc_id=v_wc_id)
                )

        self._pending.append((group, parent_payload, variation_payloads, category_slug))

        if len(self._pending) >= WOO_BATCH_SIZE:
            self._send_batch()

    def flush(self) -> None:
        """Send all remaining queued groups. Call once after the upsert loop."""
        if self._pending:
            self._send_batch()

    def draft_disappeared(self, current_parent_skus: set) -> int:
        """
        Set WooCommerce status to 'draft' for any parent SKU that was synced
        in a previous run but is absent from the current feed.

        Args:
            current_parent_skus: Set of parent_sku strings from this run's feed.

        Returns:
            Number of products drafted.
        """
        cached = get_all_parent_skus(self._conn)
        gone = cached - current_parent_skus
        if not gone:
            return 0

        logger.info(
            "Drafting %d products that disappeared from the feed", len(gone)
        )

        drafted = 0
        gone_list = list(gone)
        for i in range(0, len(gone_list), WOO_BATCH_SIZE):
            chunk = gone_list[i : i + WOO_BATCH_SIZE]
            updates = []
            for sku in chunk:
                wc_id = get_id(self._conn, sku)
                if wc_id:
                    updates.append({"id": wc_id, "status": "draft"})
                else:
                    logger.warning("draft_disappeared: no cached ID for SKU %s", sku)

            if not updates:
                continue

            try:
                resp = self._api.post("products/batch", {"update": updates}).json()
                drafted += len(resp.get("update", []))
            except Exception as exc:
                logger.error("draft_disappeared batch error: %s", exc)

        self._conn.commit()
        return drafted

    # ------------------------------------------------------------------
    # Private batch machinery
    # ------------------------------------------------------------------

    def _send_batch(self) -> None:
        """
        Flush _pending: POST one products/batch call then one variations/batch
        call per parent. Caches all returned WooCommerce IDs.
        """
        pending = self._pending
        self._pending = []

        creates = [p for p in pending if "id" not in p[1]]
        updates = [p for p in pending if "id" in p[1]]

        batch = {}
        if creates:
            batch["create"] = [p[1] for p in creates]
        if updates:
            batch["update"] = [p[1] for p in updates]

        # Build position→SKU maps from the request payloads.
        # WC never echoes sku in batch error response items, so we recover it by index.
        create_sku_by_idx = {i: p[1].get("sku", "") for i, p in enumerate(creates)}
        update_sku_by_idx = {i: p[1].get("sku", "") for i, p in enumerate(updates)}

        try:
            resp = self._api.post("products/batch", batch).json()
        except Exception as exc:
            logger.error("products/batch request failed: %s — skipping batch", exc)
            return

        # Map returned IDs back to pending items by SKU.
        # Inject SKU by position before processing so _handle_parent_response has it.
        parent_id_map: dict = {}  # sku → wc_id

        resp_creates = resp.get("create", [])
        resp_updates = resp.get("update", [])

        for idx, item in enumerate(resp_creates):
            if not item.get("sku"):
                item["sku"] = create_sku_by_idx.get(idx, "")
            self._handle_parent_response(item, parent_id_map)

        for idx, item in enumerate(resp_updates):
            if not item.get("sku"):
                item["sku"] = update_sku_by_idx.get(idx, "")
            self._handle_parent_response(item, parent_id_map)

        ok_creates = sum(1 for item in resp_creates if not item.get("error"))
        ok_updates = sum(1 for item in resp_updates if not item.get("error"))
        errors_count = sum(1 for item in resp_creates + resp_updates if item.get("error"))
        logger.info(
            "products/batch — created: %d, updated: %d, errors: %d",
            ok_creates, ok_updates, errors_count,
        )

        self._conn.commit()

        # Now send variations for every variable parent that got an ID
        for group, parent_payload, variation_payloads, category_slug in pending:
            if group.kind == "simple" or not variation_payloads:
                continue
            parent_sku = group.parent_sku
            # Prefer freshly returned ID, fall back to cache (already-existing update)
            wc_id = parent_id_map.get(parent_sku) or get_id(self._conn, parent_sku)
            if not wc_id:
                # WC sometimes omits 'sku' in batch response — do a GET lookup.
                logger.warning(
                    "Parent SKU %s not in batch response — falling back to GET lookup",
                    parent_sku,
                )
                wc_id = self._fetch_id_by_sku(parent_sku)
                if wc_id:
                    set_id(self._conn, parent_sku, wc_id)
                    self._conn.commit()
                else:
                    logger.error(
                        "Could not resolve WooCommerce ID for parent SKU %s — skipping its variations",
                        parent_sku,
                    )
                    continue
            self._send_variations_batch(wc_id, group, variation_payloads)

        self._conn.commit()

    def _handle_parent_response(self, item: dict, parent_id_map: dict) -> None:
        """
        Process one item from a products/batch response.

        Caches the returned ID, logs any error. On 'already exists' error,
        attempts a lookup-and-cache via GET so the next run sees it.

        Args:
            item:           One entry from resp['create'] or resp['update'].
            parent_id_map:  Mutable dict populated with sku→wc_id for this batch.
        """
        error = item.get("error")
        sku = item.get("sku", "")

        if error:
            code = error.get("code", "")
            msg = error.get("message", "")
            is_duplicate = (
                "already exists" in msg.lower()
                or "duplicitní" in msg.lower()
                or code == "product_invalid_sku"
            )
            if is_duplicate:
                # Product is in WC but not in our cache — fetch and cache its ID.
                if sku:
                    wc_id = self._fetch_id_by_sku(sku)
                    if wc_id:
                        set_id(self._conn, sku, wc_id)
                        parent_id_map[sku] = wc_id
                else:
                    logger.warning("Duplicate SKU error but sku unknown — cannot recover")
            else:
                logger.error("Parent SKU %s error: [%s] %s", sku, code, msg)
            return

        wc_id = item.get("id")
        if wc_id and sku:
            set_id(self._conn, sku, wc_id)
            parent_id_map[sku] = wc_id
        elif wc_id and not sku:
            logger.warning(
                "WooCommerce returned product id=%d with no sku — cannot cache; "
                "variations will fall back to GET lookup",
                wc_id,
            )

    def _send_variations_batch(
        self,
        parent_wc_id: int,
        group: ProductGroup,
        variation_payloads: list,
    ) -> None:
        """
        POST one variations/batch call for a single parent product.

        Splits payloads into create/update by cache lookup, sends the batch,
        and caches all returned variation IDs.

        Args:
            parent_wc_id:       WooCommerce ID of the parent product.
            group:              ProductGroup (for parent_sku reference in cache).
            variation_payloads: List of variation payloads from build_variation_payload.
        """
        creates = [p for p in variation_payloads if "id" not in p]
        updates = [p for p in variation_payloads if "id" in p]

        batch = {}
        if creates:
            batch["create"] = creates
        if updates:
            batch["update"] = updates

        if not batch:
            return

        endpoint = f"products/{parent_wc_id}/variations/batch"
        try:
            resp = self._api.post(endpoint, batch).json()
        except Exception as exc:
            logger.error(
                "variations/batch failed for parent %s (wc_id=%d): %s",
                group.parent_sku, parent_wc_id, exc,
            )
            return

        for item in resp.get("create", []) + resp.get("update", []):
            error = item.get("error")
            sku = item.get("sku", "")
            if error:
                code = error.get("code", "")
                msg = error.get("message", "")
                if "already exists" in msg.lower() or code == "product_invalid_sku":
                    wc_id = self._fetch_variation_id_by_sku(parent_wc_id, sku)
                    if wc_id:
                        set_id(self._conn, sku, wc_id, parent_sku=group.parent_sku)
                else:
                    logger.error(
                        "Variation SKU %s (parent %s) error: [%s] %s",
                        sku, group.parent_sku, code, msg,
                    )
                continue
            wc_id = item.get("id")
            if wc_id and sku:
                set_id(self._conn, sku, wc_id, parent_sku=group.parent_sku)

    def _fetch_id_by_sku(self, sku: str) -> int | None:
        """
        GET /products?sku={sku} and return the WooCommerce product ID.
        Used as fallback when a create fails with 'already exists'.

        Args:
            sku: Product SKU to look up.

        Returns:
            WooCommerce product ID, or None on failure.
        """
        return self._get_first_id("products", {"sku": sku}, f"GET products?sku={sku}")

    def _fetch_variation_id_by_sku(
        self, parent_wc_id: int, sku: str
    ) -> int | None:
        """
        GET /products/{parent_id}/variations?sku={sku} and return the variation ID.
        Used as fallback when a variation create fails with 'already exists'.

        Args:
            parent_wc_id: WooCommerce ID of the parent product.
            sku:          Variation SKU to look up.

        Returns:
            WooCommerce variation ID, or None on failure.
        """
        return self._get_first_id(
            f"products/{parent_wc_id}/variations",
            {"sku": sku},
            f"GET variations?sku={sku} (parent wc_id={parent_wc_id})",
        )

    def _get_first_id(self, endpoint: str, params: dict, label: str) -> int | None:
        """
        GET the given endpoint with params and return the 'id' of the first result.

        Shared by _fetch_id_by_sku and _fetch_variation_id_by_sku to avoid
        duplicated try/except boilerplate.

        Args:
            endpoint: WooCommerce REST API endpoint (e.g. "products").
            params:   Query params dict (e.g. {"sku": "ABC123"}).
            label:    Human-readable description for error logging.

        Returns:
            Integer WooCommerce ID, or None if not found or on any error.
        """
        try:
            results = self._api.get(endpoint, params=params).json()
            if results and isinstance(results, list):
                return results[0].get("id")
        except Exception as exc:
            logger.error("%s failed: %s", label, exc)
        return None
