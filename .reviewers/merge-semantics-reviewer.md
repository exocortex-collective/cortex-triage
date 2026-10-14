# merge-semantics-reviewer
model: sonnet
verification: mutation
**Checks:** Action-merge correctness: None (abstain) vs False (vote), union/conservative/aggressive/veto rules, rule changes that cancel other chains' actions.
**Spawn when:** diff touches `src/triage/engine/merge.py`, `matcher.py`, `rules.py`, `src/triage/models/`, Action fields, or `config/rules*.yaml` / `examples/`; skip otherwise.

**Proof:** flips the merge rule or None/False handling in a scratch copy, runs tests/test_matcher.py, and reports whether any test fails; judgement is labelled as such.

**Your focus:** the parallel-chain action merge. Every `auto_run` chain votes; the result is merged per field.
Rules: `label/add_label/remove_label` union; `archive/mark_read` = `all()` of non-None opinions; `star/important` = `any()`; `force_*` vetoes gated on truthiness and applied last; fallback mappings and `default_label` fire only if nothing produced a label.

**What to flag**
1. A chain/rule/default Action that sets `archive`/`mark_read` to `False` where it means "no opinion" (False is a vote; it suppresses other chains). Fix: leave unset (`None`).
2. Code that converts `None` to `False` (or truthiness-tests an opinion flag, `if action.archive:`) in merge, model defaults, or the importer path.
3. A change to merge that alters a field's rule class (union/conservative/aggressive/veto) without a test per field.
4. A new Action field with no entry in `merge_actions` or no test of abstain vs vote.
5. Changes to default_label/fallback ordering that let a label-less main-chain match suppress fallbacks (commit 0f5caae class).
6. `evaluate()` (serial path) used or extended for production behaviour; the worker calls `evaluate_parallel`.

**Proof:** mutate the field's merge expression in a scratch copy (never the working tree) and run `uv run pytest tests/test_matcher.py tests/test_rules.py`; print the test count. No test fails = the rule is unpinned: report as the finding.

**Do NOT flag**
- `force_*` flags using truthiness (intended).
- `archive: false` that deliberately forces a dissent vote, when a comment or test says so.
- Example YAML under `examples/` that shows null fields.
- Pre-existing issues in files this PR did not touch.
