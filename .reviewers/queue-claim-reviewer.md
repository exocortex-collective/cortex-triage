# queue-claim-reviewer
model: sonnet
verification: mixed
**Checks:** Queue claim-token discipline: stale resets that keep claimed_by, complete/fail_or_retry called without the claim token, wrong fail_or_retry import, new hand-written claim SQL.
**Spawn when:** diff touches `src/triage/services/{worker,labeling_worker,teach_bot}.py` or any SQL containing `UPDATE queue`, `claimed_by`, `claimed_at`, `FOR UPDATE SKIP LOCKED`; skip otherwise.

**Your focus:** a job row has exactly one claimant, proven by `claimed_by`. A reset that leaves the token lets the stalled worker later `complete()` a job another worker is running.

**What to flag**
1. Any `UPDATE queue SET status = 'pending'` that does not also set `claimed_by = NULL` (and `claimed_at`) before WHERE. Also update `EXPECTED` counts in `tests/test_claim_token_reset.py`: a count going UP needs the guard.
2. `complete(...)` / `fail_or_retry(...)` called without the worker's claim token, or `retry.fail_or_retry` imported in worker.py/labeling_worker.py where `ops.fail_or_retry` (which matches the claim) is meant.
3. New hand-written claim CTE where `ops.claim()` fits; a claim that sets `claimed_at` but not `claimed_by`.
4. A handler path (return/continue/except) that leaves a claimed job neither completed, failed nor retried.
5. Ignoring the False return of `complete()` (claim lost) as success.

**Proof:** mutate in a scratch copy (never the working tree): remove `claimed_by = NULL` from one reset, run `uv run pytest tests/test_claim_token_reset.py`, print counts. To show a reset is unguarded, run `grep -n "UPDATE queue" -A4 src/triage/services/*.py` and quote the matching lines.

**Do NOT flag**
- teach_bot.py's `retry.fail_or_retry` import and its call without a claim token (its claim lives in `cortex_utils.learning.apply`; cortex-xluv).
- Claim logic inside cortex-utils (reviewed there).
- Resets already guarded and listed in `EXPECTED`.
- Priority/backfill values (queue-priority conventions are not this lens).
