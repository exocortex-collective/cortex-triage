"""Campaign registry and creator domains (cortex-l1s0)."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg2
import pytest

from triage.services import campaigns as cp

KS = "no-reply@kickstarter.com"
TITLE = "Widgetron W2: Pocket Laser Cutter"


def corpus(*extra: tuple[str, str]) -> list[tuple[str, str]]:
    """Enough unrelated mail that a brand token is rare and a word is not."""
    filler = [
        (f"news{i}@example{i % 40}.com", f"Your cutter weekly {i}") for i in range(4000)
    ]
    return filler + list(extra)


@pytest.mark.parametrize(
    "subject, title",
    [
        (f"You just backed {TITLE}", TITLE),
        (f"We've collected your pledge for {TITLE}", TITLE),
        (f"Confirmed: You’ve adjusted your pledge for {TITLE}", TITLE),
        (
            "Project Update #3: Mycologist's Primer by Double Proficiency"
            " by Hunters Books",
            "Mycologist's Primer by Double Proficiency",
        ),
        (f"Project Update #9 (Poll): {TITLE} by Acme", TITLE),
        (f"{TITLE} reached its funding goal", TITLE),
        (f"New message about {TITLE}", TITLE),
        ("Back another project by Acme", None),
        (f"Acme just launched {TITLE}", None),
    ],
)
def test_campaign_title(subject: str, title: str | None) -> None:
    assert cp.campaign_title(subject) == title


def test_registry_trusts_only_the_exact_kickstarter_domain() -> None:
    mail = [
        (KS, f"You just backed {TITLE}"),
        ("info@kickstarterproject.com", "You just backed Fake Campaign"),
        ("x@mail.kickstarter.com.evil.io", "You just backed Other Fake"),
    ]
    assert cp.registry(mail) == {TITLE}


def test_creator_domain_links_on_its_own_brand() -> None:
    mail = corpus(*[("hello@widgetron.com", f"Widgetron news {i}") for i in range(3)])
    assert cp.creator_domains(mail, {TITLE}) == {
        ("widgetron.com", TITLE): ("widgetron", 3)
    }


def test_one_mention_is_not_a_creator() -> None:
    mail = corpus(("hello@widgetron.com", "Widgetron news"))
    assert cp.creator_domains(mail, {TITLE}) == {}


def test_brand_in_subject_but_not_domain_is_not_a_creator() -> None:
    mail = corpus(*[("deals@shop.com", f"Widgetron sale {i}") for i in range(5)])
    assert cp.creator_domains(mail, {TITLE}) == {}


def test_common_word_in_domain_is_not_a_creator() -> None:
    mail = corpus(
        *[("bill@cutterwireless.com", f"Your cutter bill {i}") for i in range(5)]
    )
    assert cp.creator_domains(mail, {TITLE}) == {}


def test_retailer_is_never_a_creator(monkeypatch: pytest.MonkeyPatch) -> None:
    mail = corpus(*[("x@widgetron.com", f"Widgetron {i}") for i in range(3)])
    monkeypatch.setattr(cp, "RETAILERS", cp.RETAILERS | {"widgetron.com"})
    assert cp.creator_domains(mail, {TITLE}) == {}


def test_subdomain_sender_counts_as_its_domain() -> None:
    mail = corpus(*[("hi@news.widgetron.com", f"Widgetron news {i}") for i in range(3)])
    assert set(cp.creator_domains(mail, {TITLE})) == {("widgetron.com", TITLE)}


def test_own_volume_does_not_make_a_token_rare() -> None:
    # 2,000 other mails with the token once: log(2000/1) = 7.6, under MIN_IDF.
    # Counting the domain's own 3,000 mails would give log(5000) = 8.5.
    others = [(f"n{i}@example{i % 40}.com", f"Weekly {i}") for i in range(1999)]
    others.append(("x@example.com", "Widgetron mention"))
    own = [("hi@widgetron.com", f"Widgetron {i}") for i in range(3000)]
    assert cp.creator_domains(others + own, {TITLE}) == {}


def test_short_title_token_is_ignored() -> None:
    title = "Zq Pocket Laser"
    mail = corpus(*[("hi@zqtools.com", f"zq deal {i}") for i in range(3)])
    assert cp.creator_domains(mail, {title}) == {}


def test_kickstarter_is_never_a_creator() -> None:
    title = "Kickstarter Gold"
    mail = corpus(*[(KS, f"Kickstarter digest {i}") for i in range(3)])
    assert cp.creator_domains(mail, {title}) == {}


DSN = os.environ.get("CORTEX_TEST_DSN")
SCHEMA = f"t_campaigns_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def conn() -> Iterator[Any]:
    if not DSN:
        pytest.skip("set CORTEX_TEST_DSN to a throwaway Postgres to run these")
    c = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA}")
    with c.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    c.commit()
    try:
        cp.ensure_schema(c)
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
        c.commit()
        c.close()


def test_store_adds_campaigns_keeps_aliases_and_replaces_links(conn: Any) -> None:
    other = "Widgetron W1: Pocket Laser Cutter"
    assert (
        cp.store(conn, {TITLE, other}, {("widgetron.com", TITLE): ("widgetron", 3)})
        == 2
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE triage_campaigns SET alias_of = %s WHERE title = %s", (TITLE, other)
        )
    conn.commit()

    assert cp.store(conn, {TITLE}, {("widgetron.com", other): ("widgetron", 4)}) == 0

    with conn.cursor() as cur:
        cur.execute("SELECT title, alias_of FROM triage_campaigns ORDER BY title")
        assert cur.fetchall() == [(other, TITLE), (TITLE, None)]
        cur.execute("SELECT domain, title, token, mails FROM triage_campaign_domains")
        assert cur.fetchall() == [("widgetron.com", other, "widgetron", 4)]


def test_store_with_no_links_clears_them(conn: Any) -> None:
    cp.store(conn, {TITLE}, {("widgetron.com", TITLE): ("widgetron", 3)})
    cp.store(conn, {TITLE}, {})
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM triage_campaign_domains")
        assert cur.fetchone() == (0,)


def test_store_refuses_an_empty_registry(conn: Any) -> None:
    cp.store(conn, {TITLE}, {("widgetron.com", TITLE): ("widgetron", 3)})
    with pytest.raises(ValueError):
        cp.store(conn, set(), {})
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM triage_campaign_domains")
        assert cur.fetchone() == (1,)
