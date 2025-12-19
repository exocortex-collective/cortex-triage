"""Data models for email pattern detection (subscriptions, recurring receipts)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class IntervalType(str, Enum):
    """Types of recurring intervals."""

    MONTHLY = "monthly"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"
    DAY_OF_WEEK = "day_of_week"  # e.g., every Monday
    IRREGULAR = "irregular"  # Detected pattern but no clear interval


class PatternStatus(str, Enum):
    """Lifecycle status of a pattern."""

    EMERGING = "emerging"  # < 3 occurrences, not yet confirmed
    ACTIVE = "active"  # Confirmed pattern, actively matching
    DORMANT = "dormant"  # Expected email missed, pattern paused
    ENDED = "ended"  # Pattern explicitly ended or decayed


class IntervalParams(BaseModel):
    """Parameters describing the recurring interval.

    Examples:
        Monthly on the 19th: {"day": 19}
        Monthly with tolerance: {"day": 13, "tolerance_days": 2}
        Weekly on Monday: {"day_of_week": 0}  # 0=Monday
        Yearly on date: {"month": 12, "day": 25}
    """

    day: int | None = None  # Day of month (1-31)
    day_of_week: int | None = None  # 0=Monday, 6=Sunday
    month: int | None = None  # Month of year (1-12) for yearly
    tolerance_days: int = Field(default=3, ge=0)  # Fuzzy matching window


class EmailPattern(BaseModel):
    """A detected recurring email pattern (subscription, recurring receipt, etc.).

    Pattern identity is (sender, merchant). Amounts are tracked per-occurrence
    in PatternEmail, not as part of the pattern identity.

    Examples from real data:
        - sender="service@paypal.com", merchant="Host Havoc Ltd."
          interval_type=monthly, interval_params={"day": 19}
          (charges $10.00 on the 19th of each month)

        - sender="service@paypal.com", merchant="Bright Market LLC"
          interval_type=monthly, interval_params={"day": 13, "tolerance_days": 1}
          (charges $10.50 around the 13th)
    """

    id: int | None = None

    # Pattern identity
    sender: str  # e.g., "service@paypal.com"
    merchant: str  # Extracted: "Host Havoc Ltd." from subject/body

    # Template for matching future emails
    subject_template: str | None = None  # e.g., "{{merchant}}: ${{amount}} USD"
    body_signature: str | None = None  # Hash/fingerprint of body structure

    # Temporal pattern
    interval_type: IntervalType = IntervalType.IRREGULAR
    interval_params: IntervalParams | None = None
    next_expected: date | None = None  # Predicted next occurrence

    # Confidence and lifecycle
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    occurrence_count: int = Field(default=0, ge=0)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    status: PatternStatus = PatternStatus.EMERGING

    # Cached amount stats (computed from pattern_emails)
    typical_amount: Decimal | None = None  # Most common or median
    typical_currency: str | None = None  # e.g., "USD"
    amount_varies: bool = False  # True if amounts differ across occurrences

    # For labeling
    pattern_label: str | None = None  # e.g., "Subscriptions/Host Havoc"

    # Timestamps
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PatternEmail(BaseModel):
    """Junction record linking a pattern to a specific email occurrence.

    Stores per-occurrence data like amount that may vary between occurrences.
    """

    id: int | None = None
    pattern_id: int
    gmail_id: str

    # Per-occurrence data
    amount: Decimal | None = None  # What was charged this time
    currency: str | None = None  # USD, EUR, etc.

    # When this email was matched to the pattern
    matched_at: datetime | None = None


class PatternMatch(BaseModel):
    """Result of checking if an email matches a known pattern."""

    matched: bool = False
    pattern: EmailPattern | None = None
    is_new_pattern: bool = False  # True if this would create a new pattern
    amount_anomaly: bool = False  # True if amount differs from typical
    expected_amount: Decimal | None = None
    actual_amount: Decimal | None = None
