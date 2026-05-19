"""Supplier email parsers with a registry for extensibility."""

from __future__ import annotations

import logging
import os
import re
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class SupplierParser(ABC):
    """Base parser contract for supplier email body extraction."""

    @abstractmethod
    def parse(self, email_body: str, pdf_text: str | None = None) -> list[dict[str, str]] | None:
        """Parse a supplier email body into a list of normalized order rows.

        Returns a list because a single email may contain several distinct items
        (BUG 3) — one returned dict per item, all sharing the email-level fields
        (order_date, customer_name, ship_by, ...).
        """


class StephenParser(SupplierParser):
    """Parser for the real Stephen supplier email format.

    Handles emails where Item: appears twice per item:
      Item: 302Perch        <- item code
      Item: 2-hole house    <- item name
    And also emails with multiple items, where the same labels repeat:
      Item: 201
      Item: tube feeder red
      Item: 305
      Item: suet feeder blue
    Returns one dict per (code, name) pair.
    """

    # Single-value (email-level) fields
    SIMPLE_PATTERNS: dict[str, re.Pattern[str]] = {
        "order_date":    re.compile(r"^\s*order\s*date\s*:\s*(.+?)\s*$", re.I | re.M),
        "color":         re.compile(r"^\s*color\s*:\s*(.+?)\s*$", re.I | re.M),
        "ship_by":       re.compile(r"^\s*ship\s*by\s*:\s*(.+?)\s*$", re.I | re.M),
        "customer_name": re.compile(r"^\s*customer\s*info\s*:\s*(.+?)\s*$", re.I | re.M),
        "quantity":      re.compile(r"^\s*quantity\s*:\s*(.+?)\s*$", re.I | re.M),
        "brand":         re.compile(r"^\s*brand\s*:\s*(.+?)\s*$", re.I | re.M),
    }

    # All "Item: <value>" lines (we classify each value below)
    ITEM_LINE_RE = re.compile(r"^\s*item\s*:\s*(.+?)\s*$", re.I | re.M)
    # explicit 'Item name:' label
    ITEM_NAME_LABEL_RE = re.compile(r"^\s*item\s*name\s*:\s*(.+?)\s*$", re.I | re.M)
    # A value is considered an item-code when it starts with a digit (e.g. 201, 302Perch)
    CODE_VALUE_RE = re.compile(r"^\d[\w-]*$")

    ALL_FIELDS: tuple[str, ...] = (
        "order_date", "item_code", "item_name", "color",
        "ship_by", "customer_name", "quantity", "brand",
    )

    def parse(self, email_body: str, pdf_text: str | None = None) -> list[dict[str, str]] | None:
        """Extract all order rows from a Stephen email body (one per item)."""
        if not email_body or ":" not in email_body:
            return None

        common: dict[str, str] = {}

        # Simple single-value (email-level) fields
        for key, pattern in self.SIMPLE_PATTERNS.items():
            match = pattern.search(email_body)
            if match:
                common[key] = self._normalize(key, match.group(1))

        # ── BUG 3 FIX: collect ALL Item: lines, then pair codes with names ──
        all_item_values = [v.strip() for v in self.ITEM_LINE_RE.findall(email_body) if v.strip()]
        explicit_names  = [self._normalize("item_name", v)
                           for v in self.ITEM_NAME_LABEL_RE.findall(email_body) if v.strip()]

        codes: list[str] = []
        inline_names: list[str] = []
        for value in all_item_values:
            if self.CODE_VALUE_RE.match(value):
                codes.append(value)
            else:
                inline_names.append(value)

        # Prefer explicitly-labelled names, fall back to the inline "Item: <text>"
        names = explicit_names if explicit_names else inline_names

        # Build one order per item. Pair codes and names positionally; pad with "".
        items_count = max(len(codes), len(names), 1 if (codes or names) else 0)
        if items_count == 0:
            # No item info at all — emit a single empty-item row so the email-level
            # required-fields check below can decide whether to keep it.
            items_count = 1

        orders: list[dict[str, str]] = []
        for i in range(items_count):
            order = dict(common)  # shared email-level fields
            order["item_code"] = codes[i] if i < len(codes) else ""
            order["item_name"] = names[i] if i < len(names) else ""
            for key in self.ALL_FIELDS:
                order.setdefault(key, "")
            orders.append(order)

        # Require at minimum order_date OR customer_name on the email to be a valid order
        if not common.get("order_date") and not common.get("customer_name"):
            return None

        logger.info(
            "StephenParser: parsed %d item row(s) (codes=%s, names=%s)",
            len(orders), codes, names,
        )
        return orders

    @staticmethod
    def _normalize(field: str, value: str) -> str:
        """Strip whitespace; title-case name fields."""
        normalized = re.sub(r"\s+", " ", value).strip()
        if field in ("customer_name", "brand"):
            return normalized.title()
        return normalized


PARSER_REGISTRY: dict[str, SupplierParser] = {
    "stephen": StephenParser(),
}


class SmartParser(SupplierParser):
    """Gemini-first parser with regex fallback.

    Uses Gemini Flash API to extract fields from email body + PDF text.
    Falls back to the regex-based StephenParser if Gemini is unavailable or fails.
    """

    def __init__(self) -> None:
        self._gemini = None
        self._regex_fallback = StephenParser()
        self._gemini_init_attempted = False

    def _ensure_gemini(self) -> bool:
        """Lazy-init the Gemini parser. Returns True if available."""
        if self._gemini is not None:
            return True
        if self._gemini_init_attempted:
            return False

        self._gemini_init_attempted = True
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("GEMINI_API_KEY not set — using regex-only parsing")
            return False

        try:
            from gemini_parser import GeminiParser
            self._gemini = GeminiParser(api_key=api_key)
            logger.info("SmartParser: Gemini parser initialized successfully")
            return True
        except Exception as exc:
            logger.warning("Failed to initialize Gemini parser: %s — using regex fallback", exc)
            return False

    def parse(self, email_body: str, pdf_text: str | None = None) -> list[dict[str, str]] | None:
        """Try Gemini first, fall back to regex parser. Always returns a list."""
        # Try Gemini
        if self._ensure_gemini():
            try:
                result = self._gemini.parse(email_body, pdf_text=pdf_text)
                if result:
                    # Gemini may return either a single dict or a list of dicts.
                    if isinstance(result, dict):
                        result = [result]
                    logger.debug("Gemini parser succeeded (%d row(s))", len(result))
                    return result
                logger.debug("Gemini returned None — falling back to regex")
            except Exception as exc:
                logger.warning("Gemini parse error: %s — falling back to regex", exc)

        # Fallback to regex
        return self._regex_fallback.parse(email_body, pdf_text=pdf_text)


def get_parser(supplier: str = "stephen") -> SupplierParser:
    """Get the best available parser for a supplier.

    Returns SmartParser (Gemini + fallback) if GEMINI_API_KEY is set,
    otherwise returns the regex-only parser.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return SmartParser()
    return PARSER_REGISTRY.get(supplier, StephenParser())
