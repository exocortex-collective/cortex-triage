"""Tests for Temporal Pattern Detector."""

from datetime import date

from triage.models.patterns import IntervalParams, IntervalType
from triage.services.temporal import (
    calculate_confidence,
    detect_pattern,
    predict_next,
)


class TestDetectPattern:
    """Tests for pattern detection."""

    def test_monthly_pattern_exact(self) -> None:
        """Test detecting monthly pattern on exact same day."""
        # Host Havoc: Oct 19, Nov 19, Dec 19
        dates = [
            date(2025, 10, 19),
            date(2025, 11, 19),
            date(2025, 12, 19),
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.MONTHLY
        assert result.interval_params.day == 19
        assert result.confidence == 1.0
        assert result.next_expected == date(2026, 1, 19)

    def test_monthly_pattern_fuzzy(self) -> None:
        """Test detecting monthly pattern with slight variation."""
        # Bright Market: Oct 14, Nov 13, Dec 13
        dates = [
            date(2025, 10, 14),
            date(2025, 11, 13),
            date(2025, 12, 13),
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.MONTHLY
        # Most common day is 13 (appears twice)
        assert result.interval_params.day == 13
        # All dates within tolerance of 3 days, so confidence is 1.0
        assert result.confidence >= 0.5
        assert result.next_expected == date(2026, 1, 13)

    def test_weekly_pattern(self) -> None:
        """Test detecting weekly pattern."""
        # Every Monday (Dec 1, 8, 15 2025 are Mondays)
        dates = [
            date(2025, 12, 1),  # Monday
            date(2025, 12, 8),  # Monday
            date(2025, 12, 15),  # Monday
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.WEEKLY
        assert result.interval_params.day_of_week == 0  # Monday
        assert result.confidence >= 0.5
        assert result.next_expected == date(2025, 12, 22)  # Next Monday

    def test_insufficient_data(self) -> None:
        """Test that single date returns None."""
        dates = [date(2025, 12, 19)]
        result = detect_pattern(dates)
        assert result is None

    def test_empty_list(self) -> None:
        """Test that empty list returns None."""
        result = detect_pattern([])
        assert result is None

    def test_two_dates_monthly(self) -> None:
        """Test pattern detection with just two dates."""
        dates = [
            date(2025, 11, 19),
            date(2025, 12, 19),
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.MONTHLY
        assert result.interval_params.day == 19

    def test_irregular_pattern(self) -> None:
        """Test that irregular intervals are detected."""
        # Random dates with no clear pattern
        dates = [
            date(2025, 10, 5),
            date(2025, 10, 20),
            date(2025, 11, 10),
            date(2025, 12, 1),
        ]
        result = detect_pattern(dates)

        assert result is not None
        # Should fall back to irregular
        assert result.interval_type == IntervalType.IRREGULAR
        assert result.confidence < 0.5


class TestRealWorldPatterns:
    """Tests using real PayPal subscription data."""

    def test_host_havoc_pattern(self) -> None:
        """Test Host Havoc: $10.00 on the 19th of each month."""
        dates = [
            date(2025, 10, 19),
            date(2025, 11, 19),
            date(2025, 12, 19),
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.MONTHLY
        assert result.interval_params.day == 19
        assert result.confidence == 1.0
        # Average interval should be ~30 days
        assert result.avg_interval_days is not None
        assert 29 <= result.avg_interval_days <= 31

    def test_bright_market_pattern(self) -> None:
        """Test Bright Market: ~$10.50 around the 13th-14th."""
        dates = [
            date(2025, 10, 14),
            date(2025, 11, 13),
            date(2025, 12, 13),
        ]
        result = detect_pattern(dates)

        assert result is not None
        assert result.interval_type == IntervalType.MONTHLY
        # Should detect day 13 (most common)
        assert result.interval_params.day == 13
        # Tolerance should account for the Oct 14 outlier
        assert result.interval_params.tolerance_days >= 1

    def test_apple_services_multiple_per_month(self) -> None:
        """Test Apple Services: multiple charges per month (not a subscription)."""
        dates = [
            date(2025, 12, 14),
            date(2025, 12, 13),
            date(2025, 12, 8),
            date(2025, 12, 8),
            date(2025, 11, 28),
            date(2025, 11, 14),
        ]
        result = detect_pattern(dates)

        assert result is not None
        # Should NOT be monthly (intervals too short)
        # This is irregular - multiple charges per month
        assert result.interval_type == IntervalType.IRREGULAR


class TestPredictNext:
    """Tests for next date prediction."""

    def test_predict_monthly(self) -> None:
        """Test predicting next monthly occurrence."""
        dates = [date(2025, 10, 19), date(2025, 11, 19), date(2025, 12, 19)]
        params = IntervalParams(day=19)

        next_date = predict_next(dates, IntervalType.MONTHLY, params)

        assert next_date == date(2026, 1, 19)

    def test_predict_monthly_year_rollover(self) -> None:
        """Test predicting monthly with year rollover."""
        dates = [date(2025, 11, 15), date(2025, 12, 15)]
        params = IntervalParams(day=15)

        next_date = predict_next(dates, IntervalType.MONTHLY, params)

        assert next_date == date(2026, 1, 15)

    def test_predict_weekly(self) -> None:
        """Test predicting next weekly occurrence."""
        # Dec 1, 8, 15 2025 are Mondays
        dates = [date(2025, 12, 1), date(2025, 12, 8), date(2025, 12, 15)]
        params = IntervalParams(day_of_week=0)  # Monday

        next_date = predict_next(dates, IntervalType.WEEKLY, params)

        assert next_date == date(2025, 12, 22)  # Next Monday

    def test_predict_yearly(self) -> None:
        """Test predicting next yearly occurrence."""
        dates = [date(2023, 12, 25), date(2024, 12, 25)]
        params = IntervalParams(month=12, day=25)

        next_date = predict_next(dates, IntervalType.YEARLY, params)

        assert next_date == date(2025, 12, 25)

    def test_predict_yearly_past_date(self) -> None:
        """Test yearly prediction when this year's date has passed."""
        # If last occurrence was Dec 25, 2025, next should be Dec 25, 2026
        dates = [date(2024, 12, 25), date(2025, 12, 25)]
        params = IntervalParams(month=12, day=25)

        next_date = predict_next(dates, IntervalType.YEARLY, params)

        assert next_date == date(2026, 12, 25)

    def test_predict_empty_dates(self) -> None:
        """Test prediction with empty dates."""
        next_date = predict_next([], IntervalType.MONTHLY, IntervalParams(day=19))
        assert next_date is None


class TestCalculateConfidence:
    """Tests for confidence calculation."""

    def test_high_confidence_many_occurrences(self) -> None:
        """Test that more occurrences = higher confidence."""
        dates_3 = [date(2025, 10, 19), date(2025, 11, 19), date(2025, 12, 19)]
        dates_5 = [
            date(2025, 8, 19),
            date(2025, 9, 19),
            date(2025, 10, 19),
            date(2025, 11, 19),
            date(2025, 12, 19),
        ]
        params = IntervalParams(day=19)

        conf_3 = calculate_confidence(dates_3, IntervalType.MONTHLY, params)
        conf_5 = calculate_confidence(dates_5, IntervalType.MONTHLY, params)

        assert conf_5 > conf_3

    def test_lower_confidence_with_variation(self) -> None:
        """Test that variation reduces confidence."""
        # Perfect match
        dates_perfect = [
            date(2025, 10, 19),
            date(2025, 11, 19),
            date(2025, 12, 19),
        ]
        # With variation
        dates_varied = [
            date(2025, 10, 17),
            date(2025, 11, 19),
            date(2025, 12, 21),
        ]
        params = IntervalParams(day=19, tolerance_days=3)

        conf_perfect = calculate_confidence(dates_perfect, IntervalType.MONTHLY, params)
        conf_varied = calculate_confidence(dates_varied, IntervalType.MONTHLY, params)

        assert conf_perfect > conf_varied

    def test_minimum_dates(self) -> None:
        """Test confidence with minimum dates."""
        dates = [date(2025, 11, 19), date(2025, 12, 19)]
        params = IntervalParams(day=19)

        conf = calculate_confidence(dates, IntervalType.MONTHLY, params)

        assert 0 < conf < 1.0  # Some confidence but not high

    def test_single_date_zero_confidence(self) -> None:
        """Test that single date has zero confidence."""
        dates = [date(2025, 12, 19)]
        params = IntervalParams(day=19)

        conf = calculate_confidence(dates, IntervalType.MONTHLY, params)

        assert conf == 0.0
