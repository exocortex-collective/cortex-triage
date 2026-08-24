"""Every service must ensure the shared queue schema on boot.

cortex-rgmk: the exponential-backoff feature was merged months before it first
RAN in production. Its migration existed, was correct, and was a manual CLI
step the deploy flow never invoked -- so this worker and the labeling worker
crash-looped on `column "next_attempt_at" does not exist`.

Nothing was wrong with the migration. Nothing called it. So this asserts the
call is REACHED, not that the import is present: an import satisfies a grep and
prevents nothing, which is the distinction the incident was made of.

The labeling worker is the sharpest case -- it had no schema hook at all, while
claiming from the queue and reporting back, so it depended on the queue's shape
entirely while ensuring none of it.
"""

from __future__ import annotations

import contextlib
import importlib
from unittest.mock import MagicMock, patch

import pytest


def _conn_ctx() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    conn.cursor.return_value.__enter__.return_value = cur

    @contextlib.contextmanager
    def getter():
        yield conn

    return conn, getter


@pytest.mark.parametrize(
    ("module", "cls", "getter_name"),
    [
        ("triage.services.worker", "TriageWorker", "_get_conn"),
        ("triage.services.labeling_worker", "LabelingWorker", "_get_conn"),
        ("triage.services.teach_bot", "TeachBot", "_db_connection"),
    ],
)
def test_boot_ensures_the_shared_queue_schema(
    module: str, cls: str, getter_name: str
) -> None:
    mod = importlib.import_module(module)
    assert hasattr(mod, cls), f"{module}.{cls} moved; this test needs updating"

    conn, getter = _conn_ctx()
    service = object.__new__(getattr(mod, cls))
    setattr(service, getter_name, getter)

    with (
        patch.object(mod, "ensure_queue_schema") as ensure,
        # The service's own DDL runs against a mock and may not complete. What
        # matters is whether the queue schema was ensured.
        contextlib.suppress(Exception),
    ):
        getattr(mod, cls)._ensure_schema(service)

    assert ensure.called, (
        f"{cls}._ensure_schema does not ensure the queue schema -- this is "
        "exactly the shape of cortex-rgmk: a migration nothing calls"
    )
    assert ensure.call_args[0][0] is conn


def test_the_labeling_worker_ensures_before_it_claims() -> None:
    """It had no hook at all. Adding one that never runs would be the same bug
    with more code, so assert run() reaches it before the loop."""
    from triage.services import labeling_worker as mod

    worker = object.__new__(mod.LabelingWorker)
    worker.running = False  # so run() falls straight through the loop
    calls: list[str] = []
    worker._ensure_schema = lambda: calls.append("ensured")  # type: ignore[method-assign]

    with contextlib.suppress(Exception):
        mod.LabelingWorker.run(worker)

    assert calls == ["ensured"], "run() does not ensure the schema before claiming"
