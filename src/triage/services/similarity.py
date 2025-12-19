"""Email Similarity Engine - detect similar emails and extract templates.

Groups emails from the same sender by subject pattern and extracts
variable parts (merchant names, amounts, order IDs, etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor


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
    """A group of similar emails from the same sender."""

    sender: str
    template_name: str
    template_pattern: str  # The regex or pattern used
    merchant: str | None  # Extracted merchant (if applicable)
    gmail_ids: list[str] = field(default_factory=list)
    amounts: list[ExtractedAmount] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)  # ISO date strings


@dataclass
class SubjectPattern:
    """A subject line pattern for extracting merchant/amount from receipts."""

    name: str
    regex: str
    merchant_group: int = 0  # Capture group for merchant (1-indexed, 0 = none)
    amount_group: int = 0  # Capture group for amount (1-indexed, 0 = none)
    currency_group: int = 0  # Capture group for currency (1-indexed, 0 = none)


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

            # Special case: apple_receipt always has merchant "Apple"
            if p.name == "apple_receipt":
                result.merchant = "Apple"

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


def group_by_similarity(
    emails: list[dict],
    sender: str,
) -> list[SimilarityGroup]:
    """Group emails by subject similarity.

    Emails with the same template and merchant are grouped together.

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

        if amount:
            group.amounts.append(amount)

        if date_obj := email.get("date_header"):
            if isinstance(date_obj, datetime):
                group.dates.append(date_obj.date().isoformat())
            elif isinstance(date_obj, date):
                group.dates.append(date_obj.isoformat())
            else:
                group.dates.append(str(date_obj))

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
