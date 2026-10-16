"""Hold trash until Devon has confirmed the sender (cortex-gco6).

A rule asking for trash does not trash. The first time an address is to be
trashed its mail waits in triage_trash_queue and a trash_sender question opens
in triage_questions; the gateway's answer route records the decision and
re-enqueues the waiting mail. A model never decides any of this: rules are
written by hand and the decision is Devon's.
"""

from __future__ import annotations

import secrets
from typing import Any

from cortex_utils.logging import get_logger
from psycopg2.extras import execute_values

logger = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_trash_senders (
    email_address TEXT PRIMARY KEY,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by TEXT
);
CREATE TABLE IF NOT EXISTS triage_trash_queue (
    gmail_id TEXT PRIMARY KEY,
    from_addr TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'approved', 'cleared', 'kept')),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trash_queue_pending
    ON triage_trash_queue (from_addr) WHERE status = 'pending';
CREATE TABLE IF NOT EXISTS triage_questions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered')),
    from_addr TEXT NOT NULL,
    asked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    answer JSONB
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_triage_questions_open
    ON triage_questions (kind, from_addr) WHERE status = 'open';
"""

# cleared: handed to the labeling worker to trash; Gmail has not been called yet.
QUEUE_STATUS = {"trash": "cleared", "keep": "kept", "ask": "pending"}


def ensure_trash_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        # Two services starting together would race CREATE ... IF NOT EXISTS.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('triage_trash_schema'))")
        cur.execute(SCHEMA)
    conn.commit()


def decide(sender_decision: str | None, queue_status: str | None, addr: str) -> str:
    """trash, keep or ask, for one email a rule wants trashed."""
    if sender_decision == "approved" or queue_status in ("approved", "cleared"):
        return "trash"
    if sender_decision == "rejected" or not addr:
        return "keep"
    return "ask"


def gate(conn: Any, gmail_ids: list[str]) -> set[str]:
    """Queue each email, open one question per undecided sender, commit; return
    the ids to trash now."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT g.gmail_id, COALESCE(lower(p.from_addr), ''), s.decision, q.status
            FROM unnest(%s::text[]) AS g(gmail_id)
            LEFT JOIN emails_parsed p ON p.gmail_id = g.gmail_id
            LEFT JOIN triage_trash_senders s ON s.email_address = lower(p.from_addr)
            LEFT JOIN triage_trash_queue q ON q.gmail_id = g.gmail_id
            """,
            (gmail_ids,),
        )
        rows = [(g, addr, decide(d, s, addr)) for g, addr, d, s in cur.fetchall()]
        execute_values(
            cur,
            """
            INSERT INTO triage_trash_queue (gmail_id, from_addr, status)
            VALUES %s
            ON CONFLICT (gmail_id) DO UPDATE
               SET status = EXCLUDED.status, from_addr = EXCLUDED.from_addr,
                   updated_at = now()
             WHERE triage_trash_queue.status IS DISTINCT FROM EXCLUDED.status
               AND NOT (triage_trash_queue.status = 'approved'
                        AND EXCLUDED.status = 'pending')
            """,
            [(g, addr or None, QUEUE_STATUS[verdict]) for g, addr, verdict in rows],
        )
        asking = sorted({addr for _, addr, verdict in rows if verdict == "ask"})
        if asking:
            execute_values(
                cur,
                """
                INSERT INTO triage_questions (id, kind, from_addr)
                SELECT v.id, v.kind, v.from_addr
                FROM (VALUES %s) AS v(id, kind, from_addr)
                WHERE NOT EXISTS (
                    SELECT 1 FROM triage_trash_senders s
                    WHERE s.email_address = v.from_addr)
                ON CONFLICT (kind, from_addr) WHERE status = 'open' DO NOTHING
                """,
                [(f"q-{secrets.token_hex(6)}", "trash_sender", a) for a in asking],
            )
    conn.commit()
    if asking:
        logger.info(f"Trash held for confirmation: {asking}")
    return {g for g, _, verdict in rows if verdict == "trash"}
