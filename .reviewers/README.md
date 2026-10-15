# Guidelines

A reviewer pack for Python projects.

## Verification: every reviewer owes a demonstration

**Execute. Do not read.** Every reviewer owes a demonstration; what differs is
which kind. A finding nobody demonstrated is a hypothesis, and is reported as
one or not at all.

Each reviewer declares the KIND on the FIRST LINE of its own body:

    verification: mutation           change the code, watch the check fail
    verification: evidence-query     run the query that would produce the
                                     evidence the claim depends on, report the n
    verification: judgement          no demonstration exists to run -- say so
    verification: mixed              the honest answer for most

A missing line is **undeclared** -- a finding about this roster, not about the
code. Greppable as `^verification:`, and the round summary carries the kind
(`foo=ok(mutation)`, `bar=ok(evidence-query)`) so a reader weights findings
without opening a charter.

**The tempting declaration is `none`; the second most tempting is
`evidence-query` on a reviewer that could mutate.** Both are the disease. The
honest test is not "what could this reviewer do" but "what did it do last time
it filed a finding" -- which for this roster is answerable from PR history.

**THE OBLIGATION SITS ON THE FINDING, not the reviewer.** A reviewer's line says
what it TYPICALLY owes; it never licenses a finding to skip its proof.

**Judgement is a KIND, not an exemption.** "This abstraction is wrong", "these
two arguments are swappable" -- no mutation exists to run, so state it AS
judgement rather than avoiding it. Two of the most valuable findings in this
estate were judgements. What the rule forbids is a judgement DRESSED AS a
behavioural claim: "this exits 0 on failure" is checkable and must be checked;
"this design is confusing" is not and must say so.

**If you declare `mixed`, be able to name which recent findings were which** --
otherwise it is undeclared wearing a better word. The evidence comments in
each reviewer's file do exactly that, and are the audit trail.

**Review the fix as suspiciously as the thing it replaced, and your own most
suspiciously of all.** On PR #80 every round's finding lived inside the previous
round's fix, three rounds running -- the round-3 P2 was the round-2 fix treating
a symptom rather than removing the swallow.

**Where the rule comes from, measured rather than asserted.** cortex-triage
PR #80, 2026-09-17: nine findings across the first two rounds, eight accepted.
The one declined was the only one asserting something without running it -- it
claimed CI gates on `ruff format` when CI runs `black`, and the two disagree, so
complying would have broken the build.

**Three ways a demonstration lies, all seen on that PR in one day:**

1. **The mutant dies at collection.** A `SyntaxError` or import failure catches
   nothing. Distinguish `N errors` from `N failed` -- but a *fixture* assertion
   legitimately surfaces as errors, so read the error text before dismissing it.
2. **The denominator moves.** Print the total test count every run. Restoring an
   uncommitted file with `git checkout --` reverts it to HEAD and deletes the
   work under test; save and rewrite bytes instead.
3. **The instrument cannot see the thing.** A point-in-time snapshot cannot
   observe per-request behaviour; a grep for the shape you expect a fix to take
   cannot find a fix written another way. Ask what would be true if the claim
   were false, and whether your instrument could detect it.

**Cross-language caution.** These examples are Python. The shapes do not
translate for free -- a bash `EXIT` trap whose command fails does *not* mask the
script's exit status, so the exception-swapping hazard has no direct analogue
there. Verify the mechanism in the target language before writing a charter
against it.

## How the pack runs

Do not run these reviewers directly; invoke `pr-review-loop`, which owns posting, validation, replies, retirement, CI gating and exit conditions.

Each reviewer runs independently and reports findings without coordination. A reviewer's silence on something is not an endorsement — it just means that reviewer didn't see anything in its scope.

Each reviewer's `**Spawn when:**` header is authoritative for its scope.

## Tooling assumed in CI

Reviewers do not duplicate work already done by CI. Each Python project running this pack should have these in CI:

- `ruff check .` — lints, import sorting (`I`), unused imports (`F401`), unused vars (`F841`), and more
- `ruff format --check .` — formatter (Black-compatible)
- `mypy .` or `pyright .` — type checker, strict mode preferred
- `pytest -v` with `--cov` — tests with coverage tracking
- `bandit -r .` — security linter (catches `shell=True`, weak crypto, etc.)
- `vulture .` — dead-code detector
- `ruff check --select C901 --max-complexity 10 .` — mccabe complexity ceiling (see `complexity-reviewer`)

A missing tool is itself a **P1** finding on the first PR that brushes against its scope. For example, if CI doesn't run `bandit` and the PR touches `subprocess`, `external-process-reviewer` flags the CI gap as well as the code.

## Severity convention

Every finding must be tagged with a beads-style priority:

| Priority | Disposition | Examples |
|----------|-------------|----------|
| **P1** | Blocking — must fix before merge | Silent exception swallow, unsynchronized write to state shared across threads, missing `@dataclass` on a class using `field()`, leaked DB connection, command injection via `shell=True` |
| **P2** | Should fix in this PR | Missing `raise ... from`, missing `subprocess` timeout, missing tests for new branches, mccabe complexity floor violation, function-level import |
| **P3** | Advisory — deferrable with a beads ticket | Complexity heuristic findings, dead code, clarity nits, markdown structural suggestions |

**Default severity per reviewer:**

- `resource-leak-reviewer`: **P1** by default.
- `concurrency-reviewer`: **P1** for a race or deadlock it can show; otherwise **P2**.
- `error-handling-reviewer`: **P2** by default; **P1** for silent exception swallows and `return` in `finally`.
- `logging-reviewer`: **P2** by default; **P1** for sensitive data in log messages.
- `dataclass-decorator-reviewer`: **P1** by default (it's a correctness bug that escapes type checkers).
- `test-coverage-reviewer`, `external-process-reviewer`, `importlib-resources-reviewer`, `typing-consistency-reviewer`: **P2** by default; specific flags inside each reviewer may be P1.
- `complexity-reviewer`: **P2** for mccabe floor violations; **P3** for heuristic findings.
- `import-reviewer`, `dead-code-reviewer`, `clarity-reviewer`: **P3** by default.

A reviewer may promote or demote a specific finding from its default, but must state why.

## Output format

Findings must be structured. Use this template:

```
[<reviewer>] [<severity>] <one-line title>

File: path/to/file.py:LINE
Quote:
    <1-5 lines of code or text being flagged>

Issue: <one or two sentences on what's wrong>
Suggested fix:
    <concrete diff or rewritten code/text>
Reason (optional): <only if not obvious>
```

Example:

```
[concurrency-reviewer] [P2] Lock held across a network call

File: cache/store.py:24
Quote:
    with self._lock:
        if key not in self._data:
            self._data[key] = requests.get(url, timeout=30).json()

Issue: Every thread calling get() waits behind one HTTP request, up to 30s each.
Suggested fix: Fetch outside the lock, then store with self._data.setdefault(key, value)
under the lock so a concurrent fetch cannot overwrite it.
```

Structured findings are diff-able, easy to triage, and easy to deduplicate when multiple reviewers flag the same line.

## Deferring findings with beads

To defer a P2 or P3 finding to a follow-up:

1. Create a beads ticket capturing reviewer name, severity, file, and quote.
2. Link the ticket in the PR description or as a reply to the reviewer's comment.
3. The reviewer accepts the deferral only when the beads ticket exists.

**P1 findings are not deferrable** — they must be fixed in-PR.
