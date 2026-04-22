"""FastAPI backend for the Order Automation System."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from pathlib import Path

# ── Configure logging ──────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-7s │ %(name)-22s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("api")

# Load .env locally (in Cloud Run env vars are injected)
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from functions.email_parser import PARSER_REGISTRY, get_parser
from functions.gmail_client import GmailClient
from functions.word_generator import WordReportGenerator

app = FastAPI(title="Order Automation System", version="1.0.0")

# CORS — allow local dev + Firebase Hosting URLs
_allowed_origins = [
    "http://localhost:5173",
    "http://localhost:3000",
    "https://orders-bf760.web.app",
    "https://orders-bf760.firebaseapp.com",
]
# Allow extra origins via env var (comma-separated)
_extra = os.environ.get("ALLOWED_ORIGINS", "")
if _extra:
    _allowed_origins.extend([o.strip() for o in _extra.split(",") if o.strip()])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMPLATE_PATH = Path(__file__).parent / "templates" / "stephen_template.docx"
OUTPUT_DIR = Path(__file__).parent / "test_output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Cache for fetched orders (avoid re-fetching Gmail every time)
_cached_orders: list[dict] = []


def _get_gmail_client() -> GmailClient:
    logger.info("🔐 Authenticating with Gmail API...")
    client = GmailClient(
        gmail_account=os.environ.get("GMAIL_ACCOUNT", "bettercrafterorders@gmail.com"),
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
    )
    logger.info("✅ Gmail API authenticated successfully")
    return client


def _is_valid_order(order: dict) -> bool:
    """Filter out orders without at least item_code + customer_name."""
    has_item     = bool(order.get("item_code", "").strip())
    has_customer = bool(order.get("customer_name", "").strip())
    return has_item and has_customer


def _sse_event(event: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.get("/api/orders-stream")
def fetch_orders_stream(hours_back: int = 8760):
    """SSE endpoint — streams progress events while fetching/parsing emails."""

    def event_generator():
        global _cached_orders
        t0 = time.time()

        # Step 1: Auth
        yield _sse_event("progress", {"step": "auth", "message": "🔐 Authenticating with Gmail API...", "percent": 5})
        try:
            gmail = _get_gmail_client()
        except Exception as e:
            yield _sse_event("error", {"message": f"Auth failed: {e}"})
            return

        # Step 2: Fetch email list
        yield _sse_event("progress", {"step": "fetch_list", "message": "📨 Fetching email list from Gmail...", "percent": 10})
        supplier_email = os.environ.get("STEPHEN_EMAIL", "7173783020@hellofax.com")
        try:
            messages = gmail.list_supplier_messages(
                supplier_email=supplier_email,
                hours_back=hours_back,
            )
        except Exception as e:
            yield _sse_event("error", {"message": f"Failed to fetch emails: {e}"})
            return

        total = len(messages)
        logger.info("📬 Found %d emails from %s", total, supplier_email)
        yield _sse_event("progress", {"step": "fetched", "message": f"📬 Found {total} emails", "percent": 20, "total": total})

        # Step 3: Initialize parser
        using_gemini = bool(os.environ.get("GEMINI_API_KEY"))
        yield _sse_event("progress", {"step": "parser_init", "message": "🧠 Initializing parser...", "percent": 25})
        parser = get_parser("stephen")

        # Step 4: Parse emails one by one
        orders: list[dict] = []
        failed = 0
        pdf_count = 0

        for i, msg in enumerate(messages):
            idx = i + 1
            has_pdf = bool(msg.pdf_text)
            if has_pdf:
                pdf_count += 1

            result = parser.parse(msg.body, pdf_text=msg.pdf_text or None)
            if result and _is_valid_order(result):
                orders.append(result)
                customer = result.get('customer_name', '?')
                item = result.get('item_code', '?')
                logger.info(
                    "  ✅ [%d/%d] Parsed — customer=%s, item=%s%s",
                    idx, total, customer, item, " 📎PDF" if has_pdf else ""
                )
            else:
                failed += 1
                logger.info("  ❌ [%d/%d] Skipped (missing required fields)", idx, total)

            # Send progress every email
            pct = 25 + int((idx / total) * 65)  # 25% → 90%
            yield _sse_event("progress", {
                "step": "parsing",
                "message": f"🔍 Parsing email {idx}/{total}{'  📎PDF' if has_pdf else ''}",
                "percent": pct,
                "current": idx,
                "total": total,
                "parsed": len(orders),
                "failed": failed,
                "pdfs": pdf_count,
            })

        _cached_orders = orders
        elapsed = round(time.time() - t0, 1)

        logger.info("═" * 50)
        logger.info("📊 RESULTS: %d parsed, %d skipped, %d PDFs — %.1fs", len(orders), failed, pdf_count, elapsed)
        logger.info("═" * 50)

        # Step 5: Done
        yield _sse_event("complete", {
            "message": f"✅ Done in {elapsed}s",
            "percent": 100,
            "total_emails": total,
            "parsed": len(orders),
            "failed": failed,
            "pdfs_found": pdf_count,
            "elapsed": elapsed,
            "orders": orders,
        })

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/orders")
def fetch_orders(hours_back: int = 8760):
    """Fetch and parse real supplier emails from Gmail (non-streaming)."""
    global _cached_orders
    t0 = time.time()
    try:
        logger.info("═" * 50)
        logger.info("🚀 Starting order fetch (hours_back=%d)", hours_back)
        logger.info("═" * 50)

        gmail = _get_gmail_client()
        parser = get_parser("stephen")
        supplier_email = os.environ.get("STEPHEN_EMAIL", "7173783020@hellofax.com")

        logger.info("📨 Fetching emails to %s...", supplier_email)
        messages = gmail.list_supplier_messages(
            supplier_email=supplier_email,
            hours_back=hours_back,
        )
        logger.info("📬 Found %d emails", len(messages))

        orders: list[dict] = []
        failed = 0
        pdf_count = 0
        for i, msg in enumerate(messages):
            has_pdf = bool(msg.pdf_text)
            if has_pdf:
                pdf_count += 1
            result = parser.parse(msg.body, pdf_text=msg.pdf_text or None)
            if result and _is_valid_order(result):
                orders.append(result)
                logger.info(
                    "  ✅ [%d/%d] %s — %s%s",
                    i + 1, len(messages),
                    result.get('customer_name', '?'),
                    result.get('item_code', '?'),
                    " 📎" if has_pdf else "",
                )
            else:
                failed += 1
                logger.info("  ❌ [%d/%d] Skipped", i + 1, len(messages))

        _cached_orders = orders
        elapsed = round(time.time() - t0, 1)

        logger.info("═" * 50)
        logger.info("📊 DONE: %d parsed, %d skipped, %d PDFs — %.1fs", len(orders), failed, pdf_count, elapsed)
        logger.info("═" * 50)

        return {
            "total_emails": len(messages),
            "parsed": len(orders),
            "failed": failed,
            "pdfs_found": pdf_count,
            "elapsed": elapsed,
            "orders": orders,
        }
    except Exception as e:
        logger.error("💥 Error fetching orders: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate-report")
def generate_report():
    """Generate a Word report from the cached orders."""
    if not _cached_orders:
        raise HTTPException(status_code=400, detail="No orders loaded. Fetch orders first.")

    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=500, detail="Template file not found.")

    try:
        logger.info("📝 Generating Word report with %d orders...", len(_cached_orders))
        generator = WordReportGenerator(str(TEMPLATE_PATH))
        report_date = date.today()
        path = generator.generate_daily_report(_cached_orders, report_date)
        dest = OUTPUT_DIR / f"stephen_orders_{report_date.isoformat()}.docx"
        dest.write_bytes(path.read_bytes())
        logger.info("✅ Report saved: %s (%d orders)", dest.name, len(_cached_orders))
        return {
            "success": True,
            "filename": dest.name,
            "order_count": len(_cached_orders),
            "date": report_date.isoformat(),
        }
    except Exception as e:
        logger.error("💥 Report generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/download-report/{filename}")
def download_report(filename: str):
    """Download a generated Word report."""
    file_path = OUTPUT_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.get("/api/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
