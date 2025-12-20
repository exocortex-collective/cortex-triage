"""Tests for pattern_info population in worker's _get_email_data()."""

from datetime import datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from triage.models.patterns import EmailPattern, IntervalType, PatternStatus

if TYPE_CHECKING:
    from triage.services.worker import TriageWorker


class TestGetEmailDataPatternInfo:
    """Tests for pattern_info population in _get_email_data."""

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

        worker = self._make_worker()

        # Mock the connection pool and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        # Mock worker._get_pool().getconn() to return our mock connection
        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        worker._pool = mock_pool

        # Mock database row for email data
        mock_cursor.fetchone.return_value = (
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

        email = worker._get_email_data("gmail123")

        assert email is not None
        assert email.pattern_info is None
        mock_get_pattern.assert_called_once_with(mock_conn, "gmail123")

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

        worker = self._make_worker()

        # Mock the connection pool and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        worker._pool = mock_pool

        # Mock database row for email data
        mock_cursor.fetchone.return_value = (
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

        email = worker._get_email_data("gmail123")

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

        worker = self._make_worker()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        worker._pool = mock_pool

        mock_cursor.fetchone.return_value = (
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

        email = worker._get_email_data("gmail456")

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

        worker = self._make_worker()

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        mock_pool = MagicMock()
        mock_pool.getconn.return_value = mock_conn
        worker._pool = mock_pool

        mock_cursor.fetchone.return_value = (
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

        email = worker._get_email_data("gmail789")

        assert email is not None
        # pattern_info should be None because pattern.id was None
        assert email.pattern_info is None
