"""
cache_ops.py — Cache statistics and smart invalidation operations.

Input:
    cache_dir (str) path to b2b_to_woocommerce/cache/

Output:
    Cache stats, impact previews, delete counts, and audit rows.

Public API:
    get_stats(cache_dir)                              list[dict]
    preview_action(cache_dir, name, action, **kwargs) dict
    clear(cache_dir, name, operator, source)         int
    clear_ttl(cache_dir, name, days, operator, source) int
    clear_prefix(cache_dir, name, prefix, operator, source) int
    get_audit(cache_dir, limit)                       list[dict]
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone


class CacheOperationError(ValueError):
    """Raised when a cache operation cannot be safely applied."""

_CACHES = {
    "sku":          ("sku_cache.db",    "sku_cache",    "SKU cache"),
    "translations": ("translations.db", "translations", "Translation cache"),
    "images":       ("image_cache.db",  "image_cache",  "Image cache"),
}

_AUDIT_DB = "cache_audit.db"

_TTL_COLUMNS = {
    "images": ["uploaded_at"],
    "translations": ["updated_at", "created_at"],
    "sku": ["updated_at", "created_at"],
}


def preview_action(cache_dir: str, name: str, action: str, **kwargs) -> dict:
    """Estimate impact of a cache action without mutating data.

    Args:
        cache_dir: absolute path to the cache/ directory.
        name: one of 'sku', 'translations', 'images'.
        action: 'clear' | 'ttl' | 'prefix'.
        kwargs: action-specific values (days for ttl, prefix for prefix).

    Returns:
        Dict with keys:
            action, cache, rows_affected, size_human, size_bytes,
            estimated_ai_calls, estimated_cost_czk, warning

    Raises:
        CacheOperationError: unknown cache/action or invalid arguments.
    """
    _ensure_cache(name)

    path = _cache_path(cache_dir, name)
    table = _CACHES[name][1]
    size_bytes = os.path.getsize(path) if os.path.exists(path) else 0
    warning = ""

    if action == "clear":
        rows = _count_rows(path, table)
    elif action == "ttl":
        days = kwargs.get("days")
        if days is None:
            raise CacheOperationError("Missing 'days' for TTL preview")
        rows = _count_ttl_rows(path, name, int(days))
        if rows == 0:
            warning = "Žádné záznamy neodpovídají TTL podmínce."
    elif action == "prefix":
        prefix = (kwargs.get("prefix") or "").strip()
        if not prefix:
            raise CacheOperationError("Prefix nesmí být prázdný")
        rows = _count_prefix_rows(path, name, prefix)
        if rows == 0:
            warning = "Prefix nic nenašel; akce pravděpodobně nic nesmaže."
    else:
        raise CacheOperationError(f"Unknown preview action: {action!r}")

    est_ai_calls = rows if name == "translations" else 0
    est_cost = round(est_ai_calls * 0.08, 2)  # konzervativní orientační odhad
    if name == "translations" and action == "clear" and rows > 0:
        warning = warning or "Po vymazání překladové cache porostou AI náklady při dalším běhu."

    return {
        "action": action,
        "cache": name,
        "rows_affected": rows,
        "size_bytes": size_bytes,
        "size_human": _human_size(size_bytes),
        "estimated_ai_calls": est_ai_calls,
        "estimated_cost_czk": est_cost,
        "warning": warning,
    }


def get_stats(cache_dir: str) -> list:
    """Return stats for all three caches.

    Args:
        cache_dir: absolute path to the cache/ directory

    Returns:
        List of dicts with keys:
            name, label, rows, size_bytes, size_human, exists,
            oldest_ts, newest_ts, ttl_supported
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
                "oldest_ts": None,
                "newest_ts": None,
                "ttl_supported": False,
            })
            continue

        size = os.path.getsize(path)
        oldest_ts = None
        newest_ts = None
        ttl_supported = False
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

            ts_col = _pick_existing_column(conn, table, _TTL_COLUMNS.get(name, []))
            if ts_col:
                ttl_supported = True
                oldest_ts = _safe_scalar(conn, f"SELECT MIN({ts_col}) FROM {table}")
                newest_ts = _safe_scalar(conn, f"SELECT MAX({ts_col}) FROM {table}")

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
            "oldest_ts": oldest_ts,
            "newest_ts": newest_ts,
            "ttl_supported": ttl_supported,
        })
    return result


def clear(cache_dir: str, name: str, operator: str = "unknown", source: str = "ui") -> int:
    """Delete all rows from the named cache and VACUUM.

    Args:
        cache_dir: absolute path to the cache/ directory
        name:      one of 'sku', 'translations', 'images'

    Returns:
        Number of rows deleted.

    Raises:
        CacheOperationError: if name is not a known cache.
    """
    _ensure_cache(name)

    filename, table, _ = _CACHES[name]
    path = os.path.join(cache_dir, filename)
    if not os.path.exists(path):
        _audit(cache_dir, name=name, action="clear", operator=operator, source=source, rows_deleted=0, details="db_missing")
        return 0

    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(f"DELETE FROM {table}")
        deleted = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    _audit(cache_dir, name=name, action="clear", operator=operator, source=source, rows_deleted=deleted, details="full_table")
    return deleted


def clear_ttl(
    cache_dir: str,
    name: str,
    days: int,
    operator: str = "unknown",
    source: str = "ui",
) -> int:
    """Delete cache records older than N days.

    Args:
        cache_dir: absolute path to the cache/ directory.
        name: one of 'sku', 'translations', 'images'.
        days: positive number of days. Older rows are deleted.
        operator: user/IP that triggered the action.
        source: caller origin, default 'ui'.

    Returns:
        Number of rows deleted.

    Raises:
        CacheOperationError: invalid cache name, days, or unsupported table schema.
    """
    _ensure_cache(name)
    if days <= 0:
        raise CacheOperationError("TTL days musí být kladné číslo")

    path = _cache_path(cache_dir, name)
    table = _CACHES[name][1]
    if not os.path.exists(path):
        _audit(cache_dir, name=name, action="ttl", operator=operator, source=source, rows_deleted=0, details=f"days={days};db_missing")
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(path)
    try:
        ts_col = _pick_existing_column(conn, table, _TTL_COLUMNS.get(name, []))
        if not ts_col:
            raise CacheOperationError(
                f"TTL není pro cache '{name}' dostupné (chybí časový sloupec)."
            )
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {ts_col} IS NOT NULL AND {ts_col} < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    _audit(cache_dir, name=name, action="ttl", operator=operator, source=source, rows_deleted=deleted, details=f"days={days};cutoff={cutoff}")
    return deleted


def clear_prefix(
    cache_dir: str,
    name: str,
    prefix: str,
    operator: str = "unknown",
    source: str = "ui",
) -> int:
    """Delete cache records matching a prefix filter.

    Args:
        cache_dir: absolute path to the cache/ directory.
        name: one of 'sku', 'translations', 'images'.
        prefix: text prefix for cache-specific matching.
        operator: user/IP that triggered the action.
        source: caller origin, default 'ui'.

    Returns:
        Number of rows deleted.

    Raises:
        CacheOperationError: invalid cache name or empty prefix.
    """
    _ensure_cache(name)
    prefix = (prefix or "").strip()
    if not prefix:
        raise CacheOperationError("Prefix nesmí být prázdný")

    path = _cache_path(cache_dir, name)
    if not os.path.exists(path):
        _audit(cache_dir, name=name, action="prefix", operator=operator, source=source, rows_deleted=0, details=f"prefix={prefix};db_missing")
        return 0

    sql, params = _prefix_delete_query(name, prefix)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(sql, params)
        deleted = cur.rowcount
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()

    _audit(cache_dir, name=name, action="prefix", operator=operator, source=source, rows_deleted=deleted, details=f"prefix={prefix}")
    return deleted


def get_audit(cache_dir: str, limit: int = 30) -> list:
    """Return latest cache mutation audit records.

    Args:
        cache_dir: absolute path to cache/ directory.
        limit: max number of records.

    Returns:
        List of dict rows sorted newest-first.
    """
    db_path = os.path.join(cache_dir, _AUDIT_DB)
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ts, cache_name, action, operator, source, rows_deleted, details FROM cache_audit ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _audit(
    cache_dir: str,
    name: str,
    action: str,
    operator: str,
    source: str,
    rows_deleted: int,
    details: str,
) -> None:
    """Persist one cache mutation event to cache_audit.db.

    Audit failures are swallowed to avoid blocking cache operations.
    """
    db_path = os.path.join(cache_dir, _AUDIT_DB)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                cache_name   TEXT NOT NULL,
                action       TEXT NOT NULL,
                operator     TEXT NOT NULL,
                source       TEXT NOT NULL,
                rows_deleted INTEGER NOT NULL,
                details      TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cache_audit (ts, cache_name, action, operator, source, rows_deleted, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                name,
                action,
                operator or "unknown",
                source or "ui",
                int(rows_deleted),
                details,
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _ensure_cache(name: str) -> None:
    if name not in _CACHES:
        raise CacheOperationError(f"Unknown cache name: {name!r}. Valid: {list(_CACHES)}")


def _cache_path(cache_dir: str, name: str) -> str:
    return os.path.join(cache_dir, _CACHES[name][0])


def _safe_scalar(conn: sqlite3.Connection, sql: str):
    try:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def _pick_existing_column(conn: sqlite3.Connection, table: str, candidates: list) -> str | None:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return None
    cols = {r[1] for r in rows}
    for c in candidates:
        if c in cols:
            return c
    return None


def _count_rows(path: str, table: str) -> int:
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _count_ttl_rows(path: str, name: str, days: int) -> int:
    if not os.path.exists(path):
        return 0
    table = _CACHES[name][1]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        col = _pick_existing_column(conn, table, _TTL_COLUMNS.get(name, []))
        if not col:
            raise CacheOperationError(
                f"TTL není pro cache '{name}' dostupné (chybí časový sloupec)."
            )
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL AND {col} < ?",
            (cutoff,),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _prefix_count_query(name: str, prefix: str) -> tuple[str, tuple]:
    like = f"{prefix}%"
    if name == "sku":
        return "SELECT COUNT(*) FROM sku_cache WHERE sku LIKE ?", (like,)
    if name == "translations":
        return "SELECT COUNT(*) FROM translations WHERE hash LIKE ? OR result LIKE ?", (like, like)
    return "SELECT COUNT(*) FROM image_cache WHERE original_url LIKE ? OR gcs_url LIKE ?", (like, like)


def _prefix_delete_query(name: str, prefix: str) -> tuple[str, tuple]:
    like = f"{prefix}%"
    if name == "sku":
        return "DELETE FROM sku_cache WHERE sku LIKE ?", (like,)
    if name == "translations":
        return "DELETE FROM translations WHERE hash LIKE ? OR result LIKE ?", (like, like)
    return "DELETE FROM image_cache WHERE original_url LIKE ? OR gcs_url LIKE ?", (like, like)


def _count_prefix_rows(path: str, name: str, prefix: str) -> int:
    if not os.path.exists(path):
        return 0
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        sql, params = _prefix_count_query(name, prefix)
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
