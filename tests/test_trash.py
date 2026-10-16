"""The trash action: any chain can send an email to Gmail Trash."""

from __future__ import annotations

from unittest.mock import MagicMock

from cortex_utils.triage_config import load_rules_from_string
from cortex_utils.triage_config.models import Action, EvaluationResult

from tests.test_footprint import _email
from tests.test_spam import _labeling_worker
from triage.engine.matcher import evaluate_parallel
from triage.engine.merge import merge_actions
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


def test_trashed_email_gets_the_trash_label() -> None:
    worker = _labeling_worker({"g1": {"label": "Cortex/A", "trash": True}}, set())
    worker._get_current_cortex_labels = MagicMock(return_value={"g1": []})
    worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
    worker.gmail.batch_modify.assert_called_once_with(
        ["g1"], ["id:Cortex/A", "id:TRASH"], []
    )


def test_repair_also_trashes() -> None:
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
