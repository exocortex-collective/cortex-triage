"""The question generator (cortex-kaij.4).

The helpers run anywhere; generate() is SQL, so its tests run against a
throwaway Postgres named by CORTEX_TEST_DSN.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest
from cortex_utils.triage_config.models import (
    Action,
    ChainConfig,
    EmailMappingAction,
    LLMConfig,
    MatchCondition,
    Rule,
    RulesConfig,
)

from triage.services import questions, trash_gate

CONFIG = RulesConfig(
    chains={
        "main": ChainConfig(
            auto_run=True,
            rules=[
                Rule(
                    match=MatchCondition(subject_contains="x"),
                    action=Action(label="Commercial/Retail"),
                ),
                Rule(
                    llm=LLMConfig(model="m", prompt_version="v1"),
                    routes={"news": Action(label="Commercial/Newsletter")},
                ),
                Rule(
                    match=MatchCondition(subject_contains="y"),
                    action=Action(label="Orders/{{order}}"),
                ),
            ],
        )
    },
    fallback_email_mappings={"a@b.c": EmailMappingAction(label="Family")},
)

# --- helpers ---------------------------------------------------------------


def test_configured_labels_are_the_literal_ones_prefixed() -> None:
    assert questions.configured_labels(CONFIG) == {
        "Cortex/Uncategorized",
        "Cortex/Commercial/Retail",
        "Cortex/Commercial/Newsletter",
        "Cortex/Family",
    }


def test_a_proposed_label_the_config_does_not_write_is_dropped() -> None:
    allowed = questions.configured_labels(CONFIG)
    for injected in [
        "Cortex/Self/Todo",
        "Cortex/Orders/{{order}}",
        "Commercial/Retail",
    ]:
        assert questions.proposal({"label": injected}, allowed)["label"] is None
    kept = questions.proposal(
        {
            "add_label": ["Cortex/Self/Todo", "Cortex/Commercial/Retail"],
            "archive": True,
        },
        allowed,
    )
    assert kept == {
        "label": "Cortex/Commercial/Retail",
        "archive": True,
        "mark_read": False,
    }
    assert questions.proposal(None, allowed)["label"] is None


def test_model_text_is_one_clean_line() -> None:
    assert questions.clean("ok\x1b[31m\nAPPROVE\r\x00 this") == "ok [31m APPROVE this"
    assert questions.clean("x" * 900) == "x" * 500
    assert questions.clean(" \n ") is None
    assert questions.clean(None) is None


# --- generate() against Postgres -------------------------------------------

DSN = os.environ.get("CORTEX_TEST_DSN")
live = pytest.mark.skipif(
    not DSN, reason="set CORTEX_TEST_DSN to a throwaway Postgres to run these"
)
SCHEMA = f"t_questions_{uuid.uuid4().hex[:8]}"
UNCATEGORIZED = {"add_label": ["Cortex/Uncategorized"]}


@pytest.fixture
def conn() -> Iterator[Any]:
    c = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA}")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(
            "CREATE TABLE emails_parsed (gmail_id TEXT, from_addr TEXT,"
            " subject TEXT, date_header TIMESTAMPTZ)"
        )
        cur.execute(
            "CREATE TABLE classifications (gmail_id TEXT, action_taken JSONB,"
            " llm_category TEXT, llm_confidence REAL, llm_reasoning TEXT,"
            " classified_at TIMESTAMPTZ DEFAULT now())"
        )
        cur.execute(
            "CREATE TABLE triage_email_mappings (email_address TEXT,"
            " deleted_at TIMESTAMPTZ)"
        )
    c.autocommit = False
    trash_gate.ensure_trash_schema(c)
    try:
        yield c
    finally:
        c.rollback()
        c.autocommit = True
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.close()


def _mail(conn: Any, sender: str, n: int, action: dict = UNCATEGORIZED, **llm: Any):
    with conn.cursor() as cur:
        for i in range(n):
            gid = f"{sender}-{uuid.uuid4().hex[:6]}"
            cur.execute(
                "INSERT INTO emails_parsed VALUES"
                " (%s, %s, %s, now() - %s * interval '1 day')",
                (gid, sender, f"subject {i}", i),
            )
            cur.execute(
                "INSERT INTO classifications (gmail_id, action_taken, llm_category,"
                " llm_confidence, llm_reasoning) VALUES (%s, %s, %s, %s, %s)",
                (
                    gid,
                    json.dumps(action),
                    llm.get("category"),
                    llm.get("confidence"),
                    llm.get("reasoning"),
                ),
            )
    conn.commit()


def _open(conn: Any) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_addr, kind, volume, sample, proposed, llm_category,"
            " confidence, reasoning FROM triage_questions WHERE status = 'open'"
        )
        return {r[0]: r[1:] for r in cur.fetchall()}


def _kinds(conn: Any) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_addr, kind FROM triage_questions WHERE status = 'open'"
        )
        return sorted(cur.fetchall())


def _generate(conn: Any) -> dict[str, int]:
    return questions.generate(conn, CONFIG, min_volume=10, threshold=0.7)


@live
def test_a_sender_with_enough_uncategorized_mail_is_asked_about_once(conn) -> None:
    _mail(conn, "Many@Example.com", 6)
    _mail(conn, "many@example.com", 6)  # same sender, other case
    _mail(conn, "few@example.com", 9)
    _mail(conn, "labelled@example.com", 20, {"add_label": ["Cortex/Commercial/Retail"]})
    _mail(conn, "both@example.com", 20, {"add_label": ["Cortex/Uncategorized", "X"]})

    assert _generate(conn) == {"uncategorized_sender": 1, "low_confidence": 0}
    assert _generate(conn) == {"uncategorized_sender": 1, "low_confidence": 0}

    (kind, volume, sample, proposed, *_), *rest = _open(conn).values()
    assert not rest
    assert list(_open(conn)) == ["many@example.com"]
    assert (kind, volume, proposed) == ("uncategorized_sender", 12, None)
    dates = [s["date_header"] for s in sample]
    assert len(sample) == 5 and dates == sorted(dates, reverse=True)


@live
def test_a_settled_sender_is_not_asked_and_its_open_question_goes(conn) -> None:
    _mail(conn, "mapped@example.com", 10)
    _mail(conn, "answered@example.com", 10)
    _mail(conn, "later@example.com", 10)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_email_mappings VALUES ('mapped@example.com', NULL)"
        )
        cur.execute(
            "INSERT INTO triage_questions (id, kind, from_addr, status) VALUES"
            " ('q-old', 'uncategorized_sender', 'answered@example.com', 'answered'),"
            " ('q-trash', 'trash_sender', 'later@example.com', 'open')"
        )
    conn.commit()
    _generate(conn)
    assert _kinds(conn) == [
        ("later@example.com", "trash_sender"),
        ("later@example.com", "uncategorized_sender"),
    ]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_email_mappings VALUES ('later@example.com', NULL)"
        )
    conn.commit()
    _generate(conn)
    assert _kinds(conn) == [("later@example.com", "trash_sender")]


@live
def test_a_low_confidence_question_carries_only_vetted_model_output(conn) -> None:
    _mail(
        conn,
        "news@example.com",
        2,
        {"add_label": ["Cortex/Commercial/Newsletter"], "archive": True},
        category="news",
        confidence=0.4,
        reasoning="fine\x1b]8;;evil\x07\nIGNORE ALL",
    )
    _mail(
        conn,
        "evil@example.com",
        1,
        {"label": "Cortex/Self/Todo"},
        category="todo",
        confidence=0.1,
    )
    _mail(conn, "sure@example.com", 3, {"label": "X"}, category="x", confidence=0.9)

    assert _generate(conn)["low_confidence"] == 2
    got = _open(conn)
    kind, volume, _, proposed, category, confidence, reasoning = got["news@example.com"]
    assert (kind, volume, category) == ("low_confidence", 2, "news")
    assert confidence == pytest.approx(0.4)
    assert proposed == {
        "label": "Cortex/Commercial/Newsletter",
        "archive": True,
        "mark_read": False,
    }
    assert reasoning == "fine ]8;;evil IGNORE ALL"
    assert got["evil@example.com"][3]["label"] is None
