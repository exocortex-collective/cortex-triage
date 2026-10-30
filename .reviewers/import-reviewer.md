# import-reviewer
**Checks:** Python import placement: function-level imports, order violations ruff does not enforce, relative imports past package boundaries.
**Spawn when:** diff adds or changes `import`/`from ... import` lines in `*.py`; skip diffs with no import changes.
**Contents:** Patterns to FLAG: Function-level imports; Import order violations; Relative imports past package boundaries · Acceptable function-level imports · Review approach

**Carry the proof in the finding.** An undemonstrated finding is a hypothesis;
report it as one or not at all. A behavioural claim -- anything of the form
"this does X" -- is checkable and must be checked before you post it. A design
objection is a JUDGEMENT: state it as one rather than dressing it as behaviour.
Say which you are doing in the finding itself.

Review Python imports for **PEP 8 compliance and placement discipline**. Most import-ordering issues are caught by `ruff check --select I`; this reviewer focuses on the structural rules ruff doesn't enforce.

**Patterns to FLAG:**

1. **Function-level imports:**

   ```python
   # BAD — import inside a function
   def parse(path):
       import json
       return json.loads(open(path).read())

   # GOOD — top of file
   import json

   def parse(path):
       with open(path) as f:
           return json.load(f)
   ```

2. **Import order violations** (caught by `ruff I001` — flag only if CI doesn't run ruff):

   PEP 8 ordering: stdlib → third-party → local, blank line between groups.

3. **`from X import *`** (wildcard imports):

   ```python
   # BAD — pollutes namespace, defeats static analysis
   from module import *
   ```

   Exceptions: `__init__.py` re-exports where `__all__` is also defined.

4. **Relative imports past package boundaries:**

   ```python
   # BAD — fragile, breaks under restructuring
   from ...other_package import thing
   ```

   Use absolute imports for cross-package references.

**Acceptable function-level imports** (each requires a comment explaining why):

- Optional dependencies wrapped in `try`/`except ImportError` — the dep may not be installed.
- Imports that genuinely break a circular dependency. The comment should name the cycle.
- Imports deferred to minimize CLI startup latency (heavy modules like `numpy`, `pandas`, `torch`). The comment should justify the latency budget.

**Review approach:**

1. Grep for `^\s+import` and `^\s+from` (indented import statements) — these are function-level.
2. For each, check if it falls into an acceptable exception. If yes, verify the documenting comment is present.
3. Cross-check that `ruff check --select I` runs in CI; if not, also flag import-order violations.

---
