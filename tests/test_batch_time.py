"""A batch yields after BATCH_SECONDS and polls signals between jobs
(triage-1702)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, call

import pytest
from cortex_utils.triage_config.models import RulesConfig

from triage.services import worker as worker_module

JOBS = [{"id": n, "payload": {"gmail_id": f"g{n}"}} for n in (1, 2, 3)]


def _worker(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> Any:
    w = worker_module.TriageWorker(MagicMock(), RulesConfig(), MagicMock(), MagicMock())
    conn = MagicMock()
    w._get_conn = contextmanager(lambda: iter([conn]))  # type: ignore[method-assign]
    w._conn = conn
    monkeypatch.setattr(w, "_should_run_maintenance", lambda: False)
    monkeypatch.setattr(w, "_claim_jobs", MagicMock(return_value=list(JOBS)))
    monkeypatch.setattr(w, "process_job", MagicMock(return_value=True))
    monkeypatch.setattr(w, "_check_and_process_signals", MagicMock())
    monkeypatch.setattr(worker_module, "release", MagicMock(return_value=True))
    times = iter(clock)
    monkeypatch.setattr(worker_module.time, "monotonic", lambda: next(times))
    return w


def test_a_batch_past_its_budget_hands_the_rest_back(monkeypatch) -> None:
    budget = worker_module.BATCH_SECONDS
    # start, before job 1, before job 2 (over budget)
    w = _worker(monkeypatch, [0.0, 0.0, budget + 1])
    assert w._process_batch() == (1, 0)
    assert w.process_job.call_args_list == [call(JOBS[0])]
    assert worker_module.release.call_args_list == [
        call(w._conn, 2, 0, w._claim_token),
        call(w._conn, 3, 0, w._claim_token),
    ]
    w._check_and_process_signals.assert_called_once()


def test_a_batch_within_its_budget_runs_whole_and_polls_between_jobs(
    monkeypatch,
) -> None:
    w = _worker(monkeypatch, [0.0, 1.0, 2.0, 3.0])
    assert w._process_batch() == (3, 0)
    assert w.process_job.call_count == 3
    worker_module.release.assert_not_called()
    assert w._check_and_process_signals.call_count == 3


def test_a_shutdown_polled_mid_batch_hands_the_rest_back(monkeypatch) -> None:
    w = _worker(monkeypatch, [0.0, 0.0, 1.0, 2.0])

    def stop() -> None:
        w.running = False

    w._check_and_process_signals.side_effect = stop
    assert w._process_batch() == (1, 0)
    assert [c.args[1] for c in worker_module.release.call_args_list] == [2, 3]


def test_a_failed_release_does_not_stop_the_rest(monkeypatch) -> None:
    w = _worker(monkeypatch, [0.0, 0.0, worker_module.BATCH_SECONDS + 1])
    worker_module.release.side_effect = [RuntimeError("db gone"), True]
    assert w._process_batch() == (1, 0)
    assert [c.args[1] for c in worker_module.release.call_args_list] == [2, 3]


def test_the_budget_is_about_a_minute() -> None:
    """Long enough to amortise a claim, short enough that live mail and a config
    reload are never held behind a whole LLM-heavy batch."""
    assert 10 <= worker_module.BATCH_SECONDS <= 120
