# merge-semantics-reviewer
model: sonnet
verification: mutation
**Checks:** Action-merge correctness: None (abstain) vs False (vote), union/conservative/aggressive/veto rules, rule changes that cancel other chains' actions.
**Spawn when:** diff touches `src/triage/engine/merge.py`, `matcher.py`, `rules.py`, `services/worker.py` (default_label fallback), a cortex-utils pin bump touching Action/merge, or `config/rules*.yaml` / `examples/`; skip otherwise.

**Your focus:** the parallel-chain action merge. Every `auto_run` chain votes; the result is merged per field.
Rules: `label/add_label/remove_label` union; `archive/mark_read` = `all()` of non-None opinions; `star/important` = `any()`; `force_*` vetoes gated on truthiness and applied last; fallback mappings and `default_label` fire only if nothing produced a label.

**What to flag**
1. A chain/rule/default Action that sets `archive`/`mark_read` to `False` where it means "no opinion" (False is a vote; it suppresses other chains). Fix: leave unset (`None`).
2. Code that converts `None` to `False` (or truthiness-tests an opinion flag, `if action.archive:`) in merge or the worker's default/fallback assembly.
3. A change to merge that alters a field's rule class (union/conservative/aggressive/veto) without a test per field in the same PR.
4. A cortex-utils pin bump that adds an Action field with no entry in `merge_actions` or no test of abstain vs vote.
5. Changes to default_label/fallback ordering that let a label-less main-chain match suppress fallbacks (commit 0f5caae class).
6. `evaluate()` (serial path) used or extended for production behaviour; the worker calls `evaluate_parallel`.

**Proof:** the existing suite does NOT pin these rules (flipping `all`/`any` in `merge.py` leaves every test green), so a green run proves nothing. Prove it in a scratch script instead: build two `EvaluationResult`s (matched, `Action(archive=True)` and `Action(archive=False)`) and assert `merge_actions(...).archive is False`; likewise `None` + `True` gives `True` and `None` + `None` gives `None`. Run it against the PR's merge.py (the assertion must be able to fail), and report any rule the PR changes or breaks. A changed rule with no NEW test in the PR is the finding.

**Do NOT flag**
- `force_*` flags using truthiness (intended).
- `archive: false` that deliberately forces a dissent vote, when a comment or test says so.
- Example YAML under `examples/` that shows null fields.
- Pre-existing issues in files this PR did not touch.
