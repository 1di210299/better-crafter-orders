"""Scheduled Firebase Cloud Function entrypoint for supplier order automation."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import firebase_admin
from firebase_admin import initialize_app
from firebase_functions import options, scheduler_fn

import config
from email_parser import get_parser
from firestore_client import ProcessedEmailRecord, ProcessedEmailStore
from gmail_client import GmailClient
from word_generator import WordReportGenerator
from onedrive_client import download_docx, upload_docx

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN", "")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID", "")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not firebase_admin._apps:
    if config.STORAGE_BUCKET:
        initialize_app(options={"storageBucket": config.STORAGE_BUCKET})
    else:
        initialize_app()


def _is_valid_order(order: dict) -> bool:
    """Filter out orders without at least item_code + customer_name."""
    return bool(order.get("item_code", "").strip()) and bool(order.get("customer_name", "").strip())


@scheduler_fn.on_schedule(
    # BUG 5: Run once daily at 02:00 America/New_York (ET, handles DST automatically).
    schedule="0 2 * * *",
    timezone=scheduler_fn.Timezone("America/New_York"),
    region="us-central1",
    memory=options.MemoryOption.MB_512,
    # BUG 5: bumped timeout so long Gmail/Gemini runs don't get killed silently.
    timeout_sec=540,
)
def process_stephen_orders(event: scheduler_fn.ScheduledEvent) -> None:
    """Process outgoing emails to Stephen, generate a Word report, and upload it."""
    del event

    # ── BUG 5: health-check log so we always know the job triggered ─────────
    run_started_utc = datetime.now(timezone.utc).isoformat()
    logger.info("🚀 Job started at %s (UTC)", run_started_utc)

    client_id = GMAIL_CLIENT_ID
    client_secret = GMAIL_CLIENT_SECRET
    refresh_token = GMAIL_REFRESH_TOKEN

    # Set GEMINI_API_KEY env so get_parser() picks it up
    os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY

    try:
        gmail_client = GmailClient(
            gmail_account=config.GMAIL_ACCOUNT,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
        )
    except Exception as error:
        # BUG 5: surface OAuth/refresh-token failures loudly instead of silent exit.
        logger.error("❌ Gmail client init failed (OAuth refresh token?): %s", error, exc_info=error)
        raise

    store = ProcessedEmailStore(config.FIRESTORE_COLLECTION)
    parser = get_parser("stephen")

    # BUG 1 / BUG 5: use the real signature (start_date / end_date) instead of the
    # non-existent `hours_back` kwarg that was raising TypeError on every run and
    # silently killing the whole job (so Erick's email — and many others — were
    # never processed at all).
    lookback_days = max(1, int(config.SEARCH_HOURS_BACK / 24) or 1)
    start_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    messages = gmail_client.list_supplier_messages(
        supplier_email=config.STEPHEN_EMAIL,
        start_date=start_date,
    )

    logger.info("📬 Gmail returned %d candidate messages (since %s)", len(messages), start_date)

    if not messages:
        logger.info("No supplier emails found; exiting gracefully")
        return

    parsed_orders: list[dict[str, str]] = []
    message_to_order: list[tuple[str, dict[str, str]]] = []

    for message in messages:
        # BUG 1: per-message trace so dropped orders (Erick etc.) are visible in logs.
        body_preview = (message.body or "").replace("\n", " ⏎ ")[:160]
        logger.info(
            "🔍 msg=%s thread=%s pdfs=%d body_preview=%r",
            message.message_id, message.thread_id,
            len(message.pdf_filenames), body_preview,
        )
        try:
            if store.is_processed(message.message_id):
                logger.info("⏭️  Skipping already processed message: %s", message.message_id)
                continue

            parsed = parser.parse(message.body, pdf_text=message.pdf_text or None)
            if not parsed:
                logger.warning("❌ Parser returned nothing for msg=%s", message.message_id)
                continue

            # BUG 3: parser now returns list[dict] — one entry per item in the email.
            for sub in parsed:
                if not _is_valid_order(sub):
                    logger.warning(
                        "❌ Skipped sub-order from msg=%s (missing item_code/customer_name): %s",
                        message.message_id, sub,
                    )
                    continue

                order = _sanitize_order(sub)

                # BUG 4: override the body's "order date" with the actual Gmail send date.
                if message.internal_date_ms:
                    try:
                        sent_dt = datetime.fromtimestamp(
                            message.internal_date_ms / 1000, tz=timezone.utc
                        )
                        order["order_date"] = sent_dt.strftime("%m/%d")
                    except Exception as exc:
                        logger.warning("Could not format internalDate for %s: %s",
                                       message.message_id, exc)

                logger.info(
                    "✅ Parsed order msg=%s customer=%r item_code=%r date=%r",
                    message.message_id,
                    order.get("customer_name"), order.get("item_code"), order.get("order_date"),
                )
                parsed_orders.append(order)
                message_to_order.append((message.message_id, order))
        except Exception as error:
            logger.error("Failed to process message %s", message.message_id, exc_info=error)
            continue

    if not parsed_orders:
        logger.info("No new parseable orders found")
        return

    # ── Append to OneDrive master file (Bird Feeders & Houses - Steven 2026) ──
    try:
        from word_generator import append_orders_to_existing_docx
        logger.info("📥 Downloading Steven's OneDrive file...")
        docx_bytes = download_docx()
        updated_bytes, appended, skipped = append_orders_to_existing_docx(docx_bytes, parsed_orders)
        if appended > 0:
            logger.info("📤 Uploading updated file (%d new rows, %d skipped)...", appended, skipped)
            upload_docx(updated_bytes)
            logger.info("✅ OneDrive file updated successfully")
        else:
            logger.info("⏭️  All %d orders already in OneDrive file — skipping upload", skipped)
    except Exception as error:
        logger.error("⚠️  OneDrive append failed (continuing without it): %s", error, exc_info=error)
        # Don't stop here — still mark emails as processed even if OneDrive fails

    report_generator = WordReportGenerator(_resolve_template_path(config.TEMPLATE_PATH))
    report_date = date.today()

    try:
        report_path = report_generator.generate_daily_report(parsed_orders, report_date)
        uploaded = report_generator.upload_report(report_path, report_date)
        logger.info("Report uploaded to %s", uploaded.storage_path)
        logger.info("Signed URL (valid 7 days): %s", uploaded.signed_url)
    except Exception as error:
        logger.error("Failed to generate or upload report", exc_info=error)
        return

    for message_id, order in message_to_order:
        try:
            store.mark_processed(
                ProcessedEmailRecord(
                    message_id=message_id,
                    customer_name=order.get("customer_name", ""),
                    order_date=order.get("order_date", ""),
                )
            )
        except Exception as error:
            logger.error("Failed to mark message as processed: %s", message_id, exc_info=error)



def _sanitize_order(parsed: dict[str, Any]) -> dict[str, str]:
    """Normalize parser output into report-safe string fields."""
    return {
        "order_date": str(parsed.get("order_date", "")).strip(),
        "item_code": str(parsed.get("item_code", "")).strip(),
        "quantity": str(parsed.get("quantity", "")).strip(),
        "color": str(parsed.get("color", "")).strip(),
        "ship_by": str(parsed.get("ship_by", "")).strip(),
        "customer_name": str(parsed.get("customer_name", "")).strip(),
    }


def _resolve_template_path(template_path: str) -> str:
    """Resolve template path for local and deployed execution contexts."""
    direct_path = Path(template_path)
    if direct_path.exists():
        return str(direct_path)

    fallback_path = Path(__file__).resolve().parent.parent / template_path
    if fallback_path.exists():
        return str(fallback_path)

    raise FileNotFoundError(f"Unable to locate template file: {template_path}")
