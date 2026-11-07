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
import re
import secrets
from typing import Any

from cortex_utils.triage_config.models import RulesConfig
from psycopg2.extras import execute_values

from triage.services import trash_gate

KINDS = ("uncategorized_sender", "low_confidence")
SAMPLE_SIZE = 5
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")

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

LOW_CONFIDENCE = f"""
SELECT s.* FROM (
    SELECT lower(p.from_addr) AS addr, COUNT(*) AS volume, {_SAMPLE} AS sample,
           MIN(c.llm_confidence) AS confidence,
           (array_agg(c.llm_category ORDER BY c.classified_at DESC))[1]
               AS llm_category,
           (array_agg(c.llm_reasoning ORDER BY c.classified_at DESC))[1]
               AS reasoning,
           (array_agg(c.action_taken ORDER BY c.classified_at DESC))[1] AS action
    FROM classifications c
    JOIN emails_parsed p ON p.gmail_id = c.gmail_id
    WHERE c.llm_confidence < %(threshold)s AND p.from_addr IS NOT NULL
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
    """Model text bound for a human: no control characters, one line, capped."""
    if text is None:
        return None
    return " ".join(_CONTROL.sub(" ", text).split())[:limit] or None


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


def generate(
    conn: Any, config: RulesConfig, min_volume: int, threshold: float
) -> dict[str, int]:
    """Open or refresh one question per unsettled sender, close the stale ones,
    commit; return how many are open per kind."""
    default_label = json.dumps([f"{config.label_prefix}/{config.default_label}"])
    allowed = configured_labels(config)
    with conn.cursor() as cur:
        # The gateway answers under this lock, so a question answered while we
        # read cannot be re-opened by the insert below.
        cur.execute(trash_gate.LOCK)
        cur.execute(
            UNCATEGORIZED,
            {
                "kind": "uncategorized_sender",
                "default_label": default_label,
                "min_volume": min_volume,
            },
        )
        found: dict[str, list[tuple[Any, ...]]] = {
            "uncategorized_sender": [
                (addr, volume, sample, None, None, None, None)
                for addr, volume, sample in cur.fetchall()
            ]
        }
        cur.execute(LOW_CONFIDENCE, {"kind": "low_confidence", "threshold": threshold})
        found["low_confidence"] = [
            (addr, volume, sample, proposal(action, allowed), category, conf, why)
            for addr, volume, sample, conf, category, why, action in cur.fetchall()
        ]
        for kind, rows in found.items():
            _store(cur, kind, rows)
    conn.commit()
    return {kind: len(rows) for kind, rows in found.items()}


def _store(cur: Any, kind: str, rows: list[tuple[Any, ...]]) -> None:
    # An open question whose sender is now settled, or below the bar, goes away.
    cur.execute(
        "DELETE FROM triage_questions WHERE kind = %s AND status = 'open'"
        " AND NOT (from_addr = ANY(%s))",
        (kind, [r[0] for r in rows]),
    )
    if not rows:
        return
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
