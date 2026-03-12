"""
_cache.py — SQLite SKU→WooCommerce-ID cache.

Schema:
    sku_cache(
        sku        TEXT PRIMARY KEY,
        wc_id      INTEGER NOT NULL,
        parent_sku TEXT NOT NULL DEFAULT ''   -- '' for parents, mpn for variations
    )

Only parents have parent_sku == ''. Querying all parent SKUs from the previous
run is how woo_client detects products that disappeared from the feed.
"""

import sqlite3


def open_cache(path: str) -> sqlite3.Connection:
    """
    Open (or create) the SKU cache database at *path*.

    Returns an open sqlite3.Connection with WAL mode enabled for safe
    concurrent access. Caller is responsible for closing it.

    Args:
        path: Absolute path to the .db file. Directory must already exist.

    Returns:
        sqlite3.Connection
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sku_cache (
            sku        TEXT PRIMARY KEY,
            wc_id      INTEGER NOT NULL,
            parent_sku TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    return conn


def get_id(conn: sqlite3.Connection, sku: str) -> int | None:
    """
    Return the WooCommerce product/variation ID for *sku*, or None if unknown.

    Args:
        conn: Open cache connection.
        sku:  SKU string to look up.

    Returns:
        int WooCommerce ID, or None.
    """
    row = conn.execute(
        "SELECT wc_id FROM sku_cache WHERE sku = ?", (sku,)
    ).fetchone()
    return row[0] if row else None


def set_id(
    conn: sqlite3.Connection,
    sku: str,
    wc_id: int,
    parent_sku: str = "",
) -> None:
    """
    Insert or replace the SKU→ID mapping in the cache.

    Args:
        conn:       Open cache connection.
        sku:        SKU string (product or variation).
        wc_id:      WooCommerce product/variation ID.
        parent_sku: mpn of the parent group; empty string for parent products.
    """
    conn.execute(
        "INSERT OR REPLACE INTO sku_cache (sku, wc_id, parent_sku) VALUES (?, ?, ?)",
        (sku, wc_id, parent_sku),
    )
    # Caller batches commits for performance — no commit here.


def get_all_parent_skus(conn: sqlite3.Connection) -> set:
    """
    Return the set of all parent SKUs (parent_sku == '') ever synced.

    Used by woo_client.draft_disappeared() to detect products no longer
    present in the B2B feed.

    Args:
        conn: Open cache connection.

    Returns:
        set of SKU strings.
    """
    rows = conn.execute(
        "SELECT sku FROM sku_cache WHERE parent_sku = ''"
    ).fetchall()
    return {row[0] for row in rows}
