#!/usr/bin/env python3
"""Triage worker service - processes emails from queue through rules engine."""

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg2
from psycopg2.extras import Json

from triage.engine.matcher import evaluate
from triage.engine.rules import load_rules, validate_rules
from triage.models.rules import Action, Email, EvaluationResult, RulesConfig

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
    rules_path: str
    duckdb_api_url: str
    ollama_url: str
    gmail_token_path: str
    batch_size: int = 100
    poll_interval: int = 5  # seconds between queue polls when empty


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
        except Exception as e:
            logger.error(f"Ollama intent check failed: {e}")
            return False

    def classify(
        self, from_addr: str, subject: str, body: str, model: str, prompt_version: str
    ) -> tuple[str, float, str]:
        """Classify an email using LLM.

        Returns:
            Tuple of (category, confidence, reasoning).
        """
        # TODO: Load prompts from versioned config
        prompt = f"""You are categorizing emails for a personal Gmail inbox.

Categories:
- automated_noise: GitHub notifications, service alerts, social media, marketing
- human_request: Personal emails requiring response (friends, family, colleagues)
- action_item: Has deadline (invoices, permission slips, renewals, bills)
- wrong_email: Misdirected mail
- subscription: Newsletters, marketing, recurring service emails
- school: Kids' school communications

Email:
From: {from_addr}
Subject: {subject}
Body preview: {body[:500] if body else '(empty)'}

Respond with JSON only:
{{"category": "...", "confidence": 0.0-1.0, "reasoning": "brief explanation"}}"""

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

            import json

            data = json.loads(text)
            return (
                data.get("category", "unknown"),
                float(data.get("confidence", 0.5)),
                data.get("reasoning", ""),
            )
        except Exception as e:
            logger.error(f"Ollama classification failed: {e}")
            return ("unknown", 0.0, str(e))


class GmailClient:
    """Client for Gmail API operations."""

    def __init__(self, token_path: str):
        self.token_path = token_path
        self._service = None

    def _get_service(self) -> Any:
        """Get authenticated Gmail API service."""
        if self._service is None:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials.from_authorized_user_file(self.token_path)
            self._service = build("gmail", "v1", credentials=creds)
        return self._service

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
        except Exception as e:
            logger.error(f"Failed to apply label {label_name} to {gmail_id}: {e}")
            return False

    def archive(self, gmail_id: str) -> bool:
        """Archive an email (remove INBOX label)."""
        try:
            service = self._get_service()
            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body={"removeLabelIds": ["INBOX"]},
            ).execute()
            logger.debug(f"Archived {gmail_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to archive {gmail_id}: {e}")
            return False

    def mark_read(self, gmail_id: str) -> bool:
        """Mark an email as read (remove UNREAD label)."""
        try:
            service = self._get_service()
            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()
            logger.debug(f"Marked read {gmail_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to mark read {gmail_id}: {e}")
            return False

    def star(self, gmail_id: str) -> bool:
        """Star an email."""
        try:
            service = self._get_service()
            service.users().messages().modify(
                userId="me",
                id=gmail_id,
                body={"addLabelIds": ["STARRED"]},
            ).execute()
            logger.debug(f"Starred {gmail_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to star {gmail_id}: {e}")
            return False

    def _get_or_create_label(self, label_name: str) -> str | None:
        """Get label ID, creating the label if it doesn't exist."""
        try:
            service = self._get_service()

            # List all labels
            results = service.users().labels().list(userId="me").execute()
            labels = results.get("labels", [])

            # Check if label exists
            for label in labels:
                if label["name"] == label_name:
                    label_id: str = label["id"]
                    return label_id

            # Create label (handles nested labels automatically)
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
            return created_id
        except Exception as e:
            logger.error(f"Failed to get/create label {label_name}: {e}")
            return None


class TriageWorker:
    def __init__(
        self,
        config: Config,
        rules: RulesConfig,
        ollama: OllamaClient,
        gmail: GmailClient,
    ):
        self.config = config
        self.rules = rules
        self.ollama = ollama
        self.gmail = gmail
        self.running = True
        self._pg_conn: psycopg2.extensions.connection | None = None

    def _get_pg_conn(self) -> psycopg2.extensions.connection:
        if self._pg_conn is None or self._pg_conn.closed:
            self._pg_conn = psycopg2.connect(
                host=self.config.postgres_host,
                port=self.config.postgres_port,
                dbname=self.config.postgres_db,
                user=self.config.postgres_user,
                password=self.config.postgres_password,
            )
        return self._pg_conn

    def _ensure_schema(self) -> None:
        """Ensure classifications table exists."""
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
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
            """
            )
            conn.commit()
        logger.info("classifications schema ensured")

    def _claim_jobs(self, batch_size: int) -> list[dict[str, Any]]:
        """Claim a batch of pending triage jobs."""
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE queue
                SET status = 'processing', claimed_at = NOW()
                WHERE id IN (
                    SELECT id FROM queue
                    WHERE queue_name = 'triage' AND status = 'pending'
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING id, payload
            """,
                (batch_size,),
            )
            jobs = [{"id": row[0], "payload": row[1]} for row in cur.fetchall()]
            conn.commit()
        return jobs

    def _get_email_data(self, gmail_id: str) -> Email | None:
        """Fetch email data from emails_parsed."""
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, gmail_id, from_addr, to_addrs, subject, body_text
                FROM emails_parsed
                WHERE gmail_id = %s
            """,
                (gmail_id,),
            )
            row = cur.fetchone()
            if row:
                # to_addrs is an array, use first one
                to_addr = row[3][0] if row[3] else ""
                return Email(
                    id=row[0],
                    gmail_id=row[1],
                    from_addr=row[2] or "",
                    to_addr=to_addr,
                    subject=row[4] or "",
                    body=row[5],
                    has_attachment=False,  # TODO: check attachments table
                )
        return None

    def _get_body_from_api(self, gmail_id: str) -> str | None:
        """Fetch body from DuckDB API if not in parsed table."""
        try:
            response = httpx.get(
                f"{self.config.duckdb_api_url}/body",
                params={"gmail_id": gmail_id},
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                body: str | None = data.get("body_text") or data.get("body_html")
                return body
        except Exception as e:
            logger.warning(f"Failed to fetch body from API for {gmail_id}: {e}")
        return None

    def _execute_action(self, action: Action, gmail_id: str) -> bool:
        """Execute an action on an email. Returns True if all actions succeeded."""
        success = True

        if action.label and not self.gmail.apply_label(gmail_id, action.label):
            success = False

        if action.archive and not self.gmail.archive(gmail_id):
            success = False

        if action.mark_read and not self.gmail.mark_read(gmail_id):
            success = False

        if action.star and not self.gmail.star(gmail_id):
            success = False

        return success

    def _store_classification(
        self,
        email: Email,
        result: EvaluationResult,
        action_executed: bool,
    ) -> None:
        """Store classification result."""
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO classifications (
                    email_id, gmail_id, matched_chain, matched_rule_index,
                    rules_version, llm_category, llm_confidence, llm_reasoning,
                    action_taken, action_executed
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
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
        conn = self._get_pg_conn()
        with conn.cursor() as cur:
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

        try:
            # Get email data
            email = self._get_email_data(gmail_id)
            if not email:
                logger.warning(f"Parsed email not found: {gmail_id}")
                self._fail_job(job["id"], "Parsed email not found")
                return False

            # Fetch body from API if not in parsed table
            if not email.body:
                email.body = self._get_body_from_api(gmail_id)

            # Evaluate rules
            result = evaluate(self.rules, email, self.ollama)

            # Execute action if matched
            action_executed = False
            if result.matched and result.action:
                action_executed = self._execute_action(result.action, gmail_id)
                if action_executed:
                    logger.info(
                        f"Processed {gmail_id}: chain={result.chain}, "
                        f"rule={result.rule_index}, action={result.action}"
                    )
                else:
                    logger.warning(
                        f"Action failed for {gmail_id}: {result.action}"
                    )
            else:
                logger.info(f"No match for {gmail_id}")

            # Store classification
            self._store_classification(email, result, action_executed)

            # Mark job complete
            self._complete_job(job["id"])
            return True

        except Exception as e:
            logger.error(f"Failed to process {gmail_id}: {e}")
            self._fail_job(job["id"], str(e))
            return False

    def run(self) -> None:
        """Main processing loop."""
        self._ensure_schema()

        logger.info("Triage worker started")

        processed = 0
        failed = 0

        while True:
            if not self.running:
                break

            jobs = self._claim_jobs(self.config.batch_size)

            if not jobs:
                logger.debug(f"No jobs, sleeping {self.config.poll_interval}s")
                time.sleep(self.config.poll_interval)
                continue

            logger.info(f"Processing batch of {len(jobs)} jobs")

            for job in jobs:
                if self.process_job(job):
                    processed += 1
                else:
                    failed += 1

                if (processed + failed) % 100 == 0:
                    logger.info(f"Progress: {processed} processed, {failed} failed")

        logger.info(f"Triage worker stopped: {processed} processed, {failed} failed")

    def run_once(self) -> tuple[int, int]:
        """Process all pending jobs once, then exit. Returns (processed, failed)."""
        self._ensure_schema()

        logger.info("Triage worker running once")

        processed = 0
        failed = 0

        while True:
            jobs = self._claim_jobs(self.config.batch_size)

            if not jobs:
                break

            logger.info(f"Processing batch of {len(jobs)} jobs")

            for job in jobs:
                if self.process_job(job):
                    processed += 1
                else:
                    failed += 1

                if (processed + failed) % 100 == 0:
                    logger.info(f"Progress: {processed} processed, {failed} failed")

        logger.info(f"Triage worker complete: {processed} processed, {failed} failed")
        return processed, failed

    def stop(self) -> None:
        """Signal the worker to stop."""
        self.running = False


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
        "RULES_PATH",
        "DUCKDB_API_URL",
        "OLLAMA_URL",
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
        rules_path=os.environ["RULES_PATH"],
        duckdb_api_url=os.environ["DUCKDB_API_URL"],
        ollama_url=os.environ["OLLAMA_URL"],
        gmail_token_path=os.environ["GMAIL_TOKEN_PATH"],
        batch_size=args.batch_size,
    )

    # Load and validate rules
    try:
        rules = load_rules(config.rules_path)
        errors = validate_rules(rules)
        if errors:
            for error in errors:
                logger.error(f"Rules validation error: {error}")
            sys.exit(1)
        logger.info(f"Loaded rules from {config.rules_path}")
    except Exception as e:
        logger.error(f"Failed to load rules: {e}")
        sys.exit(1)

    # Initialize clients
    ollama = OllamaClient(config.ollama_url)
    gmail = GmailClient(config.gmail_token_path)

    worker = TriageWorker(config, rules, ollama, gmail)

    # Handle signals for graceful shutdown
    def signal_handler(signum: int, frame: Any) -> None:
        logger.info("Received shutdown signal")
        worker.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    if args.once:
        processed, failed = worker.run_once()
        sys.exit(0 if failed == 0 else 1)
    else:
        worker.run()


if __name__ == "__main__":
    main()
