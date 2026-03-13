#!/usr/bin/env python3
"""
web_ui.py — Webové rozhraní pro selektivní B2B → WooCommerce sync.

Spuštění:
    python web_ui.py
    → otevři http://localhost:8000

Nebo s uvicorn přímo:
    uvicorn web_ui:app --reload --port 8000
"""

import asyncio
import os
import sys
import tempfile

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

_HERE = os.path.dirname(os.path.abspath(__file__))

app = FastAPI(title="B2B Sync UI", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=os.path.join(_HERE, "templates"))

# Prevent concurrent syncs — one at a time.
_sync_lock = asyncio.Lock()

# Currently running subprocess (None when idle).
_current_proc: asyncio.subprocess.Process | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main UI page."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/stop")
async def stop():
    """Terminate the currently running sync subprocess."""
    global _current_proc
    if _current_proc is not None and _current_proc.returncode is None:
        _current_proc.terminate()
        return JSONResponse({"ok": True, "message": "Sync zastaven."})
    return JSONResponse({"ok": False, "message": "Žádný sync neběží."})


@app.post("/sync")
async def sync(
    request: Request,
    urls_text: str = Form(...),
    dry_run: str = Form("off"),
    limit: str = Form(""),
    source: str = Form(""),
    skip_on_rate_limit: str = Form("off"),
):
    """
    SSE endpoint — runs select_sync.py as a subprocess and streams its log output
    line-by-line to the browser.

    Args:
        urls_text:           Raw textarea content with one URL per line.
        dry_run:             "on" for dry-run mode, anything else for live sync.
        skip_on_rate_limit:  "on" to pass --skip-on-rate-limit to select_sync.py.
    """
    is_dry_run = dry_run == "on"
    is_skip_rl = skip_on_rate_limit == "on"
    limit_int = limit.strip() if limit.strip().isdigit() else None
    _LOCAL_XML = os.path.join(_HERE, "partner_b2b_full.xml")
    source_str = _LOCAL_XML if source.strip() == "local" else None

    async def generate():
        global _current_proc

        if _sync_lock.locked():
            yield _sse("⚠️ Sync již probíhá — počkej na dokončení.")
            yield _sse("__DONE__")
            return

        async with _sync_lock:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            try:
                tmp.write(urls_text)
                tmp.close()

                cmd = [sys.executable, "-u", os.path.join(_HERE, "select_sync.py"),
                       "--urls", tmp.name]
                if is_dry_run:
                    cmd.append("--dry-run")
                if limit_int:
                    cmd += ["--limit", limit_int]
                if source_str:
                    cmd += ["--source", source_str]
                if is_skip_rl:
                    cmd.append("--skip-on-rate-limit")

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=_HERE,
                )
                _current_proc = proc

                async for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    yield _sse(line)

                await proc.wait()
                _current_proc = None
                status = "✅ Hotovo" if proc.returncode == 0 else f"❌ Chyba (kód {proc.returncode})"
                yield _sse(status)

            except Exception as exc:
                _current_proc = None
                yield _sse(f"❌ Fatální chyba serveru: {exc}")
            finally:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

            yield _sse("__DONE__")

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(data: str) -> str:
    """Format a single SSE message."""
    # Escape newlines so a single log message stays as one SSE event.
    escaped = data.replace("\n", "↵")
    return f"data: {escaped}\n\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_ui:app", host="127.0.0.1", port=8000, reload=False)
