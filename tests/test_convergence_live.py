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

from tests.test_footprint import BASE, FB
from triage.engine.footprint import build_footprint, chain_hashes
from triage.services.worker import CONVERGENCE_RETRY_AFTER, TriageWorker

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
    gmail_id TEXT, config_version_id INT, footprint JSONB,
    convergence_failures INT NOT NULL DEFAULT 0, convergence_failed_at TIMESTAMPTZ
);
"""

SENDER = "a@example.com"
RULES = load_rules_from_string(BASE)
CLEAN = build_footprint(RULES, chain_hashes(RULES), [("main", 1)], SENDER)

# (gmail_id, internal_date, config_version_id, footprint, from_addr), newest
# first. "affected" has no footprint, so it must be re-triaged; "old_clean" is
# older than it and must not be reached in the same scan.
EMAILS = [
    ("newer", 600, 3, None, SENDER),
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
    """Committed versions, read on a second connection so an uncommitted
    stamp does not count."""
    other = psycopg2.connect(DSN, options="-c search_path=t_converge")
    try:
        with other.cursor() as cur:
            cur.execute("SELECT gmail_id, config_version_id FROM classifications")
            return dict(cur.fetchall())
    finally:
        other.close()


def test_unaffected_mail_is_stamped_and_affected_mail_returned(conn: Any) -> None:
    assert _worker(conn)._converge_and_get_next_email() == {"gmail_id": "affected"}
    assert _versions(conn) == {
        "newer": 3,
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


def test_the_selector_checks_rows_against_their_sender(conn: Any) -> None:
    mapped = load_rules_from_string(FB)  # a fallback mapping for SENDER
    clean = build_footprint(mapped, chain_hashes(mapped), [("main", 1)], SENDER)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE classifications SET footprint = %s WHERE footprint IS NOT NULL",
            (Json(clean),),
        )
    conn.commit()
    worker = _worker(conn)
    worker.rules = mapped
    assert worker._converge_and_get_next_email() == {"gmail_id": "affected"}
    assert _versions(conn)["new_clean"] == 2


def test_giving_up_stamps_the_loaded_version_not_the_active_one(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE triage_config_versions (version INT, is_active BOOLEAN);"
            " INSERT INTO triage_config_versions VALUES (3, TRUE);"
        )
    conn.commit()
    _worker(conn)._mark_convergence_failed("affected")
    assert _versions(conn)["affected"] == 2


def test_the_kill_switch_stops_convergence(conn: Any) -> None:
    worker = _worker(conn)
    worker.config.enable_convergence = False
    assert worker._converge_and_get_next_email() is None
    assert _versions(conn)["new_clean"] == 1


def test_deferred_mail_stays_behind_and_is_skipped(conn: Any) -> None:
    worker = _worker(conn)
    with conn.cursor() as cur:  # a second candidate, behind the deferred one
        cur.execute(
            "UPDATE classifications SET footprint = NULL WHERE gmail_id = 'old_clean'"
        )
    conn.commit()
    worker._defer_convergence("affected")
    assert _versions(conn)["affected"] == 1
    assert worker._converge_and_get_next_email() == {"gmail_id": "old_clean"}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT convergence_failures FROM classifications"
            " WHERE gmail_id = 'affected'"
        )
        assert cur.fetchone() == (1,)


def test_deferred_mail_is_retried_after_the_window(conn: Any) -> None:
    worker = _worker(conn)
    worker._defer_convergence("affected")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE classifications SET convergence_failed_at ="
            " NOW() - %s - interval '1 second'",
            (CONVERGENCE_RETRY_AFTER,),
        )
    conn.commit()
    assert worker._converge_and_get_next_email() == {"gmail_id": "affected"}


def test_a_successful_store_clears_the_failures() -> None:
    c = psycopg2.connect(DSN, options="-c search_path=t_converge_ddl")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_converge_ddl CASCADE")
        cur.execute("CREATE SCHEMA t_converge_ddl")
    c.autocommit = False
    try:
        worker = _worker(c)
        worker._ensure_schema()
        email = MagicMock(id=1, gmail_id="g")
        result = MagicMock(action=None)
        result.configure_mock(chain="main", rule_index=0, llm_category=None)
        result.configure_mock(llm_confidence=None, llm_reasoning=None)
        worker._store_classification(email, result, False, None, 1)
        worker._defer_convergence("g")
        with c.cursor() as cur:
            cur.execute("SELECT convergence_failures FROM classifications")
            assert cur.fetchone() == (1,)
        worker._store_classification(email, result, False, None, 2)
        with c.cursor() as cur:
            cur.execute(
                "SELECT convergence_failures, convergence_failed_at, config_version_id"
                " FROM classifications"
            )
            assert cur.fetchone() == (0, None, 2)
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_converge_ddl CASCADE")
        c.close()
