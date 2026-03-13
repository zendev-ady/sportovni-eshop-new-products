"""
cache_ops.py — Stats and clear operations for the three pipeline SQLite caches.

Input:  cache_dir (str) — path to b2b_to_woocommerce/cache/
Output: list of stats dicts, or row count on clear

Public API:
    get_stats(cache_dir)       list[dict]  stats for all 3 caches
    clear(cache_dir, name)     int         rows deleted from named cache
"""

import os
import sqlite3

_CACHES = {
    "sku":          ("sku_cache.db",    "sku_cache",    "SKU cache"),
    "translations": ("translations.db", "translations", "Translation cache"),
    "images":       ("image_cache.db",  "image_cache",  "Image cache"),
}


def get_stats(cache_dir: str) -> list:
    """Return stats for all three caches.

    Args:
        cache_dir: absolute path to the cache/ directory

    Returns:
        List of dicts with keys:
            name, label, rows, size_bytes, size_human, exists
    """
    result = []
    for name, (filename, table, label) in _CACHES.items():
        path = os.path.join(cache_dir, filename)
        if not os.path.exists(path):
            result.append({
                "name": name,
                "label": label,
                "rows": 0,
                "size_bytes": 0,
                "size_human": "0 B",
                "exists": False,
            })
            continue

        size = os.path.getsize(path)
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.close()
        except Exception:
            count = 0

        result.append({
            "name": name,
            "label": label,
            "rows": count,
            "size_bytes": size,
            "size_human": _human_size(size),
            "exists": True,
        })
    return result


def clear(cache_dir: str, name: str) -> int:
    """Delete all rows from the named cache and VACUUM.

    Args:
        cache_dir: absolute path to the cache/ directory
        name:      one of 'sku', 'translations', 'images'

    Returns:
        Number of rows deleted.

    Raises:
        ValueError: if name is not a known cache.
    """
    if name not in _CACHES:
        raise ValueError(f"Unknown cache name: {name!r}. Valid: {list(_CACHES)}")

    filename, table, _ = _CACHES[name]
    path = os.path.join(cache_dir, filename)
    if not os.path.exists(path):
        return 0

    conn = sqlite3.connect(path)
    cur = conn.execute(f"DELETE FROM {table}")
    deleted = cur.rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return deleted


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
