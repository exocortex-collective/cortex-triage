"""Live-Postgres tests for the SQL that keeps spam out of triage (cortex-kvyn).

The convergence picker and the labeling worker's spam lookup are predicates in
SQL, and a fake cursor that matches statement text passes a deleted or
inverted predicate. These run the real queries against real tables.

Skipped unless CORTEX_TEST_DSN points at a throwaway Postgres; CI starts one.
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

from triage.services.labeling_worker import SPAM_TARGET, LabelingWorker
from triage.services.worker import TriageWorker

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

# Only the columns the two queries read.
SCHEMA = """
CREATE TABLE triage_config_versions (
    version INT PRIMARY KEY, is_active BOOLEAN, label_prefix TEXT
);
CREATE TABLE emails_raw (
    id BIGSERIAL PRIMARY KEY, gmail_id TEXT UNIQUE, label_ids TEXT[],
    internal_date BIGINT
);
CREATE TABLE emails_parsed (gmail_id TEXT PRIMARY KEY, from_addr TEXT);
CREATE TABLE triage_email_mappings (
    email_address TEXT, mapping_type TEXT, deleted_at TIMESTAMPTZ
);
CREATE TABLE classifications (
    gmail_id TEXT, config_version_id INT, action_taken JSONB, footprint JSONB,
    classified_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO triage_config_versions VALUES (2, TRUE, 'Cortex');
"""

# (gmail_id, label_ids, internal_date) -- the spam message is the newest, so a
# picker without the filter returns it first.
EMAILS = [
    ("spam", ["SPAM", "UNREAD"], 300),
    ("ham", ["INBOX"], 200),
    ("nolabels", None, 100),
]


@pytest.fixture
def conn() -> Iterator[Any]:
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_spam CASCADE")
        cur.execute("CREATE SCHEMA t_spam")
        cur.execute("SET search_path = t_spam")
        cur.execute(SCHEMA)
        for gmail_id, labels, date in EMAILS:
            cur.execute(
                "INSERT INTO emails_raw (gmail_id, label_ids, internal_date)"
                " VALUES (%s, %s, %s)",
                (gmail_id, labels, date),
            )
            cur.execute(
                "INSERT INTO classifications (gmail_id, config_version_id,"
                " action_taken) VALUES (%s, 1, %s)",
                (gmail_id, '{"add_label": ["Cortex/Transactional"]}'),
            )
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_spam CASCADE")
        c.close()


def _bind(worker: Any, conn: Any) -> None:
    @contextmanager
    def get_conn() -> Iterator[Any]:
        yield conn

    worker._get_conn = get_conn


def _triage_worker(conn: Any) -> Any:
    config = MagicMock(enable_convergence=True, convergence_batch_size=500)
    rules = load_rules_from_string("version: 1\nchains: {}\n")
    worker = TriageWorker(config, rules, MagicMock(), MagicMock(), config_version=2)
    _bind(worker, conn)
    return worker


def test_convergence_never_picks_spam(conn: Any) -> None:
    worker = _triage_worker(conn)
    assert worker._converge_and_get_next_email() == {"gmail_id": "ham"}


def test_convergence_returns_nothing_when_only_spam_is_behind(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE classifications SET config_version_id = 2")
        cur.execute(
            "UPDATE classifications SET config_version_id = 1 WHERE gmail_id = 'spam'"
        )
    assert _triage_worker(conn)._converge_and_get_next_email() is None


def test_convergence_still_picks_a_message_with_null_label_ids(conn: Any) -> None:
    # NOT 'SPAM' = ANY(NULL) is NULL, not true: without the COALESCE a message
    # with no label_ids would never converge.
    with conn.cursor() as cur:
        cur.execute("UPDATE classifications SET config_version_id = 2")
        cur.execute(
            "UPDATE classifications SET config_version_id = 1"
            " WHERE gmail_id = 'nolabels'"
        )
    worker = _triage_worker(conn)
    assert worker._converge_and_get_next_email() == {"gmail_id": "nolabels"}


def test_convergence_no_longer_skips_priority_senders(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO emails_parsed VALUES ('ham', 'vip@example.com')")
        cur.execute(
            "INSERT INTO triage_email_mappings VALUES"
            " ('vip@example.com', 'priority', NULL)"
        )
    worker = _triage_worker(conn)
    assert worker._converge_and_get_next_email() == {"gmail_id": "ham"}


def test_labeling_spam_lookup_is_scoped_to_the_batch(conn: Any) -> None:
    worker = LabelingWorker(MagicMock(), MagicMock())
    _bind(worker, conn)
    assert set(worker._get_latest_classifications(["ham"])) == {"ham"}


def test_labeling_targets_spam_with_no_labels(conn: Any) -> None:
    worker = LabelingWorker(MagicMock(), MagicMock())
    _bind(worker, conn)
    result = worker._get_latest_classifications(["spam", "ham", "nolabels"])
    assert result["spam"] == SPAM_TARGET
    assert result["ham"] == {"add_label": ["Cortex/Transactional"]}
    assert result["nolabels"] == {"add_label": ["Cortex/Transactional"]}
