"""Tests for pattern lifecycle management."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from triage.models.patterns import (
    EmailPattern,
    IntervalParams,
    IntervalType,
    PatternStatus,
)
from triage.services.lifecycle import (
    LifecycleConfig,
    LifecycleReport,
    apply_decay,
    check_maintenance_needed,
    check_overdue_patterns,
    end_pattern,
    process_pattern_match,
    revive_pattern,
    run_lifecycle_maintenance,
)


class TestLifecycleConfig:
    """Tests for LifecycleConfig dataclass."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = LifecycleConfig()
        assert config.decay_per_miss == 0.20
        assert config.dormant_after_misses == 2
        assert config.end_after_misses == 6
        assert config.min_confidence_active == 0.3
        assert config.revival_confidence == 0.5
        assert config.maintenance_interval_hours == 24

    def test_custom_config(self) -> None:
        """Test custom configuration values."""
        config = LifecycleConfig(
            decay_per_miss=0.15,
            dormant_after_misses=3,
            end_after_misses=9,
        )
        assert config.decay_per_miss == 0.15
        assert config.dormant_after_misses == 3
        assert config.end_after_misses == 9


class TestLifecycleReport:
    """Tests for LifecycleReport dataclass."""

    def test_default_report(self) -> None:
        """Test default report values."""
        report = LifecycleReport()
        assert report.patterns_checked == 0
        assert report.patterns_decayed == 0
        assert report.patterns_dormant == 0
        assert report.patterns_ended == 0
        assert report.errors == []

    def test_report_with_data(self) -> None:
        """Test report with populated values."""
        report = LifecycleReport(
            patterns_checked=10,
            patterns_decayed=5,
            patterns_dormant=2,
            patterns_ended=1,
            errors=["Error 1"],
        )
        assert report.patterns_checked == 10
        assert report.patterns_decayed == 5
        assert report.patterns_dormant == 2
        assert report.patterns_ended == 1
        assert len(report.errors) == 1


@pytest.fixture
def sample_active_pattern() -> EmailPattern:
    """Active monthly pattern for testing."""
    return EmailPattern(
        id=1,
        sender="service@paypal.com",
        merchant="Host Havoc Ltd.",
        interval_type=IntervalType.MONTHLY,
        interval_params=IntervalParams(day=19),
        next_expected=date(2025, 12, 19),
        confidence=0.8,
        occurrence_count=5,
        first_seen=datetime(2025, 7, 19),
        last_seen=datetime(2025, 11, 19),
        status=PatternStatus.ACTIVE,
        consecutive_misses=0,
    )


@pytest.fixture
def sample_dormant_pattern() -> EmailPattern:
    """Dormant pattern for testing revival."""
    return EmailPattern(
        id=2,
        sender="service@paypal.com",
        merchant="Cancelled Service",
        interval_type=IntervalType.MONTHLY,
        interval_params=IntervalParams(day=15),
        next_expected=date(2025, 10, 15),
        confidence=0.3,
        occurrence_count=3,
        first_seen=datetime(2025, 5, 15),
        last_seen=datetime(2025, 8, 15),
        status=PatternStatus.DORMANT,
        consecutive_misses=3,
    )


class TestApplyDecay:
    """Tests for apply_decay function."""

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.update_pattern")
    @patch("triage.services.lifecycle._get_pattern_dates")
    def test_first_miss_reduces_confidence(
        self,
        mock_get_dates: MagicMock,
        mock_update: MagicMock,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """First missed period should reduce confidence by decay_per_miss."""
        mock_get_pattern.return_value = sample_active_pattern
        mock_get_dates.return_value = []
        mock_update.return_value = sample_active_pattern

        config = LifecycleConfig()
        conn = MagicMock()

        apply_decay(conn, 1, config)

        # Check update was called with reduced confidence
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["consecutive_misses"] == 1
        assert call_kwargs["confidence"] == pytest.approx(0.6)  # 0.8 - 0.2
        assert "status" not in call_kwargs  # Not enough misses for dormant

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.update_pattern")
    @patch("triage.services.lifecycle._get_pattern_dates")
    def test_second_miss_transitions_to_dormant(
        self,
        mock_get_dates: MagicMock,
        mock_update: MagicMock,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Second missed period should transition to dormant."""
        # Set up pattern with 1 existing miss
        sample_active_pattern.consecutive_misses = 1
        sample_active_pattern.confidence = 0.6
        mock_get_pattern.return_value = sample_active_pattern
        mock_get_dates.return_value = []
        mock_update.return_value = sample_active_pattern

        config = LifecycleConfig()
        conn = MagicMock()

        apply_decay(conn, 1, config)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["consecutive_misses"] == 2
        assert call_kwargs["confidence"] == pytest.approx(0.4)  # 0.6 - 0.2
        assert call_kwargs["status"] == PatternStatus.DORMANT

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.update_pattern")
    @patch("triage.services.lifecycle._get_pattern_dates")
    def test_sixth_miss_ends_pattern(
        self,
        mock_get_dates: MagicMock,
        mock_update: MagicMock,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Sixth missed period should end the pattern."""
        # Set up pattern with 5 existing misses
        sample_active_pattern.consecutive_misses = 5
        sample_active_pattern.confidence = 0.0
        sample_active_pattern.status = PatternStatus.DORMANT
        mock_get_pattern.return_value = sample_active_pattern
        mock_get_dates.return_value = []
        mock_update.return_value = sample_active_pattern

        config = LifecycleConfig()
        conn = MagicMock()

        apply_decay(conn, 1, config)

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["consecutive_misses"] == 6
        assert call_kwargs["status"] == PatternStatus.ENDED

    @patch("triage.services.lifecycle.get_pattern_by_id")
    def test_decay_pattern_not_found(
        self,
        mock_get_pattern: MagicMock,
    ) -> None:
        """Decay should return None for non-existent pattern."""
        mock_get_pattern.return_value = None
        conn = MagicMock()

        result = apply_decay(conn, 999, LifecycleConfig())
        assert result is None


class TestRevivePattern:
    """Tests for revive_pattern function."""

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.update_pattern")
    def test_revive_dormant_pattern(
        self,
        mock_update: MagicMock,
        mock_get_pattern: MagicMock,
        sample_dormant_pattern: EmailPattern,
    ) -> None:
        """Reviving dormant pattern should set active status and reset confidence."""
        mock_get_pattern.return_value = sample_dormant_pattern
        mock_update.return_value = sample_dormant_pattern

        config = LifecycleConfig()
        conn = MagicMock()

        revive_pattern(conn, 2, config)

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["status"] == PatternStatus.ACTIVE
        assert call_kwargs["confidence"] == 0.5  # revival_confidence
        assert call_kwargs["consecutive_misses"] == 0

    @patch("triage.services.lifecycle.get_pattern_by_id")
    def test_revive_active_pattern_noop(
        self,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Reviving an active pattern should be a no-op."""
        mock_get_pattern.return_value = sample_active_pattern
        conn = MagicMock()

        result = revive_pattern(conn, 1, LifecycleConfig())
        assert result == sample_active_pattern


class TestEndPattern:
    """Tests for end_pattern function."""

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.update_pattern")
    def test_end_active_pattern(
        self,
        mock_update: MagicMock,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Ending an active pattern should set ended status."""
        mock_get_pattern.return_value = sample_active_pattern
        mock_update.return_value = sample_active_pattern

        conn = MagicMock()

        end_pattern(conn, 1, reason="cancelled subscription")

        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["status"] == PatternStatus.ENDED

    @patch("triage.services.lifecycle.get_pattern_by_id")
    def test_end_already_ended_pattern(
        self,
        mock_get_pattern: MagicMock,
    ) -> None:
        """Ending an already ended pattern should be a no-op."""
        ended_pattern = EmailPattern(
            id=3,
            sender="test@example.com",
            merchant="Old Service",
            status=PatternStatus.ENDED,
        )
        mock_get_pattern.return_value = ended_pattern
        conn = MagicMock()

        result = end_pattern(conn, 3)
        assert result == ended_pattern


class TestProcessPatternMatch:
    """Tests for process_pattern_match function."""

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.add_email_to_pattern")
    @patch("triage.services.lifecycle.update_pattern")
    @patch("triage.services.lifecycle._get_pattern_dates")
    def test_match_resets_consecutive_misses(
        self,
        mock_get_dates: MagicMock,
        mock_update: MagicMock,
        mock_add_email: MagicMock,
        mock_get_pattern: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Matching email should reset consecutive misses."""
        sample_active_pattern.consecutive_misses = 1
        mock_get_pattern.return_value = sample_active_pattern
        mock_get_dates.return_value = [date(2025, 11, 19)]
        mock_update.return_value = sample_active_pattern

        conn = MagicMock()

        process_pattern_match(
            conn,
            pattern_id=1,
            gmail_id="test123",
            email_date=datetime(2025, 12, 19),
            amount=Decimal("10.00"),
            currency="USD",
        )

        # Verify add_email_to_pattern was called
        mock_add_email.assert_called_once()

        # Verify update includes consecutive_misses=0
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["consecutive_misses"] == 0

    @patch("triage.services.lifecycle.get_pattern_by_id")
    @patch("triage.services.lifecycle.add_email_to_pattern")
    @patch("triage.services.lifecycle.update_pattern")
    @patch("triage.services.lifecycle._get_pattern_dates")
    def test_match_revives_dormant_pattern(
        self,
        mock_get_dates: MagicMock,
        mock_update: MagicMock,
        mock_add_email: MagicMock,
        mock_get_pattern: MagicMock,
        sample_dormant_pattern: EmailPattern,
    ) -> None:
        """Matching email to dormant pattern should revive it."""
        mock_get_pattern.return_value = sample_dormant_pattern
        mock_get_dates.return_value = [date(2025, 8, 15)]
        mock_update.return_value = sample_dormant_pattern

        conn = MagicMock()

        process_pattern_match(
            conn,
            pattern_id=2,
            gmail_id="revival123",
            email_date=datetime(2025, 12, 15),
        )

        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["status"] == PatternStatus.ACTIVE
        assert call_kwargs["confidence"] == 0.5  # revival_confidence
        assert call_kwargs["consecutive_misses"] == 0


class TestCheckMaintenanceNeeded:
    """Tests for check_maintenance_needed function."""

    def test_maintenance_needed_no_previous_run(self) -> None:
        """Maintenance should be needed if never run before."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.fetchone.return_value = None

        config = LifecycleConfig()
        result = check_maintenance_needed(conn, config)
        assert result is True

    def test_maintenance_needed_after_interval(self) -> None:
        """Maintenance should be needed if interval has elapsed."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        # Last run 25 hours ago
        old_time = datetime.now() - timedelta(hours=25)
        cursor.fetchone.return_value = (old_time.isoformat(),)

        config = LifecycleConfig(maintenance_interval_hours=24)
        result = check_maintenance_needed(conn, config)
        assert result is True

    def test_maintenance_not_needed_within_interval(self) -> None:
        """Maintenance should not be needed if within interval."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        # Last run 1 hour ago
        recent_time = datetime.now() - timedelta(hours=1)
        cursor.fetchone.return_value = (recent_time.isoformat(),)

        config = LifecycleConfig(maintenance_interval_hours=24)
        result = check_maintenance_needed(conn, config)
        assert result is False


class TestCheckOverduePatterns:
    """Tests for check_overdue_patterns function."""

    def test_finds_overdue_patterns(self) -> None:
        """Should find patterns past their next_expected date."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        # Mock database row
        cursor.fetchall.return_value = [
            {
                "id": 1,
                "sender": "test@example.com",
                "merchant": "Test Merchant",
                "subject_template": None,
                "body_signature": None,
                "interval_type": "monthly",
                "interval_params": {"day": 15},
                "next_expected": date(2025, 12, 15),
                "confidence": 0.8,
                "occurrence_count": 3,
                "first_seen": datetime(2025, 9, 15),
                "last_seen": datetime(2025, 11, 15),
                "status": "active",
                "consecutive_misses": 0,
                "typical_amount": None,
                "typical_currency": None,
                "amount_varies": False,
                "pattern_label": None,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
            }
        ]

        result = check_overdue_patterns(conn, date(2025, 12, 20))

        assert len(result) == 1
        assert result[0].merchant == "Test Merchant"
        assert result[0].status == PatternStatus.ACTIVE


class TestRunLifecycleMaintenance:
    """Tests for run_lifecycle_maintenance function."""

    @patch("triage.services.lifecycle.check_overdue_patterns")
    @patch("triage.services.lifecycle.apply_decay")
    @patch("triage.services.lifecycle._update_maintenance_timestamp")
    def test_maintenance_processes_overdue_patterns(
        self,
        mock_update_ts: MagicMock,
        mock_apply_decay: MagicMock,
        mock_check_overdue: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Maintenance should process all overdue patterns."""
        mock_check_overdue.return_value = [sample_active_pattern]

        # Pattern becomes dormant after decay
        decayed = EmailPattern(**sample_active_pattern.model_dump())
        decayed.status = PatternStatus.DORMANT
        decayed.consecutive_misses = 2
        mock_apply_decay.return_value = decayed

        conn = MagicMock()
        config = LifecycleConfig()

        report = run_lifecycle_maintenance(conn, config, date(2025, 12, 25))

        assert report.patterns_checked == 1
        assert report.patterns_decayed == 1
        assert report.patterns_dormant == 1
        assert report.patterns_ended == 0
        mock_apply_decay.assert_called_once()
        mock_update_ts.assert_called_once()

    @patch("triage.services.lifecycle.check_overdue_patterns")
    @patch("triage.services.lifecycle._update_maintenance_timestamp")
    def test_maintenance_no_overdue_patterns(
        self,
        mock_update_ts: MagicMock,
        mock_check_overdue: MagicMock,
    ) -> None:
        """Maintenance with no overdue patterns should report zeros."""
        mock_check_overdue.return_value = []

        conn = MagicMock()
        config = LifecycleConfig()

        report = run_lifecycle_maintenance(conn, config, date(2025, 12, 25))

        assert report.patterns_checked == 0
        assert report.patterns_decayed == 0
        mock_update_ts.assert_called_once()

    @patch("triage.services.lifecycle.check_overdue_patterns")
    @patch("triage.services.lifecycle.apply_decay")
    @patch("triage.services.lifecycle._update_maintenance_timestamp")
    def test_maintenance_handles_errors(
        self,
        mock_update_ts: MagicMock,
        mock_apply_decay: MagicMock,
        mock_check_overdue: MagicMock,
        sample_active_pattern: EmailPattern,
    ) -> None:
        """Maintenance should continue after errors and report them."""
        mock_check_overdue.return_value = [sample_active_pattern]
        mock_apply_decay.side_effect = Exception("Database error")

        conn = MagicMock()
        config = LifecycleConfig()

        report = run_lifecycle_maintenance(conn, config, date(2025, 12, 25))

        assert report.patterns_checked == 1
        assert report.patterns_decayed == 0  # Failed
        assert len(report.errors) == 1
        assert "Database error" in report.errors[0]
