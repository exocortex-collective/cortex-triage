"""Tests for retroactive action system - auto-enqueue on pattern activation."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from triage.services.patterns import (
    add_email_to_pattern,
    enqueue_pattern_for_retriage,
)


class TestEnqueuePatternForRetriage:
    """Tests for enqueue_pattern_for_retriage function."""

    def test_enqueues_pattern_emails(self) -> None:
        """Should enqueue all emails in a pattern to triage queue."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 3  # 3 emails enqueued

        result = enqueue_pattern_for_retriage(conn, pattern_id=1)

        assert result == 3
        cursor.execute.assert_called_once()
        # Verify SQL includes pattern_id and priority
        sql = cursor.execute.call_args[0][0]
        assert "queue" in sql
        assert "pattern_emails" in sql
        assert "triage" in sql

    def test_uses_backfill_priority(self) -> None:
        """Should use priority=-100 for backfill by default."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 1

        enqueue_pattern_for_retriage(conn, pattern_id=1)

        # Check that -100 is passed as priority
        params = cursor.execute.call_args[0][1]
        assert params[0] == -100  # priority
        assert params[1] == 1  # pattern_id

    def test_custom_priority(self) -> None:
        """Should allow custom priority."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 1

        enqueue_pattern_for_retriage(conn, pattern_id=1, priority=-50)

        params = cursor.execute.call_args[0][1]
        assert params[0] == -50

    def test_skips_already_pending(self) -> None:
        """Should not enqueue emails already pending in queue."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 0  # No new emails enqueued (all already pending)

        result = enqueue_pattern_for_retriage(conn, pattern_id=1)

        assert result == 0
        # SQL should include NOT EXISTS check
        sql = cursor.execute.call_args[0][0]
        assert "NOT EXISTS" in sql


class TestAddEmailToPatternRetroactive:
    """Tests for retroactive trigger in add_email_to_pattern."""

    @patch("triage.services.patterns.enqueue_pattern_for_retriage")
    def test_triggers_enqueue_at_third_email(self, mock_enqueue: MagicMock) -> None:
        """Should trigger retroactive enqueue when pattern becomes active."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        # First cursor call: INSERT pattern_email
        # Second cursor call: UPDATE pattern stats
        cursor.fetchone.side_effect = [
            # pattern_email row
            {
                "id": 1,
                "pattern_id": 1,
                "gmail_id": "test123",
                "amount": None,
                "currency": None,
                "matched_at": datetime.now(),
            },
            # UPDATE RETURNING (occurrence_count=3, status='active')
            {"occurrence_count": 3, "status": "active"},
        ]

        add_email_to_pattern(conn, pattern_id=1, gmail_id="test123")

        # Should trigger retroactive enqueue
        mock_enqueue.assert_called_once_with(conn, 1)

    @patch("triage.services.patterns.enqueue_pattern_for_retriage")
    def test_no_trigger_at_second_email(self, mock_enqueue: MagicMock) -> None:
        """Should NOT trigger enqueue for second email (still emerging)."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        cursor.fetchone.side_effect = [
            {
                "id": 1,
                "pattern_id": 1,
                "gmail_id": "test123",
                "amount": None,
                "currency": None,
                "matched_at": datetime.now(),
            },
            # occurrence_count=2, status='emerging'
            {"occurrence_count": 2, "status": "emerging"},
        ]

        add_email_to_pattern(conn, pattern_id=1, gmail_id="test123")

        mock_enqueue.assert_not_called()

    @patch("triage.services.patterns.enqueue_pattern_for_retriage")
    def test_no_trigger_at_fourth_email(self, mock_enqueue: MagicMock) -> None:
        """Should NOT trigger enqueue for 4th+ email (already active)."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        cursor.fetchone.side_effect = [
            {
                "id": 1,
                "pattern_id": 1,
                "gmail_id": "test123",
                "amount": None,
                "currency": None,
                "matched_at": datetime.now(),
            },
            # occurrence_count=4, status='active' (already was active)
            {"occurrence_count": 4, "status": "active"},
        ]

        add_email_to_pattern(conn, pattern_id=1, gmail_id="test123")

        # Should NOT trigger - only triggers at exactly 3
        mock_enqueue.assert_not_called()

    @patch("triage.services.patterns.enqueue_pattern_for_retriage")
    def test_no_trigger_when_already_active(self, mock_enqueue: MagicMock) -> None:
        """Should not trigger if pattern was already active."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        cursor.fetchone.side_effect = [
            {
                "id": 1,
                "pattern_id": 1,
                "gmail_id": "test123",
                "amount": None,
                "currency": None,
                "matched_at": datetime.now(),
            },
            # 5 occurrences, already active
            {"occurrence_count": 5, "status": "active"},
        ]

        add_email_to_pattern(conn, pattern_id=1, gmail_id="test123")

        mock_enqueue.assert_not_called()

    @patch("triage.services.patterns.enqueue_pattern_for_retriage")
    def test_handles_amount_and_currency(self, mock_enqueue: MagicMock) -> None:
        """Should pass through amount and currency correctly."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        cursor.fetchone.side_effect = [
            {
                "id": 1,
                "pattern_id": 1,
                "gmail_id": "test123",
                "amount": Decimal("10.00"),
                "currency": "USD",
                "matched_at": datetime.now(),
            },
            {"occurrence_count": 3, "status": "active"},
        ]

        result = add_email_to_pattern(
            conn,
            pattern_id=1,
            gmail_id="test123",
            amount=Decimal("10.00"),
            currency="USD",
        )

        assert result.amount == Decimal("10.00")
        assert result.currency == "USD"
        mock_enqueue.assert_called_once()


class TestRetroactiveIdempotency:
    """Tests for idempotent behavior of retroactive enqueue."""

    def test_sql_prevents_duplicates(self) -> None:
        """SQL should prevent duplicate queue entries."""
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 0

        enqueue_pattern_for_retriage(conn, pattern_id=1)

        sql = cursor.execute.call_args[0][0]
        # Should check for existing pending entries
        assert "NOT EXISTS" in sql
        assert "pending" in sql
