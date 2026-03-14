#!/usr/bin/env python3
"""
web_ui.py — Entry point for B2B Sync web dashboard.

Run from b2b_to_woocommerce/ directory:
    python web_ui.py
    → http://localhost:8000
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.main:app", host="127.0.0.1", port=8000, reload=False)
