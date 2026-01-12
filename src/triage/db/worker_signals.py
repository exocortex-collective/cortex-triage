"""Worker signal queue - control messages for worker management.

Signals are lightweight control messages sent to workers for:
- config_reload: Hot-reload configuration without restart
- shutdown: Graceful shutdown
- pause/resume: Pause/resume processing
- log_level: Change log level dynamically
"""

import json
from datetime import datetime, timedelta
from typing import Any

import psycopg2
from cortex_utils.logging import get_logger

logger = get_logger(__name__)


class WorkerSignal:
    """Worker signal for control messages."""

    def __init__(
        self,
        signal_id: int,
        signal_type: str,
        target_worker: str,
        payload: dict[str, Any] | None = None,
        created_at: datetime | None = None,
    ):
        self.signal_id = signal_id
        self.signal_type = signal_type
        self.target_worker = target_worker
        self.payload = payload or {}
        self.created_at = created_at or datetime.utcnow()


def send_signal(
    conn: psycopg2.extensions.connection,
    signal_type: str,
    target_worker: str = "all",
    payload: dict[str, Any] | None = None,
    expires_in_seconds: int | None = None,
) -> int:
    """Send a signal to workers.

    Args:
        conn: Database connection
        signal_type: Type of signal (config_reload, shutdown, pause, resume, log_level)
        target_worker: Target worker(s) - 'triage', 'labeling', 'all' (default: 'all')
        payload: Optional payload data
        expires_in_seconds: Signal expiration time in seconds (None = never expires)

    Returns:
        Signal ID

    Raises:
        ValueError: If signal_type is invalid
    """
    valid_signal_types = {"config_reload", "shutdown", "pause", "resume", "log_level"}
    if signal_type not in valid_signal_types:
        raise ValueError(
            f"Invalid signal type: {signal_type}. "
            f"Must be one of: {', '.join(valid_signal_types)}"
        )

    cursor = conn.cursor()

    try:
        expires_at = None
        if expires_in_seconds:
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in_seconds)

        cursor.execute(
            """
            INSERT INTO worker_signals (
                signal_type,
                target_worker,
                payload,
                expires_at
            ) VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (
                signal_type,
                target_worker,
                json.dumps(payload) if payload else None,
                expires_at,
            ),
        )

        result = cursor.fetchone()
        assert result is not None
        signal_id: int = result[0]
        conn.commit()

        logger.info(f"Sent {signal_type} signal to {target_worker} (id={signal_id})")
        return signal_id

    except Exception as e:
        conn.rollback()
        raise Exception(f"Failed to send signal: {e}") from e
    finally:
        cursor.close()


def poll_signals(
    conn: psycopg2.extensions.connection,
    worker_type: str,
    worker_id: str,
) -> list[WorkerSignal]:
    """Poll for pending signals targeting this worker.

    Args:
        conn: Database connection
        worker_type: Worker type ('triage', 'labeling', etc.)
        worker_id: Unique worker ID

    Returns:
        List of pending signals (oldest first)

    Marks retrieved signals as 'processed' to prevent duplicate processing.
    """
    cursor = conn.cursor()

    try:
        # Fetch pending signals for this worker type or 'all'
        cursor.execute(
            """
            UPDATE worker_signals
            SET
                status = 'processed',
                processed_at = NOW(),
                processed_by = %s
            WHERE id IN (
                SELECT id FROM worker_signals
                WHERE status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND (target_worker = %s OR target_worker = 'all')
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, signal_type, target_worker, payload, created_at
            """,
            (worker_id, worker_type),
        )

        signals = []
        for row in cursor.fetchall():
            signal_id, signal_type, target_worker, payload, created_at = row
            signals.append(
                WorkerSignal(
                    signal_id=signal_id,
                    signal_type=signal_type,
                    target_worker=target_worker,
                    payload=payload or {},
                    created_at=created_at,
                )
            )

        conn.commit()

        if signals:
            logger.info(
                f"Received {len(signals)} signal(s) for worker {worker_id}: "
                f"{[s.signal_type for s in signals]}"
            )

        return signals

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to poll signals: {e}")
        return []
    finally:
        cursor.close()


def expire_old_signals(conn: psycopg2.extensions.connection, days: int = 7) -> int:
    """Mark old processed signals as expired for cleanup.

    Args:
        conn: Database connection
        days: Age threshold in days (default: 7)

    Returns:
        Number of signals marked as expired
    """
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE worker_signals
            SET status = 'expired'
            WHERE status = 'processed'
              AND processed_at < NOW() - INTERVAL '1 day' * %s
            """,
            (days,),
        )

        count: int = cursor.rowcount or 0
        conn.commit()

        if count > 0:
            logger.info(f"Marked {count} old signals as expired")

        return count

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to expire old signals: {e}")
        return 0
    finally:
        cursor.close()


def get_pending_signal_count(
    conn: psycopg2.extensions.connection,
    worker_type: str | None = None,
) -> int:
    """Get count of pending signals.

    Args:
        conn: Database connection
        worker_type: Optional filter by worker type

    Returns:
        Number of pending signals
    """
    cursor = conn.cursor()

    try:
        if worker_type:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM worker_signals
                WHERE status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                  AND (target_worker = %s OR target_worker = 'all')
                """,
                (worker_type,),
            )
        else:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM worker_signals
                WHERE status = 'pending'
                  AND (expires_at IS NULL OR expires_at > NOW())
                """
            )

        result = cursor.fetchone()
        assert result is not None
        count: int = result[0]
        return count

    finally:
        cursor.close()
