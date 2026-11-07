"""A truncated model answer classifies with rules only (cortex-hox9)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cortex_utils.llm import LLMError, LLMTruncatedError
from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Email

from tests.test_footprint import _converge_once
from triage.services.worker import TriageWorker, evaluate_or_rules_only

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
  negated:
    auto_run: true
    rules:
      - match: {negate: true, subject_intent: is_spam}
        action: {add_label: [Cortex/NotSpam], archive: true}
      - match: {negate: true, any_of: [{all_of: [{subject_intent: is_spam}]}]}
        action: {add_label: [Cortex/NotSpam], archive: true}
intents:
  is_spam: {model: m1, prompt: "Spam? {subject}"}
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


def test_rules_only_skips_a_negated_model_condition() -> None:
    config = load_rules_from_string(CONFIG)
    llm = _llm(LLMTruncatedError("cut off"))
    llm.check_intent.return_value = False
    action, _, _ = evaluate_or_rules_only(config, _email("llm and rule"), llm, [])
    assert action is not None and action.add_label == ["Cortex/Rule"]
    assert not action.archive


def _truncating_worker(monkeypatch: pytest.MonkeyPatch) -> TriageWorker:
    from triage.services import worker as worker_module

    monkeypatch.setattr(worker_module, "register_worker", MagicMock())
    llm = _llm(LLMTruncatedError("cut off"))
    llm.check_intent.return_value = False
    worker = TriageWorker(
        MagicMock(poll_interval=0),
        load_rules_from_string(CONFIG),
        llm,
        MagicMock(),
        config_version=1,
    )
    for name, value in (
        ("_get_email_data", _email("llm and rule")),
        ("_execute_action", True),
        ("_get_body_from_api", ""),
        ("_store_classification", None),
        ("_complete_job", None),
    ):
        monkeypatch.setattr(worker, name, MagicMock(return_value=value))
    return worker


def _assert_stored_without_a_footprint(worker: TriageWorker) -> None:
    stored = worker._store_classification.call_args.args  # type: ignore[attr-defined]
    assert stored[1].action.add_label == ["Cortex/Rule"]
    assert stored[3] is None


def test_process_job_stores_no_footprint_for_a_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _truncating_worker(monkeypatch)
    assert worker.process_job({"id": 1, "payload": {"gmail_id": "g"}})
    _assert_stored_without_a_footprint(worker)


def test_convergence_stores_no_footprint_for_a_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _truncating_worker(monkeypatch)
    _converge_once(worker, monkeypatch)
    _assert_stored_without_a_footprint(worker)
