"""The question generator (cortex-kaij.4).

The helpers run anywhere; generate() is SQL, so its tests run against a
throwaway Postgres named by CORTEX_TEST_DSN.
"""

from __future__ import annotations

import json
import os
import threading
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
    assert questions.clean("a\u202eevil\u200bx\u2066y") == "a evil x y"


def test_the_worker_asks_once_per_interval_and_rolls_back_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from triage.services import worker as worker_module

    conn = MagicMock()
    worker = worker_module.TriageWorker.__new__(worker_module.TriageWorker)
    worker.config = MagicMock(question_min_volume=10, question_confidence=0.7)
    worker.rules = CONFIG
    worker.last_questions = 0.0
    worker._get_conn = contextmanager(lambda: iter([conn]))  # type: ignore[method-assign]
    generate = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(questions, "generate", generate)

    worker._ask_questions()
    worker._ask_questions()
    assert generate.call_count == 1
    assert generate.call_args.args[1:] == (CONFIG, 10, 0.7)
    conn.rollback.assert_called_once()

    worker.last_questions -= worker_module.QUESTION_INTERVAL_SECONDS
    worker._ask_questions()
    assert generate.call_count == 2


def test_the_worker_wires_the_generator_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from triage.services import worker as worker_module

    worker = worker_module.TriageWorker(MagicMock(), CONFIG, MagicMock(), MagicMock())
    worker._get_conn = contextmanager(lambda: iter([MagicMock()]))  # type: ignore[method-assign]
    monkeypatch.setattr(worker_module, "ensure_queue_schema", MagicMock())
    schema = MagicMock()
    monkeypatch.setattr(trash_gate, "ensure_trash_schema", schema)
    worker._ensure_schema()
    schema.assert_called_once()

    for name in (
        "_ensure_schema",
        "_check_and_process_signals",
        "_cleanup_stale_workers",
    ):
        monkeypatch.setattr(worker, name, MagicMock())
    ask = MagicMock()
    monkeypatch.setattr(worker, "_ask_questions", ask)

    def idle() -> tuple[int, int]:
        worker.running = False
        return (1, 0)

    monkeypatch.setattr(worker, "_process_batch", idle)
    monkeypatch.setattr(worker_module, "register_worker", MagicMock())
    worker.run()
    ask.assert_called_once()


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
            " deleted_at TIMESTAMPTZ, label TEXT, archive BOOLEAN, mark_read BOOLEAN)"
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


def _mail(
    conn: Any, sender: str | None, n: int, action: dict = UNCATEGORIZED, **llm: Any
):
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
    _mail(conn, None, 12)  # no sender to ask about

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
        category="to\u202edo",
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
    assert got["evil@example.com"][4] == "to do"


@live
def test_a_refresh_keeps_the_question_and_updates_its_volume(conn) -> None:
    _mail(conn, "grow@example.com", 10)
    _generate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM triage_questions")
        (first,) = cur.fetchone()
    _mail(conn, "grow@example.com", 3)
    _generate(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT id, volume FROM triage_questions")
        assert cur.fetchall() == [(first, 13)]


@live
def test_a_sender_answered_after_the_read_is_not_asked_again(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO triage_questions (id, kind, from_addr, status) VALUES"
            " ('q-1', 'uncategorized_sender', 'x@example.com', 'answered')"
        )
        stored = questions._store(
            cur,
            "uncategorized_sender",
            [("x@example.com", 10, [], None, None, None, None)],
        )
    conn.commit()
    assert stored == 0
    assert _kinds(conn) == []


@live
def test_generate_waits_for_an_answer_in_progress(conn) -> None:
    _mail(conn, "wait@example.com", 10)
    answering = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA}")
    try:
        with answering.cursor() as cur:
            cur.execute(trash_gate.LOCK)  # the gateway's answer route holds this
        done = threading.Event()
        worker = threading.Thread(target=lambda: (_generate(conn), done.set()))
        worker.start()
        assert not done.wait(1.0)
        answering.commit()
        assert done.wait(10.0)
        worker.join()
    finally:
        answering.close()
    assert _kinds(conn) == [("wait@example.com", "uncategorized_sender")]


@live
def test_low_confidence_reports_the_least_sure_email_and_excludes_the_threshold(
    conn,
) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE classifications ALTER classified_at DROP DEFAULT")
    conn.commit()
    sure_old = {"label": "Cortex/Commercial/Retail"}
    _mail(
        conn,
        "two@example.com",
        1,
        sure_old,
        category="a",
        confidence=0.3,
        reasoning="least sure",
    )
    _mail(
        conn,
        "two@example.com",
        1,
        {"label": "Cortex/Family"},
        category="b",
        confidence=0.6,
        reasoning="newer",
    )
    _mail(conn, "edge@example.com", 1, sure_old, category="c", confidence=0.7)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE classifications SET classified_at = now() -"
            " CASE llm_category WHEN 'a' THEN interval '2 days' ELSE interval '0' END"
        )
    conn.commit()

    assert _generate(conn)["low_confidence"] == 1
    _, volume, _, proposed, category, confidence, reasoning = _open(conn)[
        "two@example.com"
    ]
    assert (volume, category, reasoning) == (2, "a", "least sure")
    assert confidence == pytest.approx(0.3)
    assert proposed["label"] == "Cortex/Commercial/Retail"


def test_family_groups_subdomains() -> None:
    assert questions.family("a@i.forever21.com") == "forever21.com"
    assert questions.family("b@forever21.com") == "forever21.com"
    assert questions.family("c@contact.joebiden.com") != questions.family(
        "d@democrats.org"
    )


def _mappings(conn: Any, *rows: tuple[Any, ...]) -> None:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                "INSERT INTO triage_email_mappings VALUES (%s, %s, %s, %s, %s)", row
            )
    conn.commit()


def _proposed(conn: Any) -> dict[str, Any]:
    _generate(conn)
    return {addr: row[3] for addr, row in _open(conn).items()}


@live
def test_a_family_that_agrees_proposes_its_label(conn) -> None:
    _mappings(
        conn,
        ("News@Shop.Example.com", None, "Commercial/Retail", True, True),
        ("deals@SHOP.Example.COM", None, "Commercial/Retail", True, True),
        ("gone@shop.example.com", "2026-01-01", "Self/Todo", False, False),
    )
    _mail(conn, "promo@mail.shop.example.com", 10)
    assert _proposed(conn)["promo@mail.shop.example.com"] == {
        "label": "Cortex/Commercial/Retail",
        "archive": True,
        "mark_read": True,
    }


@live
def test_a_family_that_disagrees_proposes_nothing(conn) -> None:
    _mappings(
        conn,
        ("mom@gmail.com", None, "Family", False, False),
        ("dad@gmail.com", None, "Family", False, False),
        ("pal@gmail.com", None, "Commercial/Retail", False, False),
    )
    _mail(conn, "stranger@gmail.com", 10)
    assert _proposed(conn)["stranger@gmail.com"] is None


@live
def test_rules_evidence_takes_the_commonest_allowed_label_and_its_majority(
    conn,
) -> None:
    _mappings(conn, ("x@elsewhere.org", None, "Hobby/Kites", False, False))
    _mail(conn, "ruled@other.org", 10)
    _mail(
        conn,
        "ruled@other.org",
        2,
        {
            "add_label": ["Cortex/Commercial/Newsletter"],
            "archive": True,
            "mark_read": True,
        },
    )
    _mail(
        conn,
        "ruled@other.org",
        1,
        {"add_label": ["Cortex/Commercial/Newsletter"], "archive": True},
    )
    _mail(conn, "ruled@other.org", 1, {"add_label": ["Cortex/Commercial/Newsletter"]})
    _mail(conn, "ruled@other.org", 2, {"add_label": ["Cortex/Commercial/Retail"]})
    _mail(
        conn, "ruled@other.org", 5, {"add_label": ["Cortex/Orders/123"]}
    )  # not allowed
    _mail(conn, "kites@third.net", 10)
    _mail(
        conn, "kites@third.net", 2, {"add_label": ["Cortex/Hobby/Kites"]}
    )  # mapped only
    _mail(conn, "half@fifth.net", 10)
    _mail(conn, "half@fifth.net", 1, {"add_label": ["Cortex/Family"], "archive": True})
    _mail(conn, "half@fifth.net", 1, {"add_label": ["Cortex/Family"]})
    _mail(conn, "odd@fourth.net", 10)
    _mail(conn, "odd@fourth.net", 4, {"add_label": ["Cortex/Self/Todo"]})
    got = _proposed(conn)
    # 4 Newsletter beat 2 Retail; 3 of 4 archived is a majority, 2 of 4 read is not
    assert got["ruled@other.org"] == {
        "label": "Cortex/Commercial/Newsletter",
        "archive": True,
        "mark_read": False,
    }
    assert got["kites@third.net"]["label"] == "Cortex/Hobby/Kites"
    assert got["half@fifth.net"]["archive"] is False  # half is not a majority
    assert got["odd@fourth.net"] is None  # Self/Todo is neither configured nor mapped


@live
def test_low_confidence_prefers_its_own_allowed_label_then_the_evidence(conn) -> None:
    _mappings(conn, ("a@brand.com", None, "Commercial/Retail", True, False))
    _mail(
        conn,
        "own@brand.com",
        1,
        {"label": "Cortex/Family"},
        category="f",
        confidence=0.2,
    )
    _mail(
        conn,
        "bad@brand.com",
        1,
        {"label": "Cortex/Self/Todo"},
        category="t",
        confidence=0.2,
    )
    got = _proposed(conn)
    assert got["own@brand.com"]["label"] == "Cortex/Family"
    assert got["bad@brand.com"] == {
        "label": "Cortex/Commercial/Retail",
        "archive": True,
        "mark_read": False,
    }
