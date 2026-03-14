"""
main.py — FastAPI web dashboard for B2B → WooCommerce sync pipeline.

Run from b2b_to_woocommerce/ directory:
    uvicorn web.main:app --host 0.0.0.0 --port 8000 --reload

Routes:
    GET  /                      Selektivní sync (home — URL textarea)
    GET  /overview              Dashboard (last run stats, quick actions)
    GET  /logs                  Live log stream page
    GET  /history               Run history table
    GET  /cache                 Cache stats + clear actions

    POST /select-sync/run       SSE stream for select_sync.py subprocess
    POST /select-sync/stop      Terminate select-sync subprocess

    POST /sync/run              Start live sync → redirect to /logs
    POST /sync/dry              Start dry run  → redirect to /logs
    POST /sync/stop             Stop running sync → redirect to /overview
    GET  /sync/stream           SSE stream (EventSource endpoint)
    GET  /sync/status           JSON status for polling

    POST /cache/{name}/clear    Clear named cache → redirect to /cache
    GET  /logs/download         Download sync.log file
"""

import os
import subprocess
import sys
import tempfile
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

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

_HERE             = os.path.dirname(os.path.abspath(__file__))
_PIPELINE_DIR     = os.path.dirname(_HERE)
_CACHE_DIR        = os.path.join(_PIPELINE_DIR, "cache")
_LOG_FILE         = os.path.join(_PIPELINE_DIR, "logs", "sync.log")
_RUNS_DB          = os.path.join(_CACHE_DIR, "runs.db")
_SELECT_SYNC_PATH = os.path.join(_PIPELINE_DIR, "select_sync.py")

# Select-sync subprocess state (one at a time)
_select_proc: Optional[subprocess.Popen] = None
_select_lock  = None   # initialised in lifespan (needs running event loop for asyncio.Lock)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _select_lock
    import asyncio
    _select_lock = asyncio.Lock()
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
        source      = state.source,
        run_limit   = state.limit,
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
async def home(request: Request):
    return templates.TemplateResponse("select_sync.html", {
        "request": request,
        "active":  "sync",
    })


@app.get("/overview", response_class=HTMLResponse)
async def dashboard(request: Request):
    status       = sync_runner.get_status()
    last_run     = run_history.get_last(_RUNS_DB)
    recent_lines = _log_tail(20)
    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "active":       "overview",
        "status":       status,
        "last_run":     last_run,
        "recent_lines": recent_lines,
    })


@app.post("/select-sync/run")
async def select_sync_run(
    urls_text:          str = Form(...),
    dry_run:            str = Form("off"),
    limit:              str = Form(""),
    source:             str = Form(""),
    skip_on_rate_limit: str = Form("off"),
):
    """SSE endpoint — spawns select_sync.py and streams stdout line-by-line."""
    global _select_proc

    is_dry     = dry_run == "on"
    is_skip_rl = skip_on_rate_limit == "on"
    limit_val  = limit.strip() if limit.strip().isdigit() else None
    source_val = (
        os.path.join(_PIPELINE_DIR, "partner_b2b_full.xml")
        if source.strip() == "local"
        else None
    )

    async def generate():
        global _select_proc

        if _select_lock.locked():
            yield "data: ⚠️ Sync již probíhá — počkej na dokončení.\n\n"
            yield "data: __DONE__\n\n"
            return

        async with _select_lock:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            try:
                tmp.write(urls_text)
                tmp.close()

                cmd = [sys.executable, "-u", _SELECT_SYNC_PATH, "--urls", tmp.name]
                if is_dry:      cmd.append("--dry-run")
                if limit_val:   cmd += ["--limit", limit_val]
                if source_val:  cmd += ["--source", source_val]
                if is_skip_rl:  cmd.append("--skip-on-rate-limit")

                # Use Popen + thread (not asyncio.create_subprocess_exec) for Windows compat
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=_PIPELINE_DIR,
                )
                _select_proc = proc

                import asyncio as _asyncio
                loop = _asyncio.get_event_loop()
                q: _asyncio.Queue = _asyncio.Queue()

                def _reader():
                    try:
                        for raw in proc.stdout:
                            line = raw.decode("utf-8", errors="replace").rstrip()
                            escaped = line.replace("\n", "↵")
                            _asyncio.run_coroutine_threadsafe(q.put(("line", escaped)), loop)
                    except Exception:
                        pass
                    proc.wait()
                    _asyncio.run_coroutine_threadsafe(q.put(("done", proc.returncode)), loop)

                threading.Thread(target=_reader, daemon=True, name="select-reader").start()

                while True:
                    event, data = await q.get()
                    if event == "line":
                        yield f"data: {data}\n\n"
                    elif event == "done":
                        label = "✅ Hotovo" if data == 0 else f"❌ Chyba (kód {data})"
                        yield f"data: {label}\n\n"
                        yield "data: __DONE__\n\n"
                        break

            except Exception as exc:
                yield f"data: ❌ Fatální chyba: {exc}\n\n"
                yield "data: __DONE__\n\n"
            finally:
                _select_proc = None
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/select-sync/stop")
async def select_sync_stop():
    """Terminate the running select-sync subprocess."""
    global _select_proc
    if _select_proc is not None and _select_proc.poll() is None:
        _select_proc.terminate()
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "message": "Žádný sync neběží."})


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
    previews = {}
    for cache in stats:
        name = cache["name"]
        try:
            previews[name] = {
                "clear": cache_ops.preview_action(_CACHE_DIR, name, "clear"),
            }
        except Exception as exc:
            previews[name] = {"error": str(exc)}

    audit_rows = cache_ops.get_audit(_CACHE_DIR, limit=25)
    return templates.TemplateResponse("cache.html", {
        "request": request,
        "active":  "cache",
        "caches":  stats,
        "previews": previews,
        "audit_rows": audit_rows,
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
        return RedirectResponse("/overview?err=already_running", status_code=303)
    return RedirectResponse("/logs", status_code=303)


@app.post("/sync/dry")
async def sync_dry(
    limit:  str = Form(default="20"),
    source: str = Form(default=""),
):
    limit_int = int(limit) if limit.strip().isdigit() else 20
    started = await sync_runner.start("dry", limit=limit_int, source=source or None)
    if not started:
        return RedirectResponse("/overview?err=already_running", status_code=303)
    return RedirectResponse("/logs", status_code=303)


@app.post("/sync/stop")
async def sync_stop():
    await sync_runner.stop()
    return RedirectResponse("/overview", status_code=303)


@app.post("/sync/replay/{run_id}")
async def sync_replay(run_id: int):
    runs = run_history.get_all(_RUNS_DB, limit=200)
    selected = None
    for run in runs:
        if run.get("id") == run_id:
            selected = run
            break

    if not selected:
        return RedirectResponse("/history?err=not_found", status_code=303)

    started = await sync_runner.start(
        selected.get("mode") or "live",
        limit=selected.get("run_limit"),
        source=selected.get("source") or None,
    )
    if not started:
        return RedirectResponse("/history?err=already_running", status_code=303)
    return RedirectResponse("/logs", status_code=303)


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
async def cache_clear(request: Request, name: str):
    operator = request.client.host if request.client else "unknown"
    try:
        deleted = cache_ops.clear(_CACHE_DIR, name, operator=operator, source="ui")
    except cache_ops.CacheOperationError:
        return RedirectResponse("/cache?err=unknown", status_code=303)
    return RedirectResponse(f"/cache?cleared={name}&rows={deleted}", status_code=303)


@app.get("/cache/{name}/preview")
async def cache_preview(name: str, action: str = "clear", days: int = 0, prefix: str = ""):
    try:
        if action == "ttl":
            result = cache_ops.preview_action(_CACHE_DIR, name, action, days=days)
        elif action == "prefix":
            result = cache_ops.preview_action(_CACHE_DIR, name, action, prefix=prefix)
        else:
            result = cache_ops.preview_action(_CACHE_DIR, name, "clear")
        return JSONResponse(result)
    except cache_ops.CacheOperationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/cache/{name}/invalidate/ttl")
async def cache_invalidate_ttl(
    request: Request,
    name: str,
    days: str = Form(default=""),
):
    operator = request.client.host if request.client else "unknown"
    days = (days or "").strip()
    if not days.isdigit():
        return RedirectResponse(f"/cache?err=invalid_ttl&cache={name}", status_code=303)

    try:
        deleted = cache_ops.clear_ttl(
            _CACHE_DIR,
            name,
            days=int(days),
            operator=operator,
            source="ui",
        )
    except cache_ops.CacheOperationError as exc:
        return RedirectResponse(f"/cache?err=ttl&msg={quote_plus(str(exc))}", status_code=303)

    return RedirectResponse(f"/cache?ttl={name}&rows={deleted}&days={int(days)}", status_code=303)


@app.post("/cache/{name}/invalidate/prefix")
async def cache_invalidate_prefix(
    request: Request,
    name: str,
    prefix: str = Form(default=""),
):
    operator = request.client.host if request.client else "unknown"
    try:
        deleted = cache_ops.clear_prefix(
            _CACHE_DIR,
            name,
            prefix=prefix,
            operator=operator,
            source="ui",
        )
    except cache_ops.CacheOperationError as exc:
        return RedirectResponse(f"/cache?err=prefix&msg={quote_plus(str(exc))}", status_code=303)

    safe_prefix = prefix.strip()[:80]
    return RedirectResponse(f"/cache?prefix={name}&rows={deleted}&q={quote_plus(safe_prefix)}", status_code=303)


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
