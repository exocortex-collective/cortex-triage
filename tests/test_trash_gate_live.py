"""Live-Postgres tests for the trash gate (cortex-gco6).

The gate's SQL is the behaviour, so it runs against real tables. Skipped
unless CORTEX_TEST_DSN points at a throwaway Postgres.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

from triage.services import trash_gate

DSN = os.environ.get("CORTEX_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)

SENDERS = {
    "new1": "New@Example.com",
    "new2": "new@example.com",
    "ok": "Ok@Example.com",
    "no": "no@example.com",
    "once": "once@example.com",
}


@pytest.fixture
def conn() -> Iterator[Any]:
    c = psycopg2.connect(DSN, options="-c search_path=t_trash")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS t_trash CASCADE")
        cur.execute("CREATE SCHEMA t_trash")
        cur.execute("CREATE TABLE emails_parsed (gmail_id TEXT, from_addr TEXT)")
        for gmail_id, addr in SENDERS.items():
            cur.execute("INSERT INTO emails_parsed VALUES (%s, %s)", (gmail_id, addr))
    c.autocommit = False
    trash_gate.ensure_trash_schema(c)
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_trash_senders (email_address, decision) VALUES"
            " ('ok@example.com', 'approved'), ('no@example.com', 'rejected')"
        )
        cur.execute(
            "INSERT INTO triage_trash_queue (gmail_id, from_addr, status)"
            " VALUES ('once', 'once@example.com', 'approved')"
        )
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS t_trash CASCADE")
        c.close()


def _rows(conn: Any, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return sorted(cur.fetchall())


def test_a_new_sender_is_held_and_asked_about_once(conn: Any) -> None:
    assert trash_gate.gate(conn, ["new1"]) == set()
    assert trash_gate.gate(conn, ["new2"]) == set()
    assert _rows(
        conn,
        "SELECT gmail_id, from_addr, status FROM triage_trash_queue"
        " WHERE gmail_id LIKE 'new%'",
    ) == [
        ("new1", "new@example.com", "pending"),
        ("new2", "new@example.com", "pending"),
    ]
    assert _rows(conn, "SELECT kind, status, from_addr FROM triage_questions") == [
        ("trash_sender", "open", "new@example.com")
    ]


def test_decisions_and_one_off_approvals_are_honoured(conn: Any) -> None:
    allowed = trash_gate.gate(conn, ["ok", "no", "once"])
    assert allowed == {"ok", "once"}
    assert _rows(conn, "SELECT gmail_id, status FROM triage_trash_queue") == [
        ("no", "kept"),
        ("ok", "cleared"),
        ("once", "cleared"),
    ]
    assert _rows(conn, "SELECT * FROM triage_questions") == []


def test_mail_with_no_sender_is_kept_without_a_question(conn: Any) -> None:
    assert trash_gate.gate(conn, ["unparsed"]) == set()
    assert _rows(
        conn,
        "SELECT gmail_id, from_addr, status FROM triage_trash_queue"
        " WHERE gmail_id = 'unparsed'",
    ) == [("unparsed", None, "kept")]
    assert _rows(conn, "SELECT * FROM triage_questions") == []


def test_an_answered_question_does_not_stop_a_new_one(conn: Any) -> None:
    trash_gate.gate(conn, ["new1"])
    with conn.cursor() as cur:
        cur.execute("UPDATE triage_questions SET status = 'answered'")
        cur.execute("DELETE FROM triage_trash_queue WHERE gmail_id = 'new1'")
    conn.commit()
    trash_gate.gate(conn, ["new1"])
    assert _rows(conn, "SELECT status FROM triage_questions") == [
        ("answered",),
        ("open",),
    ]


def test_a_decision_releases_or_keeps_held_mail(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_trash_queue (gmail_id, from_addr, status) VALUES"
            " ('ok', 'ok@example.com', 'pending'), ('no', 'no@example.com', 'pending')"
        )
    conn.commit()
    assert trash_gate.gate(conn, ["ok", "no"]) == {"ok"}
    assert _rows(
        conn,
        "SELECT gmail_id, status FROM triage_trash_queue"
        " WHERE gmail_id IN ('ok', 'no')",
    ) == [("no", "kept"), ("ok", "cleared")]


def test_the_schema_can_be_ensured_again(conn: Any) -> None:
    trash_gate.ensure_trash_schema(conn)
    trash_gate.ensure_trash_schema(conn)
    assert trash_gate.gate(conn, ["new1"]) == set()


def test_the_gate_waits_for_an_answer_in_progress(conn: Any) -> None:
    other = psycopg2.connect(DSN, options="-c search_path=t_trash")
    try:
        with other.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('triage_trash_gate'))")
        done = threading.Event()
        gate = threading.Thread(
            target=lambda: (trash_gate.gate(conn, ["new1"]), done.set())
        )
        gate.start()
        assert not done.wait(0.5)
        other.commit()
        assert done.wait(5)
        gate.join()
    finally:
        other.close()
