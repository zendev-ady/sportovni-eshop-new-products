"""
sync_runner.py — Manages run_sync.py as a subprocess with SSE log streaming.

Uses subprocess.Popen in a background thread (not asyncio.create_subprocess_exec)
to avoid the Windows ProactorEventLoop requirement.

Public API:
    start(mode, limit, source)  → bool   start subprocess, False if already running
    stop()                      → None   terminate subprocess
    stream()                    → AsyncGenerator[str, None]  SSE-formatted strings
    get_status()                → dict   current run state (JSON-serializable)
    register_on_complete(cb)            register async callback called on run end
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Callable, List, Optional

_PIPELINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUN_SYNC_PATH = os.path.join(_PIPELINE_DIR, "run_sync.py")

logger = logging.getLogger(__name__)

_on_complete_cbs: List[Callable] = []
_subscribers: List[asyncio.Queue] = []


@dataclass
class RunState:
    """Mutable state of the currently running (or last) sync job."""
    _proc: Optional[subprocess.Popen] = None
    mode: str = "idle"                   # 'idle' | 'live' | 'dry'
    source: Optional[str] = None          # ''/None means default remote URL
    limit: Optional[int] = None           # None means all groups
    started_at: Optional[str] = None     # ISO timestamp (UTC)
    log_buffer: List[str] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    errors: int = 0
    drafted: int = 0
    exit_code: Optional[int] = None      # None while running


state = RunState()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    """Return True if subprocess is currently alive."""
    return state._proc is not None and state._proc.poll() is None


async def start(
    mode: str = "live",
    limit: Optional[int] = None,
    source: Optional[str] = None,
) -> bool:
    """Start run_sync.py subprocess in a background thread.

    Args:
        mode:   'live' for full sync, 'dry' for dry run (--dry-run)
        limit:  max product groups to process (--limit N)
        source: override XML source URL or file path (--source)

    Returns:
        True if subprocess was started, False if one is already running.
    """
    if is_running():
        return False

    cmd = [sys.executable, "-u", _RUN_SYNC_PATH]
    if mode == "dry":
        cmd += ["--dry-run"]
    if limit:
        cmd += ["--limit", str(limit)]
    if source and source.strip():
        cmd += ["--source", source.strip()]

    state.mode = mode
    state.source = source.strip() if source and source.strip() else None
    state.limit = int(limit) if limit else None
    state.started_at = datetime.now(timezone.utc).isoformat()
    state.log_buffer.clear()
    state.created = state.updated = state.errors = state.drafted = 0
    state.exit_code = None

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=_PIPELINE_DIR,
    )
    state._proc = proc

    # Get the running event loop to schedule coroutines from the reader thread
    loop = asyncio.get_event_loop()
    thread = threading.Thread(
        target=_reader_thread,
        args=(proc, loop),
        daemon=True,
        name="sync-reader",
    )
    thread.start()
    return True


async def stop() -> None:
    """Terminate the running subprocess."""
    if is_running():
        state._proc.terminate()


async def stream() -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted strings.

    On connect: replays the full log buffer (handles reconnects).
    Then streams live lines until the subprocess exits.
    Sends SSE keepalive comments every 10 s to prevent proxy timeouts.
    """
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)

    # Replay buffer so a reconnecting browser catches up
    for line in list(state.log_buffer):
        yield f"data: {_sse_escape(line)}\n\n"

    # If process already finished, close immediately
    if not is_running() and state.exit_code is not None:
        yield f"event: done\ndata: {state.exit_code}\n\n"
        _remove_sub(q)
        return

    try:
        while True:
            try:
                event, data = await asyncio.wait_for(q.get(), timeout=10.0)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event == "line":
                yield f"data: {_sse_escape(data)}\n\n"
            elif event == "done":
                yield f"event: done\ndata: {data}\n\n"
                break
    finally:
        _remove_sub(q)


def get_status() -> dict:
    """Return current run state as a JSON-serializable dict."""
    return {
        "running":    is_running(),
        "mode":       state.mode,
        "source":     state.source,
        "limit":      state.limit,
        "started_at": state.started_at,
        "created":    state.created,
        "updated":    state.updated,
        "errors":     state.errors,
        "drafted":    state.drafted,
        "exit_code":  state.exit_code,
        "log_lines":  len(state.log_buffer),
    }


def register_on_complete(cb: Callable) -> None:
    """Register an async callable(RunState) called when the subprocess exits."""
    _on_complete_cbs.append(cb)


# ---------------------------------------------------------------------------
# Internal — reader thread
# ---------------------------------------------------------------------------

def _reader_thread(proc: subprocess.Popen, loop: asyncio.AbstractEventLoop) -> None:
    """Read stdout line-by-line in a background thread, push to asyncio loop."""
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            asyncio.run_coroutine_threadsafe(_on_line(line), loop)
    except Exception as exc:
        logger.error("sync reader thread error: %s", exc)

    proc.wait()
    asyncio.run_coroutine_threadsafe(_on_done(proc.returncode), loop)


async def _on_line(line: str) -> None:
    state.log_buffer.append(line)
    _parse_stats(line)
    _broadcast("line", line)


async def _on_done(exit_code: int) -> None:
    state.exit_code = exit_code
    _broadcast("done", str(exit_code))

    for cb in list(_on_complete_cbs):
        try:
            await cb(state)
        except Exception as exc:
            logger.error("on_complete callback raised: %s", exc)


# ---------------------------------------------------------------------------
# Internal — helpers
# ---------------------------------------------------------------------------

def _broadcast(event: str, data: str) -> None:
    for q in list(_subscribers):
        q.put_nowait((event, data))


def _remove_sub(q: asyncio.Queue) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def _parse_stats(line: str) -> None:
    """Extract running totals from log output.

    Patterns emitted by woo_client:
      products/batch — created: 12, updated: 8, errors: 0
      Sync complete — 1456 groups queued for upsert, 23 products drafted
    """
    m = re.search(r"created:\s*(\d+),\s*updated:\s*(\d+),\s*errors:\s*(\d+)", line)
    if m:
        state.created += int(m.group(1))
        state.updated += int(m.group(2))
        state.errors  += int(m.group(3))

    m = re.search(r"(\d+) products drafted", line)
    if m:
        state.drafted = int(m.group(1))


def _sse_escape(text: str) -> str:
    """SSE data field must not contain raw newlines."""
    return text.replace("\n", " ").replace("\r", "")
