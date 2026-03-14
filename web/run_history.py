"""
run_history.py — SQLite persistence for sync run history.

Schema: runs table in runs.db (kept in cache/ alongside other DBs).

Public API:
    init(db_path)               create table if not exists
    insert(db_path, **kwargs)   insert a completed run record
    get_all(db_path, limit)     list of run dicts, newest first
    get_last(db_path)           most recent completed run dict, or None
"""

import sqlite3
from contextlib import contextmanager
from typing import Optional


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_s  REAL,
    exit_code   INTEGER NOT NULL,
    mode        TEXT NOT NULL,          -- 'live' | 'dry'
    source      TEXT,
    run_limit   INTEGER,
    created     INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    errors      INTEGER DEFAULT 0,
    drafted     INTEGER DEFAULT 0,
    log_snippet TEXT                    -- last 50 lines of output
)
"""


def init(db_path: str) -> None:
    """Create the runs table if it does not exist.

    Args:
        db_path: absolute path to runs.db
    """
    with _conn(db_path) as conn:
        conn.execute(_CREATE_SQL)
        _ensure_column(conn, "runs", "source", "TEXT")
        _ensure_column(conn, "runs", "run_limit", "INTEGER")


def insert(
    db_path: str,
    started_at: str,
    finished_at: str,
    duration_s: Optional[float],
    exit_code: int,
    mode: str,
    source: Optional[str] = None,
    run_limit: Optional[int] = None,
    created: int = 0,
    updated: int = 0,
    errors: int = 0,
    drafted: int = 0,
    log_snippet: str = "",
) -> int:
    """Insert a completed run and return its row id.

    Args:
        db_path:     absolute path to runs.db
        started_at:  ISO timestamp string (UTC)
        finished_at: ISO timestamp string (UTC)
        duration_s:  run duration in seconds, or None if unknown
        exit_code:   subprocess exit code (0 = success)
        mode:        'live' or 'dry'
        source:      explicit source path/url used for this run, if any
        run_limit:   numeric limit used for this run, if any
        created:     WC products created
        updated:     WC products updated
        errors:      WC API errors
        drafted:     products set to draft (disappeared from feed)
        log_snippet: last N lines of output for quick inspection

    Returns:
        Row id of the inserted record.
    """
    with _conn(db_path) as conn:
        cur = conn.execute(
            """INSERT INTO runs
               (started_at, finished_at, duration_s, exit_code, mode,
                                source, run_limit, created, updated, errors, drafted, log_snippet)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (started_at, finished_at, duration_s, exit_code, mode,
                         source, run_limit, created, updated, errors, drafted, log_snippet),
        )
        return cur.lastrowid


def get_all(db_path: str, limit: int = 50) -> list:
    """Return list of run dicts ordered by started_at descending.

    Args:
        db_path: absolute path to runs.db
        limit:   max rows to return

    Returns:
        List of dicts with all runs columns.
    """
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_last(db_path: str) -> Optional[dict]:
    """Return the most recent completed run as a dict, or None.

    Args:
        db_path: absolute path to runs.db

    Returns:
        Dict of run columns, or None if no runs recorded yet.
    """
    with _conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

@contextmanager
def _conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a missing column to an existing table.

    Args:
        conn: Open SQLite connection.
        table: Table name to inspect.
        column: Column name that should exist.
        decl: SQL declaration used in ALTER TABLE when missing.
    """
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    cols = {r[1] for r in rows}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
