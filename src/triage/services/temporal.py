"""Temporal Pattern Detector - detect recurring time patterns in email dates.

Given a list of dates when similar emails arrived, detect if there's a
recurring pattern (monthly, weekly, yearly, etc.) and predict the next
expected date.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, stdev

from triage.models.patterns import IntervalParams, IntervalType


@dataclass
class TemporalPattern:
    """Result of temporal pattern detection."""

    interval_type: IntervalType
    interval_params: IntervalParams
    confidence: float  # 0.0 - 1.0
    next_expected: date | None
    avg_interval_days: float | None = None  # Average days between occurrences


def detect_pattern(dates: list[date]) -> TemporalPattern | None:
    """Detect a recurring temporal pattern from a list of dates.

    Tries to detect patterns in order of specificity:
    1. Monthly (same day of month)
    2. Weekly (same day of week)
    3. Regular interval (every N days)

    Returns None if fewer than 2 dates or no pattern detected.

    Args:
        dates: List of dates when emails arrived, in any order

    Returns:
        TemporalPattern with detected interval type and parameters,
        or None if no pattern found
    """
    if len(dates) < 2:
        return None

    # Sort dates chronologically
    sorted_dates = sorted(dates)

    # Try each pattern type
    monthly = _detect_monthly(sorted_dates)
    if monthly and monthly.confidence >= 0.5:
        return monthly

    weekly = _detect_weekly(sorted_dates)
    if weekly and weekly.confidence >= 0.5:
        return weekly

    # Fall back to irregular with average interval
    return _detect_irregular(sorted_dates)


def _detect_monthly(dates: list[date]) -> TemporalPattern | None:
    """Detect monthly pattern (recurring on same day of month).

    Examples:
        [Oct 19, Nov 19, Dec 19] -> monthly on day 19, confidence 1.0
        [Oct 14, Nov 13, Dec 13] -> monthly on day 13, confidence 0.67
    """
    if len(dates) < 2:
        return None

    # Extract days of month
    days = [d.day for d in dates]

    # Find most common day
    day_counts = Counter(days)
    most_common_day, count = day_counts.most_common(1)[0]

    # Calculate how many dates match the most common day (with tolerance)
    tolerance = 3
    matches = sum(1 for d in days if abs(d - most_common_day) <= tolerance)

    # Confidence based on match ratio
    confidence = matches / len(dates)

    # Calculate actual tolerance needed
    deviations = [abs(d - most_common_day) for d in days]
    max_deviation = max(deviations)
    actual_tolerance = min(max_deviation, tolerance)

    # Verify intervals are roughly monthly (25-35 days apart)
    intervals = _get_intervals(dates)
    if intervals:
        avg_interval = mean(intervals)
        if avg_interval < 25 or avg_interval > 35:
            # Not monthly - intervals too short or too long
            return None

    # Predict next date
    last_date = dates[-1]
    next_month = last_date.month + 1
    next_year = last_date.year
    if next_month > 12:
        next_month = 1
        next_year += 1

    # Handle edge case: day doesn't exist in next month (e.g., 31st)
    try:
        next_expected = date(next_year, next_month, most_common_day)
    except ValueError:
        # Day doesn't exist, use last day of month
        if next_month == 12:
            next_expected = date(next_year + 1, 1, 1) - timedelta(days=1)
        else:
            next_expected = date(next_year, next_month + 1, 1) - timedelta(days=1)

    return TemporalPattern(
        interval_type=IntervalType.MONTHLY,
        interval_params=IntervalParams(
            day=most_common_day,
            tolerance_days=actual_tolerance,
        ),
        confidence=confidence,
        next_expected=next_expected,
        avg_interval_days=mean(intervals) if intervals else None,
    )


def _detect_weekly(dates: list[date]) -> TemporalPattern | None:
    """Detect weekly pattern (recurring on same day of week).

    Examples:
        [Mon Dec 2, Mon Dec 9, Mon Dec 16] -> weekly on Monday
    """
    if len(dates) < 2:
        return None

    # Check if intervals are roughly weekly (5-9 days apart)
    intervals = _get_intervals(dates)
    if not intervals:
        return None

    avg_interval = mean(intervals)
    if avg_interval < 5 or avg_interval > 9:
        return None

    # Extract days of week (0=Monday, 6=Sunday)
    weekdays = [d.weekday() for d in dates]

    # Find most common weekday
    weekday_counts = Counter(weekdays)
    most_common_weekday, count = weekday_counts.most_common(1)[0]

    # Confidence based on match ratio
    confidence = count / len(dates)

    # Predict next date
    last_date = dates[-1]
    days_until_next = (most_common_weekday - last_date.weekday()) % 7
    if days_until_next == 0:
        days_until_next = 7  # Next week
    next_expected = last_date + timedelta(days=days_until_next)

    return TemporalPattern(
        interval_type=IntervalType.WEEKLY,
        interval_params=IntervalParams(
            day_of_week=most_common_weekday,
            tolerance_days=1,
        ),
        confidence=confidence,
        next_expected=next_expected,
        avg_interval_days=avg_interval,
    )


def _detect_irregular(dates: list[date]) -> TemporalPattern:
    """Fallback: no specific pattern, just calculate average interval.

    Always returns a result (irregular pattern) for any input with 2+ dates.
    """
    intervals = _get_intervals(dates)

    if not intervals:
        return TemporalPattern(
            interval_type=IntervalType.IRREGULAR,
            interval_params=IntervalParams(),
            confidence=0.0,
            next_expected=None,
        )

    avg_interval = mean(intervals)

    # Low confidence for irregular patterns
    # Higher if intervals are consistent (low std dev)
    if len(intervals) >= 2:
        interval_stdev = stdev(intervals)
        # Normalize: if stdev is 0, confidence is 0.5; higher stdev = lower confidence
        if avg_interval > 0:
            consistency = max(0, 1 - (interval_stdev / avg_interval))
        else:
            consistency = 0
        confidence = 0.3 * consistency  # Cap at 0.3 for irregular
    else:
        confidence = 0.1

    # Predict next based on average interval
    last_date = dates[-1]
    next_expected = last_date + timedelta(days=round(avg_interval))

    return TemporalPattern(
        interval_type=IntervalType.IRREGULAR,
        interval_params=IntervalParams(tolerance_days=round(avg_interval / 4)),
        confidence=confidence,
        next_expected=next_expected,
        avg_interval_days=avg_interval,
    )


def _get_intervals(dates: list[date]) -> list[int]:
    """Get list of intervals (in days) between consecutive dates."""
    if len(dates) < 2:
        return []

    sorted_dates = sorted(dates)
    return [
        (sorted_dates[i + 1] - sorted_dates[i]).days
        for i in range(len(sorted_dates) - 1)
    ]


def calculate_confidence(
    dates: list[date],
    interval_type: IntervalType,
    interval_params: IntervalParams,
) -> float:
    """Calculate confidence score for a pattern given dates.

    Factors:
    - Number of occurrences (more = higher confidence)
    - Consistency (how well dates match the pattern)

    Returns:
        Float between 0.0 and 1.0
    """
    if len(dates) < 2:
        return 0.0

    # Base confidence from occurrence count
    # 2 occurrences = 0.3, 3 = 0.5, 4 = 0.7, 5+ = 0.8+
    occurrence_score = min(0.8, 0.1 + len(dates) * 0.15)

    # Consistency score based on pattern type
    if interval_type == IntervalType.MONTHLY:
        days = [d.day for d in dates]
        if interval_params.day:
            deviations = [abs(d - interval_params.day) for d in days]
            avg_deviation = mean(deviations)
            consistency = max(0, 1 - avg_deviation / interval_params.tolerance_days)
        else:
            consistency = 0.5
    elif interval_type == IntervalType.WEEKLY:
        weekdays = [d.weekday() for d in dates]
        if interval_params.day_of_week is not None:
            matches = sum(1 for w in weekdays if w == interval_params.day_of_week)
            consistency = matches / len(weekdays)
        else:
            consistency = 0.5
    else:
        consistency = 0.3  # Irregular patterns get lower consistency

    # Combined score
    return occurrence_score * (0.5 + 0.5 * consistency)


def predict_next(
    dates: list[date],
    interval_type: IntervalType,
    interval_params: IntervalParams,
) -> date | None:
    """Predict the next expected date based on pattern.

    Args:
        dates: Historical dates (will use the most recent)
        interval_type: Type of interval
        interval_params: Parameters for the interval

    Returns:
        Predicted next date, or None if unpredictable
    """
    if not dates:
        return None

    last_date = max(dates)

    if interval_type == IntervalType.MONTHLY:
        if interval_params.day:
            next_month = last_date.month + 1
            next_year = last_date.year
            if next_month > 12:
                next_month = 1
                next_year += 1
            try:
                return date(next_year, next_month, interval_params.day)
            except ValueError:
                # Day doesn't exist in month
                if next_month == 12:
                    return date(next_year + 1, 1, 1) - timedelta(days=1)
                return date(next_year, next_month + 1, 1) - timedelta(days=1)

    elif interval_type == IntervalType.WEEKLY:
        if interval_params.day_of_week is not None:
            days_until = (interval_params.day_of_week - last_date.weekday()) % 7
            if days_until == 0:
                days_until = 7
            return last_date + timedelta(days=days_until)

    elif interval_type == IntervalType.YEARLY:
        month = interval_params.month
        day = interval_params.day
        if month is not None and day is not None:
            next_year = last_date.year
            target = date(next_year, month, day)
            if target <= last_date:
                next_year += 1
                target = date(next_year, month, day)
            return target

    # Fallback: use average interval
    intervals = _get_intervals(dates)
    if intervals:
        avg_interval = mean(intervals)
        return last_date + timedelta(days=round(avg_interval))

    return None
