"""Live-Postgres test that a footprint survives the real DDL and upsert.

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
from cortex_utils.triage_config.models import Action, EvaluationResult

from tests.test_footprint import BASE, _email
from triage.engine.footprint import build_footprint, chain_hashes, is_affected
from triage.services.worker import TriageWorker

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

# classifications as it was before the footprint column, so _ensure_schema has
# to upgrade it rather than create it.
OLD_CLASSIFICATIONS = """
CREATE TABLE classifications (
    id BIGSERIAL PRIMARY KEY, email_id BIGINT NOT NULL, gmail_id TEXT NOT NULL,
    matched_chain TEXT, matched_rule_index INT, config_version_id INTEGER,
    llm_category TEXT, llm_confidence REAL, llm_reasoning TEXT,
    action_taken JSONB, action_executed BOOLEAN DEFAULT FALSE,
    classified_at TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT unique_gmail_id UNIQUE (gmail_id)
);
"""


@pytest.fixture
def worker() -> Iterator[Any]:
    c = psycopg2.connect(DSN, options="-c search_path=t_footprint")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_footprint CASCADE")
        cur.execute("CREATE SCHEMA t_footprint")
        cur.execute(OLD_CLASSIFICATIONS)
    c.autocommit = False

    @contextmanager
    def get_conn() -> Iterator[Any]:
        yield c

    w = TriageWorker(
        MagicMock(), load_rules_from_string(BASE), MagicMock(), MagicMock()
    )
    w._get_conn = get_conn  # type: ignore[method-assign]
    try:
        yield w
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_footprint CASCADE")
        c.close()


def _stored(worker: Any) -> Any:
    with worker._get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT footprint, config_version_id FROM classifications"
            " WHERE gmail_id = 'g'"
        )
        footprint, version = cur.fetchone()
    assert version == 7  # the version passed in, not worker.config_version
    return footprint


def test_a_footprint_round_trips_through_the_real_schema(worker: Any) -> None:
    worker._ensure_schema()
    config = worker.rules
    footprint = build_footprint(
        config, chain_hashes(config), [("main", 1)], "a@example.com"
    )
    result = EvaluationResult(chain="main", rule_index=1, action=Action(label="Hit"))
    email = _email("a@example.com", "a hit")

    worker._store_classification(email, result, True, None, 7)
    assert _stored(worker) is None
    worker._store_classification(email, result, True, footprint, 7)
    stored = _stored(worker)
    assert stored == footprint
    assert not is_affected(stored, config, chain_hashes(config), "a@example.com")
    worker._store_classification(email, result, True, None, 7)
    assert _stored(worker) is None  # a re-triage without one clears the stale one
