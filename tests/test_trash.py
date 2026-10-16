"""The trash action: any chain can send an email to Gmail Trash."""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Action, EvaluationResult

from tests.test_footprint import _email
from tests.test_spam import _labeling_worker
from triage.engine.matcher import evaluate_parallel
from triage.engine.merge import merge_actions
from triage.services import trash_gate
from triage.services.labeling_worker import LabelingJob


def _merge(*actions: Action) -> Action:
    results = [EvaluationResult(matched=True, action=a, chain="c") for a in actions]
    merged = merge_actions(results)
    assert merged is not None
    return merged


def test_one_chain_saying_trash_wins() -> None:
    assert _merge(Action(label="Cortex/A"), Action(trash=True)).trash is True


def test_no_chain_saying_trash_leaves_it_unset() -> None:
    assert _merge(Action(label="Cortex/A"), Action(trash=False)).trash is None


def test_a_trash_rule_overrides_a_priority_mapping() -> None:
    config = load_rules_from_string(
        """
version: 1
label_prefix: Cortex
chains:
  main:
    rules:
      - match: {from_glob: "*@spam.example"}
        action: {trash: true}
priority_email_mappings:
  hello@spam.example: {label: Retail, archive: true}
"""
    )
    merged, _ = evaluate_parallel(config, _email("hello@spam.example", "hi"))
    assert merged is not None and merged.trash is True


@pytest.fixture
def cleared() -> Iterator[MagicMock]:
    """The gate clears every email it is asked about."""
    with patch(
        "triage.services.trash_gate.gate", side_effect=lambda _conn, ids: set(ids)
    ) as gate:
        yield gate


@pytest.fixture
def held() -> Iterator[MagicMock]:
    """The gate holds every email for confirmation."""
    with patch("triage.services.trash_gate.gate", return_value=set()) as gate:
        yield gate


def test_held_email_keeps_its_other_labels_but_is_not_trashed(held: Any) -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": True}}, set())
    worker._get_current_cortex_labels = MagicMock(return_value={"g1": []})
    worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
    held.assert_called_once()
    assert held.call_args.args[1] == ["g1"]
    worker.gmail.batch_modify.assert_called_once_with(["g1"], ["id:Cortex/A"], [])


def test_repair_also_holds(held: Any) -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": True}}, set())
    worker._get_gmail_current_labels = MagicMock(return_value=["Cortex/A"])
    worker._process_repair_job(LabelingJob(job_id=1, gmail_id="g1", repair=True))
    worker.gmail.batch_modify.assert_called_once_with(["g1"], [], [])


def test_only_trash_requests_reach_the_gate(cleared: Any) -> None:
    worker = _labeling_worker(
        {"g1": {"label": "Cortex/A", "trash": True}, "g2": {"label": "Cortex/B"}},
        set(),
    )
    worker._get_latest_classifications(["g1", "g2"])
    assert cleared.call_args.args[1] == ["g1"]


def test_trashed_email_gets_the_trash_label(cleared: Any) -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": True}}, set())
    worker._get_current_cortex_labels = MagicMock(return_value={"g1": []})
    worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
    worker.gmail.batch_modify.assert_called_once_with(
        ["g1"], ["id:Cortex/A", "id:TRASH"], []
    )


def test_repair_also_trashes(cleared: Any) -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": True}}, set())
    worker._get_gmail_current_labels = MagicMock(return_value=["Cortex/A"])
    worker._process_repair_job(LabelingJob(job_id=1, gmail_id="g1", repair=True))
    worker.gmail.batch_modify.assert_called_once_with(["g1"], ["id:TRASH"], [])


def test_untrashed_email_is_not_trashed() -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": None}}, set())
    worker._get_current_cortex_labels = MagicMock(return_value={"g1": []})
    worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
    worker.gmail.batch_modify.assert_called_once_with(["g1"], ["id:Cortex/A"], [])


def test_repair_does_not_trash_untrashed_email() -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": None}}, set())
    worker._get_gmail_current_labels = MagicMock(return_value=[])
    worker._process_repair_job(LabelingJob(job_id=1, gmail_id="g1", repair=True))
    worker.gmail.batch_modify.assert_called_once_with(["g1"], ["id:Cortex/A"], [])


def test_decide_trashes_only_what_was_approved() -> None:
    for d, s, a in itertools.product(
        (None, "approved", "rejected"),
        (None, "pending", "approved", "trashed", "kept"),
        ("", "x@example.com"),
    ):
        verdict = trash_gate.decide(d, s, a)
        approved = d == "approved" or s in ("approved", "trashed")
        assert (verdict == "trash") == approved, (d, s, a)
        assert verdict != "ask" or (a and d is None), (d, s, a)
