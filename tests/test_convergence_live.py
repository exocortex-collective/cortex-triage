"""Live-Postgres tests for delta convergence (triage-70be).

The selector stamps mail a config change cannot reach and returns the first it
can. Its SQL and the stamping are the behaviour, so they run against real
tables. Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import psycopg2
import pytest
from cortex_utils.triage_config import load_rules_from_string
from psycopg2.extras import Json

from tests.test_footprint import BASE
from triage.engine.footprint import build_footprint, chain_hashes
from triage.services.worker import TriageWorker

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SCHEMA = """
CREATE TABLE emails_raw (
    id BIGSERIAL PRIMARY KEY, gmail_id TEXT UNIQUE, label_ids TEXT[],
    internal_date BIGINT
);
CREATE TABLE emails_parsed (gmail_id TEXT PRIMARY KEY, from_addr TEXT);
CREATE TABLE classifications (
    gmail_id TEXT, config_version_id INT, footprint JSONB
);
"""

SENDER = "a@example.com"
RULES = load_rules_from_string(BASE)
CLEAN = build_footprint(RULES, chain_hashes(RULES), [("main", 1)], SENDER)

# (gmail_id, internal_date, config_version_id, footprint, from_addr), newest
# first. "affected" has no footprint, so it must be re-triaged; "old_clean" is
# older than it and must not be reached in the same scan.
EMAILS = [
    ("current", 500, 2, None, SENDER),
    ("new_clean", 400, 1, CLEAN, SENDER),
    ("affected", 300, 1, None, SENDER),
    ("old_clean", 200, 1, CLEAN, SENDER),
]


@pytest.fixture
def conn() -> Iterator[Any]:
    c = psycopg2.connect(DSN, options="-c search_path=t_converge")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_converge CASCADE")
        cur.execute("CREATE SCHEMA t_converge")
        cur.execute(SCHEMA)
        for gmail_id, date, version, footprint, from_addr in EMAILS:
            cur.execute(
                "INSERT INTO emails_raw (gmail_id, label_ids, internal_date)"
                " VALUES (%s, '{INBOX}', %s)",
                (gmail_id, date),
            )
            cur.execute(
                "INSERT INTO emails_parsed VALUES (%s, %s)", (gmail_id, from_addr)
            )
            cur.execute(
                "INSERT INTO classifications VALUES (%s, %s, %s)",
                (gmail_id, version, Json(footprint) if footprint else None),
            )
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_converge CASCADE")
        c.close()


def _worker(conn: Any, batch: int = 500) -> Any:
    config = MagicMock(enable_convergence=True, convergence_batch_size=batch)
    worker = TriageWorker(config, RULES, MagicMock(), MagicMock(), config_version=2)

    @contextmanager
    def get_conn() -> Iterator[Any]:
        yield conn

    worker._get_conn = get_conn
    return worker


def _versions(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT gmail_id, config_version_id FROM classifications")
        return dict(cur.fetchall())


def test_unaffected_mail_is_stamped_and_affected_mail_returned(conn: Any) -> None:
    assert _worker(conn)._converge_and_get_next_email() == {"gmail_id": "affected"}
    assert _versions(conn) == {
        "current": 2,
        "new_clean": 2,
        "affected": 1,
        "old_clean": 1,
    }


def test_a_sender_with_no_parsed_row_is_re_triaged(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM emails_parsed WHERE gmail_id = 'new_clean'")
    assert _worker(conn)._converge_and_get_next_email() == {"gmail_id": "new_clean"}


def test_a_batch_of_only_unaffected_mail_returns_nothing(conn: Any) -> None:
    worker = _worker(conn, batch=1)
    assert worker._converge_and_get_next_email() is None
    assert _versions(conn)["new_clean"] == 2
    assert worker._converge_and_get_next_email() == {"gmail_id": "affected"}


def test_a_config_change_on_the_path_is_re_triaged(conn: Any) -> None:
    worker = _worker(conn)
    worker.rules = load_rules_from_string(BASE.replace("{label: Hit}", "{label: H2}"))
    assert worker._converge_and_get_next_email() == {"gmail_id": "new_clean"}
    assert _versions(conn)["new_clean"] == 1
