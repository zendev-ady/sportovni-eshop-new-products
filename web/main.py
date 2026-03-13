"""
main.py — FastAPI web dashboard for B2B → WooCommerce sync pipeline.

Run from b2b_to_woocommerce/ directory:
    uvicorn web.main:app --host 0.0.0.0 --port 8000 --reload

Routes:
    GET  /                      Dashboard (last run stats, quick actions)
    GET  /logs                  Live log stream page
    GET  /history               Run history table
    GET  /cache                 Cache stats + clear actions

    POST /sync/run              Start live sync → redirect to /logs
    POST /sync/dry              Start dry run  → redirect to /logs
    POST /sync/stop             Stop running sync → redirect to /
    GET  /sync/stream           SSE stream (EventSource endpoint)
    GET  /sync/status           JSON status for polling

    POST /cache/{name}/clear    Clear named cache → redirect to /cache
    GET  /logs/download         Download sync.log file
"""

import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

# Make pipeline root importable (for run_sync.py path resolution)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from . import sync_runner, run_history, cache_ops, telegram

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR = os.path.dirname(_HERE)
_CACHE_DIR    = os.path.join(_PIPELINE_DIR, "cache")
_LOG_FILE     = os.path.join(_PIPELINE_DIR, "logs", "sync.log")
_RUNS_DB      = os.path.join(_CACHE_DIR, "runs.db")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    run_history.init(_RUNS_DB)
    sync_runner.register_on_complete(_on_run_complete)
    yield


app = FastAPI(title="B2B Sync Dashboard", lifespan=lifespan)


async def _on_run_complete(state: sync_runner.RunState) -> None:
    """Persist run record and fire Telegram alert on failure."""
    now = datetime.now(timezone.utc).isoformat()
    duration_s = None
    if state.started_at:
        try:
            start = datetime.fromisoformat(state.started_at)
            end   = datetime.now(timezone.utc)
            duration_s = (end - start).total_seconds()
        except Exception:
            pass

    log_snippet = "\n".join(state.log_buffer[-50:])

    run_history.insert(
        db_path     = _RUNS_DB,
        started_at  = state.started_at or now,
        finished_at = now,
        duration_s  = duration_s,
        exit_code   = state.exit_code or 0,
        mode        = state.mode,
        created     = state.created,
        updated     = state.updated,
        errors      = state.errors,
        drafted     = state.drafted,
        log_snippet = log_snippet,
    )

    if state.exit_code != 0 or state.errors > 0:
        mode_label = "Dry run" if state.mode == "dry" else "Sync"
        msg = (
            f"⚠️ <b>B2B Sync alert</b>\n"
            f"{mode_label} skončil s problémy.\n"
            f"Exit code: <code>{state.exit_code}</code>\n"
            f"Chyby: <b>{state.errors}</b>\n"
            f"Začátek: {state.started_at}"
        )
        telegram.send(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60:02d}s"


def _fmt_ts(iso: str) -> str:
    """Format ISO UTC timestamp to readable local-looking string."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso


templates.env.filters["duration"] = _fmt_duration
templates.env.filters["ts"] = _fmt_ts


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    status   = sync_runner.get_status()
    last_run = run_history.get_last(_RUNS_DB)
    recent_lines = _log_tail(20)
    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "active":       "dashboard",
        "status":       status,
        "last_run":     last_run,
        "recent_lines": recent_lines,
    })


@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    status = sync_runner.get_status()
    # If idle: show last 300 lines from sync.log for context
    recent_lines = sync_runner.state.log_buffer if status["running"] else _log_tail(300)
    return templates.TemplateResponse("logs.html", {
        "request":      request,
        "active":       "logs",
        "running":      status["running"],
        "recent_lines": recent_lines,
    })


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    runs = run_history.get_all(_RUNS_DB, limit=50)
    return templates.TemplateResponse("history.html", {
        "request": request,
        "active":  "history",
        "runs":    runs,
    })


@app.get("/cache", response_class=HTMLResponse)
async def cache_page(request: Request):
    stats = cache_ops.get_stats(_CACHE_DIR)
    return templates.TemplateResponse("cache.html", {
        "request": request,
        "active":  "cache",
        "caches":  stats,
    })


# ---------------------------------------------------------------------------
# Sync actions
# ---------------------------------------------------------------------------

@app.post("/sync/run")
async def sync_run(
    source: str = Form(default=""),
    limit:  str = Form(default=""),
):
    limit_int = int(limit) if limit.strip().isdigit() else None
    started = await sync_runner.start("live", limit=limit_int, source=source or None)
    if not started:
        return RedirectResponse("/?err=already_running", status_code=303)
    return RedirectResponse("/logs", status_code=303)


@app.post("/sync/dry")
async def sync_dry(
    limit:  str = Form(default="20"),
    source: str = Form(default=""),
):
    limit_int = int(limit) if limit.strip().isdigit() else 20
    started = await sync_runner.start("dry", limit=limit_int, source=source or None)
    if not started:
        return RedirectResponse("/?err=already_running", status_code=303)
    return RedirectResponse("/logs", status_code=303)


@app.post("/sync/stop")
async def sync_stop():
    await sync_runner.stop()
    return RedirectResponse("/", status_code=303)


@app.get("/sync/stream")
async def sync_stream():
    """SSE endpoint — connect with EventSource('/sync/stream')."""
    return StreamingResponse(
        sync_runner.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


@app.get("/sync/status")
async def sync_status():
    """JSON status for Alpine.js polling on the dashboard."""
    return JSONResponse(sync_runner.get_status())


# ---------------------------------------------------------------------------
# Cache actions
# ---------------------------------------------------------------------------

@app.post("/cache/{name}/clear")
async def cache_clear(name: str):
    try:
        deleted = cache_ops.clear(_CACHE_DIR, name)
    except ValueError:
        return RedirectResponse("/cache?err=unknown", status_code=303)
    return RedirectResponse(f"/cache?cleared={name}&rows={deleted}", status_code=303)


# ---------------------------------------------------------------------------
# Log download
# ---------------------------------------------------------------------------

@app.get("/logs/download")
async def logs_download():
    if not os.path.exists(_LOG_FILE):
        return JSONResponse({"error": "Log file not found"}, status_code=404)
    return FileResponse(
        _LOG_FILE,
        media_type="text/plain",
        filename="sync.log",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log_tail(n: int) -> list:
    """Return last n lines from sync.log as a list of strings."""
    if not os.path.exists(_LOG_FILE):
        return []
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []
