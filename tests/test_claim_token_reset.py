"""Every UPDATE that returns a row to 'pending' must strip claimed_by with it.

A reset that clears claimed_at but leaves the token behind hands the next
claimant a row still certified to the previous one. The worker that stalled then
calls complete() with its old token, the token matches, and it retires a job
another worker is mid-flight on -- worse than carrying no token at all, because
the token affirmatively vouches for the wrong claimant.

Reproduced against a live Postgres before this was fixed: complete() returned
True and the row went 'completed' while the second worker still held it.

Covers the stale resets in worker/labeling_worker/teach_bot and labeling
worker's repair re-enqueue, which is not a stale reset but makes a row claimable
again the same way. Source assertions rather than live ones -- triage has no
Postgres test layer, and the invariant is cheap to state and expensive to
rediscover.
"""

import re
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "triage" / "services"

# Any UPDATE of queue whose SET list makes the row claimable again. Stops at
# WHERE so a later clause mentioning claimed_by cannot satisfy the assertion.
TO_PENDING = re.compile(r"UPDATE queue\s+SET status = 'pending'.*?(?=WHERE)", re.S)

# A file drops to 0 when it is ported to ops.claim(): the library then owns the
# stale reset, and cortex-utils tests the invariant at its source. A count going
# DOWN is the port landing; a count going UP needs the guard.
# labeling_worker keeps 1: the repair re-enqueue mutates the payload as it
# re-pends, which no library call does, so it stays hand-written and stays
# guarded here. teach_bot keeps 1 for its own stale reset -- its claim lives in
# cortex_utils.learning.apply and is tracked as cortex-xluv.
EXPECTED = {"worker.py": 0, "labeling_worker.py": 1, "teach_bot.py": 1}


@pytest.mark.parametrize("name,count", sorted(EXPECTED.items()))
def test_returning_a_row_to_pending_also_clears_the_claim_token(
    name: str, count: int
) -> None:
    matches = TO_PENDING.findall((SRC / name).read_text())
    assert len(matches) == count, (
        f"expected {count} queue->pending UPDATE(s) in {name}, found "
        f"{len(matches)} -- a new one needs the same guard, or one has moved"
    )
    for sql in matches:
        assert "claimed_by = NULL" in sql, (
            f"{name} returns a row to pending without clearing claimed_by:\n{sql}\n"
            "The next claimant inherits the previous worker's token."
        )
