"""Tests for pattern models and storage."""

from datetime import date, datetime
from decimal import Decimal

from triage.models.patterns import (
    EmailPattern,
    IntervalParams,
    IntervalType,
    PatternEmail,
    PatternMatch,
    PatternStatus,
)


class TestIntervalParams:
    """Tests for IntervalParams model."""

    def test_monthly_day_params(self) -> None:
        """Test monthly interval with specific day."""
        params = IntervalParams(day=19)
        assert params.day == 19
        assert params.tolerance_days == 3  # default

    def test_monthly_with_tolerance(self) -> None:
        """Test monthly interval with custom tolerance."""
        params = IntervalParams(day=13, tolerance_days=1)
        assert params.day == 13
        assert params.tolerance_days == 1

    def test_weekly_params(self) -> None:
        """Test weekly interval on specific day of week."""
        params = IntervalParams(day_of_week=0)  # Monday
        assert params.day_of_week == 0

    def test_yearly_params(self) -> None:
        """Test yearly interval on specific date."""
        params = IntervalParams(month=12, day=25)
        assert params.month == 12
        assert params.day == 25


class TestEmailPattern:
    """Tests for EmailPattern model."""

    def test_minimal_pattern(self) -> None:
        """Test pattern with minimal required fields."""
        pattern = EmailPattern(
            sender="service@paypal.com",
            merchant="Host Havoc Ltd.",
        )
        assert pattern.sender == "service@paypal.com"
        assert pattern.merchant == "Host Havoc Ltd."
        assert pattern.interval_type == IntervalType.IRREGULAR
        assert pattern.status == PatternStatus.EMERGING
        assert pattern.confidence == 0.0
        assert pattern.occurrence_count == 0

    def test_full_pattern(self) -> None:
        """Test pattern with all fields - like Host Havoc example."""
        pattern = EmailPattern(
            id=1,
            sender="service@paypal.com",
            merchant="Host Havoc Ltd.",
            subject_template="{{merchant}}: ${{amount}} USD",
            interval_type=IntervalType.MONTHLY,
            interval_params=IntervalParams(day=19),
            next_expected=date(2026, 1, 19),
            confidence=0.8,
            occurrence_count=3,
            first_seen=datetime(2025, 10, 19),
            last_seen=datetime(2025, 12, 19),
            status=PatternStatus.ACTIVE,
            typical_amount=Decimal("10.00"),
            typical_currency="USD",
            amount_varies=False,
            pattern_label="Subscriptions/Host Havoc",
        )
        assert pattern.id == 1
        assert pattern.interval_type == IntervalType.MONTHLY
        assert pattern.interval_params is not None
        assert pattern.interval_params.day == 19
        assert pattern.status == PatternStatus.ACTIVE
        assert pattern.typical_amount == Decimal("10.00")

    def test_bright_market_pattern(self) -> None:
        """Test pattern for Bright Market - monthly with tolerance."""
        pattern = EmailPattern(
            sender="service@paypal.com",
            merchant="Bright Market LLC",
            interval_type=IntervalType.MONTHLY,
            interval_params=IntervalParams(day=13, tolerance_days=1),
            occurrence_count=3,
            first_seen=datetime(2025, 10, 14),
            last_seen=datetime(2025, 12, 13),
            status=PatternStatus.ACTIVE,
            typical_amount=Decimal("10.50"),
            typical_currency="USD",
        )
        # Oct 14, Nov 13, Dec 13 - around the 13th
        assert pattern.interval_params is not None
        assert pattern.interval_params.day == 13
        assert pattern.interval_params.tolerance_days == 1


class TestPatternEmail:
    """Tests for PatternEmail model."""

    def test_pattern_email_with_amount(self) -> None:
        """Test linking an email to a pattern with amount."""
        pe = PatternEmail(
            pattern_id=1,
            gmail_id="abc123",
            amount=Decimal("10.00"),
            currency="USD",
        )
        assert pe.pattern_id == 1
        assert pe.gmail_id == "abc123"
        assert pe.amount == Decimal("10.00")
        assert pe.currency == "USD"

    def test_pattern_email_different_amount(self) -> None:
        """Test that different amounts can be recorded."""
        # Normal charge
        pe1 = PatternEmail(
            pattern_id=1,
            gmail_id="email1",
            amount=Decimal("10.50"),
            currency="USD",
        )
        # Different charge (like user mentioned - $2 instead of $10.50)
        pe2 = PatternEmail(
            pattern_id=1,
            gmail_id="email2",
            amount=Decimal("2.00"),
            currency="USD",
        )
        assert pe1.amount == Decimal("10.50")
        assert pe2.amount == Decimal("2.00")
        # Both belong to same pattern
        assert pe1.pattern_id == pe2.pattern_id


class TestPatternMatch:
    """Tests for PatternMatch result model."""

    def test_no_match(self) -> None:
        """Test when email doesn't match any pattern."""
        match = PatternMatch(matched=False)
        assert not match.matched
        assert match.pattern is None

    def test_match_with_anomaly(self) -> None:
        """Test match with amount anomaly detection."""
        pattern = EmailPattern(
            sender="service@paypal.com",
            merchant="Bright Market LLC",
            typical_amount=Decimal("10.50"),
        )
        match = PatternMatch(
            matched=True,
            pattern=pattern,
            amount_anomaly=True,
            expected_amount=Decimal("10.50"),
            actual_amount=Decimal("2.00"),
        )
        assert match.matched
        assert match.amount_anomaly
        assert match.expected_amount == Decimal("10.50")
        assert match.actual_amount == Decimal("2.00")

    def test_new_pattern_detection(self) -> None:
        """Test when email would create a new pattern."""
        match = PatternMatch(
            matched=False,
            is_new_pattern=True,
        )
        assert not match.matched
        assert match.is_new_pattern


class TestIntervalType:
    """Tests for IntervalType enum."""

    def test_all_interval_types(self) -> None:
        """Verify all interval types are valid."""
        assert IntervalType.MONTHLY.value == "monthly"
        assert IntervalType.WEEKLY.value == "weekly"
        assert IntervalType.BIWEEKLY.value == "biweekly"
        assert IntervalType.QUARTERLY.value == "quarterly"
        assert IntervalType.YEARLY.value == "yearly"
        assert IntervalType.DAY_OF_WEEK.value == "day_of_week"
        assert IntervalType.IRREGULAR.value == "irregular"


class TestPatternStatus:
    """Tests for PatternStatus enum."""

    def test_lifecycle_states(self) -> None:
        """Verify all lifecycle states exist."""
        assert PatternStatus.EMERGING.value == "emerging"
        assert PatternStatus.ACTIVE.value == "active"
        assert PatternStatus.DORMANT.value == "dormant"
        assert PatternStatus.ENDED.value == "ended"

    def test_status_transitions(self) -> None:
        """Document expected status transitions."""
        # emerging -> active (after 3 occurrences)
        # active -> dormant (after missed expected email)
        # dormant -> active (if matching email arrives)
        # active/dormant -> ended (user cancels or pattern decays)
        states = [s.value for s in PatternStatus]
        assert "emerging" in states
        assert "active" in states
        assert "dormant" in states
        assert "ended" in states
