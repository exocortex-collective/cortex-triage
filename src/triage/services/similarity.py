"""Email Similarity Engine - detect similar emails and extract templates.

Groups emails from the same sender by subject pattern and extracts
variable parts (merchant names, amounts, order IDs, etc.).
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg2
import requests
from cortex_utils.logging import get_logger
from cortex_utils.triage_config.models import BUILTIN_BODY_EXTRACTION_PROMPTS_DATA
from psycopg2.extras import RealDictCursor

logger = get_logger(__name__)

# DuckDB API URL for fetching email bodies (required for body extraction)
DUCKDB_API_URL = os.environ.get("DUCKDB_API_URL")

# LLM URL for LLM-based extraction (required for LLM features)
# Support both LLM_URL (new) and OLLAMA_URL (legacy) for backward compatibility
LLM_URL = os.environ.get("LLM_URL") or os.environ.get("OLLAMA_URL")

# LLM model for extraction (default: qwen2.5:3b - fast and good at extraction)
LLM_MODEL = os.environ.get("LLM_MODEL") or os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")

# Merchant name validation bounds
MIN_MERCHANT_LENGTH = 2  # Minimum chars for valid merchant name
MAX_MERCHANT_LENGTH = 100  # Maximum chars for valid merchant name

# Get built-in Apple merchant extraction prompt
APPLE_MERCHANT_PROMPT_TEMPLATE = BUILTIN_BODY_EXTRACTION_PROMPTS_DATA[
    "apple_merchant_v1"
]["template"]


@dataclass
class ExtractedAmount:
    """An amount extracted from a subject or body."""

    amount: Decimal
    currency: str  # USD, EUR, etc.


@dataclass
class SubjectMatch:
    """Result of matching a subject against a template."""

    template_name: str
    merchant: str | None = None
    amount: ExtractedAmount | None = None
    variables: dict[str, str] = field(default_factory=dict)


@dataclass
class SimilarityGroup:
    """A group of similar emails from the same sender.

    Note: gmail_ids, amounts, and dates lists are parallel - they have the same
    length and corresponding indices refer to the same email.
    """

    sender: str
    template_name: str
    template_pattern: str  # The regex or pattern used
    merchant: str | None  # Extracted merchant (if applicable)
    gmail_ids: list[str] = field(default_factory=list)
    amounts: list[ExtractedAmount | None] = field(default_factory=list)
    dates: list[str | None] = field(default_factory=list)  # ISO date strings or None


@dataclass
class SubjectPattern:
    """A subject line pattern for extracting merchant/amount from receipts."""

    name: str
    regex: str
    merchant_group: int = 0  # Capture group for merchant (1-indexed, 0 = none)
    amount_group: int = 0  # Capture group for amount (1-indexed, 0 = none)
    currency_group: int = 0  # Capture group for currency (1-indexed, 0 = none)


@dataclass
class BodyPattern:
    """A body pattern for extracting merchant/amount from email bodies.

    Used when the subject line doesn't contain the merchant info but the body does.
    Matches are determined by sender and/or subject, then extraction happens from body.
    """

    name: str
    # Match criteria (to determine when to apply this pattern)
    sender_regex: str | None = None  # Regex to match sender address
    subject_regex: str | None = None  # Regex to match subject line
    # Extraction from body
    merchant_regex: str | None = None  # Regex to extract merchant from body
    merchant_group: int = 1  # Capture group for merchant (1-indexed)
    amount_regex: str | None = None  # Regex to extract amount from body
    amount_group: int = 1  # Capture group for amount
    currency_regex: str | None = None  # Regex to extract currency from body
    currency_group: int = 1  # Capture group for currency
    # Custom extractor function (takes body text, returns merchant name)
    merchant_extractor: Callable[[str], str | None] | None = None


# =============================================================================
# Subject Template Patterns
# =============================================================================
# Groups are 1-indexed, 0 means not present

SUBJECT_PATTERNS: list[SubjectPattern] = [
    # PayPal receipt: "Host Havoc Ltd.: $10.00 USD"
    # Handles $, €, £ and various currency codes
    SubjectPattern(
        name="paypal_receipt",
        regex=r"^(.+?):\s*[$€£]?([\d,]+\.?\d*)\s*([A-Z]{3})$",
        merchant_group=1,
        amount_group=2,
        currency_group=3,
    ),
    # PayPal auto-payment: "You sent an automatic payment to Wikimedia Foundation, Inc."
    SubjectPattern(
        name="paypal_auto_payment",
        regex=r"^You sent an automatic payment to (.+?)\.{0,3}$",
        merchant_group=1,
    ),
    # PayPal payment received:
    # "Item #ABC123 - Notification of Payment Received from Name (email)"
    SubjectPattern(
        name="paypal_payment_received",
        regex=(
            r"^Item #([A-Z0-9]+) - Notification of Payment Received from "
            r"(.+?) \((.+?)\)$"
        ),
    ),
    # Apple receipt: "Your receipt from Apple."
    SubjectPattern(
        name="apple_receipt",
        regex=r"^Your receipt from Apple\.$",
    ),
    # Generic order shipped: "Your {merchant} order is on its way"
    SubjectPattern(
        name="order_shipped",
        regex=r"^Your (.+?) order is on its way$",
        merchant_group=1,
    ),
]

# Status patterns - these don't have merchants, just categorization
STATUS_PATTERNS: list[tuple[str, str]] = [
    ("recurring_reactivated", r"^Recurring Payment Reactivated$"),
    ("amount_changing", r"^Your automatic payment amount is changing$"),
    ("status_changed", r"^Your automatic payment status has changed$"),
    ("payment_authorized", r"^You authorized a payment$"),
]


# =============================================================================
# Body Template Patterns
# =============================================================================
# Used when merchant info is in the body, not the subject


def _extract_apple_merchant(body_text: str) -> str | None:
    """Extract product name from Apple receipt body using LLM.

    Uses Qwen to parse varied Apple receipt formats for subscriptions and purchases.
    Falls back to regex patterns if LLM extraction fails.

    Examples:
        "TIDAL Music: HiFi Sound" -> "TIDAL Music"
        "iCloud+ with 200 GB" -> "iCloud+ 200GB"
        "Plex: Stream Live TV Channels" -> "Plex Pass"
    """
    # Try LLM extraction first
    if LLM_URL:
        try:
            # Extract just the product line from the receipt table
            product_match = re.search(
                r"\|\s*(.+?)(?:Renews|\n|$)", body_text, re.IGNORECASE
            )
            if not product_match:
                # No clean product line found - skip LLM and use regex fallback
                logger.debug(
                    "Could not find product line for LLM extraction, "
                    "falling back to regex."
                )
            else:
                product_line = product_match.group(1).strip()

                # Format prompt from built-in template
                prompt = APPLE_MERCHANT_PROMPT_TEMPLATE.format(
                    product_line=product_line
                )
                # Remove indentation from prompt
                prompt = textwrap.dedent(prompt).strip()

                # Use OpenAI-compatible endpoint
                response = requests.post(
                    f"{LLM_URL.rstrip('/')}/v1/chat/completions",
                    json={
                        "model": LLM_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 50,
                        "temperature": 0.1,
                    },
                    timeout=15,
                )

                if response.status_code == 200:
                    result: dict[str, Any] = response.json()
                    # Parse OpenAI-compatible response format
                    choices = result.get("choices", [])
                    if choices and len(choices) > 0:
                        message = choices[0].get("message", {})
                        merchant_raw = message.get("content", "")
                        if not isinstance(merchant_raw, str):
                            return None
                        merchant = merchant_raw.strip()
                        # Clean up common LLM artifacts
                        merchant = merchant.strip('"').strip("'").strip()
                        if (
                            merchant
                            and len(merchant) > MIN_MERCHANT_LENGTH
                            and len(merchant) < MAX_MERCHANT_LENGTH
                        ):
                            logger.debug(f"LLM extracted Apple merchant: {merchant}")
                            return merchant

        except (requests.RequestException, ValueError) as e:
            logger.warning(f"LLM extraction failed for Apple receipt: {e}")

    # Fallback to regex patterns
    # Try iCloud+ with storage size
    match = re.search(r"iCloud\+\s*with\s+(\d+)\s*GB", body_text, re.IGNORECASE)
    if match:
        return f"iCloud+ {match.group(1)}GB"

    # Try Plex Pass
    if re.search(r"Plex\s+Pass", body_text, re.IGNORECASE):
        return "Plex Pass"

    # Try AppleCare+ with device type
    if re.search(r"AppleCare\+", body_text, re.IGNORECASE):
        device_match = re.search(
            r"(IPAD\s+(?:MINI|PRO|AIR)|IPHONE|MACBOOK|APPLE\s+WATCH|MAC\s+\w+)",
            body_text,
            re.IGNORECASE,
        )
        if device_match:
            device = device_match.group(1).title()
            device = re.sub(r"\s+", " ", device).strip()
            return f"AppleCare+ {device}"
        return "AppleCare+"

    # Generic fallback: first product after table marker
    match = re.search(r"\|\s*([A-Za-z][A-Za-z0-9 \+]+)", body_text)
    if match:
        product = match.group(1).strip()
        product = re.sub(
            r"\s*\(?(?:Monthly|Yearly|Technical|with)\b.*",
            "",
            product,
            flags=re.IGNORECASE,
        ).strip()
        if product and len(product) > 2:
            return product

    return None


BODY_PATTERNS: list[BodyPattern] = [
    # Apple receipt - uses custom extraction via _extract_apple_merchant()
    BodyPattern(
        name="apple_receipt",
        sender_regex=r"no_reply@email\.apple\.com",
        subject_regex=r"Your receipt from Apple\.",
        amount_regex=r"\|\s*\$(\d+\.?\d*)",
        amount_group=1,
        merchant_extractor=_extract_apple_merchant,
    ),
]


def fetch_body_text(gmail_id: str) -> str | None:
    """Fetch email body text from DuckDB API.

    Args:
        gmail_id: Gmail message ID

    Returns:
        Plain text body content, or None if not available or API not configured
    """
    if not DUCKDB_API_URL:
        return None
    try:
        response = requests.get(
            f"{DUCKDB_API_URL}/mail_text",
            params={"gmail_id": gmail_id},
            timeout=5,
        )
        if response.status_code == 200:
            try:
                data: dict[str, str | None] = response.json()
                return data.get("text")
            except requests.exceptions.JSONDecodeError:
                logger.warning("Failed to decode JSON from DuckDB API for %s", gmail_id)
                return None
        elif response.status_code == 404:
            return None
        else:
            logger.warning(
                "DuckDB API error fetching body for %s: %s",
                gmail_id,
                response.status_code,
            )
            return None
    except requests.RequestException as e:
        logger.warning("Failed to fetch body from DuckDB API for %s: %s", gmail_id, e)
        return None


def match_body(
    sender: str,
    subject: str,
    body_text: str,
) -> SubjectMatch | None:
    """Match email body against known patterns and extract variables.

    Args:
        sender: Email sender address
        subject: Email subject line
        body_text: Plain text body content

    Returns:
        SubjectMatch with extracted merchant/amount, or None if no match
    """
    for p in BODY_PATTERNS:
        # Check if this pattern applies to this email
        if p.sender_regex and not re.search(p.sender_regex, sender, re.IGNORECASE):
            continue
        if p.subject_regex and not re.search(p.subject_regex, subject, re.IGNORECASE):
            continue

        # Pattern matches - try to extract from body
        result = SubjectMatch(template_name=p.name)

        # Use custom extractor if provided, otherwise use regex
        if p.merchant_extractor:
            result.merchant = p.merchant_extractor(body_text)
        elif p.merchant_regex:
            match = re.search(p.merchant_regex, body_text, re.IGNORECASE)
            if match and p.merchant_group > 0:
                merchant = match.group(p.merchant_group).strip()
                # Normalize whitespace
                merchant = re.sub(r"\s+", " ", merchant)
                result.merchant = merchant

        if p.amount_regex:
            match = re.search(p.amount_regex, body_text)
            if match and p.amount_group > 0:
                try:
                    amount_str = match.group(p.amount_group).replace(",", "")
                    result.amount = ExtractedAmount(
                        amount=Decimal(amount_str),
                        currency="USD",  # Default, could extract if needed
                    )
                except (ValueError, IndexError):
                    pass

        return result

    return None


def match_subject(subject: str) -> SubjectMatch | None:
    """Match a subject against known patterns and extract variables.

    Returns None if no pattern matches.
    """
    # Try receipt/transaction patterns first
    for p in SUBJECT_PATTERNS:
        match = re.match(p.regex, subject, re.IGNORECASE)
        if match:
            result = SubjectMatch(template_name=p.name)

            if p.merchant_group > 0:
                merchant = match.group(p.merchant_group).strip()
                # Clean up truncated merchants (ending with ...)
                merchant = re.sub(r"\.{2,}$", "", merchant).strip()
                result.merchant = merchant

            if p.amount_group > 0 and p.currency_group > 0:
                try:
                    amount_str = match.group(p.amount_group).replace(",", "")
                    result.amount = ExtractedAmount(
                        amount=Decimal(amount_str),
                        currency=match.group(p.currency_group).upper(),
                    )
                except (ValueError, IndexError):
                    pass

            # Generic merchant for patterns that need body extraction
            # These will be enriched by match_body() in group_by_similarity()
            if p.name == "apple_receipt":
                result.merchant = "Apple"  # Will be refined by body extraction

            return result

    # Try status patterns (no extraction needed)
    for name, pattern in STATUS_PATTERNS:
        if re.match(pattern, subject, re.IGNORECASE):
            return SubjectMatch(template_name=name)

    return None


def extract_merchant_from_subject(
    subject: str,
) -> tuple[str | None, ExtractedAmount | None]:
    """Extract merchant and amount from a subject line.

    Returns (merchant, amount) tuple. Either or both may be None.

    Examples:
        "Host Havoc Ltd.: $10.00 USD" -> ("Host Havoc Ltd.", Amount(10.00, "USD"))
        "Apple Services: €9.99 EUR" -> ("Apple Services", Amount(9.99, "EUR"))
        "Your receipt from Apple." -> ("Apple", None)
    """
    result = match_subject(subject)
    if result:
        return result.merchant, result.amount

    return None, None


def get_sender_emails(
    conn: psycopg2.extensions.connection,
    sender: str,
    limit: int = 100,
) -> list[dict]:
    """Get recent emails from a sender.

    Returns list of dicts with gmail_id, subject, date_header, from_addr.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                ep.gmail_id,
                ep.subject,
                ep.date_header,
                ep.from_addr
            FROM emails_parsed ep
            WHERE ep.from_addr = %s
            ORDER BY ep.date_header DESC
            LIMIT %s
            """,
            (sender, limit),
        )
        return [dict(row) for row in cur.fetchall()]


# Merchants that are too generic - try body extraction for these
GENERIC_MERCHANTS = {"Apple"}


def group_by_similarity(
    emails: list[dict],
    sender: str,
) -> list[SimilarityGroup]:
    """Group emails by subject similarity.

    Emails with the same template and merchant are grouped together.
    For emails with generic merchants (like "Apple"), attempts to extract
    more specific merchant from email body.

    Args:
        emails: List of email dicts with gmail_id, subject, date_header
        sender: The sender address

    Returns:
        List of SimilarityGroup objects
    """
    # Group key: (template_name, merchant)
    groups: dict[tuple[str, str | None], SimilarityGroup] = {}

    for email in emails:
        subject = email.get("subject", "")
        gmail_id = email.get("gmail_id", "")
        result = match_subject(subject)

        if result is None:
            # No pattern match - put in "unknown" bucket
            template_name = "unknown"
            merchant = None
            amount = None
        else:
            template_name = result.template_name
            merchant = result.merchant
            amount = result.amount

            # If merchant is generic, try to extract from body
            if merchant in GENERIC_MERCHANTS:
                body_text = fetch_body_text(gmail_id)
                if body_text:
                    body_result = match_body(sender, subject, body_text)
                    if body_result and body_result.merchant:
                        merchant = body_result.merchant
                        # Also use body amount if available
                        if body_result.amount:
                            amount = body_result.amount

        key = (template_name, merchant)

        if key not in groups:
            groups[key] = SimilarityGroup(
                sender=sender,
                template_name=template_name,
                template_pattern="",  # Could store regex here
                merchant=merchant,
            )

        group = groups[key]
        group.gmail_ids.append(email["gmail_id"])

        # Always append to keep lists parallel with gmail_ids
        group.amounts.append(amount)

        date_str: str | None = None
        if date_obj := email.get("date_header"):
            if isinstance(date_obj, datetime):
                date_str = date_obj.date().isoformat()
            elif isinstance(date_obj, date):
                date_str = date_obj.isoformat()
            else:
                date_str = str(date_obj)
        group.dates.append(date_str)

    return list(groups.values())


def find_similar_emails(
    conn: psycopg2.extensions.connection,
    sender: str,
    subject: str,
    limit: int = 100,
) -> SimilarityGroup | None:
    """Find emails similar to the given subject from the same sender.

    This is the main entry point for pattern detection. Given a new email,
    find historical emails that match the same pattern.

    Returns the similarity group if found, None otherwise.
    """
    # First, match the current subject
    result = match_subject(subject)
    if result is None:
        return None

    merchant = result.merchant

    # Get historical emails from this sender
    emails = get_sender_emails(conn, sender, limit)

    # Group them
    groups = group_by_similarity(emails, sender)

    # Find the group that matches our template + merchant
    for group in groups:
        if group.template_name == result.template_name and group.merchant == merchant:
            return group

    return None


def analyze_sender(
    conn: psycopg2.extensions.connection,
    sender: str,
    limit: int = 100,
) -> list[SimilarityGroup]:
    """Analyze all emails from a sender and return similarity groups.

    Useful for discovering patterns from a sender.
    """
    emails = get_sender_emails(conn, sender, limit)
    return group_by_similarity(emails, sender)
