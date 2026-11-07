"""Kickstarter campaign registry and creator domains (cortex-l1s0, step 1).

Labels are the API, so the registry is a closed set of dispatch targets. It is
built only from Kickstarter's own structured mail, matched on the exact sender
domain: a substring match lets lookalike senders (kickstarterproject.com and
six others in this mailbox) add entries.

A creator domain is a sender whose own domain name carries a rare token from
a campaign title. Matching subjects alone links ordinary words ("darkness",
"toolbox") to unrelated senders. Nothing here asks a model.
"""

from __future__ import annotations

import argparse
import math
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

KICKSTARTER = re.compile(r"@kickstarter\.com$", re.IGNORECASE)

# Subjects that name a campaign Devon backed or receives backer mail for.
# Ads ("Back another project by X", "X just launched Y") match none of these.
TITLE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^You just backed\s+(.+?)\s*$",
        r"^We.ve collected your pledge for\s+(.+?)\s*$",
        r"^Confirmed: You.ve adjusted your pledge for\s+(.+?)\s*$",
        r"^Project Update #\d+(?:\s*\([^)]*\))?:\s*(.+)\s+by\s+.+?$",
        r"^(.+?)\s+reached its funding goal\s*$",
        r"^New message about\s+(.+?)\s*$",
    )
]

TOKEN = re.compile(r"[^\W\d_][\w'+-]+", re.UNICODE)

# Measured 2026-11-07 on 98,724 subjects: the five creators score 7.9 to 9.2,
# the nearest non-creator links (heroforge "hero", printablescenery
# "printable") 7.7 and 7.6.
# ponytail: a fixed floor on a growing corpus; re-measure if links drift.
MIN_IDF = 7.8
MIN_HITS = 2
MIN_TOKEN = 4

# Resellers that name creators' products; repetition alone would link them.
RETAILERS = frozenset(
    {
        "amazon.com",
        "microcenter.com",
        "ebay.com",
        "aliexpress.com",
        "walmart.com",
        "matterhackers.com",
        "myminifactory.com",
        "miniaturemarket.com",
        "greenstuffworld.com",
    }
)


def campaign_title(subject: str) -> str | None:
    for pattern in TITLE_PATTERNS:
        if m := pattern.match(subject):
            return m.group(1).strip()
    return None


def registry(mail: Iterable[tuple[str, str]]) -> set[str]:
    """Campaign titles named by Kickstarter's own mail."""
    return {
        title
        for from_addr, subject in mail
        if KICKSTARTER.search(from_addr) and (title := campaign_title(subject))
    }


def tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN.findall(text)}


def domain(from_addr: str) -> str:
    """Registrable-ish domain: the last two labels of the sender's host."""
    host = from_addr.rsplit("@", 1)[-1].lower()
    return ".".join(host.split(".")[-2:])


def creator_domains(
    mail: list[tuple[str, str]], campaigns: set[str]
) -> dict[tuple[str, str], tuple[str, int]]:
    """(domain, campaign) -> (token, mails) for every creator link.

    A domain is a campaign's creator when its own name contains a title token
    (heygears.com, sovol3d.com), its subjects repeat that token, and the token
    is rare in everyone ELSE's mail. Rarity is measured without the domain's
    own mail because a creator that mails often makes its brand common.
    """
    df: Counter[str] = Counter()
    per_domain: dict[str, Counter[str]] = defaultdict(Counter)
    sent: Counter[str] = Counter()
    for from_addr, subject in mail:
        words = tokens(subject)
        d = domain(from_addr)
        df.update(words)
        per_domain[d].update(words)
        sent[d] += 1
    links: dict[tuple[str, str], tuple[str, int]] = {}
    for d, own in per_domain.items():
        if d in RETAILERS or d == "kickstarter.com":
            continue
        name = d.split(".")[0]
        for campaign in campaigns:
            for t in tokens(campaign):
                hits = own[t]
                if len(t) < MIN_TOKEN or t not in name or hits < MIN_HITS:
                    continue
                elsewhere = max(1, df[t] - hits)
                if math.log((len(mail) - sent[d]) / elsewhere) >= MIN_IDF:
                    links[(d, campaign)] = (t, hits)
    return links


# alias_of folds a renamed campaign into one label; it is set by hand
# (Devon decides SAME vs SPLIT) and store() never touches it.
SCHEMA = """
CREATE TABLE IF NOT EXISTS triage_campaigns (
    title TEXT PRIMARY KEY,
    alias_of TEXT REFERENCES triage_campaigns(title),
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS triage_campaign_domains (
    domain TEXT NOT NULL,
    title TEXT NOT NULL REFERENCES triage_campaigns(title) ON DELETE CASCADE,
    token TEXT NOT NULL,
    mails INTEGER NOT NULL,
    PRIMARY KEY (domain, title)
);
"""

MAIL = """
SELECT lower(p.from_addr), p.subject
FROM emails_parsed p JOIN emails_raw r USING (gmail_id)
WHERE r.deleted_at IS NULL AND p.subject IS NOT NULL AND p.from_addr IS NOT NULL
"""


def ensure_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('triage_campaign_schema'))")
        cur.execute(SCHEMA)
    conn.commit()


def derive(conn: Any) -> tuple[set[str], dict[tuple[str, str], tuple[str, int]]]:
    with conn.cursor() as cur:
        cur.execute(MAIL)
        mail = cur.fetchall()
    campaigns = registry(mail)
    return campaigns, creator_domains(mail, campaigns)


def store(
    conn: Any,
    campaigns: set[str],
    links: dict[tuple[str, str], tuple[str, int]],
) -> int:
    """Add new campaigns and replace the creator links. Returns campaigns added.

    Campaigns are only ever added: a title that leaves the mailbox keeps its
    row, its alias and any label already filed under it.
    """
    if not campaigns:
        # An empty read (wrong database, failed query) must not wipe the links.
        raise ValueError("no campaigns derived; refusing to replace creator links")
    with conn.cursor() as cur:
        added = execute_values(
            cur,
            "INSERT INTO triage_campaigns (title) VALUES %s "
            "ON CONFLICT (title) DO NOTHING RETURNING title",
            [(c,) for c in sorted(campaigns)],
            fetch=True,
        )
        cur.execute("DELETE FROM triage_campaign_domains")
        if links:
            execute_values(
                cur,
                "INSERT INTO triage_campaign_domains (domain, title, token, mails) "
                "VALUES %s",
                [(d, c, t, n) for (d, c), (t, n) in sorted(links.items())],
            )
    conn.commit()
    return len(added)


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive the campaign registry")
    parser.add_argument("--write", action="store_true", help="store, not just print")
    args = parser.parse_args()
    conn = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )
    try:
        campaigns, links = derive(conn)
        for c in sorted(campaigns):
            print(f"campaign  {c}")
        for (d, c), (t, n) in sorted(links.items()):
            print(f"creator   {d}  {t} x{n}  {c}")
        if args.write:
            ensure_schema(conn)
            print(f"stored; {store(conn, campaigns, links)} new campaigns")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
