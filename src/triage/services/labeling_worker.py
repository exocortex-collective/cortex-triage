#!/usr/bin/env python3
"""Labeling worker - batches Gmail label operations for efficiency.

This worker consumes from the 'labeling' queue and uses Gmail's batchModify
API to apply labels to multiple messages in a single call, significantly
reducing API latency compared to individual calls.
"""

import argparse
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from psycopg2.pool import SimpleConnectionPool

from triage.gmail import (
    QUOTA_LABELS_CREATE,
    QUOTA_LABELS_LIST,
    QUOTA_MESSAGES_BATCH_MODIFY,
    get_rate_limiter,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# How long a job can be in 'processing' before it's considered stale
# and reset to 'pending' (visibility timeout)
VISIBILITY_TIMEOUT_MINUTES = 5

# Max retries for rate-limited requests
MAX_429_RETRIES = 3
ACQUIRE_TIMEOUT_SECONDS = 60.0


@dataclass
class Config:
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    gmail_token_path: str
    batch_size: int = 50  # Gmail batchModify max is 1000, but 50 is reasonable
    poll_interval: int = 10  # Seconds to wait when queue is empty
    batch_delay: int = 5  # Seconds to wait between batches to let work accumulate


@dataclass
class LabelingJob:
    """A single labeling job from the queue."""

    job_id: int
    gmail_id: str
    add_labels: list[str]
    remove_labels: list[str]
    archive: bool
    mark_read: bool
    star: bool


class GmailBatchClient:
    """Client for batch Gmail API operations."""

    def __init__(self, token_path: str):
        self.token_path = token_path
        self._service: Any = None
        self._label_cache: dict[str, str] = {}  # name -> id
        self._rate_limiter = get_rate_limiter()

    def _get_service(self) -> Any:
        """Get authenticated Gmail API service."""
        if self._service is None:
            creds = Credentials.from_authorized_user_file(self.token_path)
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def _execute_with_rate_limit(
        self, request_factory: Callable[[], Any], quota_units: int, operation: str
    ) -> Any:
        """Execute a Gmail API request with rate limiting and retry.

        Args:
            request_factory: A callable that creates a new Gmail API request object
            quota_units: Number of quota units this operation consumes
            operation: Human-readable operation name for logging

        Returns:
            The API response

        Raises:
            HttpError: If request fails after retries
        """
        total_attempts = MAX_429_RETRIES + 1
        for attempt in range(total_attempts):
            # Acquire quota tokens (blocks up to timeout)
            if not self._rate_limiter.acquire(
                quota_units, timeout=ACQUIRE_TIMEOUT_SECONDS
            ):
                raise RuntimeError(
                    f"Timed out waiting for Gmail API quota for operation: {operation}"
                )

            try:
                # Create a new request object for each attempt
                # (HttpRequest objects are single-use)
                request = request_factory()
                result = request.execute()
                self._rate_limiter.record_success()
                return result

            except HttpError as e:
                if e.resp.status == 429:
                    # Parse Retry-After header if present
                    # Can be seconds (integer) or HTTP-date; we only handle seconds
                    retry_after = e.resp.get("Retry-After")
                    retry_seconds: float | None = None
                    if retry_after:
                        try:
                            retry_seconds = float(retry_after)
                        except ValueError:
                            logger.warning(
                                f"Could not parse Retry-After header: '{retry_after}'. "
                                "Falling back to exponential backoff."
                            )

                    self._rate_limiter.record_429(retry_seconds)

                    if attempt < MAX_429_RETRIES:
                        logger.warning(
                            f"{operation}: Rate limited "
                            f"(attempt {attempt + 1}/{total_attempts}), will retry"
                        )
                        continue
                    else:
                        logger.error(f"{operation}: Rate limited, max retries exceeded")

                # Re-raise for non-429 errors or after max retries
                raise

        # Should not reach here, but satisfy type checker
        raise RuntimeError("Unexpected end of retry loop")

    def _ensure_label_cache(self) -> None:
        """Populate label cache if empty."""
        if self._label_cache:
            return

        service = self._get_service()
        results = self._execute_with_rate_limit(
            lambda: service.users().labels().list(userId="me"),
            QUOTA_LABELS_LIST,
            "labels.list",
        )
        for label in results.get("labels", []):
            self._label_cache[label["name"]] = label["id"]
        logger.debug(f"Cached {len(self._label_cache)} Gmail labels")

    def _get_or_create_label(self, label_name: str) -> str | None:
        """Get label ID, creating the label if it doesn't exist.

        For nested labels (e.g., "Cortex/Aurelia/Memes"), creates parent
        labels first if they don't exist.
        """
        try:
            self._ensure_label_cache()

            # Normalize label name
            label_name = "/".join(filter(None, label_name.split("/")))
            if not label_name:
                return None

            if label_name in self._label_cache:
                return self._label_cache[label_name]

            # For nested labels, ensure parent labels exist first
            if "/" in label_name:
                parts = label_name.split("/")
                for i in range(1, len(parts)):
                    parent_name = "/".join(parts[:i])
                    if (
                        parent_name not in self._label_cache
                        and self._create_single_label(parent_name) is None
                    ):
                        return None

            return self._create_single_label(label_name)

        except Exception as e:
            logger.error(f"Failed to get/create label {label_name}: {e}")
            return None

    def _create_single_label(self, label_name: str) -> str | None:
        """Create a single label."""
        if label_name in self._label_cache:
            return self._label_cache[label_name]

        try:
            service = self._get_service()
            label_body = {
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            }
            created = self._execute_with_rate_limit(
                lambda: service.users().labels().create(userId="me", body=label_body),
                QUOTA_LABELS_CREATE,
                f"labels.create({label_name})",
            )
            logger.info(f"Created label: {label_name}")
            created_id: str = created["id"]
            self._label_cache[label_name] = created_id
            return created_id
        except HttpError as e:
            if e.resp.status == 409:
                logger.info(f"Label '{label_name}' already exists, refreshing cache")
                self._label_cache.clear()
                self._ensure_label_cache()
                return self._label_cache.get(label_name)
            logger.error(
                f"Gmail API error creating label {label_name}: "
                f"HTTP {e.resp.status} - {e.reason}"
            )
            return None
        except Exception as e:
            logger.error(f"Failed to create label {label_name}: {e}")
            return None

    def get_label_id(self, label_name: str) -> str | None:
        """Get label ID for a label name, creating if needed."""
        return self._get_or_create_label(label_name)

    def get_existing_label_id(self, label_name: str) -> str | None:
        """Get label ID for an existing label (no creation)."""
        self._ensure_label_cache()
        return self._label_cache.get(label_name)

    def batch_modify(
        self,
        gmail_ids: list[str],
        add_label_ids: list[str],
        remove_label_ids: list[str],
    ) -> bool:
        """Apply label modifications to multiple messages at once.

        Gmail's batchModify API can handle up to 1000 message IDs per call.
        Returns True on success.
        """
        if not gmail_ids:
            return True

        try:
            service = self._get_service()
            body: dict[str, Any] = {"ids": gmail_ids}

            if add_label_ids:
                body["addLabelIds"] = add_label_ids
            if remove_label_ids:
                body["removeLabelIds"] = remove_label_ids

            self._execute_with_rate_limit(
                lambda: service.users().messages().batchModify(userId="me", body=body),
                QUOTA_MESSAGES_BATCH_MODIFY,
                f"messages.batchModify({len(gmail_ids)} msgs)",
            )

            logger.info(
                f"Batch modified {len(gmail_ids)} messages: "
                f"+{len(add_label_ids)} labels, -{len(remove_label_ids)} labels"
            )
            return True

        except HttpError as e:
            logger.error(
                f"Gmail batch modify failed: HTTP {e.resp.status} - {e.reason}"
            )
            return False
        except Exception as e:
            logger.error(f"Gmail batch modify failed: {e}")
            return False


class LabelingWorker:
    """Worker that batches label operations for efficiency."""

    def __init__(self, config: Config, gmail: GmailBatchClient):
        self.config = config
        self.gmail = gmail
        self.running = True
        self._pool: SimpleConnectionPool | None = None

    def stop(self) -> None:
        """Signal the worker to stop."""
        self.running = False

    def close(self) -> None:
        """Close resources."""
        if self._pool:
            self._pool.closeall()
            self._pool = None
            logger.info("Database connection pool closed")

    def _get_pool(self) -> SimpleConnectionPool:
        """Get or create connection pool."""
        if self._pool is None:
            self._pool = SimpleConnectionPool(
                minconn=1,
                maxconn=5,
                host=self.config.postgres_host,
                port=self.config.postgres_port,
                dbname=self.config.postgres_db,
                user=self.config.postgres_user,
                password=self.config.postgres_password,
            )
        return self._pool

    @contextmanager
    def _get_conn(self) -> Iterator[psycopg2.extensions.connection]:
        """Get a connection from the pool."""
        conn = self._get_pool().getconn()
        try:
            yield conn
        finally:
            self._get_pool().putconn(conn)

    def _claim_jobs(self, batch_size: int) -> list[LabelingJob]:
        """Claim a batch of pending labeling jobs.

        Also resets stale jobs that have been processing for too long
        (visibility timeout), handling crashed/killed workers.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            # Reset stale jobs and claim new ones in a single transaction
            cur.execute(
                """
                WITH reset_stale AS (
                    UPDATE queue
                    SET status = 'pending', claimed_at = NULL
                    WHERE queue_name = 'labeling'
                      AND status = 'processing'
                      AND claimed_at < NOW() - (INTERVAL '1 minute' * %s)
                ),
                claimable AS (
                    SELECT id FROM queue
                    WHERE queue_name = 'labeling' AND status = 'pending'
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE queue
                SET status = 'processing', claimed_at = NOW()
                WHERE id IN (SELECT id FROM claimable)
                RETURNING id, payload
                """,
                (VISIBILITY_TIMEOUT_MINUTES, batch_size),
            )
            rows = cur.fetchall()
            conn.commit()

        jobs = []
        for row in rows:
            job_id, payload = row
            jobs.append(
                LabelingJob(
                    job_id=job_id,
                    gmail_id=payload["gmail_id"],
                    add_labels=payload.get("add_labels", []),
                    remove_labels=payload.get("remove_labels", []),
                    archive=payload.get("archive", False),
                    mark_read=payload.get("mark_read", False),
                    star=payload.get("star", False),
                )
            )
        return jobs

    def _complete_jobs(self, job_ids: list[int]) -> None:
        """Mark jobs as completed."""
        if not job_ids:
            return
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE queue
                SET status = 'completed', completed_at = NOW()
                WHERE id = ANY(%s)
                """,
                (job_ids,),
            )
            conn.commit()

    def _fail_jobs(self, job_ids: list[int], error: str) -> None:
        """Mark jobs as failed."""
        if not job_ids:
            return
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE queue
                SET status = 'failed',
                    last_error = %s,
                    attempts = attempts + 1
                WHERE id = ANY(%s)
                """,
                (error[:1000], job_ids),
            )
            conn.commit()

    def _resolve_labels(
        self, label_names: list[str], create: bool = True
    ) -> list[str] | None:
        """Convert label names to IDs.

        Returns None if any required label creation fails (create=True).
        For removals (create=False), missing labels are skipped silently.
        """
        label_ids = []
        for name in label_names:
            if create:
                label_id = self.gmail.get_label_id(name)
                if not label_id:
                    logger.error(f"Failed to create required label: {name}")
                    return None  # Fail the operation
            else:
                label_id = self.gmail.get_existing_label_id(name)
                if not label_id:
                    logger.debug(f"Label not found for removal (ok): {name}")
                    continue
            label_ids.append(label_id)
        return label_ids

    def _build_operation_key(
        self,
        add_labels: list[str],
        remove_labels: list[str],
        archive: bool,
        mark_read: bool,
        star: bool,
    ) -> tuple:
        """Build a hashable key for grouping similar operations."""
        return (
            tuple(sorted(add_labels)),
            tuple(sorted(remove_labels)),
            archive,
            mark_read,
            star,
        )

    def _process_batch(self) -> tuple[int, int]:
        """Process a batch of jobs. Returns (processed, failed)."""
        jobs = self._claim_jobs(self.config.batch_size)

        if not jobs:
            return 0, 0

        logger.info(f"Processing batch of {len(jobs)} labeling jobs")

        # Group jobs by operation type for batching
        operation_groups: dict[tuple, list[LabelingJob]] = defaultdict(list)

        for job in jobs:
            # Build full list of labels to add (including system labels)
            add_labels = list(job.add_labels)
            remove_labels = list(job.remove_labels)

            if job.star:
                add_labels.append("STARRED")
            if job.archive:
                remove_labels.append("INBOX")
            if job.mark_read:
                remove_labels.append("UNREAD")

            key = self._build_operation_key(
                add_labels, remove_labels, job.archive, job.mark_read, job.star
            )
            operation_groups[key].append(job)

        processed = 0
        failed = 0

        # Process each operation group as a batch
        for key, group_jobs in operation_groups.items():
            add_labels, remove_labels, _, _, _ = key

            gmail_ids = [j.gmail_id for j in group_jobs]
            job_ids = [j.job_id for j in group_jobs]

            # Resolve label names to IDs
            add_label_ids = self._resolve_labels(list(add_labels), create=True)
            if add_label_ids is None:
                # Failed to create required labels - fail these jobs
                self._fail_jobs(job_ids, "Failed to create required label")
                failed += len(group_jobs)
                continue

            remove_label_ids = self._resolve_labels(list(remove_labels), create=False)
            # remove_label_ids is always a list when create=False (skips missing)
            assert remove_label_ids is not None  # for type checker

            # Execute batch modify
            success = self.gmail.batch_modify(
                gmail_ids, add_label_ids, remove_label_ids
            )

            if success:
                self._complete_jobs(job_ids)
                processed += len(group_jobs)
                logger.debug(
                    f"Completed batch: {len(gmail_ids)} messages, "
                    f"labels +{list(add_labels)} -{list(remove_labels)}"
                )
            else:
                self._fail_jobs(job_ids, "Gmail batch modify failed")
                failed += len(group_jobs)

        return processed, failed

    def run(self) -> None:
        """Main processing loop."""
        logger.info("Labeling worker started")

        total_processed = 0
        total_failed = 0

        while self.running:
            processed, failed = self._process_batch()

            if processed == 0 and failed == 0:
                logger.debug(f"No jobs, sleeping {self.config.poll_interval}s")
                time.sleep(self.config.poll_interval)
                continue

            total_processed += processed
            total_failed += failed

            if (total_processed + total_failed) % 100 == 0:
                logger.info(
                    f"Progress: {total_processed} processed, {total_failed} failed"
                )

            # Delay between batches to let work accumulate
            if self.running:
                logger.debug(f"Batch complete, waiting {self.config.batch_delay}s")
                time.sleep(self.config.batch_delay)

        logger.info(
            f"Labeling worker stopped: "
            f"{total_processed} processed, {total_failed} failed"
        )

    def run_once(self) -> tuple[int, int]:
        """Process all pending jobs once, then exit."""
        logger.info("Labeling worker running once")

        total_processed = 0
        total_failed = 0

        while True:
            processed, failed = self._process_batch()

            if processed == 0 and failed == 0:
                break

            total_processed += processed
            total_failed += failed

            if (total_processed + total_failed) % 100 == 0:
                logger.info(
                    f"Progress: {total_processed} processed, {total_failed} failed"
                )

        logger.info(
            f"Labeling worker complete: "
            f"{total_processed} processed, {total_failed} failed"
        )
        return total_processed, total_failed


def main() -> None:
    parser = argparse.ArgumentParser(description="Labeling worker service")
    parser.add_argument(
        "--once", action="store_true", help="Process all pending jobs once and exit"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of jobs to process per batch (default: 50)",
    )
    parser.add_argument(
        "--batch-delay",
        type=int,
        default=5,
        help="Seconds to wait between batches (default: 5)",
    )
    args = parser.parse_args()

    required_vars = [
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "GMAIL_TOKEN_PATH",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    config = Config(
        postgres_host=os.environ["POSTGRES_HOST"],
        postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        postgres_db=os.environ["POSTGRES_DB"],
        postgres_user=os.environ["POSTGRES_USER"],
        postgres_password=os.environ["POSTGRES_PASSWORD"],
        gmail_token_path=os.environ["GMAIL_TOKEN_PATH"],
        batch_size=args.batch_size,
        batch_delay=args.batch_delay,
    )

    gmail = GmailBatchClient(config.gmail_token_path)
    worker = LabelingWorker(config, gmail)

    def shutdown_handler(signum: int, frame: Any) -> None:
        logger.info("Received shutdown signal")
        worker.stop()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    try:
        if args.once:
            processed, failed = worker.run_once()
            sys.exit(0 if failed == 0 else 1)
        else:
            worker.run()
    finally:
        worker.close()


if __name__ == "__main__":
    main()
