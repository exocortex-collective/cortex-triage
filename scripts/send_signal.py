#!/usr/bin/env python3
"""Send a signal to triage workers.

Usage:
    python scripts/send_signal.py config_reload  # Reload config on all triage workers
    python scripts/send_signal.py shutdown      # Shutdown all workers
"""

import argparse
import logging
import os
import sys

import psycopg2

from triage.db.worker_signals import send_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Send control signal to triage workers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Signal types:
    config_reload  - Reload configuration without restart
    shutdown       - Graceful shutdown
    pause          - Pause processing (not yet implemented)
    resume         - Resume processing (not yet implemented)
    log_level      - Change log level (requires --payload '{"level": "DEBUG"}')

Examples:
    # Reload config on all triage workers
    python scripts/send_signal.py config_reload

    # Reload config on specific worker type
    python scripts/send_signal.py config_reload --target labeling

    # Shutdown all workers
    python scripts/send_signal.py shutdown

    # Change log level
    python scripts/send_signal.py log_level --payload '{"level": "DEBUG"}'

Environment variables:
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
        """,
    )

    parser.add_argument(
        "signal_type",
        choices=["config_reload", "shutdown", "pause", "resume", "log_level"],
        help="Type of signal to send",
    )

    parser.add_argument(
        "--target",
        default="all",
        choices=["all", "triage", "labeling", "parse", "attachment"],
        help="Target worker type (default: all)",
    )

    parser.add_argument(
        "--payload",
        help='Optional JSON payload (e.g., \'{"level": "DEBUG"}\')',
    )

    parser.add_argument(
        "--expires-in",
        type=int,
        help="Signal expiration time in seconds (default: never expires)",
    )

    return parser.parse_args()


def get_db_connection() -> psycopg2.extensions.connection:
    """Create database connection from environment variables."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db = os.getenv("POSTGRES_DB", "cortex")
    user = os.getenv("POSTGRES_USER", "cortex")
    password = os.getenv("POSTGRES_PASSWORD")

    if not password:
        logger.error("POSTGRES_PASSWORD environment variable not set")
        sys.exit(1)

    try:
        conn = psycopg2.connect(
            host=host,
            port=port,
            dbname=db,
            user=user,
            password=password,
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        sys.exit(1)


def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Parse payload if provided
    payload = None
    if args.payload:
        import json

        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON payload: {e}")
            sys.exit(1)

    # Connect to database
    conn = get_db_connection()
    logger.info(
        f"Connected to database: "
        f"{os.getenv('POSTGRES_HOST')}/{os.getenv('POSTGRES_DB')}"
    )

    try:
        # Send signal
        signal_id = send_signal(
            conn,
            signal_type=args.signal_type,
            target_worker=args.target,
            payload=payload,
            expires_in_seconds=args.expires_in,
        )

        logger.info("")
        logger.info("=" * 60)
        logger.info(f"SUCCESS: Signal sent (id={signal_id})")
        logger.info("=" * 60)
        logger.info("")
        logger.info(f"Signal type:   {args.signal_type}")
        logger.info(f"Target:        {args.target}")
        if payload:
            logger.info(f"Payload:       {payload}")
        logger.info("")
        logger.info("Workers will process this signal on their next poll cycle")
        logger.info("(poll interval: typically 30 seconds)")
        logger.info("")

    except Exception as e:
        logger.error(f"Failed to send signal: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
