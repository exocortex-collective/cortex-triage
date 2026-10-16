"""Footprints: which part of the config decided a classification (triage-70be)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Email, RulesConfig

from triage.engine.footprint import build_footprint, chain_hashes, is_affected
from triage.engine.matcher import evaluate_parallel

BASE = """
version: 1
label_prefix: Cortex
default_label: Uncategorized
intents:
  is_x: {model: m1, prompt: "Is it x? {subject}"}
  unused: {model: m1, prompt: "Unused? {subject}"}
chains:
  main:
    auto_run: true
    rules:
      - match: {from_addr: jump@example.com}
        jump: sub
      - match: {subject_contains: hit}
        action: {label: Hit}
      - match: {email_intent: is_x}
        action: {label: X}
  sub:
    rules:
      - match: {subject_contains: deep}
        action: {label: Deep}
"""


def _email(from_addr: str, subject: str) -> Email:
    return Email(
        id=1,
        gmail_id="g",
        from_addr=from_addr,
        to_addrs=["me@example.com"],
        subject=subject,
    )


def _path(
    config: RulesConfig, from_addr: str, subject: str
) -> list[tuple[str, int | None]]:
    llm = MagicMock()
    llm.check_email_intent.return_value = False
    path: list[tuple[str, int | None]] = []
    evaluate_parallel(config, _email(from_addr, subject), llm, path)
    return path


@pytest.mark.parametrize(
    ("from_addr", "subject", "expected"),
    [
        ("a@example.com", "a hit", [("main", 1)]),
        ("jump@example.com", "deep", [("sub", 0), ("main", 0)]),
        ("jump@example.com", "a hit", [("sub", None), ("main", 1)]),
        ("a@example.com", "nothing", [("main", None)]),
    ],
    ids=["direct", "via-jump", "jump-missed", "no-match"],
)
def test_the_path_records_where_each_chain_stopped(
    from_addr: str, subject: str, expected: list[tuple[str, int | None]]
) -> None:
    assert _path(load_rules_from_string(BASE), from_addr, subject) == expected


def _affected(old_yaml: str, new_yaml: str, from_addr: str, subject: str) -> bool:
    old, new = load_rules_from_string(old_yaml), load_rules_from_string(new_yaml)
    fp = build_footprint(old, chain_hashes(old), _path(old, from_addr, subject))
    return is_affected(fp, new, chain_hashes(new))


ZZ = "      - match: {subject_contains: zz}\n        action: {label: Z}\n"
INTENT_RULE = "      - match: {email_intent: is_x}"
HIT_RULE = "      - match: {subject_contains: hit}"
AFTER_HIT = BASE.replace(INTENT_RULE, ZZ + INTENT_RULE)
BEFORE_HIT = BASE.replace(HIT_RULE, ZZ + HIT_RULE)
M2 = BASE.replace("is_x: {model: m1", "is_x: {model: m2")
UNUSED_M2 = BASE.replace("unused: {model: m1", "unused: {model: m2")
OTHER_DEFAULT = BASE.replace("default_label: Uncategorized", "default_label: Other")
SUB_AUTO = BASE.replace("  sub:\n", "  sub:\n    auto_run: true\n")
DEEPER = BASE.replace("{label: Deep}", "{label: Deeper}")
A, J = "a@example.com", "jump@example.com"


@pytest.mark.parametrize(
    ("new", "from_addr", "subject", "affected"),
    [
        (BASE, A, "a hit", False),
        (AFTER_HIT, A, "a hit", False),
        (BEFORE_HIT, A, "a hit", True),
        (AFTER_HIT, A, "nothing", True),
        (M2, A, "nothing", True),
        (M2, A, "a hit", False),
        (UNUSED_M2, A, "nothing", False),
        (OTHER_DEFAULT, A, "a hit", True),
        (SUB_AUTO, A, "a hit", True),
        (DEEPER, J, "deep", True),
        (DEEPER, A, "a hit", False),
    ],
    ids=[
        "unchanged",
        "rule-added-after-stop",
        "rule-added-before-stop",
        "rule-added-to-exhausted-chain",
        "intent-changed-and-reached",
        "intent-changed-not-reached",
        "unreferenced-intent-changed",
        "default-label-changed",
        "chain-made-auto-run",
        "jumped-chain-changed",
        "unentered-chain-changed",
    ],
)
def test_is_affected_matches_what_a_change_can_reach(
    new: str, from_addr: str, subject: str, affected: bool
) -> None:
    assert _affected(BASE, new, from_addr, subject) is affected


def test_a_missing_or_foreign_footprint_is_always_affected() -> None:
    config = load_rules_from_string(BASE)
    assert is_affected(None, config, chain_hashes(config))
    assert is_affected({"v": 999, "g": "x", "c": []}, config, chain_hashes(config))


def test_the_worker_stores_the_footprint_of_what_it_evaluated() -> None:
    from triage.services.worker import TriageWorker

    config = load_rules_from_string(BASE)
    llm = MagicMock()
    llm.check_email_intent.return_value = False
    worker = TriageWorker(MagicMock(), config, llm, MagicMock())
    worker._get_email_data = MagicMock(  # type: ignore[method-assign]
        return_value=_email("a@example.com", "a hit")
    )
    worker._execute_action = MagicMock(return_value=True)  # type: ignore[method-assign]
    worker._store_classification = MagicMock()  # type: ignore[method-assign]
    worker._complete_job = MagicMock()  # type: ignore[method-assign]
    assert worker.process_job({"id": 1, "payload": {"gmail_id": "g"}})
    footprint = worker._store_classification.call_args.args[3]
    assert footprint == build_footprint(config, chain_hashes(config), [("main", 1)])
    assert not is_affected(footprint, config, chain_hashes(config))
