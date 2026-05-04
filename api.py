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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional


class GenerateReportRequest(BaseModel):
    orders: Optional[List[dict]] = None  # frontend can send orders directly

from functions.email_parser import PARSER_REGISTRY, get_parser
from functions.gmail_client import GmailClient
from functions.word_generator import WordReportGenerator

# ── Hardcoded business constants ──────────────────
STEPHEN_EMAIL = "7173783020@hellofax.com"
INBOX_ACCOUNT = "bettercrafter1@gmail.com"

# Gmail OAuth credentials (for webhook processing)
GMAIL_CLIENT_ID     = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")

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
        gmail_account=os.environ.get("GMAIL_ACCOUNT", "bettercrafter1@gmail.com"),
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
def fetch_orders_stream(start_date: str = None, end_date: str = None):
    """SSE endpoint — streams progress events while fetching/parsing emails.
    start_date and end_date are ISO strings like '2025-07-11'
    """

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
        supplier_email = STEPHEN_EMAIL  # hardcoded constant — no env var needed
        logger.info("🎯 Target supplier (hardcoded): %s", supplier_email)
        logger.info("📅 Date range: %s → %s", start_date or "(no start)", end_date or "(no end)")
        try:
            messages = gmail.list_supplier_messages(
                supplier_email=supplier_email,
                start_date=start_date,
                end_date=end_date,
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

            logger.info("  🔍 [%d/%d] msg_id=%s body=%d chars pdf=%d chars files=%s",
                        idx, total, msg.message_id[:10], len(msg.body),
                        len(msg.pdf_text or ""), msg.pdf_filenames or [])

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
                missing = []
                if result:
                    for k in ("customer_name", "item_code", "quantity", "ship_by"):
                        if not result.get(k):
                            missing.append(k)
                else:
                    missing = ["<parser returned None>"]
                logger.info("  ❌ [%d/%d] Skipped — missing: %s", idx, total, missing)

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
def fetch_orders(start_date: str = None, end_date: str = None):
    """Fetch and parse real supplier emails from Gmail (non-streaming)."""
    global _cached_orders
    t0 = time.time()
    try:
        logger.info("═" * 50)
        logger.info("🚀 Starting order fetch start=%s end=%s", start_date, end_date)
        logger.info("═" * 50)

        gmail = _get_gmail_client()
        parser = get_parser("stephen")
        supplier_email = STEPHEN_EMAIL  # hardcoded constant — no env var needed

        logger.info("🎯 Target supplier (hardcoded): %s", supplier_email)
        logger.info("📅 Date range: %s → %s", start_date or "(no start)", end_date or "(no end)")
        messages = gmail.list_supplier_messages(
            supplier_email=supplier_email,
            start_date=start_date,
            end_date=end_date,
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
def generate_report(body: GenerateReportRequest = None):
    """Generate a Word report from the cached orders.
    
    Orders can be sent in the POST body (preferred) or read from in-memory cache.
    Sending in the body avoids issues with multiple Cloud Run instances.
    """
    logger.info("📝 generate_report called — body_orders=%s, cache=%d",
                len(body.orders) if body and body.orders else "none",
                len(_cached_orders))

    # Prefer orders from request body (avoids cross-instance cache miss)
    orders_to_use = (body.orders if body and body.orders else None) or _cached_orders

    logger.info("📝 orders_to_use count: %d", len(orders_to_use))

    if not orders_to_use:
        logger.warning("⚠️ No orders available — cache empty and no body orders")
        raise HTTPException(status_code=400, detail="No orders loaded. Fetch orders first.")

    logger.info("📋 TEMPLATE_PATH: %s — exists=%s", TEMPLATE_PATH, TEMPLATE_PATH.exists())
    if not TEMPLATE_PATH.exists():
        raise HTTPException(status_code=500, detail=f"Template file not found: {TEMPLATE_PATH}")

    try:
        logger.info("📝 Generating Word report with %d orders...", len(orders_to_use))
        generator = WordReportGenerator(str(TEMPLATE_PATH))
        report_date = date.today()
        path = generator.generate_daily_report(orders_to_use, report_date)
        dest = OUTPUT_DIR / f"stephen_orders_{report_date.isoformat()}.docx"
        dest.write_bytes(path.read_bytes())
        logger.info("✅ Report saved: %s (%d orders)", dest.name, len(orders_to_use))
        return {
            "success": True,
            "filename": dest.name,
            "order_count": len(orders_to_use),
            "date": report_date.isoformat(),
        }
    except Exception as e:
        logger.error("💥 Report generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/append-to-onedrive")
def append_to_onedrive(body: GenerateReportRequest = None):
    """Append parsed orders to the existing OneDrive Word document.

    Downloads the live .docx from OneDrive, appends new order rows (skipping
    duplicates), and uploads it back.
    """
    try:
        from functions.onedrive_client import download_docx, upload_docx, get_file_name
        from functions.word_generator import append_orders_to_existing_docx
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"OneDrive module not available: {e}")

    orders_to_use = (body.orders if body and body.orders else None) or _cached_orders
    if not orders_to_use:
        raise HTTPException(status_code=400, detail="No orders loaded. Fetch orders first.")

    try:
        file_name = get_file_name()
        logger.info("📥 Downloading '%s' from OneDrive...", file_name)
        docx_bytes = download_docx()

        logger.info("✏️  Processing %d order(s)...", len(orders_to_use))
        updated_bytes, appended, skipped = append_orders_to_existing_docx(docx_bytes, orders_to_use)

        if appended == 0:
            logger.info("⏭️  All %d orders already in file — nothing to upload", skipped)
            return {
                "success": True,
                "file_name": file_name,
                "orders_appended": 0,
                "orders_skipped": skipped,
                "message": f"All {skipped} order(s) already exist in the file — no changes made.",
            }

        logger.info("📤 Uploading updated document back to OneDrive...")
        upload_docx(updated_bytes)

        logger.info("✅ OneDrive updated: %s (+%d rows, %d skipped)", file_name, appended, skipped)
        return {
            "success": True,
            "file_name": file_name,
            "orders_appended": appended,
            "orders_skipped": skipped,
        }
    except Exception as e:
        logger.error("💥 OneDrive append failed: %s", e, exc_info=True)
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


@app.post("/api/gmail-webhook", status_code=200)
async def gmail_webhook(request: Request):
    """Receive Gmail push notifications from Google Pub/Sub."""
    import base64
    import json as _json

    # Log everything about the incoming request
    headers = dict(request.headers)
    raw = await request.body()
    logger.info("═" * 50)
    logger.info("📩 WEBHOOK HIT — method=%s url=%s", request.method, request.url)
    logger.info("📩 Headers: %s", {k: v for k, v in headers.items() if k.lower() in ('content-type','content-length','user-agent')})
    logger.info("📩 Body (%d bytes): %s", len(raw), raw[:500])

    try:
        body = _json.loads(raw)
    except Exception as e:
        logger.error("❌ Could not parse webhook body as JSON: %s — raw=%s", e, raw[:200])
        return {"status": "ok"}  # Return 200 so Pub/Sub stops retrying

    # Pub/Sub wraps the payload in message.data (base64-encoded)
    encoded = body.get("message", {}).get("data", "")
    if not encoded:
        logger.warning("⚠️  No message.data in webhook body: %s", body)
        return {"status": "no_data"}

    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
        notification = _json.loads(decoded)
    except Exception as e:
        logger.error("Failed to decode Pub/Sub message: %s", e)
        return {"status": "ok"}  # Return 200 so Pub/Sub stops retrying

    email_address = notification.get("emailAddress", "")
    history_id    = notification.get("historyId", "")
    logger.info("═" * 50)
    logger.info("📬 WEBHOOK RECEIVED — account=%s historyId=%s", email_address, history_id)
    logger.info("═" * 50)

    # Only process if we have Gmail credentials
    if not GMAIL_REFRESH_TOKEN:
        logger.warning("⚠️  GMAIL_REFRESH_TOKEN not set — skipping webhook processing")
        return {"status": "skipped", "reason": "no_credentials"}

    try:
        from datetime import date as _date
        from functions.email_parser import get_parser
        from functions.firestore_client import ProcessedEmailRecord, ProcessedEmailStore
        from functions.onedrive_client import download_docx, upload_docx
        from functions.word_generator import append_orders_to_existing_docx

        logger.info("🔐 Authenticating Gmail for account=%s...", INBOX_ACCOUNT)
        gmail = GmailClient(
            gmail_account=INBOX_ACCOUNT,
            client_id=GMAIL_CLIENT_ID,
            client_secret=GMAIL_CLIENT_SECRET,
            refresh_token=GMAIL_REFRESH_TOKEN,
        )
        store  = ProcessedEmailStore("processed_emails")
        parser = get_parser("stephen")

        # Fetch today's emails to catch any missed ones + the new one
        today_str = _date.today().isoformat()
        logger.info("📨 Fetching emails from Stephen (%s) since today (%s)...", STEPHEN_EMAIL, today_str)
        messages = gmail.list_supplier_messages(
            supplier_email=STEPHEN_EMAIL,
            start_date=today_str,
            end_date=None,
        )
        logger.info("📬 Found %d email(s) from Stephen today", len(messages))

        # Also catch any unprocessed emails from the last 7 days as safety net
        if len(messages) == 0:
            logger.info("📨 No emails today — fetching last 7 days as catch-up...")
            from datetime import timedelta
            week_ago = (_date.today() - timedelta(days=7)).isoformat()
            messages = gmail.list_supplier_messages(
                supplier_email=STEPHEN_EMAIL,
                start_date=week_ago,
                end_date=None,
            )
            logger.info("📬 Found %d email(s) from last 7 days", len(messages))

        new_orders = []
        processed_ids = []
        for msg in messages:
            already = store.is_processed(msg.message_id)
            if already:
                logger.debug("  ⏭️  msg_id=%s already processed — skipping", msg.message_id[:12])
                continue
            logger.info("  🔍 Parsing new msg_id=%s (body=%d chars, pdf=%d chars)",
                        msg.message_id[:12], len(msg.body), len(msg.pdf_text or ""))
            parsed = parser.parse(msg.body, pdf_text=msg.pdf_text or None)
            if not parsed or not parsed.get("item_code", "").strip():
                logger.warning("  ❌ msg_id=%s — parser returned no valid order (result=%s)",
                               msg.message_id[:12], parsed)
                continue
            order = {k: str(v).strip() for k, v in parsed.items()}
            logger.info("  ✅ msg_id=%s → customer=%s item=%s date=%s",
                        msg.message_id[:12], order.get("customer_name"), order.get("item_code"), order.get("order_date"))
            new_orders.append(order)
            processed_ids.append(msg.message_id)

        if not new_orders:
            logger.info("ℹ️  No new parseable orders — nothing to append to OneDrive")
            return {"status": "ok", "orders_found": 0}

        logger.info("📥 Downloading OneDrive document...")
        docx_bytes = download_docx()
        logger.info("✏️  Appending %d new order(s) to document...", len(new_orders))
        updated, appended, skipped = append_orders_to_existing_docx(docx_bytes, new_orders)

        if appended > 0:
            logger.info("📤 Uploading updated document to OneDrive...")
            upload_docx(updated)
            logger.info("✅ OneDrive updated: +%d rows appended, %d duplicates skipped", appended, skipped)
        else:
            logger.info("⏭️  All %d orders already in OneDrive — no upload needed", skipped)

        # Mark emails as processed
        for msg_id in processed_ids:
            try:
                idx = processed_ids.index(msg_id)
                store.mark_processed(ProcessedEmailRecord(
                    message_id=msg_id,
                    customer_name=new_orders[idx].get("customer_name", ""),
                    order_date=new_orders[idx].get("order_date", ""),
                ))
                logger.info("🗂️  Marked msg_id=%s as processed", msg_id[:12])
            except Exception as e:
                logger.error("⚠️  Failed to mark msg_id=%s as processed: %s", msg_id[:12], e)

        logger.info("═" * 50)
        logger.info("🏁 WEBHOOK DONE — appended=%d skipped=%d", appended, skipped)
        logger.info("═" * 50)
        return {"status": "ok", "orders_appended": appended, "orders_skipped": skipped}

    except Exception as e:
        logger.error("═" * 50)
        logger.error("💥 WEBHOOK FAILED: %s", e, exc_info=True)
        logger.error("═" * 50)
        # Return 200 so Pub/Sub doesn't keep retrying on parse errors
        return {"status": "error", "detail": str(e)}


@app.post("/api/renew-gmail-watch")
def renew_gmail_watch():
    """Renew the Gmail push notification watch (expires every 7 days).
    Call this from Cloud Scheduler once a day.
    """
    if not GMAIL_REFRESH_TOKEN:
        raise HTTPException(status_code=503, detail="GMAIL_REFRESH_TOKEN not set")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build as _build

        GCP_PROJECT  = os.environ.get("GCP_PROJECT", "ordersbc-494213")
        PUBSUB_TOPIC = f"projects/{GCP_PROJECT}/topics/gmail-orders"

        creds = Credentials(
            token=None,
            refresh_token=GMAIL_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GMAIL_CLIENT_ID,
            client_secret=GMAIL_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        )
        creds.refresh(Request())
        svc = _build("gmail", "v1", credentials=creds, cache_discovery=False)
        resp = svc.users().watch(
            userId=INBOX_ACCOUNT,
            body={"topicName": PUBSUB_TOPIC, "labelIds": ["INBOX"], "labelFilterBehavior": "INCLUDE"},
        ).execute()
        logger.info("✅ Gmail watch renewed — historyId=%s expiration=%s", resp.get("historyId"), resp.get("expiration"))
        return {"status": "ok", "historyId": resp.get("historyId"), "expiration": resp.get("expiration")}
    except Exception as e:
        logger.error("💥 Failed to renew Gmail watch: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/health")
def health():
    return {"status": "ok", "date": date.today().isoformat()}


# ── Serve frontend static files (built by Vite in Docker stage 1) ──
_frontend_dir = Path(__file__).parent / "frontend_dist"
if _frontend_dir.exists():
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse as _FR

    # Mount Vite's /assets directory
    app.mount("/assets", StaticFiles(directory=str(_frontend_dir / "assets")), name="assets")

    @app.get("/")
    def _serve_index():
        return _FR(str(_frontend_dir / "index.html"))

    @app.get("/{full_path:path}")
    def _serve_spa(full_path: str):
        # Serve actual file if exists, else fallback to index.html (SPA routing)
        file_path = _frontend_dir / full_path
        if file_path.is_file():
            return _FR(str(file_path))
        return _FR(str(_frontend_dir / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
