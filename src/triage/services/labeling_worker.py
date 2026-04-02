#!/usr/bin/env python3
"""Labeling worker - batches Gmail label operations for efficiency.

This worker consumes from the 'labeling' queue and uses Gmail's batchModify
API to apply labels to multiple messages in a single call, significantly
reducing API latency compared to individual calls.
"""

import argparse
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg2
from cortex_utils.logging import configure_logging, get_logger
from cortex_utils.metrics import (
    EMAILS_LABELED,
    ERRORS,
    PROCESSING_DURATION,
    QUEUE_PROCESSED,
    start_metrics_server,
)
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

logger = get_logger(__name__)

# Max retries for rate-limited requests
MAX_429_RETRIES = 3
# Timeout should be longer than MAX_BACKOFF_SECONDS (60s) to avoid race condition
ACQUIRE_TIMEOUT_SECONDS = 75.0


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
    visibility_timeout_minutes: int = (
        5  # How long a job can be in 'processing' before reset
    )


@dataclass
class LabelingJob:
    """A single labeling job from the queue.

    Only gmail_id is used from the payload. Target state (labels, archive, etc.)
    is queried from the classifications table at processing time, making stale
    jobs harmless.
    """

    job_id: int
    gmail_id: str
    repair: bool = False  # If true, query Gmail API for current label state


class GmailBatchClient:
    """Client for batch Gmail API operations."""

    # System labels have fixed IDs that don't need to be looked up
    SYSTEM_LABELS: frozenset[str] = frozenset(
        {
            "INBOX",
            "UNREAD",
            "STARRED",
            "SPAM",
            "TRASH",
            "IMPORTANT",
            "SENT",
            "DRAFT",
        }
    )

    def __init__(self, token_path: str):
        self.token_path = token_path
        self._service: Any = None
        self._label_cache: dict[str, str] = {}  # name -> id
        self._label_cache_lock = threading.Lock()  # Protects cache operations
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
        """Populate label cache if empty. Thread-safe."""
        with self._label_cache_lock:
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
        """Create a single label. Thread-safe."""
        with self._label_cache_lock:
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
            with self._label_cache_lock:
                self._label_cache[label_name] = created_id
                return created_id
        except HttpError as e:
            if e.resp.status == 409:
                logger.info(f"Label '{label_name}' already exists, refreshing cache")
                with self._label_cache_lock:
                    self._label_cache.clear()
                # _ensure_label_cache acquires its own lock
                self._ensure_label_cache()
                with self._label_cache_lock:
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
        # Check system labels first (they have fixed IDs, case-insensitive)
        if label_name.upper() in self.SYSTEM_LABELS:
            return label_name.upper()
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

        # Gmail API requires at least one label operation
        if not add_label_ids and not remove_label_ids:
            logger.debug(
                f"Skipping batch_modify for {len(gmail_ids)} messages: "
                "no label changes to apply"
            )
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

    queue_name = "labeling"  # Queue name for this worker

    def __init__(self, config: Config, gmail: GmailBatchClient):
        self.config = config
        self.gmail = gmail
        self.running = True
        self._pool: SimpleConnectionPool | None = None
        self._managed_prefix: str | None = None  # Loaded on first use

    def stop(self) -> None:
        """Signal the worker to stop."""
        self.running = False

    def close(self) -> None:
        """Close resources."""
        if self._pool:
            self._pool.closeall()
            self._pool = None
            logger.info("Database connection pool closed")

    def _get_managed_prefix(self) -> str:
        """Get the label prefix from the active triage config.

        Empty string means manage all user labels.
        Non-empty (e.g., "Cortex") means only manage labels under that prefix.
        """
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT label_prefix FROM triage_config_versions "
                "WHERE is_active = TRUE LIMIT 1"
            )
            row = cur.fetchone()
            self._managed_prefix = row[0] if row else "Cortex"
            logger.info(f"Label management prefix: {self._managed_prefix!r}")
        return self._managed_prefix

    def _is_managed_label(self, label_name: str) -> bool:
        """Check if a label is managed by triage (based on configured prefix)."""
        if label_name.upper() in GmailBatchClient.SYSTEM_LABELS:
            return False
        prefix = self._get_managed_prefix()
        if not prefix:
            return True
        return label_name.startswith(f"{prefix}/")

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
                    WHERE queue_name = %s
                      AND status = 'processing'
                      AND claimed_at < NOW() - (INTERVAL '1 minute' * %s)
                ),
                claimable AS (
                    SELECT id FROM queue
                    WHERE queue_name = %s AND status = 'pending'
                    ORDER BY priority DESC, created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE queue
                SET status = 'processing', claimed_at = NOW()
                WHERE id IN (SELECT id FROM claimable)
                RETURNING id, payload
                """,
                (
                    self.queue_name,
                    self.config.visibility_timeout_minutes,
                    self.queue_name,
                    batch_size,
                ),
            )
            rows = cur.fetchall()
            conn.commit()

        jobs = []
        for row in rows:
            job_id, payload = row
            # Only extract gmail_id and repair flag from payload.
            # Target state (labels, archive, etc.) is queried from classifications
            # table at processing time, making stale jobs harmless.
            jobs.append(
                LabelingJob(
                    job_id=job_id,
                    gmail_id=payload["gmail_id"],
                    repair=payload.get("repair", False),
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

    def _get_current_cortex_labels(self, gmail_ids: list[str]) -> dict[str, list[str]]:
        """Get current managed labels for a batch of gmail_ids.

        Queries emails_raw joined with gmail_labels to find all labels
        under the managed prefix currently on each message. Used to ensure
        stale labels are removed when re-triaging.

        Returns a dict mapping gmail_id -> list of managed label names.
        """
        if not gmail_ids:
            return {}

        prefix = self._get_managed_prefix()

        with self._get_conn() as conn, conn.cursor() as cur:
            if prefix:
                # Scoped to prefix
                cur.execute(
                    """
                    SELECT er.gmail_id, gl.name
                    FROM emails_raw er
                    CROSS JOIN LATERAL unnest(er.label_ids) AS label_id
                    JOIN gmail_labels gl ON gl.id = label_id
                    WHERE er.gmail_id = ANY(%s)
                      AND gl.name LIKE %s
                    """,
                    (gmail_ids, f"{prefix}/%%"),
                )
            else:
                # Manage all user labels (Label_* IDs, excludes system labels)
                cur.execute(
                    """
                    SELECT er.gmail_id, gl.name
                    FROM emails_raw er
                    CROSS JOIN LATERAL unnest(er.label_ids) AS label_id
                    JOIN gmail_labels gl ON gl.id = label_id
                    WHERE er.gmail_id = ANY(%s)
                      AND gl.id LIKE 'Label_%%'
                    """,
                    (gmail_ids,),
                )
            rows = cur.fetchall()

        result: dict[str, list[str]] = defaultdict(list)
        for gmail_id, label_name in rows:
            result[gmail_id].append(label_name)
        return result

    def _get_latest_classifications(
        self, gmail_ids: list[str]
    ) -> dict[str, dict | None]:
        """Get the latest classification for multiple emails.

        Returns dict mapping gmail_id -> action_taken dict (or None if not found).
        The action_taken dict contains: label, add_label, remove_label, archive,
        mark_read, star.
        """
        if not gmail_ids:
            return {}

        with self._get_conn() as conn, conn.cursor() as cur:
            # Use DISTINCT ON to get latest classification per gmail_id
            cur.execute(
                """
                SELECT DISTINCT ON (gmail_id) gmail_id, action_taken
                FROM classifications
                WHERE gmail_id = ANY(%s)
                ORDER BY gmail_id, classified_at DESC
                """,
                (gmail_ids,),
            )
            rows = cur.fetchall()

        return {gmail_id: action_taken for gmail_id, action_taken in rows}

    def _get_gmail_current_labels(self, gmail_id: str) -> list[str] | None:
        """Query Gmail API for current Cortex/* label names on a message.

        Used in repair mode when emails_raw.label_ids may be stale.
        Returns list of Cortex/* label names currently on the message,
        or None on failure (caller should fail the job and retry later).
        """
        try:
            service = self.gmail._get_service()
            msg = self.gmail._execute_with_rate_limit(
                lambda: service.users()
                .messages()
                .get(userId="me", id=gmail_id, format="minimal"),
                1,  # messages.get is 1 quota unit
                f"messages.get({gmail_id})",
            )
            label_ids = msg.get("labelIds", [])

            # Convert IDs to names using label cache
            self.gmail._ensure_label_cache()
            id_to_name = {v: k for k, v in self.gmail._label_cache.items()}

            # Only return labels we manage (scoped by label_prefix config).
            # System labels (INBOX, SENT, etc.) have non-Label_ IDs and are
            # never managed. User labels have IDs like "Label_123".
            return [
                id_to_name[lid]
                for lid in label_ids
                if lid in id_to_name
                and lid.startswith("Label_")
                and self._is_managed_label(id_to_name[lid])
            ]
        except Exception as e:
            logger.error(f"Failed to get Gmail labels for {gmail_id}: {e}")
            return None

    def _get_target_labels(self, classification: dict) -> set[str]:
        """Extract target labels from a classification's action_taken.

        Returns set of label names that should be on the email.
        """
        # Collect all labels from classification
        labels_to_process: list[str] = []

        if label := classification.get("label"):
            labels_to_process.append(label)

        if add_label := classification.get("add_label"):
            if isinstance(add_label, str):
                labels_to_process.append(add_label)
            elif isinstance(add_label, list):
                labels_to_process.extend(add_label)

        return {
            lbl
            for lbl in labels_to_process
            if isinstance(lbl, str) and self._is_managed_label(lbl)
        }

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
        """Process a batch of jobs. Returns (processed, failed).

        Uses reconciliation model: queries current state and target state at
        processing time, making stale jobs harmless.
        """
        jobs = self._claim_jobs(self.config.batch_size)

        if not jobs:
            return 0, 0

        start_time = time.time()
        logger.info(f"Processing batch of {len(jobs)} labeling jobs")

        # Separate repair jobs (need individual Gmail API calls) from normal jobs
        normal_jobs = [j for j in jobs if not j.repair]
        repair_jobs = [j for j in jobs if j.repair]

        processed, failed = 0, 0

        try:
            # Process normal jobs in batch
            if normal_jobs:
                p, f = self._process_normal_batch(normal_jobs)
                processed += p
                failed += f

            # Process repair jobs individually (Gmail API calls for current state)
            for job in repair_jobs:
                p, f = self._process_repair_job(job)
                processed += p
                failed += f

            return processed, failed
        finally:
            PROCESSING_DURATION.labels(queue="labeling", operation="batch").observe(
                time.time() - start_time
            )

    def _process_normal_batch(self, jobs: list[LabelingJob]) -> tuple[int, int]:
        """Process jobs using emails_raw for current state.

        Reconciles current Cortex/* labels (from DB) against target state
        (from classifications table).
        """
        gmail_ids = [j.gmail_id for j in jobs]

        # Get current Cortex/* labels from database
        current_labels_map = self._get_current_cortex_labels(gmail_ids)

        # Get target state from classifications table
        classifications = self._get_latest_classifications(gmail_ids)

        # Build reconciled operations for each job
        # Key: (add_labels, remove_labels, archive, mark_read, star)
        operation_groups: dict[tuple, list[LabelingJob]] = defaultdict(list)
        skip_job_ids: list[int] = []

        for job in jobs:
            classification = classifications.get(job.gmail_id)
            if not classification:
                logger.warning(f"No classification for {job.gmail_id}, skipping")
                skip_job_ids.append(job.job_id)
                continue

            # Compute target Cortex/* labels
            target = self._get_target_labels(classification)

            # Current Cortex/* labels
            current = set(current_labels_map.get(job.gmail_id, []))

            # Reconcile: add missing, remove extra
            add_labels = list(target - current)
            remove_labels = list(current - target)

            # System label actions from classification
            archive = classification.get("archive") or False
            mark_read = classification.get("mark_read") or False
            star = classification.get("star") or False
            important = classification.get("important") or False

            if star:
                add_labels.append("STARRED")
            if important:
                add_labels.append("IMPORTANT")
            if archive:
                remove_labels.append("INBOX")
            if mark_read:
                remove_labels.append("UNREAD")

            key = self._build_operation_key(
                add_labels, remove_labels, archive, mark_read, star
            )
            operation_groups[key].append(job)

        # Mark skipped jobs as completed (no classification = nothing to do)
        if skip_job_ids:
            self._complete_jobs(skip_job_ids)

        processed = len(skip_job_ids)
        failed = 0

        # Process each operation group as a batch
        for key, group_jobs in operation_groups.items():
            add_labels, remove_labels, _, _, _ = key

            gmail_ids = [j.gmail_id for j in group_jobs]
            job_ids = [j.job_id for j in group_jobs]

            # Resolve label names to IDs
            add_label_ids = self._resolve_labels(list(add_labels), create=True)
            if add_label_ids is None:
                self._fail_jobs(job_ids, "Failed to create required label")
                failed += len(group_jobs)
                ERRORS.labels(
                    service="labeling-worker", error_type="label_create_failed"
                ).inc()
                QUEUE_PROCESSED.labels(queue="labeling", status="error").inc(
                    len(group_jobs)
                )
                continue

            remove_label_ids = self._resolve_labels(list(remove_labels), create=False)
            assert remove_label_ids is not None  # create=False always returns list

            # Execute batch modify
            success = self.gmail.batch_modify(
                gmail_ids, add_label_ids, remove_label_ids
            )

            if success:
                self._complete_jobs(job_ids)
                processed += len(group_jobs)
                QUEUE_PROCESSED.labels(queue="labeling", status="success").inc(
                    len(group_jobs)
                )
                EMAILS_LABELED.inc(len(gmail_ids))
                logger.debug(
                    f"Completed batch: {len(gmail_ids)} messages, "
                    f"labels +{list(add_labels)} -{list(remove_labels)}"
                )
            else:
                self._fail_jobs(job_ids, "Gmail batch modify failed")
                failed += len(group_jobs)
                QUEUE_PROCESSED.labels(queue="labeling", status="error").inc(
                    len(group_jobs)
                )
                ERRORS.labels(
                    service="labeling-worker", error_type="batch_modify_failed"
                ).inc()

        return processed, failed

    def _process_repair_job(self, job: LabelingJob) -> tuple[int, int]:
        """Process a single repair job using Gmail API for current state.

        Used when emails_raw.label_ids may be stale and we need to query
        Gmail directly for the actual label state.
        """
        # Get target state from classifications
        classifications = self._get_latest_classifications([job.gmail_id])
        classification = classifications.get(job.gmail_id)

        if not classification:
            logger.warning(f"No classification for repair job {job.gmail_id}")
            self._complete_jobs([job.job_id])
            return 1, 0

        # Get current labels from Gmail API (not database)
        current_labels = self._get_gmail_current_labels(job.gmail_id)
        if current_labels is None:
            self._fail_jobs([job.job_id], "Failed to get current labels from Gmail API")
            QUEUE_PROCESSED.labels(queue="labeling", status="error").inc()
            ERRORS.labels(
                service="labeling-worker", error_type="gmail_api_failed"
            ).inc()
            return 0, 1
        current = set(current_labels)

        # Compute target
        target = self._get_target_labels(classification)

        # Reconcile
        add_labels = list(target - current)
        remove_labels = list(current - target)

        # System label actions
        archive = classification.get("archive") or False
        mark_read = classification.get("mark_read") or False
        star = classification.get("star") or False

        if star:
            add_labels.append("STARRED")
        if archive:
            remove_labels.append("INBOX")
        if mark_read:
            remove_labels.append("UNREAD")

        # Resolve and apply
        add_label_ids = self._resolve_labels(add_labels, create=True)
        if add_label_ids is None:
            self._fail_jobs([job.job_id], "Failed to create required label")
            ERRORS.labels(
                service="labeling-worker", error_type="label_create_failed"
            ).inc()
            QUEUE_PROCESSED.labels(queue="labeling", status="error").inc()
            return 0, 1

        remove_label_ids = self._resolve_labels(remove_labels, create=False)
        assert remove_label_ids is not None

        success = self.gmail.batch_modify(
            [job.gmail_id], add_label_ids, remove_label_ids
        )

        if success:
            self._complete_jobs([job.job_id])
            QUEUE_PROCESSED.labels(queue="labeling", status="success").inc()
            EMAILS_LABELED.inc()
            logger.debug(
                f"Repair completed: {job.gmail_id}, "
                f"labels +{add_labels} -{remove_labels}"
            )
            return 1, 0
        else:
            self._fail_jobs([job.job_id], "Gmail batch modify failed")
            QUEUE_PROCESSED.labels(queue="labeling", status="error").inc()
            ERRORS.labels(
                service="labeling-worker", error_type="batch_modify_failed"
            ).inc()
            return 0, 1

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


def repair_failed_jobs(config: Config) -> int:
    """Re-enqueue failed labeling jobs with repair=True flag.

    Returns the number of jobs re-enqueued.
    """
    conn = None
    try:
        conn = psycopg2.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_db,
            user=config.postgres_user,
            password=config.postgres_password,
        )
        with conn.cursor() as cur:
            # Re-enqueue failed labeling jobs with repair flag
            queue_name = LabelingWorker.queue_name
            cur.execute(
                """
                UPDATE queue
                SET status = 'pending',
                    payload = jsonb_set(payload, '{repair}', 'true'),
                    attempts = 0,
                    last_error = NULL,
                    claimed_at = NULL
                WHERE queue_name = %s AND status = 'failed'
                RETURNING id
                """,
                (queue_name,),
            )
            count: int = cur.rowcount or 0
            conn.commit()
            logger.info(f"Re-enqueued {count} failed jobs with repair=true")
            return count
    finally:
        if conn:
            conn.close()


def main() -> None:
    # Configure logging
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    configure_logging(service_name="labeling-worker", level=log_level)

    parser = argparse.ArgumentParser(description="Labeling worker service")
    parser.add_argument(
        "--once", action="store_true", help="Process all pending jobs once and exit"
    )
    parser.add_argument(
        "--repair-failed",
        action="store_true",
        help="Re-enqueue failed jobs with repair=true flag and exit",
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

    def check_required_vars(vars_list: list[str]) -> None:
        """Check for required environment variables and exit if any are missing."""
        missing = [v for v in vars_list if not os.environ.get(v)]
        if missing:
            logger.error(
                f"Missing required environment variables: {', '.join(missing)}"
            )
            sys.exit(1)

    # For repair-failed mode, we only need database vars
    repair_required_vars = [
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    ]

    # Handle --repair-failed mode first (doesn't need Gmail or metrics server)
    if args.repair_failed:
        check_required_vars(repair_required_vars)
        config = Config(
            postgres_host=os.environ["POSTGRES_HOST"],
            postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
            postgres_db=os.environ["POSTGRES_DB"],
            postgres_user=os.environ["POSTGRES_USER"],
            postgres_password=os.environ["POSTGRES_PASSWORD"],
            gmail_token_path="",  # Not needed for repair mode
            batch_size=args.batch_size,
            batch_delay=args.batch_delay,
        )
        repair_failed_jobs(config)
        sys.exit(0)

    # Normal mode - need all vars including Gmail
    check_required_vars(repair_required_vars + ["GMAIL_TOKEN_PATH"])

    # Start metrics server
    try:
        metrics_port = int(os.environ.get("METRICS_PORT", "8001"))
    except ValueError:
        logger.error("Invalid METRICS_PORT, using default 8001")
        metrics_port = 8001
    start_metrics_server(port=metrics_port)
    logger.info(f"Metrics server started on port {metrics_port}")

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
