"""Pattern Lifecycle Manager - manages state transitions and confidence decay.

Handles:
- Confidence decay when expected emails don't arrive
- State transitions: emerging → active → dormant → ended
- Revival of dormant patterns when matching emails arrive
- Periodic maintenance to detect missed patterns
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg2
from psycopg2.extras import RealDictCursor

from triage.models.patterns import EmailPattern, IntervalType, PatternStatus
from triage.services.patterns import (
    _row_to_pattern,
    add_email_to_pattern,
    get_pattern_by_id,
    update_pattern,
)
from triage.services.temporal import predict_next

logger = logging.getLogger(__name__)


@dataclass
class LifecycleConfig:
    """Configuration for pattern lifecycle management."""

    decay_per_miss: float = 0.20  # Confidence reduction per missed period
    dormant_after_misses: int = 2  # Misses before dormant
    end_after_misses: int = 6  # Misses before auto-end
    min_confidence_active: float = 0.3  # Minimum confidence to stay active
    revival_confidence: float = 0.5  # Confidence when reviving from dormant
    maintenance_interval_hours: int = 24  # Hours between maintenance runs


@dataclass
class LifecycleReport:
    """Summary of a lifecycle maintenance run."""

    patterns_checked: int = 0
    patterns_decayed: int = 0
    patterns_dormant: int = 0
    patterns_ended: int = 0
    errors: list[str] = field(default_factory=list)


def process_pattern_match(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    gmail_id: str,
    email_date: datetime,
    amount: Decimal | None = None,
    currency: str | None = None,
    config: LifecycleConfig | None = None,
) -> EmailPattern | None:
    """Process an email matching a pattern - update stats and possibly revive.

    Called by the pattern detection system when an email is matched to a pattern.
    This updates the pattern's occurrence count, last_seen, amounts, and handles
    revival if the pattern was dormant.

    Args:
        conn: Database connection
        pattern_id: ID of the pattern that was matched
        gmail_id: Gmail ID of the matched email
        email_date: Date/time of the email
        amount: Transaction amount (if applicable)
        currency: Currency code (if applicable)
        config: Lifecycle configuration (defaults to LifecycleConfig())

    Returns:
        Updated EmailPattern, or None if pattern not found
    """
    if config is None:
        config = LifecycleConfig()
    pattern = get_pattern_by_id(conn, pattern_id)
    if not pattern:
        logger.warning(f"Pattern {pattern_id} not found for match processing")
        return None

    # Add email to pattern (updates occurrence_count, last_seen, amounts)
    add_email_to_pattern(conn, pattern_id, gmail_id, amount, currency, email_date)

    # Reset consecutive misses since we got an email
    updates: dict = {"consecutive_misses": 0}

    # Calculate next expected date
    if pattern.interval_type != IntervalType.IRREGULAR and pattern.interval_params:
        # Get dates from pattern_emails to calculate next expected
        dates = _get_pattern_dates(conn, pattern_id)
        if dates:
            next_exp = predict_next(
                dates, pattern.interval_type, pattern.interval_params
            )
            if next_exp:
                updates["next_expected"] = next_exp

    # Revival: if dormant and email arrives, revive to active
    if pattern.status == PatternStatus.DORMANT:
        updates["status"] = PatternStatus.ACTIVE
        updates["confidence"] = config.revival_confidence
        logger.info(f"Revived dormant pattern {pattern_id} ({pattern.merchant})")

    # Apply updates
    updated = update_pattern(conn, pattern_id, **updates)
    conn.commit()

    return updated


def check_overdue_patterns(
    conn: psycopg2.extensions.connection,
    today: date,
) -> list[EmailPattern]:
    """Find patterns that have missed their expected date.

    Returns patterns where:
    - next_expected < today
    - status is emerging, active, or dormant (not ended)

    Args:
        conn: Database connection
        today: Current date to check against

    Returns:
        List of overdue patterns
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM email_patterns
            WHERE next_expected < %s
            AND status IN ('emerging', 'active', 'dormant')
            ORDER BY next_expected
            """,
            (today,),
        )
        rows = cur.fetchall()

    return [_row_to_pattern(row) for row in rows]


def apply_decay(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    config: LifecycleConfig,
) -> EmailPattern | None:
    """Apply decay to a pattern that missed an expected email.

    Increments consecutive_misses, reduces confidence, and may transition
    to dormant or ended status.

    Args:
        conn: Database connection
        pattern_id: Pattern to decay
        config: Lifecycle configuration

    Returns:
        Updated pattern, or None if not found
    """
    pattern = get_pattern_by_id(conn, pattern_id)
    if not pattern:
        return None

    new_misses = pattern.consecutive_misses + 1
    new_confidence = max(0.0, pattern.confidence - config.decay_per_miss)

    updates: dict = {
        "consecutive_misses": new_misses,
        "confidence": new_confidence,
    }

    # Calculate next expected for the following period
    if pattern.interval_type != IntervalType.IRREGULAR and pattern.interval_params:
        dates = _get_pattern_dates(conn, pattern_id)
        if dates:
            next_exp = predict_next(
                dates, pattern.interval_type, pattern.interval_params
            )
            if next_exp:
                updates["next_expected"] = next_exp

    # State transitions based on miss count
    if new_misses >= config.end_after_misses:
        updates["status"] = PatternStatus.ENDED
        logger.info(
            f"Auto-ended pattern {pattern_id} ({pattern.merchant}) "
            f"after {new_misses} misses"
        )
    elif new_misses >= config.dormant_after_misses:
        if pattern.status != PatternStatus.DORMANT:
            updates["status"] = PatternStatus.DORMANT
            logger.info(
                f"Pattern {pattern_id} ({pattern.merchant}) now dormant "
                f"after {new_misses} misses"
            )

    updated = update_pattern(conn, pattern_id, **updates)
    conn.commit()

    return updated


def revive_pattern(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    config: LifecycleConfig,
) -> EmailPattern | None:
    """Revive a dormant pattern to active status.

    Args:
        conn: Database connection
        pattern_id: Pattern to revive
        config: Lifecycle configuration

    Returns:
        Updated pattern, or None if not found
    """
    pattern = get_pattern_by_id(conn, pattern_id)
    if not pattern:
        return None

    if pattern.status != PatternStatus.DORMANT:
        logger.debug(f"Pattern {pattern_id} is {pattern.status}, not dormant")
        return pattern

    updated = update_pattern(
        conn,
        pattern_id,
        status=PatternStatus.ACTIVE,
        confidence=config.revival_confidence,
        consecutive_misses=0,
    )
    conn.commit()

    logger.info(f"Revived pattern {pattern_id} ({pattern.merchant})")
    return updated


def end_pattern(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
    reason: str = "manual",
) -> EmailPattern | None:
    """Mark a pattern as ended.

    Args:
        conn: Database connection
        pattern_id: Pattern to end
        reason: Reason for ending (for logging)

    Returns:
        Updated pattern, or None if not found
    """
    pattern = get_pattern_by_id(conn, pattern_id)
    if not pattern:
        return None

    if pattern.status == PatternStatus.ENDED:
        logger.debug(f"Pattern {pattern_id} already ended")
        return pattern

    updated = update_pattern(conn, pattern_id, status=PatternStatus.ENDED)
    conn.commit()

    logger.info(f"Ended pattern {pattern_id} ({pattern.merchant}): {reason}")
    return updated


def run_lifecycle_maintenance(
    conn: psycopg2.extensions.connection,
    config: LifecycleConfig,
    today: date,
) -> LifecycleReport:
    """Run periodic lifecycle maintenance on all patterns.

    Checks for overdue patterns and applies decay. Should be called
    periodically (e.g., daily) to manage pattern lifecycle.

    Args:
        conn: Database connection
        config: Lifecycle configuration
        today: Current date

    Returns:
        Report of maintenance actions taken
    """
    report = LifecycleReport()

    # Find all overdue patterns
    overdue = check_overdue_patterns(conn, today)
    report.patterns_checked = len(overdue)

    logger.info(f"Lifecycle maintenance: checking {len(overdue)} overdue patterns")

    for pattern in overdue:
        if pattern.id is None:
            continue  # Skip patterns without IDs (shouldn't happen from DB)
        try:
            updated = apply_decay(conn, pattern.id, config)
            if updated:
                report.patterns_decayed += 1
                if updated.status == PatternStatus.DORMANT:
                    report.patterns_dormant += 1
                elif updated.status == PatternStatus.ENDED:
                    report.patterns_ended += 1
        except Exception as e:
            error_msg = f"Error decaying pattern {pattern.id}: {e}"
            logger.error(error_msg)
            report.errors.append(error_msg)

    # Update maintenance timestamp
    _update_maintenance_timestamp(conn)

    logger.info(
        f"Lifecycle maintenance complete: "
        f"{report.patterns_decayed} decayed, "
        f"{report.patterns_dormant} dormant, "
        f"{report.patterns_ended} ended"
    )

    return report


def check_maintenance_needed(
    conn: psycopg2.extensions.connection,
    config: LifecycleConfig,
) -> bool:
    """Check if lifecycle maintenance should run.

    Returns True if the configured interval has elapsed since last run.

    Args:
        conn: Database connection
        config: Lifecycle configuration

    Returns:
        True if maintenance is needed
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT value FROM sync_state
            WHERE key = 'lifecycle_last_check'
            """
        )
        row = cur.fetchone()

    if not row:
        # Never run, needs maintenance
        return True

    try:
        last_check = datetime.fromisoformat(row[0])
        elapsed = datetime.now(UTC) - last_check
        return elapsed > timedelta(hours=config.maintenance_interval_hours)
    except (ValueError, TypeError):
        # Invalid timestamp, run maintenance
        return True


def _update_maintenance_timestamp(
    conn: psycopg2.extensions.connection,
) -> None:
    """Update the last maintenance timestamp in sync_state."""
    now = datetime.now(UTC).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sync_state (key, value)
            VALUES ('lifecycle_last_check', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (now,),
        )
    conn.commit()


def _get_pattern_dates(
    conn: psycopg2.extensions.connection,
    pattern_id: int,
) -> list[date]:
    """Get all email dates for a pattern (for next_expected calculation)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DATE(matched_at) as email_date
            FROM pattern_emails
            WHERE pattern_id = %s
            ORDER BY matched_at
            """,
            (pattern_id,),
        )
        rows = cur.fetchall()

    return [row[0] for row in rows if row[0]]
