"""Gemini-powered email/PDF parser for supplier order extraction."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Fields we want Gemini to extract
ORDER_FIELDS = (
    "order_date",
    "item_code",
    "item_name",
    "quantity",
    "color",
    "ship_by",
    "customer_name",
    "brand",
)

EXTRACTION_PROMPT = """\
You are a data-extraction assistant. Given the text content of a supplier order email \
(and optionally text extracted from an attached PDF), extract one ORDER ROW per item.

An email may contain multiple items (different item codes / names on separate lines).
You MUST return ONE object per item. Email-level fields (order_date, customer_name,
ship_by, brand, color, quantity) are repeated for every item in that email.

Return ONLY a JSON object of the form:
    {{ "orders": [ {{ ...8 fields... }}, {{ ...8 fields... }} ] }}

Each order object must have exactly these 8 keys:
- order_date: The order date (keep original format, e.g. "4/15" or "04/15/2026")
- item_code: The item number/code (alphanumeric, e.g. "302Perch", "100", "201")
- item_name: The item description/name (e.g. "tube feeder red", "2-hole house")
- quantity: The quantity ordered (just the number)
- color: The color of the item. If not mentioned, use ""
- ship_by: The ship-by / delivery date
- customer_name: The customer name or customer info
- brand: The brand name

Rules:
1. Return ONLY valid JSON in the shape {{"orders": [...]}}. No prose, no markdown fences.
2. If a field cannot be found, use an empty string "".
3. Do NOT invent or hallucinate data. Only extract what is present.
4. For item_code: look for "Item:" followed by a code starting with digits, or "Item No.", "Item #", etc.
5. For item_name: look for "Item:" followed by a name starting with letters, or "Item name:", or the product description.
6. For customer_name: look for "Customer info:", "Customer:", "Ship to:", "Customer name:", etc.
7. Title-case customer_name and brand values.
8. If the email contains N items, return N order objects (NOT one). Pair each item_code
   with its corresponding item_name in document order.
9. If the email contains only one item, still return a single-element list under "orders".

---
EMAIL BODY:
{email_body}
{pdf_section}
---

JSON:
"""

PDF_SECTION_TEMPLATE = """
---
ATTACHED PDF TEXT:
{pdf_text}
"""


class GeminiParser:
    """Parses supplier emails using Google Gemini Flash API."""

    def __init__(self, api_key: str | None = None, model_name: str = "gemini-2.0-flash") -> None:
        """Initialize with Gemini API key (from param or env)."""
        key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise ValueError(
                "GEMINI_API_KEY is required. Set it in .env or pass it directly."
            )
        self._client = genai.Client(api_key=key)
        self._model_name = model_name
        logger.info("GeminiParser initialized with model: %s", model_name)

    def parse(
        self,
        email_body: str,
        pdf_text: str | None = None,
    ) -> list[dict[str, str]] | None:
        """Extract one or more order rows from email body (+ optional PDF text).

        Returns a list of dicts (one per item, BUG 3 fix), or None on failure.
        """
        if not email_body and not pdf_text:
            return None

        # Build prompt
        pdf_section = ""
        if pdf_text and pdf_text.strip():
            pdf_section = PDF_SECTION_TEMPLATE.format(pdf_text=pdf_text.strip())

        prompt = EXTRACTION_PROMPT.format(
            email_body=email_body or "(no email body)",
            pdf_section=pdf_section,
        )

        try:
            response = self._client.models.generate_content(
                model=self._model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=1024,
                ),
            )
            raw_text = response.text.strip()
            parsed = self._parse_json_response(raw_text)

            if parsed is None:
                logger.warning("Gemini returned unparseable response: %s", raw_text[:200])
                return None

            # Accept either {"orders": [...]} or a bare list/dict for backwards compat.
            if isinstance(parsed, dict) and "orders" in parsed and isinstance(parsed["orders"], list):
                raw_orders = parsed["orders"]
            elif isinstance(parsed, list):
                raw_orders = parsed
            elif isinstance(parsed, dict):
                raw_orders = [parsed]
            else:
                logger.warning("Gemini returned unexpected JSON shape: %r", parsed)
                return None

            results: list[dict[str, str]] = []
            for entry in raw_orders:
                if not isinstance(entry, dict):
                    continue
                row: dict[str, str] = {}
                for field in ORDER_FIELDS:
                    val = str(entry.get(field, "")).strip()
                    if field in ("customer_name", "brand") and val:
                        val = val.title()
                    row[field] = val
                results.append(row)

            if not results:
                return None

            # Require at minimum order_date or customer_name on the *email* (first row).
            head = results[0]
            if not head.get("order_date") and not head.get("customer_name"):
                logger.warning("Gemini extracted no order_date and no customer_name — skipping")
                return None

            logger.info("Gemini parsed %d order row(s)", len(results))
            return results

        except Exception as exc:
            logger.error("Gemini API call failed: %s", exc, exc_info=True)
            return None

    @staticmethod
    def _parse_json_response(text: str) -> dict | list | None:
        """Parse JSON from Gemini response, handling markdown fences."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.M)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.M)
        cleaned = cleaned.strip()

        try:
            obj = json.loads(cleaned)
            if isinstance(obj, (dict, list)):
                return obj
        except json.JSONDecodeError:
            pass

        # Try to find a JSON object {...} or array [...] in the text
        match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.S)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return None
