"""Tests for pattern_info population in worker's _get_email_data()."""

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from triage.models.patterns import EmailPattern, IntervalType, PatternStatus

if TYPE_CHECKING:
    from triage.services.worker import TriageWorker


class TestGetEmailDataPatternInfo:
    """Tests for pattern_info population in _get_email_data."""

    def setup_method(self) -> None:
        """Set up the worker and mock DB connection for each test."""
        self.worker = self._make_worker()

        # Mock the connection pool and cursor
        self.mock_conn = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cursor

        # Mock worker._get_pool().getconn() to return our mock connection
        mock_pool = MagicMock()
        mock_pool.getconn.return_value = self.mock_conn
        self.worker._pool = mock_pool

    def _make_worker(self) -> "TriageWorker":
        """Create a minimal worker mock for testing _get_email_data."""
        # We need to import here to avoid import-time issues with mocking
        from triage.services.worker import TriageWorker

        # Create a worker with mock dependencies
        config = MagicMock()
        rules = MagicMock()
        ollama = MagicMock()
        gmail = MagicMock()

        worker = TriageWorker(config, rules, ollama, gmail)
        return worker

    @patch("triage.services.worker.get_pattern_for_email")
    def test_pattern_info_none_when_no_pattern(
        self, mock_get_pattern: MagicMock
    ) -> None:
        """pattern_info should be None when email is not linked to a pattern."""
        mock_get_pattern.return_value = None

        # Mock database row for email data
        self.mock_cursor.fetchone.return_value = (
            1,  # id
            "gmail123",  # gmail_id
            "test@example.com",  # from_addr
            ["to@example.com"],  # to_addrs
            [],  # cc_addrs
            [],  # bcc_addrs
            "Test Subject",  # subject
            "Test body",  # body
            False,  # has_attachment
            [],  # headers (raw JSONB array)
            [],  # label_ids
            1000,  # size_estimate
            1700000000000,  # internal_date
            [],  # attachment_filenames
            [],  # attachment_mime_types
        )

        email = self.worker._get_email_data("gmail123")

        assert email is not None
        assert email.pattern_info is None
        mock_get_pattern.assert_called_once_with(self.mock_conn, "gmail123")

    @patch("triage.services.worker.get_pattern_for_email")
    def test_pattern_info_populated_when_pattern_exists(
        self, mock_get_pattern: MagicMock
    ) -> None:
        """pattern_info should be populated when email is linked to a pattern."""
        # Create a mock pattern
        mock_pattern = EmailPattern(
            id=42,
            sender="service@paypal.com",
            merchant="Host Havoc Ltd.",
            interval_type=IntervalType.MONTHLY,
            confidence=0.85,
            occurrence_count=5,
            first_seen=datetime(2025, 10, 19),
            last_seen=datetime(2025, 12, 19),
            status=PatternStatus.ACTIVE,
        )
        mock_get_pattern.return_value = mock_pattern

        # Mock database row for email data
        self.mock_cursor.fetchone.return_value = (
            1,  # id
            "gmail123",  # gmail_id
            "service@paypal.com",  # from_addr
            ["user@example.com"],  # to_addrs
            [],  # cc_addrs
            [],  # bcc_addrs
            "Payment to Host Havoc",  # subject
            "Your payment of $10.00 was processed.",  # body
            False,  # has_attachment
            [],  # headers
            [],  # label_ids
            2000,  # size_estimate
            1700000000000,  # internal_date
            [],  # attachment_filenames
            [],  # attachment_mime_types
        )

        email = self.worker._get_email_data("gmail123")

        assert email is not None
        assert email.pattern_info is not None
        assert email.pattern_info.pattern_id == 42
        assert email.pattern_info.sender == "service@paypal.com"
        assert email.pattern_info.merchant == "Host Havoc Ltd."
        assert email.pattern_info.interval_type == "monthly"
        assert email.pattern_info.confidence == 0.85
        assert email.pattern_info.occurrence_count == 5
        assert email.pattern_info.status == "active"

    @patch("triage.services.worker.get_pattern_for_email")
    def test_pattern_info_with_emerging_status(
        self, mock_get_pattern: MagicMock
    ) -> None:
        """pattern_info should reflect emerging status correctly."""
        mock_pattern = EmailPattern(
            id=10,
            sender="newsletter@example.com",
            merchant="Example Newsletter",
            interval_type=IntervalType.WEEKLY,
            confidence=0.30,
            occurrence_count=2,
            first_seen=datetime(2025, 12, 1),
            last_seen=datetime(2025, 12, 8),
            status=PatternStatus.EMERGING,
        )
        mock_get_pattern.return_value = mock_pattern

        self.mock_cursor.fetchone.return_value = (
            1,
            "gmail456",
            "newsletter@example.com",
            ["user@example.com"],
            [],
            [],
            "Weekly Update",
            "Content here",
            False,
            [],
            [],
            1500,
            1700000000000,
            [],
            [],
        )

        email = self.worker._get_email_data("gmail456")

        assert email is not None
        assert email.pattern_info is not None
        assert email.pattern_info.status == "emerging"
        assert email.pattern_info.occurrence_count == 2
        assert email.pattern_info.interval_type == "weekly"

    @patch("triage.services.worker.get_pattern_for_email")
    def test_pattern_info_skipped_when_pattern_id_none(
        self, mock_get_pattern: MagicMock
    ) -> None:
        """pattern_info should be None if pattern.id is None (edge case)."""
        # Create a pattern without an id (shouldn't happen from DB, but handle it)
        mock_pattern = EmailPattern(
            id=None,  # No ID
            sender="test@example.com",
            merchant="Test",
        )
        mock_get_pattern.return_value = mock_pattern

        self.mock_cursor.fetchone.return_value = (
            1,
            "gmail789",
            "test@example.com",
            ["user@example.com"],
            [],
            [],
            "Test",
            "Body",
            False,
            [],
            [],
            500,
            1700000000000,
            [],
            [],
        )

        email = self.worker._get_email_data("gmail789")

        assert email is not None
        # pattern_info should be None because pattern.id was None
        assert email.pattern_info is None


class TestDetectOrCreatePattern:
    """Tests for _detect_or_create_pattern method."""

    def setup_method(self) -> None:
        """Set up the worker and mock DB connection for each test."""
        self.worker = self._make_worker()
        self.mock_conn = MagicMock()

    def _make_worker(self) -> "TriageWorker":
        """Create a minimal worker mock for testing."""
        from triage.services.worker import TriageWorker

        config = MagicMock()
        rules = MagicMock()
        ollama = MagicMock()
        gmail = MagicMock()
        return TriageWorker(config, rules, ollama, gmail)

    @patch("triage.services.worker.get_pattern_by_sender_merchant")
    @patch("triage.services.worker.add_email_to_pattern")
    @patch("triage.services.worker.create_pattern")
    @patch("triage.services.worker.detect_pattern")
    @patch("triage.services.worker.analyze_sender")
    def test_creates_new_pattern_when_threshold_met(
        self,
        mock_analyze: MagicMock,
        mock_detect: MagicMock,
        mock_create: MagicMock,
        mock_add_email: MagicMock,
        mock_get_pattern: MagicMock,
    ) -> None:
        """Should create a new pattern when threshold is met and temporal detected."""
        from triage.models.patterns import EmailPattern, IntervalType
        from triage.services.similarity import SimilarityGroup
        from triage.services.temporal import TemporalPattern

        # Setup: 3 emails in group (meets threshold of 3)
        mock_analyze.return_value = [
            SimilarityGroup(
                sender="paypal@paypal.com",
                template_name="paypal_receipt",
                template_pattern="",
                merchant="Host Havoc",
                gmail_ids=["email1", "email2", "email3"],
                amounts=[None, None, None],
                dates=["2025-10-01", "2025-11-01", "2025-12-01"],
            )
        ]

        # No existing pattern
        mock_get_pattern.return_value = None

        # Temporal pattern detected
        mock_detect.return_value = TemporalPattern(
            interval_type=IntervalType.MONTHLY,
            interval_params={"day_of_month": 1},
            confidence=0.9,
            next_expected=None,
        )

        # Created pattern
        created_pattern = EmailPattern(
            id=1,
            sender="paypal@paypal.com",
            merchant="Host Havoc",
            interval_type=IntervalType.MONTHLY,
        )
        mock_create.return_value = created_pattern
        mock_get_pattern.side_effect = [None, created_pattern]

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "paypal@paypal.com"
        )

        assert result is not None
        assert result.merchant == "Host Havoc"
        mock_create.assert_called_once()
        # Should link all 3 emails
        assert mock_add_email.call_count == 3

    @patch("triage.services.worker.get_pattern_by_sender_merchant")
    @patch("triage.services.worker.add_email_to_pattern")
    @patch("triage.services.worker.analyze_sender")
    def test_links_emails_to_existing_pattern(
        self,
        mock_analyze: MagicMock,
        mock_add_email: MagicMock,
        mock_get_pattern: MagicMock,
    ) -> None:
        """Should link all emails in group to existing pattern."""
        from triage.models.patterns import EmailPattern, IntervalType
        from triage.services.similarity import SimilarityGroup

        mock_analyze.return_value = [
            SimilarityGroup(
                sender="paypal@paypal.com",
                template_name="paypal_receipt",
                template_pattern="",
                merchant="Host Havoc",
                gmail_ids=["email1", "email2", "email3"],
                amounts=[None, None, None],
                dates=["2025-10-01", "2025-11-01", "2025-12-01"],
            )
        ]

        existing_pattern = EmailPattern(
            id=42,
            sender="paypal@paypal.com",
            merchant="Host Havoc",
            interval_type=IntervalType.MONTHLY,
        )
        mock_get_pattern.return_value = existing_pattern

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "paypal@paypal.com"
        )

        assert result is not None
        assert result.id == 42
        # Should link all 3 emails
        assert mock_add_email.call_count == 3

    @patch("triage.services.worker.analyze_sender")
    def test_returns_none_when_below_threshold(self, mock_analyze: MagicMock) -> None:
        """Should return None when group has fewer than threshold emails."""
        from triage.services.similarity import SimilarityGroup

        # Only 2 emails - below threshold of 3
        mock_analyze.return_value = [
            SimilarityGroup(
                sender="paypal@paypal.com",
                template_name="paypal_receipt",
                template_pattern="",
                merchant="Host Havoc",
                gmail_ids=["email1", "email2"],
                amounts=[None, None],
                dates=["2025-11-01", "2025-12-01"],
            )
        ]

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "paypal@paypal.com"
        )

        assert result is None

    @patch("triage.services.worker.get_pattern_by_sender_merchant")
    @patch("triage.services.worker.detect_pattern")
    @patch("triage.services.worker.analyze_sender")
    def test_returns_none_when_no_temporal_pattern(
        self,
        mock_analyze: MagicMock,
        mock_detect: MagicMock,
        mock_get_pattern: MagicMock,
    ) -> None:
        """Should return None when no temporal pattern can be detected."""
        from triage.services.similarity import SimilarityGroup

        mock_analyze.return_value = [
            SimilarityGroup(
                sender="random@example.com",
                template_name="unknown",
                template_pattern="",
                merchant=None,
                gmail_ids=["email1", "email2", "email3"],
                amounts=[None, None, None],
                dates=["2025-01-15", "2025-06-22", "2025-12-03"],  # Irregular
            )
        ]

        mock_get_pattern.return_value = None
        mock_detect.return_value = None  # No temporal pattern

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "random@example.com"
        )

        assert result is None

    @patch("triage.services.worker.analyze_sender")
    def test_returns_none_when_email_not_in_any_group(
        self, mock_analyze: MagicMock
    ) -> None:
        """Should return None when email is not in any similarity group."""
        from triage.services.similarity import SimilarityGroup

        # Group exists but doesn't contain the email we're checking
        mock_analyze.return_value = [
            SimilarityGroup(
                sender="paypal@paypal.com",
                template_name="paypal_receipt",
                template_pattern="",
                merchant="Host Havoc",
                gmail_ids=["other1", "other2", "other3"],
                amounts=[None, None, None],
                dates=["2025-10-01", "2025-11-01", "2025-12-01"],
            )
        ]

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email_not_in_group", "paypal@paypal.com"
        )

        assert result is None

    @patch("triage.services.worker.analyze_sender")
    def test_returns_none_when_no_groups(self, mock_analyze: MagicMock) -> None:
        """Should return None when sender has no similarity groups."""
        mock_analyze.return_value = []

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "unknown@example.com"
        )

        assert result is None

    @patch("triage.services.worker.analyze_sender")
    def test_handles_exception_gracefully(self, mock_analyze: MagicMock) -> None:
        """Should return None and log error on exception."""
        mock_analyze.side_effect = Exception("Database error")

        result = self.worker._detect_or_create_pattern(
            self.mock_conn, "email1", "test@example.com"
        )

        assert result is None
