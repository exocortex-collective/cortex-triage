"""Open per-sender questions for Devon to answer (cortex-kaij.4).

uncategorized_sender: a sender with at least min_volume emails that fell
through to the default label and has no mapping. Answering it writes a mapping
(gateway), so one answer settles all of that sender's mail.

low_confidence: a sender whose mail an llm route classified below the
confidence threshold. No active rule records a confidence today, so this kind
is inert until one does.

The model only ever proposes. llm_category, reasoning and proposed come from a
model that read outside mail, so reasoning is control-stripped and a proposed
label survives only if the config itself writes that label
(docs/agent-isolation.md). Nothing acts on a question until it is answered.

A sender whose question was answered, in any scope, is not asked again.
"""

from __future__ import annotations

import json
import secrets
import unicodedata
from collections import Counter
from typing import Any

from cortex_utils.triage_config.models import RulesConfig
from psycopg2.extras import execute_values

from triage.services import trash_gate

KINDS = ("uncategorized_sender", "low_confidence")
SAMPLE_SIZE = 5

_SAMPLE = f"""array_to_json((array_agg(json_build_object(
                 'gmail_id', c.gmail_id, 'subject', p.subject,
                 'date_header', p.date_header)
               ORDER BY p.date_header DESC NULLS LAST))[1:{SAMPLE_SIZE}])"""

# Senders already settled: a live mapping, or a question answered before.
_UNSETTLED = """
    NOT EXISTS (SELECT 1 FROM triage_email_mappings m
                WHERE m.email_address = s.addr AND m.deleted_at IS NULL)
    AND NOT EXISTS (SELECT 1 FROM triage_questions q
                    WHERE q.kind = %(kind)s AND q.from_addr = s.addr
                      AND q.status = 'answered')
"""

UNCATEGORIZED = f"""
SELECT s.* FROM (
    SELECT lower(p.from_addr) AS addr, COUNT(*) AS volume, {_SAMPLE} AS sample
    FROM classifications c
    JOIN emails_parsed p ON p.gmail_id = c.gmail_id
    WHERE c.action_taken->'add_label' = %(default_label)s::jsonb
      AND p.from_addr IS NOT NULL
    GROUP BY 1
    HAVING COUNT(*) >= %(min_volume)s
) s
WHERE {_UNSETTLED}
"""

# The fields shown with the confidence all come from that least-sure email.
_LEAST_SURE = "c.llm_confidence, c.classified_at DESC"

LOW_CONFIDENCE = f"""
SELECT s.* FROM (
    SELECT lower(p.from_addr) AS addr, COUNT(*) AS volume, {_SAMPLE} AS sample,
           MIN(c.llm_confidence) AS confidence,
           (array_agg(c.llm_category ORDER BY {_LEAST_SURE}))[1] AS llm_category,
           (array_agg(c.llm_reasoning ORDER BY {_LEAST_SURE}))[1] AS reasoning,
           (array_agg(c.action_taken ORDER BY {_LEAST_SURE}))[1] AS action
    FROM classifications c
    JOIN emails_parsed p ON p.gmail_id = c.gmail_id
    WHERE c.llm_confidence < %(threshold)s::real AND p.from_addr IS NOT NULL
    GROUP BY 1
) s
WHERE {_UNSETTLED}
"""


def configured_labels(config: RulesConfig) -> set[str]:
    """Every label the config writes literally, prefixed as the matcher emits it."""
    labels = [config.default_label]
    labels += [m.label for m in config.priority_email_mappings.values()]
    labels += [m.label for m in config.fallback_email_mappings.values()]
    for chain in config.chains.values():
        for rule in chain.rules:
            actions = [rule.action] if rule.action else []
            actions += list((rule.routes or {}).values())
            labels += [a.label for a in actions if a.label]
    return {f"{config.label_prefix}/{lbl}" for lbl in labels if "{{" not in lbl}


def clean(text: str | None, limit: int = 500) -> str | None:
    """Model text bound for a human: no control or format characters (bidi
    overrides, zero-width), one line, capped."""
    if text is None:
        return None
    kept = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text
    )
    return " ".join(kept.split())[:limit] or None


def proposal(action: dict[str, Any] | None, allowed: set[str]) -> dict[str, Any]:
    """The advisory answer, with any label the config does not write dropped."""
    action = action or {}
    adds = action.get("add_label") or []
    labels = [action.get("label")] + (adds if isinstance(adds, list) else [adds])
    label = next((lbl for lbl in labels if lbl in allowed), None)
    return {
        "label": label,
        "archive": bool(action.get("archive")),
        "mark_read": bool(action.get("mark_read")),
    }


# The labels a sender's mail already got from rules or chains, most common first.
SENDER_LABELS = """
SELECT lower(p.from_addr), l.label, COUNT(*),
       COUNT(*) FILTER (WHERE (c.action_taken->>'archive')::boolean),
       COUNT(*) FILTER (WHERE (c.action_taken->>'mark_read')::boolean)
FROM classifications c
JOIN emails_parsed p ON p.gmail_id = c.gmail_id
CROSS JOIN LATERAL jsonb_array_elements_text(
    CASE jsonb_typeof(c.action_taken->'add_label')
         WHEN 'array' THEN c.action_taken->'add_label' ELSE '[]' END) AS l(label)
WHERE lower(p.from_addr) = ANY(%(addrs)s) AND l.label <> %(default)s
GROUP BY 1, 2
ORDER BY 3 DESC
"""

MAPPINGS = """
SELECT email_address, label, archive, mark_read FROM triage_email_mappings
WHERE deleted_at IS NULL
"""


def family(addr: str) -> str:
    """The sender's domain family: the last two labels of the domain, so
    i.forever21.com and forever21.com match."""
    # ponytail: two labels misgroups co.uk-style domains; use a public-suffix
    # list if that ever matters for proposals.
    return ".".join(addr.lower().rpartition("@")[2].split(".")[-2:])


def _evidence(
    cur: Any, config: RulesConfig, addrs: list[str], allowed: set[str]
) -> dict[str, dict[str, Any]]:
    """The best trusted proposal per sender: the one label live mappings for
    its domain family agree on (a person chose it), else the label most of its
    mail already got from rules, if `allowed` holds it. Otherwise none."""
    prefix = config.label_prefix
    by_family: dict[str, Counter[tuple[str, bool, bool]]] = {}
    cur.execute(MAPPINGS)
    for email, label, archive, mark_read in cur.fetchall():
        by_family.setdefault(family(email), Counter())[
            (f"{prefix}/{label}", bool(archive), bool(mark_read))
        ] += 1
    from_rules: dict[str, dict[str, Any]] = {}
    cur.execute(
        SENDER_LABELS,
        {"addrs": addrs, "default": f"{prefix}/{config.default_label}"},
    )
    for addr, label, n, archived, read in cur.fetchall():
        if addr not in from_rules and label in allowed:
            from_rules[addr] = {
                "label": label,
                "archive": archived * 2 > n,
                "mark_read": read * 2 > n,
            }
    proposals = {}
    for addr in addrs:
        votes = by_family.get(family(addr))
        # Only a family that agrees on one label: gmail.com's mappings are
        # people, and a majority of them is not evidence about a stranger.
        if votes and len({label for label, _, _ in votes}) == 1:
            (label, archive, mark_read), _ = votes.most_common(1)[0]
            proposals[addr] = {
                "label": label,
                "archive": archive,
                "mark_read": mark_read,
            }
        elif addr in from_rules:
            proposals[addr] = from_rules[addr]
    return proposals


def generate(
    conn: Any, config: RulesConfig, min_volume: int, threshold: float
) -> dict[str, int]:
    """Open or refresh one question per unsettled sender, close the stale ones,
    commit; return how many are open per kind."""
    default_label = json.dumps([f"{config.label_prefix}/{config.default_label}"])
    with conn.cursor() as cur:
        allowed = configured_labels(config) | _mapping_labels(cur, config)
        cur.execute(
            UNCATEGORIZED,
            {
                "kind": "uncategorized_sender",
                "default_label": default_label,
                "min_volume": min_volume,
            },
        )
        uncategorized = cur.fetchall()
        cur.execute(LOW_CONFIDENCE, {"kind": "low_confidence", "threshold": threshold})
        low = cur.fetchall()
        addrs = [r[0] for r in uncategorized] + [r[0] for r in low]
        evidence = _evidence(cur, config, addrs, allowed)
        found: dict[str, list[tuple[Any, ...]]] = {
            "uncategorized_sender": [
                (addr, volume, sample, evidence.get(addr), None, None, None)
                for addr, volume, sample in uncategorized
            ],
            "low_confidence": [
                (
                    addr,
                    volume,
                    sample,
                    _first(proposal(action, allowed), evidence.get(addr)),
                    category,
                    conf,
                    why,
                )
                for addr, volume, sample, conf, category, why, action in low
            ],
        }
        # The gateway answers under this lock, so the answered re-check in
        # _store cannot race an answer. Taken after the slow reads.
        cur.execute(trash_gate.LOCK)
        opened = {kind: _store(cur, kind, rows) for kind, rows in found.items()}
    conn.commit()
    return opened


def _mapping_labels(cur: Any, config: RulesConfig) -> set[str]:
    """Labels a person chose for a live mapping, prefixed as applied."""
    cur.execute(MAPPINGS)
    return {f"{config.label_prefix}/{row[1]}" for row in cur.fetchall()}


def _first(
    own: dict[str, Any], fallback: dict[str, Any] | None
) -> dict[str, Any] | None:
    """A low_confidence question's own classification wins when it names an
    allowed label."""
    return own if own["label"] or fallback is None else fallback


def _store(cur: Any, kind: str, rows: list[tuple[Any, ...]]) -> int:
    cur.execute(
        "SELECT from_addr FROM triage_questions"
        " WHERE kind = %s AND status = 'answered' AND from_addr = ANY(%s)",
        (kind, [r[0] for r in rows]),
    )
    answered = {r[0] for r in cur.fetchall()}
    rows = [r for r in rows if r[0] not in answered]
    # An open question whose sender is now settled, or below the bar, goes away.
    cur.execute(
        "DELETE FROM triage_questions WHERE kind = %s AND status = 'open'"
        " AND NOT (from_addr = ANY(%s))",
        (kind, [r[0] for r in rows]),
    )
    if not rows:
        return 0
    execute_values(
        cur,
        """
        INSERT INTO triage_questions (id, kind, from_addr, volume, sample,
                                      proposed, llm_category, confidence, reasoning)
        VALUES %s
        ON CONFLICT (kind, from_addr) WHERE status = 'open' DO UPDATE
           SET volume = EXCLUDED.volume, sample = EXCLUDED.sample,
               proposed = EXCLUDED.proposed, llm_category = EXCLUDED.llm_category,
               confidence = EXCLUDED.confidence, reasoning = EXCLUDED.reasoning
        """,
        [
            (
                f"q-{secrets.token_hex(6)}",
                kind,
                addr,
                volume,
                json.dumps(sample),
                json.dumps(proposed) if proposed else None,
                clean(category, 100),
                confidence,
                clean(why),
            )
            for addr, volume, sample, proposed, category, confidence, why in rows
        ],
    )
    return len(rows)
