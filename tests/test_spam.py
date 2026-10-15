"""Spam is out of the pipeline (cortex-kvyn).

Triage never classifies a message in Gmail's SPAM, and the labeling worker
strips managed labels from one. Both are driven through their real entry
points; only the database and Gmail are faked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from cortex_utils.triage_config.models import Email


def _email(label_ids: list[str]) -> Email:
    return Email(
        id=1,
        gmail_id="g1",
        from_addr="billing@example.com",
        to_addrs=["me@example.com"],
        subject="Your payment failed",
        body="body",
        label_ids=label_ids,
    )


def _triage_worker() -> Any:
    from triage.services.worker import TriageWorker

    return TriageWorker(MagicMock(), MagicMock(), MagicMock(), MagicMock())


class TestTriageSkipsSpam:
    def _run(self, label_ids: list[str]) -> tuple[bool, Any, Any, Any]:
        worker = _triage_worker()
        worker._get_email_data = MagicMock(return_value=_email(label_ids))
        worker._store_classification = MagicMock()
        worker._complete_job = MagicMock()
        worker._fail_job = MagicMock()
        with patch(
            "triage.services.worker.evaluate_parallel", return_value=(None, [])
        ) as evaluate:
            ok = worker.process_job({"id": 7, "payload": {"gmail_id": "g1"}})
        return ok, evaluate, worker._store_classification, worker._complete_job

    def test_spam_is_completed_without_rules_or_classification(self) -> None:
        ok, evaluate, store, complete = self._run(["SPAM", "UNREAD"])
        assert ok is True
        evaluate.assert_not_called()
        store.assert_not_called()
        complete.assert_called_once_with(7)

    def test_non_spam_is_evaluated_and_classified(self) -> None:
        ok, evaluate, store, complete = self._run(["INBOX", "UNREAD"])
        assert ok is True
        evaluate.assert_called_once()
        store.assert_called_once()
        complete.assert_called_once_with(7)


class _FakeCursor:
    """Answers the labeling worker's queries from fixed tables."""

    def __init__(self, classifications: dict[str, dict], spam: set[str]) -> None:
        self.classifications = classifications
        self.spam = spam
        self._rows: list[tuple] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "label_prefix" in sql:
            self._rows = [("Cortex",)]
        elif "FROM classifications" in sql:
            ids = params[0]
            self._rows = [(g, a) for g, a in self.classifications.items() if g in ids]
        elif "'SPAM' = ANY(label_ids)" in sql:
            ids = params[0]
            self._rows = [(g,) for g in ids if g in self.spam]
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return self._rows


def _labeling_worker(classifications: dict[str, dict], spam: set[str]) -> Any:
    from triage.services.labeling_worker import LabelingWorker

    gmail = MagicMock()
    gmail.get_label_id.side_effect = lambda name: f"id:{name}"
    gmail.get_existing_label_id.side_effect = lambda name: f"id:{name}"
    gmail.batch_modify.return_value = True
    worker = LabelingWorker(MagicMock(), gmail)
    conn = MagicMock()
    conn.cursor.return_value = _FakeCursor(classifications, spam)
    pool = MagicMock()
    pool.getconn.return_value = conn
    worker._pool = pool
    worker._complete_jobs = MagicMock()
    worker._fail_jobs = MagicMock()
    return worker


TXN = {
    "add_label": ["Cortex/Transactional/Subscription"],
    "archive": True,
    "mark_read": True,
}


class TestLabelingStripsSpam:
    def _batch(self, spam: set[str], current: list[str]) -> Any:
        from triage.services.labeling_worker import LabelingJob

        worker = _labeling_worker({"g1": TXN}, spam)
        worker._get_current_cortex_labels = MagicMock(return_value={"g1": current})
        worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
        return worker

    def test_spam_loses_managed_labels_and_keeps_system_labels(self) -> None:
        worker = self._batch({"g1"}, ["Cortex/Transactional/Subscription"])
        worker.gmail.batch_modify.assert_called_once_with(
            ["g1"], [], ["id:Cortex/Transactional/Subscription"]
        )
        worker._complete_jobs.assert_called_once_with([1])

    def test_spam_without_a_classification_is_still_stripped(self) -> None:
        from triage.services.labeling_worker import LabelingJob

        worker = _labeling_worker({}, {"g1"})
        worker._get_current_cortex_labels = MagicMock(
            return_value={"g1": ["Cortex/Uncategorized"]}
        )
        worker._process_normal_batch([LabelingJob(job_id=1, gmail_id="g1")])
        worker.gmail.batch_modify.assert_called_once_with(
            ["g1"], [], ["id:Cortex/Uncategorized"]
        )

    def test_non_spam_follows_its_classification(self) -> None:
        worker = self._batch(set(), [])
        worker.gmail.batch_modify.assert_called_once_with(
            ["g1"], ["id:Cortex/Transactional/Subscription"], ["id:INBOX", "id:UNREAD"]
        )
