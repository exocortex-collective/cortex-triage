"""Pattern storage operations - CRUD for email_patterns and pattern_emails."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from triage.models.patterns import (
    EmailPattern,
    IntervalParams,
    IntervalType,
    PatternEmail,
    PatternStatus,
)

logger = logging.getLogger(__name__)

# Pattern activation threshold - occurrences needed to become active
PATTERN_ACTIVATION_COUNT = 3


def get_pattern_by_sender_merchant(
    conn: psycopg2.extensions.connection,
    sender: str,
    merchant: str,
) -> EmailPattern | None:
    """Find a pattern by sender + merchant (the unique identity)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM email_patterns
            WHERE sender = %s AND merchant = %s
            """,
            (sender, merchant),
        )
        row = cur.fetchone()
        if row:
            return _row_to_pattern(row)
    return None


def get_pattern_by_id(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
) -> EmailPattern | None:
    """Get a pattern by ID."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM email_patterns WHERE id = %s", (pattern_id,))
        row = cur.fetchone()
        if row:
            return _row_to_pattern(row)
    return None


def get_patterns_for_sender(
    conn: psycopg2.extensions.connection,
    sender: str,
    status_filter: list[PatternStatus] | None = None,
) -> list[EmailPattern]:
    """Get all patterns for a sender, optionally filtered by status."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if status_filter:
            cur.execute(
                """
                SELECT * FROM email_patterns
                WHERE sender = %s AND status = ANY(%s)
                ORDER BY last_seen DESC
                """,
                (sender, [s.value for s in status_filter]),
            )
        else:
            cur.execute(
                """
                SELECT * FROM email_patterns
                WHERE sender = %s
                ORDER BY last_seen DESC
                """,
                (sender,),
            )
        return [_row_to_pattern(row) for row in cur.fetchall()]


def get_active_patterns(
    conn: psycopg2.extensions.connection,
    limit: int = 100,
) -> list[EmailPattern]:
    """Get active and emerging patterns."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM email_patterns
            WHERE status IN ('emerging', 'active')
            ORDER BY last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [_row_to_pattern(row) for row in cur.fetchall()]


def create_pattern(
    conn: psycopg2.extensions.connection,
    sender: str,
    merchant: str,
    first_seen: datetime,
    subject_template: str | None = None,
    interval_type: IntervalType = IntervalType.IRREGULAR,
    interval_params: IntervalParams | None = None,
    pattern_label: str | None = None,
) -> EmailPattern:
    """Create a new pattern (emerging status, 0 occurrences)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            INSERT INTO email_patterns (
                sender, merchant, subject_template,
                interval_type, interval_params,
                confidence, occurrence_count,
                first_seen, last_seen, status, pattern_label
            ) VALUES (
                %s, %s, %s,
                %s, %s,
                0.0, 0,
                %s, %s, 'emerging', %s
            )
            RETURNING *
            """,
            (
                sender,
                merchant,
                subject_template,
                interval_type.value,
                Json(interval_params.model_dump() if interval_params else None),
                first_seen,
                first_seen,
                pattern_label,
            ),
        )
        row = cur.fetchone()
        logger.info(f"Created pattern: sender={sender}, merchant={merchant}")
        return _row_to_pattern(row)


def update_pattern(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    **updates: Any,
) -> EmailPattern | None:
    """Update pattern fields.

    Allowed fields: interval_type, interval_params, next_expected, confidence,
    occurrence_count, last_seen, status, consecutive_misses, typical_amount,
    typical_currency, amount_varies, pattern_label, subject_template, body_signature
    """
    allowed = {
        "interval_type",
        "interval_params",
        "next_expected",
        "confidence",
        "occurrence_count",
        "last_seen",
        "status",
        "consecutive_misses",
        "typical_amount",
        "typical_currency",
        "amount_varies",
        "pattern_label",
        "subject_template",
        "body_signature",
    }

    # Filter to allowed fields
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        logger.debug(f"No valid fields to update for pattern {pattern_id}")
        return get_pattern_by_id(conn, pattern_id)

    # Convert enums and special types
    val = updates.get("interval_type")
    if isinstance(val, IntervalType):
        updates["interval_type"] = val.value

    val = updates.get("interval_params")
    if isinstance(val, IntervalParams):
        updates["interval_params"] = Json(val.model_dump())
    elif isinstance(val, dict):
        updates["interval_params"] = Json(val)

    val = updates.get("status")
    if isinstance(val, PatternStatus):
        updates["status"] = val.value

    # Build SET clause
    set_parts = [f"{k} = %s" for k in updates]
    set_parts.append("updated_at = NOW()")
    set_clause = ", ".join(set_parts)

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"UPDATE email_patterns SET {set_clause} WHERE id = %s RETURNING *",
            (*updates.values(), pattern_id),
        )
        row = cur.fetchone()
        if row:
            return _row_to_pattern(row)
    return None


def add_email_to_pattern(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    gmail_id: str,
    amount: Decimal | None = None,
    currency: str | None = None,
    email_date: datetime | None = None,
) -> PatternEmail:
    """Add an email to a pattern and update pattern stats.

    This is idempotent - if the email is already linked, it updates the record.
    Also updates the pattern's occurrence_count, last_seen, and amount stats.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        # Upsert the pattern_email link
        cur.execute(
            """
            INSERT INTO pattern_emails (pattern_id, gmail_id, amount, currency)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (pattern_id, gmail_id)
            DO UPDATE SET amount = EXCLUDED.amount, currency = EXCLUDED.currency
            RETURNING *
            """,
            (pattern_id, gmail_id, amount, currency),
        )
        pe_row = cur.fetchone()

        # Update pattern stats and get resulting state
        cur.execute(
            """
            WITH stats AS (
                SELECT
                    COUNT(*) as cnt,
                    MIN(matched_at) as first_match,
                    MAX(matched_at) as last_match,
                    MODE() WITHIN GROUP (ORDER BY amount) as typical_amt,
                    MODE() WITHIN GROUP (ORDER BY currency) as typical_curr,
                    COUNT(DISTINCT amount) > 1 as varies
                FROM pattern_emails
                WHERE pattern_id = %s
            )
            UPDATE email_patterns
            SET
                occurrence_count = stats.cnt,
                last_seen = COALESCE(%s, stats.last_match),
                typical_amount = stats.typical_amt,
                typical_currency = stats.typical_curr,
                amount_varies = stats.varies,
                status = CASE
                    WHEN stats.cnt >= %s AND status = 'emerging' THEN 'active'
                    ELSE status
                END,
                -- Confidence heuristic:
                -- - Emerging (<3 occurrences): 15% per occurrence
                -- - Active (>=3): Starts at 50%, +10% per extra, capped at 100%
                confidence = CASE
                    WHEN stats.cnt >= %s THEN LEAST(0.5 + (stats.cnt - %s) * 0.1, 1.0)
                    ELSE stats.cnt * 0.15
                END,
                updated_at = NOW()
            FROM stats
            WHERE email_patterns.id = %s
            RETURNING
                occurrence_count,
                status,
                (stats.cnt = %s AND status = 'emerging') AS transitioned
            """,
            (
                pattern_id,
                email_date,
                PATTERN_ACTIVATION_COUNT,
                PATTERN_ACTIVATION_COUNT,
                PATTERN_ACTIVATION_COUNT,
                pattern_id,
                PATTERN_ACTIVATION_COUNT,
            ),
        )
        result = cur.fetchone()

        logger.info(f"Added email {gmail_id} to pattern {pattern_id}")

        # Check if pattern just became active (transitioned from emerging)
        # This is the transition point where we trigger retroactive processing
        if result and result["transitioned"]:
            logger.info(
                f"Pattern {pattern_id} became active, triggering retroactive enqueue"
            )
            enqueue_pattern_for_retriage(conn, pattern_id)

        return PatternEmail(
            id=pe_row["id"],
            pattern_id=pe_row["pattern_id"],
            gmail_id=pe_row["gmail_id"],
            amount=pe_row["amount"],
            currency=pe_row["currency"],
            matched_at=pe_row["matched_at"],
        )


def get_pattern_emails(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    limit: int = 100,
) -> list[PatternEmail]:
    """Get emails linked to a pattern, most recent first."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM pattern_emails
            WHERE pattern_id = %s
            ORDER BY matched_at DESC
            LIMIT %s
            """,
            (pattern_id, limit),
        )
        return [
            PatternEmail(
                id=row["id"],
                pattern_id=row["pattern_id"],
                gmail_id=row["gmail_id"],
                amount=row["amount"],
                currency=row["currency"],
                matched_at=row["matched_at"],
            )
            for row in cur.fetchall()
        ]


def enqueue_pattern_for_retriage(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    priority: int = -100,
) -> int:
    """Enqueue all emails in a pattern for re-triage.

    When a pattern becomes ACTIVE, this re-enqueues all historical emails
    so they can be re-classified with the pattern now confirmed.

    Uses priority=-100 (backfill) to avoid blocking real-time processing.

    Args:
        conn: Database connection
        pattern_id: Pattern whose emails should be enqueued
        priority: Queue priority (default -100 for backfill)

    Returns:
        Number of emails enqueued
    """
    with conn.cursor() as cur:
        # Insert emails into triage queue, skipping any already pending
        cur.execute(
            """
            INSERT INTO queue (queue_name, payload, status, priority)
            SELECT 'triage',
                   jsonb_build_object('gmail_id', pe.gmail_id),
                   'pending',
                   %s
            FROM pattern_emails pe
            WHERE pe.pattern_id = %s
            AND NOT EXISTS (
                SELECT 1 FROM queue q
                WHERE q.queue_name = 'triage'
                AND q.payload->>'gmail_id' = pe.gmail_id
                AND q.status = 'pending'
            )
            """,
            (priority, pattern_id),
        )
        count = cur.rowcount or 0

    logger.info(f"Enqueued {count} emails from pattern {pattern_id} for retriage")
    return count


def get_pattern_for_email(
    conn: psycopg2.extensions.connection,
    gmail_id: str,
) -> EmailPattern | None:
    """Find which pattern (if any) an email belongs to."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT p.* FROM email_patterns p
            JOIN pattern_emails pe ON p.id = pe.pattern_id
            WHERE pe.gmail_id = %s
            """,
            (gmail_id,),
        )
        row = cur.fetchone()
        if row:
            return _row_to_pattern(row)
    return None


def _row_to_pattern(row: dict[str, Any]) -> EmailPattern:
    """Convert a database row to an EmailPattern model."""
    interval_params = None
    if row.get("interval_params"):
        interval_params = IntervalParams(**row["interval_params"])

    return EmailPattern(
        id=row["id"],
        sender=row["sender"],
        merchant=row["merchant"],
        subject_template=row.get("subject_template"),
        body_signature=row.get("body_signature"),
        interval_type=IntervalType(row["interval_type"]),
        interval_params=interval_params,
        next_expected=row.get("next_expected"),
        confidence=row["confidence"],
        occurrence_count=row["occurrence_count"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        status=PatternStatus(row["status"]),
        consecutive_misses=row.get("consecutive_misses", 0),
        typical_amount=row.get("typical_amount"),
        typical_currency=row.get("typical_currency"),
        amount_varies=row.get("amount_varies", False),
        pattern_label=row.get("pattern_label"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
