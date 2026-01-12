"""Worker status tracking - heartbeat and fleet visibility.

Workers register on startup, update heartbeat periodically, and track:
- Current config version
- Processing status (running, paused, shutting_down)
- Jobs processed count
- Last heartbeat timestamp
"""

import os

import psycopg2
from cortex_utils.logging import get_logger

logger = get_logger(__name__)


def register_worker(
    conn: psycopg2.extensions.connection,
    worker_id: str,
    worker_type: str,
    config_version: int | None = None,
) -> None:
    """Register worker on startup (upsert).

    Args:
        conn: Database connection
        worker_id: Unique worker identifier (e.g., "triage-12345")
        worker_type: Worker type (triage, labeling, parse, attachment)
        config_version: Current config version (None if loading from file)

    Idempotent: Safe to call on every startup.
    """
    cursor = conn.cursor()

    try:
        hostname = os.uname().nodename
        pid = os.getpid()

        cursor.execute(
            """
            INSERT INTO worker_status (
                worker_id,
                worker_type,
                current_config_version,
                status,
                last_heartbeat,
                started_at,
                jobs_processed,
                hostname,
                pid
            ) VALUES (%s, %s, %s, %s, NOW(), NOW(), 0, %s, %s)
            ON CONFLICT (worker_id) DO UPDATE SET
                status = 'running',
                last_heartbeat = NOW(),
                started_at = NOW(),
                jobs_processed = 0,
                current_config_version = EXCLUDED.current_config_version,
                hostname = EXCLUDED.hostname,
                pid = EXCLUDED.pid
            """,
            (worker_id, worker_type, config_version, "running", hostname, pid),
        )

        conn.commit()
        logger.info(
            f"Registered worker {worker_id} (type={worker_type}, "
            f"config_version={config_version})"
        )

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to register worker: {e}")
    finally:
        cursor.close()


def update_heartbeat(
    conn: psycopg2.extensions.connection,
    worker_id: str,
    jobs_processed: int | None = None,
) -> None:
    """Update worker heartbeat timestamp.

    Args:
        conn: Database connection
        worker_id: Worker identifier
        jobs_processed: Optional cumulative job count

    Call periodically (e.g., every 30 seconds) to signal liveness.
    """
    cursor = conn.cursor()

    try:
        if jobs_processed is not None:
            cursor.execute(
                """
                UPDATE worker_status
                SET last_heartbeat = NOW(),
                    jobs_processed = %s
                WHERE worker_id = %s
                """,
                (jobs_processed, worker_id),
            )
        else:
            cursor.execute(
                """
                UPDATE worker_status
                SET last_heartbeat = NOW()
                WHERE worker_id = %s
                """,
                (worker_id,),
            )

        conn.commit()

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update heartbeat for {worker_id}: {e}")
    finally:
        cursor.close()


def update_config_version(
    conn: psycopg2.extensions.connection,
    worker_id: str,
    config_version: int | None,
) -> None:
    """Update worker's current config version.

    Args:
        conn: Database connection
        worker_id: Worker identifier
        config_version: New config version (None if loading from file)

    Call after successful config reload.
    """
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE worker_status
            SET current_config_version = %s,
                last_reload_at = NOW()
            WHERE worker_id = %s
            """,
            (config_version, worker_id),
        )

        conn.commit()
        logger.info(f"Updated {worker_id} config version to {config_version}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update config version for {worker_id}: {e}")
    finally:
        cursor.close()


def update_status(
    conn: psycopg2.extensions.connection,
    worker_id: str,
    status: str,
) -> None:
    """Update worker status.

    Args:
        conn: Database connection
        worker_id: Worker identifier
        status: New status (running, paused, shutting_down)

    Valid statuses: running, paused, shutting_down
    """
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE worker_status
            SET status = %s,
                last_heartbeat = NOW()
            WHERE worker_id = %s
            """,
            (status, worker_id),
        )

        conn.commit()
        logger.info(f"Updated {worker_id} status to {status}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to update status for {worker_id}: {e}")
    finally:
        cursor.close()


def deregister_worker(
    conn: psycopg2.extensions.connection,
    worker_id: str,
) -> None:
    """Deregister worker on graceful shutdown.

    Args:
        conn: Database connection
        worker_id: Worker identifier

    Removes worker from active status tracking.
    """
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM worker_status
            WHERE worker_id = %s
            """,
            (worker_id,),
        )

        conn.commit()
        logger.info(f"Deregistered worker {worker_id}")

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to deregister worker {worker_id}: {e}")
    finally:
        cursor.close()


def cleanup_stale_workers(
    conn: psycopg2.extensions.connection,
    stale_threshold_minutes: int = 5,
) -> int:
    """Remove workers with stale heartbeats.

    Args:
        conn: Database connection
        stale_threshold_minutes: Consider workers dead after this many minutes

    Returns:
        Number of stale workers removed

    Run periodically (e.g., every minute) to clean up dead workers.
    """
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            DELETE FROM worker_status
            WHERE last_heartbeat < NOW() - INTERVAL '1 minute' * %s
            """,
            (stale_threshold_minutes,),
        )

        count: int = cursor.rowcount or 0
        conn.commit()

        if count > 0:
            logger.warning(f"Removed {count} stale worker(s)")

        return count

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to cleanup stale workers: {e}")
        return 0
    finally:
        cursor.close()


def get_worker_count(
    conn: psycopg2.extensions.connection,
    worker_type: str | None = None,
) -> int:
    """Get count of active workers.

    Args:
        conn: Database connection
        worker_type: Optional filter by worker type

    Returns:
        Number of active workers
    """
    cursor = conn.cursor()

    try:
        if worker_type:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM worker_status
                WHERE worker_type = %s
                """,
                (worker_type,),
            )
        else:
            cursor.execute("SELECT COUNT(*) FROM worker_status")

        result = cursor.fetchone()
        assert result is not None
        count: int = result[0]
        return count

    finally:
        cursor.close()
