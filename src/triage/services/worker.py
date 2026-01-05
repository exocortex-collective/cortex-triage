#!/usr/bin/env python3
"""Triage worker service - processes emails from queue through rules engine."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import psycopg2
from cortex_utils.metrics import (
    EMAILS_CLASSIFIED,
    ERRORS,
    PROCESSING_DURATION,
    QUEUE_PROCESSED,
    start_metrics_server,
)
from cortex_utils.triage_config import (
    ConfigLoadError,
    load_config_from_db,
    validate_rules,
)
from cortex_utils.triage_config.models import (
    Email,
    EvaluationResult,
    PatternInfo,
    RulesConfig,
)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from psycopg2.extras import Json
from psycopg2.pool import SimpleConnectionPool

from triage.db.worker_signals import poll_signals
from triage.db.worker_status import (
    cleanup_stale_workers,
    deregister_worker,
    register_worker,
    update_config_version,
    update_heartbeat,
)
from triage.engine.matcher import evaluate
from triage.models.patterns import EmailPattern
from triage.services.lifecycle import (
    LifecycleConfig,
    check_maintenance_needed,
    run_lifecycle_maintenance,
)
from triage.services.patterns import (
    PATTERN_ACTIVATION_COUNT,
    add_email_to_pattern,
    create_pattern,
    get_pattern_by_sender_merchant,
    get_pattern_for_email,
)
from triage.services.similarity import analyze_sender
from triage.services.temporal import detect_pattern


class LLMError(Exception):
    """Raised when LLM call fails (network, HTTP, or invalid response)."""

    pass


# SQL queries
_GET_ACTIVE_CONFIG_VERSION_SQL = """
    SELECT version FROM triage_config_versions
    WHERE is_active = TRUE
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class Config:
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    duckdb_api_url: str
    ollama_url: str
    gmail_token_path: str
    batch_size: int = 100
    poll_interval: int = 5  # seconds between queue polls when empty
    signal_poll_interval: int = (
        30  # seconds between signal polls (check for control messages)
    )
    # Convergence settings (idle-time config updates)
    enable_convergence: bool = True  # Enable/disable idle-time convergence
    priority_convergence_interval: int = 300  # seconds between bulk priority updates


# Max characters of email body to include in LLM prompts
LLM_BODY_PREVIEW_LENGTH = 1000

# Invalid LLM extraction responses to reject
INVALID_LLM_EXTRACTION_VALUES = {
    "none",
    "n/a",
    "unknown",
    "null",
    "undefined",
}

# How long a job can be in 'processing' before it's considered stale
# and reset to 'pending' (visibility timeout)
VISIBILITY_TIMEOUT_MINUTES = 5

# Maximum retry attempts before a job is considered dead letter
MAX_ATTEMPTS = 3

# Pattern quality filter: confidence threshold for irregular patterns
# Irregular patterns with confidence below this threshold are only created
# if the merchant is known (not "unknown")
PATTERN_QUALITY_CONFIDENCE_THRESHOLD = 0.5


class OllamaClient:
    """Client for Ollama LLM calls."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=60.0)

    def check_intent(self, subject: str, prompt: str, model: str) -> bool:
        """Check if subject matches an intent using LLM.

        Args:
            subject: Email subject (for logging/context).
            prompt: The fully formatted prompt string to send.
            model: Model name to use.

        Returns:
            True if the intent matches, False otherwise.

        Raises:
            LLMError: If the LLM call fails (network, HTTP, or other error).
        """
        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            answer: str = result.get("response", "").strip().lower()
            return answer == "yes"
        except httpx.RequestError as e:
            logger.error(f"Ollama intent check network error: {e}")
            raise LLMError(f"Ollama network error: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Ollama intent check HTTP error {e.response.status_code}: {e}"
            )
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except Exception as e:
            logger.error(f"Ollama intent check failed unexpectedly: {e}")
            raise LLMError(f"Ollama error: {e}") from e

    def classify(self, prompt: str, model: str) -> tuple[str, float, str]:
        """Classify an email using LLM.

        Args:
            prompt: Fully formatted classification prompt.
            model: Model name to use.

        Returns:
            Tuple of (category, confidence, reasoning).

        Raises:
            LLMError: If the LLM call fails (network, HTTP, or invalid response).
        """
        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            result = response.json()
            text = result.get("response", "{}")
            data = json.loads(text)

            # Validate response is a dict
            if not isinstance(data, dict):
                logger.error(f"Ollama returned non-dict JSON: {text}")
                raise LLMError(f"Ollama returned non-dict JSON: {text}")

            # Safe float conversion for confidence
            raw_confidence = data.get("confidence")
            try:
                confidence = (
                    float(raw_confidence) if raw_confidence is not None else 0.5
                )
            except (ValueError, TypeError):
                logger.warning(f"Invalid confidence value from LLM: {raw_confidence}")
                confidence = 0.5

            return (
                data.get("category", "unknown"),
                confidence,
                data.get("reasoning", ""),
            )
        except httpx.RequestError as e:
            logger.error(f"Ollama network error: {e}")
            raise LLMError(f"Ollama network error: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP error {e.response.status_code}: {e}")
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except json.JSONDecodeError as e:
            logger.error(f"Ollama returned invalid JSON: {e}")
            raise LLMError(f"Ollama invalid JSON: {e}") from e
        except LLMError:
            raise  # Re-raise LLMError as-is
        except Exception as e:
            logger.error(f"Ollama classification failed: {e}")
            raise LLMError(f"Ollama error: {e}") from e

    def check_email_intent(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> bool:
        """Check if full email matches an intent using LLM.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject}, {body_preview}.
            model: Model name to use.

        Returns:
            True if the intent matches, False otherwise.

        Raises:
            LLMError: If the LLM call fails (network, HTTP, or other error).
        """
        # Format the prompt with email content
        body_preview = (body or "")[:LLM_BODY_PREVIEW_LENGTH]
        formatted_prompt = prompt.format(
            from_addr=from_addr,
            subject=subject,
            body_preview=body_preview,
        )

        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": formatted_prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            answer: str = result.get("response", "").strip().lower()
            return answer == "yes"
        except httpx.RequestError as e:
            logger.error(f"Ollama email intent check network error: {e}")
            raise LLMError(f"Ollama network error: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(
                f"Ollama email intent check HTTP error {e.response.status_code}: {e}"
            )
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except Exception as e:
            logger.error(f"Ollama email intent check failed unexpectedly: {e}")
            raise LLMError(f"Ollama error: {e}") from e

    def categorize_email(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
        categories: list[str],
    ) -> str | None:
        """Categorize an email into one of the predefined categories.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject},
                {body_preview}, {categories}.
            model: Model name to use.
            categories: List of valid category names.

        Returns:
            The matched category name, or None if no valid category matched.

        Raises:
            LLMError: If the LLM call fails (network, HTTP, or other error).
        """
        # Format the prompt with email content and categories
        body_preview = (body or "")[:LLM_BODY_PREVIEW_LENGTH]
        categories_str = ", ".join(categories)
        formatted_prompt = prompt.format(
            from_addr=from_addr,
            subject=subject,
            body_preview=body_preview,
            categories=categories_str,
        )

        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": formatted_prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            answer: str = result.get("response", "").strip().lower()

            # Validate the response is one of the allowed categories
            for cat in categories:
                if cat.lower() == answer:
                    return cat  # Return original case
            # Check if answer contains a category (in case LLM adds extra text)
            # Sort by length descending to match longer categories first
            # (e.g., 'presales' before 'sales')
            for cat in sorted(categories, key=len, reverse=True):
                if cat.lower() in answer:
                    return cat
            logger.warning(
                f"LLM returned unknown category '{answer}', "
                f"expected one of {categories}"
            )
            return None  # Not an error - LLM responded but no category matched
        except httpx.RequestError as e:
            logger.error(f"Ollama categorize network error: {e}")
            raise LLMError(f"Ollama network error: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama categorize HTTP error {e.response.status_code}: {e}")
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except Exception as e:
            logger.error(f"Ollama categorize failed unexpectedly: {e}")
            raise LLMError(f"Ollama error: {e}") from e

    def extract_value(
        self,
        from_addr: str,
        subject: str,
        body: str | None,
        prompt: str,
        model: str,
    ) -> str | None:
        """Extract a single value from email using LLM.

        Used for standalone variable extraction (not classification).
        The prompt template can use {from_addr}, {subject}, {body_preview}.

        Args:
            from_addr: Email sender address.
            subject: Email subject.
            body: Email body (may be None).
            prompt: Prompt template with {from_addr}, {subject}, {body_preview}.
            model: Model name to use.

        Returns:
            Extracted value as string, or None if extraction failed or
            returned an obviously invalid response.

        Note:
            This method returns None on failure rather than raising LLMError,
            because extraction failure should cause the rule to fall through
            to the next rule (not fail the entire job).
        """
        body_preview = (body or "")[:LLM_BODY_PREVIEW_LENGTH]
        try:
            formatted = prompt.format(
                from_addr=from_addr,
                subject=subject,
                body_preview=body_preview,
            )
        except KeyError as e:
            logger.warning(f"Invalid prompt template for extraction: missing {e}")
            return None

        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": formatted,
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            value: str = result.get("response", "").strip()

            # Reject obviously invalid responses
            if not value or value.lower() in INVALID_LLM_EXTRACTION_VALUES:
                logger.debug(f"LLM extraction returned invalid or empty value: {value}")
                return None

            return value
        except httpx.RequestError as e:
            logger.warning(f"Ollama extraction network error: {e}")
            return None
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Ollama extraction HTTP error {e.response.status_code}: {e}"
            )
            return None
        except Exception as e:
            logger.warning(f"Ollama extraction failed: {e}")
            return None

    def classify_with_extraction(
        self,
        prompt: str,
        model: str,
        extract_fields: list[str] | None = None,
    ) -> tuple[str, float, str, dict[str, str]]:
        """Classify email and optionally extract fields in one LLM call.

        Used for LLM classification rules that also need to populate variables.
        The prompt should already include extraction instructions if extract_fields
        is provided.

        Args:
            prompt: Fully formatted classification prompt (including extraction
                instructions if needed).
            model: Model name to use.
            extract_fields: List of field names to extract (for validation).
                If None, no extraction is performed.

        Returns:
            Tuple of (category, confidence, reasoning, extracted_dict).
            On failure, returns ("unknown", 0.0, error_message, {}).

        Raises:
            LLMError: If the LLM call fails (network, HTTP, or invalid response).
        """
        try:
            response = self.client.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                },
            )
            response.raise_for_status()
            result = response.json()
            text = result.get("response", "{}")
            data = json.loads(text)

            # Validate response is a dict
            if not isinstance(data, dict):
                logger.error(f"Ollama returned non-dict JSON: {text}")
                raise LLMError(f"Ollama returned non-dict JSON: {text}")

            # Safe float conversion for confidence
            raw_confidence = data.get("confidence")
            try:
                confidence = (
                    float(raw_confidence) if raw_confidence is not None else 0.5
                )
            except (ValueError, TypeError):
                logger.warning(f"Invalid confidence value from LLM: {raw_confidence}")
                confidence = 0.5

            # Extract extracted fields if present
            extracted: dict[str, str] = {}
            if extract_fields:
                raw_extracted = data.get("extracted", {})
                if isinstance(raw_extracted, dict):
                    # Only include requested fields with string values
                    for field in extract_fields:
                        if field in raw_extracted:
                            val = raw_extracted[field]
                            if isinstance(val, str) and val.strip():
                                extracted[field] = val.strip()

            return (
                data.get("category", "unknown"),
                confidence,
                data.get("reasoning", ""),
                extracted,
            )
        except httpx.RequestError as e:
            logger.error(f"Ollama network error: {e}")
            raise LLMError(f"Ollama network error: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"Ollama HTTP error {e.response.status_code}: {e}")
            raise LLMError(f"Ollama HTTP {e.response.status_code}") from e
        except json.JSONDecodeError as e:
            logger.error(f"Ollama returned invalid JSON: {e}")
            raise LLMError(f"Ollama invalid JSON: {e}") from e
        except LLMError:
            raise  # Re-raise LLMError as-is
        except Exception as e:
            logger.error(f"Ollama classification with extraction failed: {e}")
            raise LLMError(f"Ollama error: {e}") from e


class GmailClient:
    """Client for Gmail API operations."""

    def __init__(self, token_path: str):
        self.token_path = token_path
        self._service: Any = None
        self._label_cache: dict[str, str] = {}  # name -> id
        self._label_cache_lock = threading.Lock()  # Protects cache operations

    def _get_service(self) -> Any:
        """Get authenticated Gmail API service."""
        if self._service is None:
            creds = Credentials.from_authorized_user_file(self.token_path)
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def _ensure_label_cache(self) -> None:
        """Populate label cache if empty. Thread-safe."""
        with self._label_cache_lock:
            if self._label_cache:
                return

            service = self._get_service()
            results = service.users().labels().list(userId="me").execute()
            for label in results.get("labels", []):
                self._label_cache[label["name"]] = label["id"]
            logger.debug(f"Cached {len(self._label_cache)} Gmail labels")

    def apply_label(self, gmail_id: str, label_name: str) -> bool:
        """Apply a label to an email.

        Creates the label if it doesn't exist.
        Returns True on success.
        """
        try:
            service = self._get_service()

            # Get or create label
            label_id = self._get_or_create_label(label_name)
            if not label_id:
                return False

            # Apply label
            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body={"addLabelIds": [label_id]},
            ).execute()

            logger.debug(f"Applied label {label_name} to {gmail_id}")
            return True
        except HttpError as e:
            logger.error(
                f"Gmail API error applying label {label_name} to {gmail_id}: "
                f"HTTP {e.resp.status} - {e.reason}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to apply label {label_name} to {gmail_id}: {e}")
            return False

    def _modify_message(
        self, gmail_id: str, body: dict[str, Any], action_name: str
    ) -> bool:
        """Helper to modify a message with a given body."""
        try:
            service = self._get_service()
            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body=body,
            ).execute()
            logger.debug(f"{action_name} {gmail_id}")
            return True
        except HttpError as e:
            logger.error(
                f"Gmail API error {action_name.lower()} {gmail_id}: "
                f"HTTP {e.resp.status} - {e.reason}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to {action_name.lower()} {gmail_id}: {e}")
            return False

    def archive(self, gmail_id: str) -> bool:
        """Archive an email (remove INBOX label)."""
        return self._modify_message(gmail_id, {"removeLabelIds": ["INBOX"]}, "Archived")

    def mark_read(self, gmail_id: str) -> bool:
        """Mark an email as read (remove UNREAD label)."""
        return self._modify_message(
            gmail_id, {"removeLabelIds": ["UNREAD"]}, "Marked read"
        )

    def star(self, gmail_id: str) -> bool:
        """Star an email."""
        return self._modify_message(gmail_id, {"addLabelIds": ["STARRED"]}, "Starred")

    def remove_label(self, gmail_id: str, label_name: str) -> bool:
        """Remove a label from an email. Returns True on success."""
        try:
            service = self._get_service()
            self._ensure_label_cache()

            # Get label ID from cache
            label_id = self._label_cache.get(label_name)
            if not label_id:
                logger.warning(f"Label not found: {label_name}")
                return False

            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body={"removeLabelIds": [label_id]},
            ).execute()

            logger.debug(f"Removed label {label_name} from {gmail_id}")
            return True
        except HttpError as e:
            logger.error(
                f"Gmail API error removing label {label_name} from {gmail_id}: "
                f"HTTP {e.resp.status} - {e.reason}"
            )
            return False
        except Exception as e:
            logger.error(f"Failed to remove label {label_name} from {gmail_id}: {e}")
            return False

    def _get_or_create_label(self, label_name: str) -> str | None:
        """Get label ID, creating the label if it doesn't exist.

        For nested labels (e.g., "Cortex/Aurelia/Memes"), creates parent
        labels first if they don't exist.

        Uses cached labels to avoid API calls on every message.
        """
        try:
            # Ensure cache is populated
            self._ensure_label_cache()

            # Normalize label name: remove leading/trailing/consecutive slashes
            label_name = "/".join(filter(None, label_name.split("/")))
            if not label_name:
                return None

            # Check cache with normalized name
            if label_name in self._label_cache:
                return self._label_cache[label_name]

            # For nested labels, ensure parent labels exist first
            # e.g., "Cortex/Family/Memes" needs "Cortex" and "Cortex/Family"
            if "/" in label_name:
                parts = label_name.split("/")
                for i in range(1, len(parts)):
                    parent_name = "/".join(parts[:i])
                    if (
                        parent_name not in self._label_cache
                        and self._create_single_label(parent_name) is None
                    ):
                        return None

            # Create the actual label
            return self._create_single_label(label_name)

        except Exception as e:
            logger.error(f"Failed to get/create label {label_name}: {e}")
            return None

    def _create_single_label(self, label_name: str) -> str | None:
        """Create a single label (no hierarchy handling). Thread-safe.

        Returns the label ID, or None on failure.
        """
        # Check cache first (may have been created as a parent)
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
            created = (
                service.users().labels().create(userId="me", body=label_body).execute()
            )
            logger.info(f"Created label: {label_name}")
            created_id: str = created["id"]

            # Add to cache (thread-safe)
            with self._label_cache_lock:
                self._label_cache[label_name] = created_id
            return created_id
        except HttpError as e:
            # Handle race condition: another worker created the label first
            if e.resp.status == 409:
                logger.info(
                    f"Label '{label_name}' already exists (race condition), "
                    "refreshing cache"
                )
                # Thread-safe cache refresh
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


class TriageWorker:
    def __init__(
        self,
        config: Config,
        rules: RulesConfig,
        ollama: OllamaClient,
        gmail: GmailClient,
        worker_id: str | None = None,
        config_version: int | None = None,
    ):
        self.config = config
        self.rules = rules
        self.ollama = ollama
        self.gmail = gmail
        self.running = True
        self._pool: SimpleConnectionPool | None = None
        self.worker_id = worker_id or f"triage-{os.getpid()}"
        self.last_signal_poll = time.time()
        self.last_cleanup = time.time()  # Track when we last cleaned up stale workers
        self.last_priority_convergence = 0.0  # Track last bulk priority update
        self.config_version = config_version  # Track which DB config version is loaded
        self.jobs_processed = 0  # Track cumulative job count for status updates

    def stop(self) -> None:
        """Signal the worker to stop."""
        self.running = False

    def reload_rules(self) -> bool:
        """Reload rules from database.

        Returns True if reload succeeded, False otherwise.
        The old rules remain active if reload fails.
        """
        try:
            # Load from database (active version)
            with self._get_conn() as conn:
                new_rules = load_config_from_db(conn)

                # Get the version number from database
                with conn.cursor() as cursor:
                    cursor.execute(_GET_ACTIVE_CONFIG_VERSION_SQL)
                    row = cursor.fetchone()
                    new_config_version = row[0] if row else None

            if new_config_version is None:
                logger.error(
                    "Config reload failed: No active config version found in database."
                )
                return False

            logger.info(f"Loaded config from database (version {new_config_version})")

            # Validate the loaded config
            errors = validate_rules(new_rules)
            if errors:
                for error in errors:
                    logger.error(f"Rules validation error: {error}")
                logger.error("Rules reload failed - keeping existing rules")
                return False

            # Success - update worker state
            old_version = self.config_version
            self.rules = new_rules
            self.config_version = new_config_version

            # Update worker status in database
            try:
                with self._get_conn() as conn:
                    update_config_version(conn, self.worker_id, new_config_version)
            except Exception as e:
                logger.warning(f"Failed to update config version in worker_status: {e}")

            logger.info(
                f"Rules reloaded successfully: "
                f"version {old_version} → {new_config_version}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to reload rules: {e}", exc_info=True)
            return False

    def reload_mappings(self) -> bool:
        """Reload email mappings from database (without changing rules).

        This is lighter than reload_rules() - only mappings change, not rules/chains.

        Returns True if reload succeeded, False otherwise.
        """
        try:
            # Load fresh config from database
            with self._get_conn() as conn:
                new_config = load_config_from_db(conn)

            # Create new RulesConfig with updated mappings (atomic update)
            # Prevents race conditions from partial updates
            new_rules = self.rules.model_copy(
                update={
                    "priority_email_mappings": new_config.priority_email_mappings,
                    "fallback_email_mappings": new_config.fallback_email_mappings,
                }
            )
            self.rules = new_rules

            logger.info(
                f"Mappings reloaded successfully: "
                f"priority={len(self.rules.priority_email_mappings)}, "
                f"fallback={len(self.rules.fallback_email_mappings)}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to reload mappings: {e}", exc_info=True)
            return False

    def _mark_convergence_failed(self, gmail_id: str) -> None:
        """Mark a convergence email as processed (update to current version).

        This prevents infinite retry loops for emails that fail convergence.
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE classifications
                        SET config_version_id = (
                            SELECT version FROM triage_config_versions
                            WHERE is_active = TRUE LIMIT 1
                        )
                        WHERE gmail_id = %s
                        """,
                        (gmail_id,),
                    )
                conn.commit()
        except Exception as e:
            logger.error(
                f"Failed to mark convergence failed for {gmail_id}: {e}",
                exc_info=True,
            )

    def _get_next_convergence_email(self) -> dict[str, Any] | None:
        """Get next email with outdated config version for idle-time processing.

        Optimization: Priority sender emails are bulk-updated first (no processing)
        since priority mappings always win. Then picks oldest non-priority email.

        Returns:
            Dict with gmail_id, or None if nothing to converge
        """
        if not self.config.enable_convergence:
            return None

        try:
            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    # Get current active config version
                    cursor.execute(
                        """
                        SELECT version FROM triage_config_versions
                        WHERE is_active = TRUE LIMIT 1
                        """
                    )
                    row = cursor.fetchone()
                    if not row:
                        return None

                    current_version = row[0]

                    # Step 1: Bulk UPDATE priority sender emails (no re-processing)
                    # These always match priority mapping regardless of rules
                    # Only run this periodically to avoid DB load
                    now = time.time()
                    if (
                        now - self.last_priority_convergence
                        >= self.config.priority_convergence_interval
                    ):
                        cursor.execute(
                            """
                            UPDATE classifications c
                            SET config_version_id = %s
                            WHERE c.config_version_id < %s
                              AND EXISTS (
                                SELECT 1
                                FROM emails_parsed ep
                                JOIN triage_email_mappings tem
                                  ON LOWER(ep.from_addr) = LOWER(tem.email_address)
                                WHERE ep.gmail_id = c.gmail_id
                                  AND tem.mapping_type = 'priority'
                                  AND tem.deleted_at IS NULL
                              )
                            """,
                            (current_version, current_version),
                        )
                        bulk_updated = cursor.rowcount

                        if bulk_updated > 0:
                            logger.info(
                                f"Convergence: bulk updated {bulk_updated} priority "
                                f"sender emails to version {current_version}"
                            )
                        self.last_priority_convergence = now

                    # Step 2: Pick oldest non-priority email with old config version
                    # Process it directly (not via queue) - true idle-time convergence
                    cursor.execute(
                        """
                        SELECT er.id as email_id, er.gmail_id
                        FROM classifications c
                        JOIN emails_raw er ON er.gmail_id = c.gmail_id
                        WHERE c.config_version_id < %s
                          AND NOT EXISTS (
                            SELECT 1
                            FROM emails_parsed ep
                            JOIN triage_email_mappings tem
                              ON LOWER(ep.from_addr) = LOWER(tem.email_address)
                            WHERE ep.gmail_id = c.gmail_id
                              AND tem.mapping_type = 'priority'
                              AND tem.deleted_at IS NULL
                          )
                        ORDER BY c.classified_at ASC
                        LIMIT 1
                        """,
                        (current_version,),
                    )
                    row = cursor.fetchone()

                conn.commit()

                if row:
                    return {"gmail_id": row[1]}
                return None

        except Exception as e:
            logger.error(f"Failed to get convergence email: {e}", exc_info=True)
            return None

    def _check_and_process_signals(self) -> None:
        """Poll for and process worker signals (config reload, shutdown, etc.).

        Also updates worker heartbeat and job count in database.
        """
        # Only poll at configured interval
        now = time.time()
        if now - self.last_signal_poll < self.config.signal_poll_interval:
            return

        self.last_signal_poll = now

        try:
            with self._get_conn() as conn:
                # Update heartbeat and job count
                update_heartbeat(conn, self.worker_id, self.jobs_processed)

                # Poll for control signals
                signals = poll_signals(conn, "triage", self.worker_id)

            for signal in signals:
                logger.info(
                    f"Processing signal: {signal.signal_type} (id={signal.signal_id})"
                )

                if signal.signal_type == "config_reload":
                    success = self.reload_rules()
                    if success:
                        logger.info("Config reloaded successfully via signal")
                    else:
                        logger.error("Config reload failed")

                elif signal.signal_type == "mappings_reload":
                    # reload_mappings() already logs success/failure details
                    self.reload_mappings()

                elif signal.signal_type == "shutdown":
                    logger.info("Received shutdown signal")
                    self.stop()

                elif signal.signal_type == "pause":
                    logger.info("Pause signal not yet implemented")

                elif signal.signal_type == "resume":
                    logger.info("Resume signal not yet implemented")

                elif signal.signal_type == "log_level":
                    new_level = signal.payload.get("level", "INFO")
                    logger.info(f"Changing log level to {new_level}")
                    logging.getLogger().setLevel(getattr(logging, new_level))

        except Exception as e:
            logger.error(f"Error processing signals: {e}", exc_info=True)

    def close(self) -> None:
        """Close resources like the database connection pool."""
        if self._pool:
            self._pool.closeall()
            self._pool = None
            logger.info("Database connection pool closed")

    def _should_run_maintenance(self) -> bool:
        """Check if lifecycle maintenance should run (24h since last run)."""
        try:
            with self._get_conn() as conn:
                return check_maintenance_needed(conn, LifecycleConfig())
        except Exception as e:
            logger.error(
                f"Error checking maintenance status, maintenance will be skipped: {e}",
                exc_info=True,
            )
            return False

    def _run_lifecycle_maintenance(self) -> None:
        """Run pattern lifecycle maintenance (decay overdue patterns)."""
        try:
            with self._get_conn() as conn:
                report = run_lifecycle_maintenance(
                    conn, LifecycleConfig(), datetime.now(UTC).date()
                )
                logger.info(
                    f"Lifecycle maintenance complete: "
                    f"checked={report.patterns_checked}, "
                    f"decayed={report.patterns_decayed}, "
                    f"dormant={report.patterns_dormant}, "
                    f"ended={report.patterns_ended}"
                )
                if report.errors:
                    for error in report.errors:
                        logger.error(f"Maintenance error: {error}")
        except Exception as e:
            logger.error(f"Lifecycle maintenance failed: {e}", exc_info=True)

    def _cleanup_stale_workers(self) -> None:
        """Clean up stale worker entries (>5 minutes without heartbeat).

        Runs every 5 minutes to prevent accumulation of dead worker entries.
        """
        # Only run cleanup every 5 minutes
        now = time.time()
        if now - self.last_cleanup < 300:  # 300 seconds = 5 minutes
            return

        self.last_cleanup = now

        try:
            with self._get_conn() as conn:
                removed = cleanup_stale_workers(conn, stale_threshold_minutes=5)
                if removed > 0:
                    logger.info(f"Cleaned up {removed} stale worker(s)")
        except Exception as e:
            logger.error(f"Stale worker cleanup failed: {e}", exc_info=True)

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
        """Get a connection from the pool, automatically returned when done."""
        conn = self._get_pool().getconn()
        try:
            yield conn
        finally:
            self._get_pool().putconn(conn)

    def _ensure_schema(self) -> None:
        """Ensure classifications table exists."""
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    CREATE TABLE IF NOT EXISTS classifications (
                        id BIGSERIAL PRIMARY KEY,
                        email_id BIGINT NOT NULL,
                        gmail_id TEXT NOT NULL,

                        -- Rule matching
                        matched_chain TEXT,
                        matched_rule_index INT,
                        rules_version TEXT,

                        -- LLM (if used)
                        llm_category TEXT,
                        llm_confidence REAL,
                        llm_reasoning TEXT,

                        -- Action taken
                        action_taken JSONB,
                        action_executed BOOLEAN DEFAULT FALSE,

                        classified_at TIMESTAMPTZ DEFAULT NOW()
                    );

                    CREATE INDEX IF NOT EXISTS idx_classifications_email
                    ON classifications(email_id);

                    CREATE INDEX IF NOT EXISTS idx_classifications_gmail
                    ON classifications(gmail_id);

                    CREATE INDEX IF NOT EXISTS idx_classifications_chain
                    ON classifications(matched_chain);

                    CREATE INDEX IF NOT EXISTS idx_classifications_date
                    ON classifications(classified_at DESC);

                    -- Unique constraint for idempotent classification
                    -- Prevents duplicate classifications for same email + rules version
                    -- (allows re-evaluation with different rules versions)
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_classifications_dedup
                    ON classifications(gmail_id, rules_version);

                    -- ==============================================
                    -- Pattern Detection Tables (subscriptions, etc.)
                    -- ==============================================

                    -- Detected recurring email patterns
                    -- Identity: (sender, merchant) - amounts tracked per-occurrence
                    CREATE TABLE IF NOT EXISTS email_patterns (
                        id BIGSERIAL PRIMARY KEY,

                        -- Pattern identity
                        sender TEXT NOT NULL,
                        merchant TEXT NOT NULL,
                        subject_template TEXT,
                        body_signature TEXT,

                        -- Temporal pattern
                        interval_type TEXT NOT NULL DEFAULT 'irregular',
                        interval_params JSONB,
                        next_expected DATE,

                        -- Confidence and lifecycle
                        confidence REAL NOT NULL DEFAULT 0.0,
                        occurrence_count INT NOT NULL DEFAULT 0,
                        first_seen TIMESTAMPTZ NOT NULL,
                        last_seen TIMESTAMPTZ NOT NULL,
                        status TEXT NOT NULL DEFAULT 'emerging',
                        consecutive_misses INT NOT NULL DEFAULT 0,

                        -- Cached amount stats (computed from pattern_emails)
                        typical_amount NUMERIC(10,2),
                        typical_currency TEXT,
                        amount_varies BOOLEAN DEFAULT FALSE,

                        -- For labeling
                        pattern_label TEXT,

                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW(),

                        CONSTRAINT unique_pattern UNIQUE (sender, merchant),
                        CONSTRAINT valid_pattern_status CHECK (
                            status IN ('emerging', 'active', 'dormant', 'ended')
                        ),
                        CONSTRAINT valid_interval_type CHECK (
                            interval_type IN (
                                'monthly', 'weekly', 'biweekly', 'quarterly',
                                'yearly', 'day_of_week', 'irregular'
                            )
                        )
                    );

                    CREATE INDEX IF NOT EXISTS idx_patterns_sender
                        ON email_patterns(sender);
                    CREATE INDEX IF NOT EXISTS idx_patterns_status
                        ON email_patterns(status)
                        WHERE status IN ('emerging', 'active', 'dormant');
                    CREATE INDEX IF NOT EXISTS idx_patterns_next_expected
                        ON email_patterns(next_expected)
                        WHERE next_expected IS NOT NULL
                        AND status IN ('emerging', 'active', 'dormant');

                    -- Junction table: which emails belong to which pattern
                    CREATE TABLE IF NOT EXISTS pattern_emails (
                        id BIGSERIAL PRIMARY KEY,
                        pattern_id BIGINT NOT NULL
                            REFERENCES email_patterns(id) ON DELETE CASCADE,
                        gmail_id TEXT NOT NULL,

                        -- Per-occurrence data (may vary)
                        amount NUMERIC(10,2),
                        currency TEXT,

                        matched_at TIMESTAMPTZ DEFAULT NOW(),

                        CONSTRAINT unique_pattern_email UNIQUE (pattern_id, gmail_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_pattern_emails_gmail
                        ON pattern_emails(gmail_id);
                    CREATE INDEX IF NOT EXISTS idx_pattern_emails_pattern
                        ON pattern_emails(pattern_id);

                    -- ==============================================
                    -- System State (key-value for lifecycle, etc.)
                    -- ==============================================
                    CREATE TABLE IF NOT EXISTS system_state (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """
            )
            conn.commit()
        logger.info("triage schema ensured (classifications + patterns + system_state)")

    def _claim_jobs(self, batch_size: int) -> list[dict[str, Any]]:
        """Claim a batch of pending triage jobs.

        Also resets stale jobs that have been processing for too long
        (visibility timeout), handling crashed/killed workers.

        Jobs that have exceeded MAX_ATTEMPTS are left in 'failed' status
        and not retried (dead letter).
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                # Reset stale jobs (only if under max attempts) and claim new ones
                cur.execute(
                    """
                    WITH reset_stale AS (
                        UPDATE queue
                        SET status = 'pending', claimed_at = NULL
                        WHERE queue_name = 'triage'
                          AND status = 'processing'
                          AND claimed_at < NOW() - (INTERVAL '1 minute' * %s)
                          AND attempts < %s
                    ),
                    expire_dead_letter AS (
                        -- Mark stale jobs that exceeded max attempts as failed
                        UPDATE queue
                        SET status = 'failed',
                            last_error = COALESCE(last_error, '') ||
                                ' [max attempts exceeded]'
                        WHERE queue_name = 'triage'
                          AND status = 'processing'
                          AND claimed_at < NOW() - (INTERVAL '1 minute' * %s)
                          AND attempts >= %s
                    ),
                    expire_pending_exhausted AS (
                        -- Immediately fail pending jobs that hit max attempts
                        UPDATE queue
                        SET status = 'failed',
                            last_error = COALESCE(last_error, '') ||
                                ' [max attempts exceeded]'
                        WHERE queue_name = 'triage'
                          AND status = 'pending'
                          AND attempts >= %s
                    ),
                    claimable AS (
                        SELECT id FROM queue
                        WHERE queue_name = 'triage'
                          AND status = 'pending'
                          AND attempts < %s
                        ORDER BY priority DESC, created_at
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE queue
                    SET status = 'processing', claimed_at = NOW()
                    WHERE id IN (SELECT id FROM claimable)
                    RETURNING id, payload, priority
                    """,
                    (
                        VISIBILITY_TIMEOUT_MINUTES,
                        MAX_ATTEMPTS,
                        VISIBILITY_TIMEOUT_MINUTES,
                        MAX_ATTEMPTS,
                        MAX_ATTEMPTS,  # expire_pending_exhausted
                        MAX_ATTEMPTS,  # claimable
                        batch_size,
                    ),
                )
                jobs = [
                    {"id": row[0], "payload": row[1], "priority": row[2]}
                    for row in cur.fetchall()
                ]
                conn.commit()
            return jobs

    def _detect_or_create_pattern(
        self,
        conn: psycopg2.extensions.connection,
        gmail_id: str,
        from_addr: str,
    ) -> EmailPattern | None:
        """Detect patterns from sender's emails and create if threshold met.

        Analyzes the sender's email history to find similarity groups.
        If the current email belongs to a group with enough occurrences,
        detects temporal patterns and creates/updates the pattern.

        Args:
            conn: Database connection
            gmail_id: Gmail ID of current email
            from_addr: Sender email address

        Returns:
            Created or updated EmailPattern, or None if no pattern detected
        """
        try:
            # Analyze sender's emails for similarity groups
            groups = analyze_sender(conn, from_addr, limit=100)

            if not groups:
                return None

            # Find which group contains this email
            target_group = next(
                (group for group in groups if gmail_id in group.gmail_ids), None
            )

            if not target_group:
                # Email not in any group (shouldn't happen, but handle gracefully)
                return None

            # Need at least PATTERN_ACTIVATION_COUNT emails to create/activate pattern
            if len(target_group.gmail_ids) < PATTERN_ACTIVATION_COUNT:
                return None

            # Determine merchant name (use template_name as fallback)
            merchant = target_group.merchant or target_group.template_name

            # Check if pattern already exists for this sender+merchant
            existing = get_pattern_by_sender_merchant(conn, from_addr, merchant)
            if existing and existing.id is not None:
                # Pattern exists - link all emails in the group to it
                # (add_email_to_pattern is idempotent, safe for already-linked emails)
                for i, gid in enumerate(target_group.gmail_ids):
                    # Get amount if available (lists are parallel with gmail_ids)
                    amount = None
                    currency = None
                    amt = target_group.amounts[i]
                    if amt is not None:
                        amount = amt.amount
                        currency = amt.currency

                    # Get email date if available
                    email_date = None
                    date_str = target_group.dates[i]
                    if date_str is not None:
                        try:
                            parsed_date = date.fromisoformat(date_str)
                            email_date = datetime.combine(
                                parsed_date, datetime.min.time()
                            ).replace(tzinfo=UTC)
                        except (ValueError, TypeError):
                            pass

                    add_email_to_pattern(
                        conn, existing.id, gid, amount, currency, email_date
                    )

                # Re-fetch to get updated stats (occurrence_count, last_seen, etc.)
                return get_pattern_by_sender_merchant(conn, from_addr, merchant)

            # No existing pattern - detect temporal pattern and create
            # Parse dates once and index by position for reuse (skip None values)
            parsed_dates: dict[int, date] = {}
            for i, date_str in enumerate(target_group.dates):
                if date_str is None:
                    continue
                try:
                    parsed_dates[i] = date.fromisoformat(date_str)
                except (ValueError, TypeError):
                    continue

            if len(parsed_dates) < 2:
                return None

            # Detect temporal pattern
            dates_list = list(parsed_dates.values())
            temporal = detect_pattern(dates_list)
            if not temporal:
                return None

            # Pattern Quality Filter
            # Only create patterns that provide value for prediction.
            # Irregular patterns with unknown merchants are noise:
            # - Can't predict when next email will arrive (irregular timing)
            # - Can't identify the sender's purpose (unknown merchant)
            # - Can't detect anomalies (no baseline to compare against)
            merchant_known = merchant and merchant != "unknown"
            has_high_confidence = (
                temporal.confidence >= PATTERN_QUALITY_CONFIDENCE_THRESHOLD
            )

            if not merchant_known and not has_high_confidence:
                logger.debug(
                    f"Skipping pattern creation for {from_addr}: "
                    f"merchant='{merchant}', interval={temporal.interval_type.value}, "
                    f"confidence={temporal.confidence:.2f} "
                    f"(pattern would provide no value)"
                )
                return None

            # Create the pattern
            first_seen = datetime.combine(min(dates_list), datetime.min.time()).replace(
                tzinfo=UTC
            )

            pattern = create_pattern(
                conn,
                sender=from_addr,
                merchant=merchant,
                first_seen=first_seen,
                subject_template=target_group.template_pattern,
                interval_type=temporal.interval_type,
                interval_params=temporal.interval_params,
            )

            if not pattern.id:
                return None

            # Link all emails in the group to this pattern
            for i, gid in enumerate(target_group.gmail_ids):
                amount = None
                currency = None
                amt = target_group.amounts[i]
                if amt is not None:
                    amount = amt.amount
                    currency = amt.currency

                # Reuse already-parsed date
                email_date = None
                if i in parsed_dates:
                    email_date = datetime.combine(
                        parsed_dates[i], datetime.min.time()
                    ).replace(tzinfo=UTC)

                add_email_to_pattern(
                    conn, pattern.id, gid, amount, currency, email_date
                )

            logger.info(
                f"Created pattern for {from_addr}/{merchant}: "
                f"{temporal.interval_type.value}, {len(target_group.gmail_ids)} emails"
            )

            # Refresh pattern to get updated stats
            return get_pattern_by_sender_merchant(conn, from_addr, merchant)

        except Exception:
            logger.error(f"Pattern detection failed for {gmail_id}", exc_info=True)
            return None

    def _get_email_data(self, gmail_id: str) -> Email | None:
        """Fetch email data from emails_parsed and headers from emails_raw."""
        with self._get_conn() as conn, conn.cursor() as cur:
            # Get parsed email, headers, labels, size, date, and attachments
            cur.execute(
                """
                    SELECT
                        ep.id, ep.gmail_id, ep.from_addr, ep.to_addrs,
                        ep.cc_addrs, ep.bcc_addrs,
                        ep.subject, ep.body_text,
                        EXISTS(
                            SELECT 1 FROM attachments a WHERE a.gmail_id = ep.gmail_id
                        ),
                        er.headers,
                        er.label_ids,
                        er.size_estimate,
                        er.internal_date,
                        (SELECT array_agg(filename) FROM attachments a
                         WHERE a.gmail_id = ep.gmail_id),
                        (SELECT array_agg(mime_type) FROM attachments a
                         WHERE a.gmail_id = ep.gmail_id)
                    FROM emails_parsed ep
                    LEFT JOIN emails_raw er ON ep.gmail_id = er.gmail_id
                    WHERE ep.gmail_id = %s
                """,
                (gmail_id,),
            )
            row = cur.fetchone()
            if row:
                # Address arrays from the DB
                to_addrs = row[3] if row[3] else []
                cc_addrs = row[4] if row[4] else []
                bcc_addrs = row[5] if row[5] else []
                has_attachment: bool = row[8] if row[8] is not None else False
                label_ids = row[10] if row[10] else []
                size_estimate = row[11]  # May be None
                internal_date = row[12]  # May be None (ms since epoch)
                attachment_filenames = row[13] if row[13] else []
                attachment_mime_types = row[14] if row[14] else []

                # Convert headers from JSONB array to dict (lowercase keys)
                # Format: [{"name": "List-ID", "value": "<foo.bar>"}, ...]
                # Duplicate headers (rare, mainly Received) are concatenated with comma
                headers_dict: dict[str, str] = {}
                raw_headers = row[9]
                if raw_headers:
                    for h in raw_headers:
                        name = h.get("name", "")
                        value = h.get("value", "")
                        if name:
                            lower_name = name.lower()
                            if lower_name in headers_dict:
                                # Concatenate values for duplicate headers
                                headers_dict[lower_name] += f", {value}"
                            else:
                                headers_dict[lower_name] = value

                # Look up pattern info if email is linked to a pattern
                # If not linked, try to detect and create a pattern
                pattern_info: PatternInfo | None = None
                from_addr = row[2] or ""
                pattern = get_pattern_for_email(conn, gmail_id)

                # If no existing pattern, try to detect one from sender's emails
                # Pattern creation is part of the job transaction - if the job
                # fails, the pattern will be rolled back and re-created on retry
                if not pattern and from_addr:
                    pattern = self._detect_or_create_pattern(conn, gmail_id, from_addr)

                if pattern and pattern.id is not None:
                    pattern_info = PatternInfo(
                        pattern_id=pattern.id,
                        sender=pattern.sender,
                        merchant=pattern.merchant,
                        interval_type=pattern.interval_type.value,
                        confidence=pattern.confidence,
                        occurrence_count=pattern.occurrence_count,
                        status=pattern.status.value,
                    )

                return Email(
                    id=row[0],
                    gmail_id=row[1],
                    from_addr=from_addr,
                    to_addrs=to_addrs,
                    cc_addrs=cc_addrs,
                    bcc_addrs=bcc_addrs,
                    subject=row[6] or "",
                    body=row[7],
                    has_attachment=has_attachment,
                    headers=headers_dict,
                    label_ids=label_ids,
                    size_estimate=size_estimate,
                    internal_date=internal_date,
                    attachment_filenames=attachment_filenames,
                    attachment_mime_types=attachment_mime_types,
                    pattern_info=pattern_info,
                )
        return None

    def _get_body_from_api(self, gmail_id: str) -> str | None:
        """Fetch body from DuckDB API if not in parsed table.

        Uses the /mail_text endpoint which returns decoded plain text,
        rendering HTML to text if no plain text part exists.
        """
        try:
            response = httpx.get(
                f"{self.config.duckdb_api_url}/mail_text",
                params={"gmail_id": gmail_id},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            text: str | None = data.get("text")
            return text
        except httpx.RequestError as e:
            logger.warning(f"DuckDB API network error for {gmail_id}: {e}")
        except httpx.HTTPStatusError as e:
            logger.warning(
                f"DuckDB API error for {gmail_id}: HTTP {e.response.status_code}"
            )
        except Exception as e:
            logger.warning(f"Failed to fetch body from API for {gmail_id}: {e}")
        return None

    def _enqueue_labeling(self, gmail_id: str, priority: int = 0) -> bool:
        """Enqueue labeling job for batch processing.

        The labeling worker queries the classifications table for the target
        state (labels, archive, etc.) - we only need to pass the gmail_id.
        """
        payload = {"gmail_id": gmail_id}

        try:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO queue (queue_name, payload, status, priority)
                    VALUES ('labeling', %s, 'pending', %s)
                    """,
                    (Json(payload), priority),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to enqueue labeling for {gmail_id}: {e}")
            return False

    def _execute_action(self, gmail_id: str, priority: int = 0) -> bool:
        """Execute an action on an email by enqueueing for batch processing."""
        return self._enqueue_labeling(gmail_id, priority)

    def _store_classification(
        self,
        email: Email,
        result: EvaluationResult,
        action_executed: bool,
    ) -> None:
        """Store classification result (idempotent - updates if exists)."""
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO classifications (
                        email_id, gmail_id, matched_chain, matched_rule_index,
                        rules_version, llm_category, llm_confidence, llm_reasoning,
                        action_taken, action_executed
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (gmail_id, rules_version) DO UPDATE SET
                        matched_chain = EXCLUDED.matched_chain,
                        matched_rule_index = EXCLUDED.matched_rule_index,
                        llm_category = EXCLUDED.llm_category,
                        llm_confidence = EXCLUDED.llm_confidence,
                        llm_reasoning = EXCLUDED.llm_reasoning,
                        action_taken = EXCLUDED.action_taken,
                        action_executed = EXCLUDED.action_executed,
                        classified_at = NOW()
                """,
                (
                    email.id,
                    email.gmail_id,
                    result.chain,
                    result.rule_index,
                    f"v{self.rules.version}",
                    result.llm_category,
                    result.llm_confidence,
                    result.llm_reasoning,
                    Json(result.action.model_dump() if result.action else None),
                    action_executed,
                ),
            )
            conn.commit()

    def _complete_job(self, job_id: int) -> None:
        """Mark job as completed."""
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE queue
                    SET status = 'completed', completed_at = NOW()
                    WHERE id = %s
                """,
                (job_id,),
            )
            conn.commit()

    def _fail_job(self, job_id: int, error: str) -> None:
        """Mark job as failed."""
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                    UPDATE queue
                    SET status = 'failed',
                        last_error = %s,
                        attempts = attempts + 1
                    WHERE id = %s
                """,
                (error[:1000], job_id),
            )
            conn.commit()

    def process_job(self, job: dict[str, Any]) -> bool:
        """Process a single triage job. Returns True on success."""
        gmail_id = job["payload"]["gmail_id"]
        start_time = time.time()

        try:
            # Get email data
            email = self._get_email_data(gmail_id)
            if not email:
                logger.warning(f"Parsed email not found: {gmail_id}")
                self._fail_job(job["id"], "Parsed email not found")
                ERRORS.labels(
                    service="triage-worker", error_type="email_not_found"
                ).inc()
                return False

            # Fetch body from API if not in parsed table
            if not email.body:
                email.body = self._get_body_from_api(gmail_id)

            # Evaluate rules (may raise LLMError if Ollama is down)
            result = evaluate(self.rules, email, self.ollama)

            # Execute action if matched
            # Propagate priority so backfill jobs stay deprioritized through pipeline
            action_executed = False
            if result.matched and result.action:
                action_executed = self._execute_action(
                    gmail_id, priority=job.get("priority", 0)
                )
                if action_executed:
                    logger.info(
                        f"Processed {gmail_id}: chain={result.chain}, "
                        f"rule={result.rule_index}, action={result.action}"
                    )
                    # Enqueue labeling job
                    QUEUE_PROCESSED.labels(queue="labeling", status="enqueued").inc()
                else:
                    logger.warning(f"Action failed for {gmail_id}: {result.action}")
            else:
                if not result.matched:
                    logger.info(f"No match for {gmail_id}")
                else:
                    logger.info(f"Matched but no action for {gmail_id}")
                # Log trace at info level for debugging
                if result.trace:
                    trace_str = "\n".join(result.trace)
                    logger.info(f"Decision trace for {gmail_id}:\n{trace_str}")

            # Store classification
            self._store_classification(email, result, action_executed)

            # Track classification method for metrics
            method = "llm" if result.llm_category else "rule"
            EMAILS_CLASSIFIED.labels(method=method).inc()

            # Mark job complete
            self._complete_job(job["id"])
            return True

        except LLMError as e:
            # LLM failures should fail the job and retry (not silently skip)
            logger.error(f"LLM error processing {gmail_id}: {e}")
            self._fail_job(job["id"], f"LLM error: {e}")
            ERRORS.labels(service="triage-worker", error_type="llm_error").inc()
            return False
        except Exception as e:
            logger.error(f"Failed to process {gmail_id}: {e}")
            self._fail_job(job["id"], str(e))
            ERRORS.labels(service="triage-worker", error_type="job_failed").inc()
            return False
        finally:
            PROCESSING_DURATION.labels(queue="triage", operation="process").observe(
                time.time() - start_time
            )

    def _process_batch(self) -> tuple[int, int]:
        """Process a single batch of jobs. Returns (processed, failed)."""
        # Check if lifecycle maintenance is needed (lazy check - runs daily)
        if self._should_run_maintenance():
            self._run_lifecycle_maintenance()

        jobs = self._claim_jobs(self.config.batch_size)

        if not jobs:
            return 0, 0

        logger.info(f"Processing batch of {len(jobs)} jobs")

        processed = 0
        failed = 0
        for job in jobs:
            if self.process_job(job):
                processed += 1
                QUEUE_PROCESSED.labels(queue="triage", status="success").inc()
            else:
                failed += 1
                QUEUE_PROCESSED.labels(queue="triage", status="error").inc()

        return processed, failed

    def run(self) -> None:
        """Main processing loop."""
        self._ensure_schema()

        logger.info(f"Triage worker started (worker_id={self.worker_id})")

        # Register worker in database
        try:
            with self._get_conn() as conn:
                register_worker(conn, self.worker_id, "triage", self.config_version)
        except Exception as e:
            logger.warning(f"Failed to register worker status: {e}")

        total_processed = 0
        total_failed = 0

        try:
            while self.running:
                # Check for control signals (config reload, shutdown, etc.)
                # Also updates heartbeat
                self._check_and_process_signals()

                # Cleanup stale worker entries periodically (every 5 minutes)
                self._cleanup_stale_workers()

                processed, failed = self._process_batch()

                if processed == 0 and failed == 0:
                    # Queue is idle - try convergence (idle-time processing)
                    convergence_job = self._get_next_convergence_email()
                    if convergence_job:
                        gmail_id = convergence_job["gmail_id"]
                        start_time = time.time()
                        processed_convergence = False
                        try:
                            email = self._get_email_data(gmail_id)
                            if not email:
                                logger.warning(
                                    f"Convergence: Parsed email not found: {gmail_id}"
                                )
                                ERRORS.labels(
                                    service="triage-worker",
                                    error_type="email_not_found",
                                ).inc()
                            else:
                                if not email.body:
                                    email.body = self._get_body_from_api(gmail_id)

                                result = evaluate(self.rules, email, self.ollama)

                                action_executed = False
                                if result.matched and result.action:
                                    action_executed = self._execute_action(
                                        gmail_id, priority=0
                                    )
                                    if action_executed:
                                        logger.info(
                                            f"Convergence processed {gmail_id}: "
                                            f"chain={result.chain}, "
                                            f"rule={result.rule_index}, "
                                            f"action={result.action}"
                                        )
                                        QUEUE_PROCESSED.labels(
                                            queue="labeling", status="enqueued"
                                        ).inc()
                                    else:
                                        logger.warning(
                                            f"Convergence action failed for "
                                            f"{gmail_id}: {result.action}"
                                        )
                                else:
                                    if not result.matched:
                                        logger.info(
                                            f"Convergence: No match for {gmail_id}"
                                        )
                                    else:
                                        logger.info(
                                            f"Convergence: Matched but no action "
                                            f"for {gmail_id}"
                                        )

                                self._store_classification(
                                    email, result, action_executed
                                )
                                method = "llm" if result.llm_category else "rule"
                                EMAILS_CLASSIFIED.labels(method=method).inc()
                                processed_convergence = True

                        except LLMError as e:
                            logger.error(
                                f"Convergence LLM error processing {gmail_id}: {e}"
                            )
                            ERRORS.labels(
                                service="triage-worker", error_type="llm_error"
                            ).inc()
                            # Mark as processed to prevent infinite retry
                            self._mark_convergence_failed(gmail_id)
                        except Exception as e:
                            logger.error(
                                f"Convergence failed to process {gmail_id}: {e}",
                                exc_info=True,
                            )
                            ERRORS.labels(
                                service="triage-worker", error_type="job_failed"
                            ).inc()
                            # Mark as processed to prevent infinite retry
                            self._mark_convergence_failed(gmail_id)
                        finally:
                            PROCESSING_DURATION.labels(
                                queue="convergence", operation="process"
                            ).observe(time.time() - start_time)

                        if processed_convergence:
                            total_processed += 1
                            self.jobs_processed += 1
                            logger.debug("Processed 1 convergence email")
                            QUEUE_PROCESSED.labels(
                                queue="convergence", status="success"
                            ).inc()
                        else:
                            total_failed += 1
                            logger.error("Convergence processing failed")
                            QUEUE_PROCESSED.labels(
                                queue="convergence", status="error"
                            ).inc()
                        continue  # Check for more work immediately

                    # No convergence work either, sleep
                    logger.debug(f"No jobs, sleeping {self.config.poll_interval}s")
                    time.sleep(self.config.poll_interval)
                    continue

                total_processed += processed
                total_failed += failed
                self.jobs_processed += processed  # Track for status updates

                if (total_processed + total_failed) % 100 == 0:
                    logger.info(
                        f"Progress: {total_processed} processed, {total_failed} failed"
                    )

        finally:
            # Deregister worker on shutdown
            try:
                with self._get_conn() as conn:
                    deregister_worker(conn, self.worker_id)
            except Exception as e:
                logger.warning(f"Failed to deregister worker: {e}")

            logger.info(
                f"Triage worker stopped: "
                f"{total_processed} processed, {total_failed} failed"
            )

    def run_once(self) -> tuple[int, int]:
        """Process all pending jobs once, then exit. Returns (processed, failed)."""
        self._ensure_schema()

        logger.info("Triage worker running once")

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
            f"Triage worker complete: {total_processed} processed, "
            f"{total_failed} failed"
        )
        return total_processed, total_failed


def load_initial_config(config: Config) -> tuple[RulesConfig, int]:
    """Load config from database on startup.

    Returns:
        (rules, config_version)

    Raises:
        ConfigLoadError: If config cannot be loaded from database
    """
    try:
        with psycopg2.connect(
            host=config.postgres_host,
            port=config.postgres_port,
            dbname=config.postgres_db,
            user=config.postgres_user,
            password=config.postgres_password,
        ) as conn:
            rules = load_config_from_db(conn)

            # Get version number
            with conn.cursor() as cursor:
                cursor.execute(_GET_ACTIVE_CONFIG_VERSION_SQL)
                row = cursor.fetchone()
                config_version = row[0] if row else None

            if config_version is None:
                raise ConfigLoadError("No active config version found in database")

            logger.info(f"✓ Loaded config from database (version {config_version})")
            return rules, config_version
    except psycopg2.Error as e:
        raise ConfigLoadError(f"Database error during initial config load: {e}") from e


def main() -> None:
    parser = argparse.ArgumentParser(description="Triage worker service")
    parser.add_argument(
        "--once", action="store_true", help="Process all pending jobs once and exit"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of jobs to process per batch (default: 100)",
    )
    args = parser.parse_args()

    # All config from environment
    required_vars = [
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DUCKDB_API_URL",
        "OLLAMA_URL",
        "GMAIL_TOKEN_PATH",
    ]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Start metrics server
    try:
        metrics_port = int(os.environ.get("METRICS_PORT", "8000"))
    except ValueError:
        logger.error("Invalid METRICS_PORT, using default 8000")
        metrics_port = 8000
    start_metrics_server(port=metrics_port)
    logger.info(f"Metrics server started on port {metrics_port}")

    config = Config(
        postgres_host=os.environ["POSTGRES_HOST"],
        postgres_port=int(os.environ.get("POSTGRES_PORT", "5432")),
        postgres_db=os.environ["POSTGRES_DB"],
        postgres_user=os.environ["POSTGRES_USER"],
        postgres_password=os.environ["POSTGRES_PASSWORD"],
        duckdb_api_url=os.environ["DUCKDB_API_URL"],
        ollama_url=os.environ["OLLAMA_URL"],
        gmail_token_path=os.environ["GMAIL_TOKEN_PATH"],
        batch_size=args.batch_size,
        enable_convergence=os.environ.get("ENABLE_CONVERGENCE", "true").lower()
        == "true",
    )

    # Load and validate rules from database
    try:
        rules, config_version = load_initial_config(config)
        errors = validate_rules(rules)
        if errors:
            for error in errors:
                logger.error(f"Rules validation error: {error}")
            sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to load rules: {e}")
        sys.exit(1)

    # Initialize clients
    ollama = OllamaClient(config.ollama_url)
    gmail = GmailClient(config.gmail_token_path)

    worker = TriageWorker(config, rules, ollama, gmail, config_version=config_version)

    # Handle signals for graceful shutdown and config reload
    def shutdown_handler(signum: int, frame: Any) -> None:
        logger.info("Received shutdown signal")
        worker.stop()

    def reload_handler(signum: int, frame: Any) -> None:
        logger.info("Received SIGHUP - reloading rules")
        worker.reload_rules()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGHUP, reload_handler)

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
