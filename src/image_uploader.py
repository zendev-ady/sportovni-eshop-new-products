"""
image_uploader.py — Lazy GCS image mirror for the B2B sync pipeline.

For each image URL in the feed, resolves it to a stable GCS public URL:
  - Cache hit  → return cached GCS URL immediately (no network I/O)
  - Cache miss → download from supplier, upload to GCS, cache the mapping

Public API
----------
resolve_images(groups)
    Mutates all group.images and variation.images in-place, replacing
    b2b supplier URLs with GCS public URLs. Images that fail to upload
    are left as the original b2b URL (warn + continue — never aborts run).

open_image_cache(path) / close_image_cache(conn)
    Low-level SQLite helpers (used by resolve_images internally).

Prerequisites
-------------
- google-cloud-storage installed:  pip install google-cloud-storage
- GCS_SERVICE_ACCOUNT_JSON points to a valid service account key file.
- Bucket objects must be publicly readable (allUsers → Storage Object Viewer).
"""

import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.config import (
    GCS_BUCKET_NAME,
    GCS_IMAGE_PREFIX,
    GCS_PUBLIC_BASE,
    GCS_SERVICE_ACCOUNT_JSON,
    GCS_IMAGE_CACHE_DB,
)
from product_grouper import ProductGroup

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite image cache
# ---------------------------------------------------------------------------

def open_image_cache(path: str) -> sqlite3.Connection:
    """Open (or create) the image cache DB. Returns an open connection."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS image_cache (
            original_url TEXT PRIMARY KEY,
            gcs_url      TEXT NOT NULL,
            uploaded_at  TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _get_cached(conn: sqlite3.Connection, original_url: str) -> str | None:
    row = conn.execute(
        "SELECT gcs_url FROM image_cache WHERE original_url = ?", (original_url,)
    ).fetchone()
    return row[0] if row else None


def _set_cached(conn: sqlite3.Connection, original_url: str, gcs_url: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO image_cache (original_url, gcs_url, uploaded_at)
        VALUES (?, ?, ?)
        """,
        (original_url, gcs_url, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

_INVALID_CHARS = re.compile(r'[^a-zA-Z0-9._\-]')


def _gcs_filename(url: str, sku: str, index: int) -> str:
    """
    Derive a stable, collision-free GCS blob name.

    Format: {sku}-{index}{ext}  e.g. "015110-0.jpg", "uid9A3F-2.png"
    sku is sanitized to filesystem-safe chars.
    Falls back to md5(url)[:16]{ext} only when sku is empty.
    """
    _, ext = os.path.splitext(os.path.basename(urlparse(url).path))
    if ext.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        ext = ".jpg"

    if sku:
        safe_sku = _INVALID_CHARS.sub("_", sku).strip("_")[:80]
        return f"{safe_sku}-{index}{ext}"

    return hashlib.md5(url.encode()).hexdigest()[:16] + ext


# ---------------------------------------------------------------------------
# Core upload logic
# ---------------------------------------------------------------------------

def _upload_to_gcs(original_url: str, bucket, sku: str, index: int) -> str | None:
    """
    Download image from original_url and upload it to GCS.

    Args:
        original_url: Absolute supplier image URL.
        bucket:       google.cloud.storage.Bucket instance.
        sku:          Product or variation SKU — used in the GCS filename.
        index:        Image index within the product (0 = first/main image).

    Returns:
        Public GCS URL string, or None on any error.
    """
    try:
        resp = requests.get(original_url, stream=True, timeout=30)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if not content_type.startswith("image"):
            logger.warning("Skipping non-image URL %s (Content-Type: %s)", original_url, content_type)
            return None

        image_data = resp.content
    except requests.RequestException as exc:
        logger.error("Failed to download image %s: %s", original_url, exc)
        return None

    filename = _gcs_filename(original_url, sku, index)
    blob_name = GCS_IMAGE_PREFIX + filename
    blob = bucket.blob(blob_name)

    try:
        blob.upload_from_string(image_data, content_type=content_type)
    except Exception as exc:
        logger.error("Failed to upload %s to GCS: %s", blob_name, exc)
        return None

    gcs_url = GCS_PUBLIC_BASE + filename
    logger.debug("Uploaded %s → %s", original_url, gcs_url)
    return gcs_url


def _resolve_one_stats(
    url: str, conn: sqlite3.Connection, bucket, sku: str, index: int
) -> tuple[str, int, int]:
    """
    Resolve a single URL.

    Returns:
        (resolved_url, was_cache_hit: 0|1, upload_ok: 0|1)
        On failure resolved_url is the original URL and upload_ok is 0.
    """
    cached = _get_cached(conn, url)
    if cached:
        return cached, 1, 1

    gcs_url = _upload_to_gcs(url, bucket, sku, index)
    if gcs_url:
        _set_cached(conn, url, gcs_url)
        return gcs_url, 0, 1

    logger.warning("Image upload failed, keeping original URL: %s", url)
    return url, 0, 0

def resolve_images(groups: list[ProductGroup]) -> None:
    """
    Replace all b2b supplier image URLs in-place with GCS public URLs.

    Lazy: only downloads + uploads images not already in the cache.
    On per-image failure: logs a warning, keeps original URL, continues.

    Args:
        groups: List[ProductGroup] — mutated in-place.
    """
    from google.cloud import storage  # deferred — optional dep in dry-run

    os.makedirs(os.path.dirname(GCS_IMAGE_CACHE_DB), exist_ok=True)
    conn = open_image_cache(GCS_IMAGE_CACHE_DB)

    client = storage.Client.from_service_account_json(GCS_SERVICE_ACCOUNT_JSON)
    bucket = client.bucket(GCS_BUCKET_NAME)

    total = hits = uploads = errors = 0

    try:
        for group in groups:
            resolved = []
            for i, url in enumerate(group.images):
                gcs_url, was_hit, ok = _resolve_one_stats(url, conn, bucket, group.parent_sku, i)
                resolved.append(gcs_url)
                total += 1
                hits += was_hit
                uploads += (not was_hit and ok)
                errors += (not ok)
            group.images = resolved

            for v in group.variations:
                resolved = []
                for i, url in enumerate(v.images):
                    gcs_url, was_hit, ok = _resolve_one_stats(url, conn, bucket, v.sku, i)
                    resolved.append(gcs_url)
                    total += 1
                    hits += was_hit
                    uploads += (not was_hit and ok)
                    errors += (not ok)
                v.images = resolved
    finally:
        conn.commit()
        conn.close()

    logger.info(
        "Image resolution complete — %d total, %d cache hits, %d uploaded, %d errors",
        total, hits, uploads, errors,
    )
