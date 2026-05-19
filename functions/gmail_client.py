"""Gmail API client for fetching supplier emails from sent mailbox."""

from __future__ import annotations

import base64
import io
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pdfplumber
from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GmailMessage:
    """Represents a Gmail message with decoded body content and optional PDF text."""

    message_id: str
    thread_id: str
    body: str
    pdf_text: str = ""
    pdf_filenames: list[str] = field(default_factory=list)
    # BUG 4: Gmail API internalDate (Unix ms). Used as the canonical "email sent date"
    # for the Word file, instead of whatever date the customer wrote inside the body.
    internal_date_ms: int = 0


class GmailClient:
    """Wrapper around Gmail API calls with retry and body extraction."""

    def __init__(
        self,
        gmail_account: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> None:
        """Initialize Gmail API service with OAuth2 refresh-token credentials."""
        self._gmail_account = gmail_account
        logger.info("  📧 Account: %s", gmail_account)
        self._credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=config.GMAIL_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[config.GMAIL_READONLY_SCOPE],
        )
        logger.info("  🔄 Refreshing OAuth2 token...")
        self._credentials.refresh(Request())
        logger.info("  ✅ Token refreshed")
        self._service: Resource = build(
            "gmail",
            "v1",
            credentials=self._credentials,
            cache_discovery=False,
        )
        logger.info("  ✅ Gmail service built")

    def list_supplier_messages(self, supplier_email: str, start_date: str = None, end_date: str = None) -> list[GmailMessage]:
        """Fetch supplier order emails using threads.list + threads.get.

        Strategy (fixes the "blank customer name" bug):
        - Use threads.list so that a thread is returned if ANY message in it
          matches the query (e.g. the PDF is only in a reply, not the original).
        - For each thread, fetch ALL messages via threads.get.
        - Use the FIRST (oldest) message body as the order data source.
        - Collect PDFs from EVERY message in the thread.

        This ensures we never parse a follow-up reply body instead of the
        original order email, even when the PDF arrives in a later reply.

        start_date / end_date: 'YYYY-MM-DD' strings, both optional.
        """
        search_query = self._build_search_query(supplier_email, start_date, end_date)
        logger.info("  🔎 Gmail threads query: %s", search_query)

        # ── Step 1: list matching thread IDs ────────────────────────────────
        thread_refs: list[dict] = []
        page_token: str | None = None
        page_num = 0

        while True:
            page_num += 1
            list_kwargs: dict[str, Any] = {
                "userId": self._gmail_account,
                "q": search_query,
                "maxResults": 500,
            }
            if page_token:
                list_kwargs["pageToken"] = page_token

            listing = self._execute_with_retry(
                "users.threads.list",
                lambda kw=list_kwargs: self._service.users()
                .threads()
                .list(**kw)
                .execute(),
            )

            batch = listing.get("threads", [])
            thread_refs.extend(batch)
            logger.info(
                "  📄 Page %d: got %d threads (total so far: %d)",
                page_num, len(batch), len(thread_refs),
            )

            page_token = listing.get("nextPageToken")
            if not page_token:
                break

        logger.info("  📬 Total thread refs: %d (across %d pages)", len(thread_refs), page_num)

        # ── Step 2: fetch each thread and extract first-message body + all PDFs
        messages: list[GmailMessage] = []

        for i, ref in enumerate(thread_refs):
            thread_id = ref.get("id")
            if not thread_id:
                continue

            logger.info("  🧵 [%d/%d] Fetching thread %s...", i + 1, len(thread_refs), thread_id[:8])

            thread_data = self._execute_with_retry(
                "users.threads.get",
                lambda tid=thread_id: self._service.users()
                .threads()
                .get(userId=self._gmail_account, id=tid, format="full")
                .execute(),
            )

            # threads.get returns messages already sorted oldest→newest
            thread_msgs: list[dict] = thread_data.get("messages", [])
            if not thread_msgs:
                logger.warning("       ⚠️  Thread %s has no messages — skipping", thread_id[:8])
                continue

            first_msg_payload = thread_msgs[0]
            first_message_id  = first_msg_payload.get("id", "")
            # BUG 4: capture the actual Gmail send timestamp of the first message.
            try:
                first_internal_date_ms = int(first_msg_payload.get("internalDate", 0))
            except (TypeError, ValueError):
                first_internal_date_ms = 0

            # Log headers of FIRST message
            headers = {
                h["name"].lower(): h["value"]
                for h in first_msg_payload.get("payload", {}).get("headers", [])
            }
            logger.info("       📅 Date:    %s", headers.get("date", "?"))
            logger.info("       📝 Subject: %s", headers.get("subject", "?"))
            logger.info("       👤 From:    %s", headers.get("from", "?"))
            logger.info("       📨 To:      %s", headers.get("to", "?"))
            if headers.get("cc"):
                logger.info("       📋 Cc:      %s", headers["cc"])
            logger.info("       📨 Thread msgs total: %d", len(thread_msgs))

            # Body always from the FIRST (original order) message
            decoded_body = self._extract_preferred_body(first_msg_payload.get("payload", {}))
            logger.info("       body=%d chars (from msg #1 of thread)", len(decoded_body))

            # PDFs collected from ALL messages in the thread
            all_pdf_texts:     list[str] = []
            all_pdf_filenames: list[str] = []

            for msg_idx, msg_payload in enumerate(thread_msgs):
                msg_id  = msg_payload.get("id", "")
                pdf_text, pdf_filenames = self._extract_pdf_attachments(msg_payload, msg_id)
                if pdf_text:
                    all_pdf_texts.append(pdf_text)
                all_pdf_filenames.extend(pdf_filenames)
                if pdf_filenames:
                    logger.info(
                        "       📎 msg #%d (%s): %d PDF(s): %s",
                        msg_idx + 1, msg_id[:8], len(pdf_filenames), pdf_filenames,
                    )

            combined_pdf_text = "\n\n---\n\n".join(all_pdf_texts)

            messages.append(
                GmailMessage(
                    message_id=first_message_id,
                    thread_id=thread_id,
                    body=decoded_body,
                    pdf_text=combined_pdf_text,
                    pdf_filenames=all_pdf_filenames,
                    internal_date_ms=first_internal_date_ms,
                )
            )

        # Preserve oldest-first order (threads.list returns newest-first)
        messages.reverse()
        logger.info("  ✅ Processed %d threads (oldest-first)", len(messages))
        return messages

    @staticmethod
    def _build_search_query(supplier_email: str, start_date: str = None, end_date: str = None) -> str:
        """Build Gmail query using Gmail's after:/before: date operators.

        HARDCODED:
        - Inbox: bettercrafter1@gmail.com (BCC'd on every order)
        - Supplier: passed in as parameter

        start_date / end_date: 'YYYY-MM-DD' strings (optional).
        If neither is given, defaults to last 7 days.
        """
        from datetime import datetime, timedelta
        inbox_account = "bettercrafter1@gmail.com"

        # Build date part of query
        if start_date or end_date:
            date_part = ""
            if start_date:
                # Gmail after: is exclusive, so use same date (it includes that day)
                date_part += f"after:{start_date.replace('-', '/')} "
            if end_date:
                # Gmail before: is exclusive, add 1 day to include end_date itself
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
                date_part += f"before:{end_dt.strftime('%Y/%m/%d')} "
        else:
            # Default: last 7 days
            since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y/%m/%d")
            date_part = f"after:{since} "

        query = (
            f"deliveredto:{inbox_account} "
            f"to:{supplier_email} "
            f"{date_part.strip()}"
        )
        # NOTE: We intentionally do NOT filter by `has:attachment filename:pdf`.
        # Some legitimate orders are sent as plain text without a PDF attachment
        # (e.g. when the label is sent later in a separate email). Filtering by
        # PDF would silently drop those orders. The parser handles both cases.
        logger.info("  🛠️  Building query:")
        logger.info("       inbox      = %s", inbox_account)
        logger.info("       supplier   = %s", supplier_email)
        logger.info("       start_date = %s", start_date or "(none)")
        logger.info("       end_date   = %s", end_date or "(none)")
        logger.info("       full q     = %s", query)
        return query

    def _execute_with_retry(self, operation: str, func: Callable[[], Any]) -> Any:
        """Execute Gmail API call with exponential backoff retry."""
        max_retries = 5
        delay_seconds = 1

        for attempt in range(1, max_retries + 1):
            try:
                return func()
            except (HttpError, TimeoutError) as error:
                if attempt == max_retries:
                    logger.error("Gmail operation failed after retries: %s", operation, exc_info=error)
                    raise

                logger.warning(
                    "Gmail operation '%s' failed on attempt %s/%s; retrying in %ss",
                    operation,
                    attempt,
                    max_retries,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds *= 2

    def _extract_preferred_body(self, payload: dict[str, Any]) -> str:
        """Prefer plain text body, then HTML fallback converted to text."""
        plain_text = self._find_body_by_mime(payload, "text/plain")
        if plain_text:
            return plain_text

        html_text = self._find_body_by_mime(payload, "text/html")
        if html_text:
            return self._strip_html(html_text)

        return ""

    def _find_body_by_mime(self, payload: dict[str, Any], target_mime: str) -> str | None:
        """Recursively scan MIME parts and decode body for a target content-type."""
        mime_type = payload.get("mimeType", "")
        if mime_type == target_mime:
            data = payload.get("body", {}).get("data")
            if data:
                return self._decode_base64_to_text(data)

        for part in payload.get("parts", []) or []:
            found = self._find_body_by_mime(part, target_mime)
            if found:
                return found

        if not payload.get("parts") and mime_type.startswith(target_mime):
            data = payload.get("body", {}).get("data")
            if data:
                return self._decode_base64_to_text(data)

        return None

    @staticmethod
    def _decode_base64_to_text(data: str) -> str:
        """Decode URL-safe base64 string into UTF-8 text."""
        padding = "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(data + padding)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _strip_html(html_content: str) -> str:
        """Convert HTML content to normalized plain text."""
        soup = BeautifulSoup(html_content, "lxml")
        return soup.get_text(separator="\n", strip=True)

    # ── PDF attachment extraction ───────────────────────────────

    def _extract_pdf_attachments(
        self, message_data: dict[str, Any], message_id: str
    ) -> tuple[str, list[str]]:
        """Extract text from all PDF attachments in a message.

        Returns (combined_pdf_text, list_of_pdf_filenames).
        """
        payload = message_data.get("payload", {})
        parts = self._collect_all_parts(payload)

        pdf_texts: list[str] = []
        pdf_filenames: list[str] = []

        for part in parts:
            mime_type = part.get("mimeType", "")
            filename = part.get("filename", "")

            if mime_type != "application/pdf" and not filename.lower().endswith(".pdf"):
                continue

            attachment_id = part.get("body", {}).get("attachmentId")
            if not attachment_id:
                continue

            try:
                att_data = self._execute_with_retry(
                    "users.messages.attachments.get",
                    lambda aid=attachment_id, mid=message_id: self._service.users()
                    .messages()
                    .attachments()
                    .get(userId=self._gmail_account, messageId=mid, id=aid)
                    .execute(),
                )
                raw_bytes = base64.urlsafe_b64decode(att_data.get("data", "") + "==")
                text = self._extract_text_from_pdf_bytes(raw_bytes)
                if text.strip():
                    pdf_texts.append(text)
                    pdf_filenames.append(filename or f"attachment_{attachment_id[:8]}.pdf")
                    logger.info(
                        "Extracted %d chars from PDF '%s' in message %s",
                        len(text), filename, message_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Failed to extract PDF '%s' from message %s: %s",
                    filename, message_id, exc,
                )

        combined_text = "\n\n---\n\n".join(pdf_texts) if pdf_texts else ""
        return combined_text, pdf_filenames

    @staticmethod
    def _collect_all_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Recursively collect all MIME parts from a message payload."""
        parts: list[dict[str, Any]] = []
        stack = [payload]
        while stack:
            current = stack.pop()
            parts.append(current)
            for child in current.get("parts", []) or []:
                stack.append(child)
        return parts

    @staticmethod
    def _extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
        """Extract text from raw PDF bytes using pdfplumber."""
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages_text = []
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages_text.append(text)
                return "\n\n".join(pages_text)
        except Exception as exc:
            logger.warning("pdfplumber failed to extract text: %s", exc)
            return ""
