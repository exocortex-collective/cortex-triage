"""A truncated model answer classifies with rules only (cortex-hox9)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cortex_utils.llm import LLMError, LLMTruncatedError
from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Email

from triage.services.worker import evaluate_or_rules_only

CONFIG = """
version: 1
label_prefix: Cortex
default_label: Uncategorized
chains:
  main:
    auto_run: true
    rules:
      - match: {subject_contains: llm}
        llm: {model: m1, prompt_version: v1}
        routes: {a: {label: A}}
  scan:
    auto_run: true
    rules:
      - match: {subject_contains: rule}
        action: {add_label: [Cortex/Rule]}
"""


def _email(subject: str) -> Email:
    return Email(
        id=1,
        gmail_id="g",
        from_addr="a@example.com",
        to_addrs=["me@example.com"],
        subject=subject,
    )


def _llm(error: Exception) -> MagicMock:
    llm = MagicMock()
    llm.classify.side_effect = error
    llm.classify_with_extraction.side_effect = error
    return llm


def test_truncation_keeps_the_rule_labels_and_reports_it() -> None:
    config = load_rules_from_string(CONFIG)
    llm = _llm(LLMTruncatedError("cut off"))
    path: list[tuple[str, int | None]] = [("stale", 9)]
    action, _, truncated = evaluate_or_rules_only(
        config, _email("llm and rule"), llm, path
    )
    assert truncated
    assert action is not None and action.add_label == ["Cortex/Rule"]
    assert ("stale", 9) not in path
    assert llm.classify.call_count + llm.classify_with_extraction.call_count == 1


def test_a_retryable_error_still_raises() -> None:
    config = load_rules_from_string(CONFIG)
    with pytest.raises(LLMError):
        evaluate_or_rules_only(config, _email("llm"), _llm(LLMError("500")), [])


def test_no_truncation_is_reported_as_none() -> None:
    config = load_rules_from_string(CONFIG)
    llm = MagicMock()
    llm.classify.return_value = ("a", 0.9, "why")
    _, _, truncated = evaluate_or_rules_only(config, _email("llm"), llm, [])
    assert not truncated
