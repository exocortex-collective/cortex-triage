"""A fresh database must not regain the constraint migration 004 drops.

cortex-apd6: `triage_email_mappings` carried an unconditional
`UNIQUE (mapping_type, email_address)` named `unique_email_mapping`. Being
unconditional it counts soft-deleted rows, so a mapping that was deleted could
never be recreated, and it SHADOWS the partial index the importer's upsert is
written to infer:

    ON CONFLICT (mapping_type, email_address) WHERE deleted_at IS NULL

postmark migration 004 drops it. But `ensure_config_schema` here created the table
WITH it inline and runs on every worker start, so every FRESH database was
handed the bug back (cortex-qvnz). `CREATE TABLE IF NOT EXISTS` no-ops on an
existing table, which is why production never showed it and why this was found
by review rather than by an outage.

These assert on the DDL the worker actually EXECUTES, captured from a mocked
connection, rather than on the source text -- the same idiom as
test_queue_schema_boot.py. A grep over the file would also match a comment
about the constraint, including the one this fix added.
"""

from __future__ import annotations

import importlib
import re
from unittest.mock import MagicMock, patch

import pytest


def _executed_ddl() -> str:
    """Every statement ensure_config_schema() hands to the cursor.

    NOT TriageWorker._ensure_schema -- that one ensures classifications,
    patterns and system_state only. The mappings DDL lives in the module-level
    ensure_config_schema(), called from main() on boot. My first version of
    this test pointed at the worker method and asserted the constraint was
    absent from SQL that never mentioned mappings at all: vacuously true. The
    `triage_email_mappings in sql` guard below is what caught it, twice.
    """
    statements: list[str] = []

    def record(sql: object, *a: object, **k: object) -> None:
        statements.append(str(sql))

    cur = MagicMock()
    # BOTH execute paths. executemany is not exotic -- it is the natural way to
    # write a parameterised ALTER, and DDL sent through it was invisible here.
    cur.execute.side_effect = record
    cur.executemany.side_effect = record
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    # `cur` must be its OWN context manager, and conn.cursor() must return it
    # directly. Binding only conn.cursor().__enter__ meant a bare
    # `cur2 = conn.cursor()` -- no `with` -- handed back a fresh unrecorded
    # MagicMock that swallowed its SQL silently.
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur

    mod = importlib.import_module("triage.services.worker")
    cfg = MagicMock()
    # Deliberately NOT wrapped in try/except, and recording the exception was
    # not enough either.
    #
    # ensure_config_schema() issues all of its DDL in ONE execute(): a single
    # 13,155-character blob holding 44 top-level statements. Of its 57
    # semicolons, 12 sit inside three $$ plpgsql bodies and one more inside a
    # `--` comment (worker.py:1846, "existing databases; this line was..."),
    # so 57 - 12 = 45 is one too many -- the count is 44. So the hazard is not
    # a truncated prefix; it is a SECOND call that never reaches the recorder.
    # psycopg2.extras.execute_batch and execute_values go through cur.mogrify
    # rather than the side_effect above, raise on the mock, and carry their
    # DDL away with them.
    #
    # Measured: with an ALTER ... ADD CONSTRAINT routed through execute_batch,
    # the capture is byte-identical to a clean run -- 1 call, 13,155 chars --
    # and the constraint is simply absent. Nothing downstream can notice, so
    # the vacuity guard passes and all three tests pass. The raised TypeError
    # was the only evidence there was, and swallowing it destroyed it.
    with patch.object(mod.psycopg2, "connect", return_value=conn):
        mod.ensure_config_schema(cfg)
    # ";" is what _guard splits on, so it must be what separates captured
    # statements. Inert against today's source, which issues a single
    # execute() -- with one statement the join character never applies. It
    # becomes load-bearing the moment there is more than one, which the
    # capture above now supports: under a two-cursor injection, an
    # ALTER ... ADD CONSTRAINT with no trailing ";" merged into the next
    # statement and borrowed its WHERE. Measured both ways against that
    # injection -- "\n" passed the defect, ";" caught it.
    return ";\n".join(statements)


def _without_comments(sql: str) -> str:
    """Strip `--` comments before matching.

    The fix for this bug documents the removed constraint in a comment, so a
    search over the raw DDL matches the prose explaining the defect rather than
    any executable SQL -- and reports the fix itself as the bug. Exactly the
    trap cortex-utils' arbiter test calls out.
    """
    # WHOLE-LINE comments only. `--.*$` ignores quoting, so a line like
    #     note TEXT DEFAULT 'a -- b', CONSTRAINT unique_email_mapping ...
    # would have the constraint stripped along with the comment and pass.
    # Every comment in this DDL is full-line, so nothing is lost, and a
    # violation sharing a line with a trailing comment stays visible.
    return "\n".join(
        "" if line.lstrip().startswith("--") else line for line in sql.splitlines()
    )


_LIVE = "WHERE deleted_at IS NULL"


@pytest.mark.parametrize(
    ("statement", "passes"),
    [
        (f"UNIQUE (mapping_type, email_address) {_LIVE};", True),
        ("UNIQUE (mapping_type, email_address);", False),
        ("UNIQUE (mapping_type, email_address) WHERE archive = true;", False),
        # mentions deleted_at, under the WRONG predicate: unique over the
        # soft-deleted rows only. Without this, weakening the regex to a
        # bare `deleted_at` survives the whole parametrize.
        ("UNIQUE (mapping_type, email_address) WHERE deleted_at IS NOT NULL;", False),
        # The `;` bound. Without it the window runs on and the statement
        # borrows the NEXT one's predicate -- the defect _guard's own docstring
        # describes, asserted nowhere until now.
        (
            f"UNIQUE (mapping_type, email_address);\nCREATE INDEX i ON t(c) {_LIVE};",
            False,
        ),
        (
            f"UNIQUE (mapping_type, email_address) {_LIVE};\nCREATE INDEX i ON t(c);",
            True,
        ),
    ],
)
def test_guard_demands_a_live_rows_predicate_in_its_own_statement(
    statement: str, passes: bool
) -> None:
    """_guard()'s FAILING path never executes on a green suite.

    Round 5: the assertion is reached only when a defect shape is present,
    which by construction never happens while the schema is correct. Replacing
    the entire body with `return`, or dropping the `;` bound so the window runs
    to end-of-string, each left all 356 tests passing.
    """
    if passes:
        _guard(statement, 0, "probe")  # must not raise
    else:
        with pytest.raises(AssertionError):
            _guard(statement, 0, "probe")


def test_without_comments_strips_whole_lines_only() -> None:
    """Load-bearing in BOTH directions, and neither was asserted.

    Round 5: degrading this to `--.*$`, or to the identity function, each left
    all 356 tests passing. The two directions are different bugs: too greedy
    carries a real violation away with the comment, too timid reports the
    comment explaining the fix as the defect itself.

    The identity case is worse than it sounds, and the DDL is laid out exactly
    wrong for it. With stripping disabled, worker.py:1839
    (`-- NO unconditional UNIQUE (mapping_type, email_address).`) MATCHES the
    UNIQUE walk, and worker.py:1844 (`--   WHERE deleted_at IS NULL`) then
    SUPPLIES the predicate that satisfies _guard -- so the prose explaining the
    fix both triggers the check and rescues it.

    The RESCUE is the load-bearing half, though, and an earlier version of this
    docstring got the tally wrong by calling them two compounding reasons for
    the green. Measured: defang 1844 and the mutant dies; defang 1839 and it
    still passes, because then there is no match to guard at all. One cause,
    not two.
    """
    banned = "CONSTRAINT unique_email_mapping UNIQUE (mapping_type, email_address)"

    # Too timid -- a whole-line comment quoting the shape must not survive.
    assert banned not in _without_comments(f"    -- we removed {banned}\nSELECT 1;")

    # Too greedy -- `--.*$` strips from the `--` inside the string literal and
    # takes the real violation with it.
    assert banned in _without_comments(f"note TEXT DEFAULT 'a -- b', {banned};")

    # A trailing comment does not take the code beside it. (The whole line
    # is kept, comment included -- only whole-line comments are dropped.)
    assert banned in _without_comments(f"{banned};  -- and a note")


@pytest.fixture(scope="module")
def ddl() -> str:
    sql = _without_comments(_executed_ddl())
    # The exact CREATE, not a substring: "triage_email_mappings" is also a
    # prefix of triage_email_mappings_history, so the loose form was satisfied
    # by the history table alone -- with the mappings DDL deleted entirely,
    # this guard passed and the constraint test passed vacuously.
    assert "CREATE TABLE IF NOT EXISTS triage_email_mappings (" in sql, (
        "the mappings DDL was not executed at all -- this test is not looking "
        "at what it thinks it is"
    )
    # ...and that it was stripped. Round 5 asserted _without_comments works;
    # nothing asserted this fixture USES it. Dropping the call left all 374
    # green -- and left a genuine unconditional UNIQUE green too, because
    # worker.py:1839 then reads as live DDL and worker.py:1844 supplies the
    # predicate that rescues it. A correct helper is worthless if the fixture
    # stops calling it, so assert the composition, not just the part.
    #
    # Asserting the PROPERTY, not a quoted phrase from worker.py. The first
    # version keyed on "-- NO unconditional UNIQUE" -- prose this test does not
    # own, in a file whose edits reach main with no PR under CLAUDE.md's
    # beads-and-docs carve-out. Rewording that comment while dropping the
    # stripping call reopened the whole defect with the suite green.
    assert not any(ln.lstrip().startswith("--") for ln in sql.splitlines()), (
        "the ddl fixture is no longer stripping comments; the prose explaining "
        "the fix is being matched as if it were executable SQL"
    )
    return sql


def test_the_swallow_stays_removed() -> None:
    """Round 3's fix has to be revertible loudly, not silently.

    _executed_ddl() deliberately does NOT wrap ensure_config_schema() in
    try/except: a raise part-way through leaves a capture byte-identical to a
    clean run, so swallowing turns "half the schema never ran" into three
    passing tests. Restoring the swallow left all 374 green -- the fix had no
    regression test, because the defect it prevents only appears once something
    else has already gone wrong.

    Drive the same path with a raise, and require it through. TypeError
    specifically, because that is what the documented hazard throws: both
    psycopg2.extras helpers die inside cur.mogrify on a MagicMock cursor with
    "sequence item 0: expected a bytes-like object". A narrow
    `except TypeError: pass` is therefore the swallow that reproduces round 3's
    defect, and an earlier version of this test raised RuntimeError and did not
    see it -- a real ALTER ... ADD CONSTRAINT routed through execute_batch
    shipped with all 375 green.
    """
    mod = importlib.import_module("triage.services.worker")
    # NOT by patching psycopg2.connect -- _executed_ddl() patches that itself,
    # so an outer patch is simply overridden and the test passes vacuously.
    # Raise from the function under capture instead.
    with (
        patch.object(
            mod,
            "ensure_config_schema",
            side_effect=TypeError("sequence item 0: expected bytes, MagicMock"),
        ),
        pytest.raises(TypeError, match="sequence item 0"),
    ):
        _executed_ddl()


# The two columns whose uniqueness is supposed to hold over LIVE rows only.
_PAIR = frozenset({"mapping_type", "email_address"})


def _key_columns(text: str) -> frozenset[str] | None:
    """Normalised column set from a parenthesised list at the start of `text`.

    Parsing rather than pattern-matching, because three separate review
    findings were all the same bug: a regex that spelled the column list one
    way and missed the others. Unquoted SQL identifiers fold to lower case, so

        (mapping_type, email_address)
        (MAPPING_TYPE, EMAIL_ADDRESS)
        ("mapping_type", "email_address")
        (email_address, mapping_type)

    are four spellings of one index, and a set comparison makes order, case
    and quoting stop mattering at once. Returns None when `text` does not open
    with a balanced list.
    """
    if not text.startswith("("):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                inner = text[1:i]
                break
    else:
        return None  # unbalanced -- not a column list we can judge

    def name(element: str) -> str:
        """The column name alone, without COLLATE / opclass / DESC / NULLS.

        Whitespace-delimited, so it does NOT handle the space-free
        `"mapping_type"COLLATE"C"` form that quoted identifiers make legal.
        Tracked on cortex-migc; nothing that emits DDL in this tree produces
        it, and pg_get_indexdef always spaces them.
        """
        return element.split()[0].strip('"').lower()

    return frozenset(name(c) for c in inner.split(",") if c.strip())


# The pair written out literally, NOT as _PAIR -- but only ONE of this
# constant's uses actually needs that, and it is worth being precise about
# which. test_pair_names_the_two_arbiter_columns compares _PAIR to a value,
# so writing that value AS _PAIR would assert the constant against itself and
# go vacuous. The parametrize below does not have that problem: its inputs are
# literal key-list STRINGS that _key_columns() parses, so a corrupted _PAIR is
# caught there ten times over regardless of how the expectations are spelled.
# Measured: corrupting _PAIR while expressing every expectation as _PAIR still
# gives 10 failed. The literal is kept for both because one spelling reads
# better than two, not because the parametrize depends on it.
_THE_PAIR = frozenset({"mapping_type", "email_address"})


def test_pair_names_the_two_arbiter_columns() -> None:
    assert _PAIR == _THE_PAIR


@pytest.mark.parametrize(
    ("key_list", "expected"),
    [
        ("(mapping_type, email_address)", _THE_PAIR),
        ("(email_address, mapping_type)", _THE_PAIR),
        ("(MAPPING_TYPE, EMAIL_ADDRESS)", _THE_PAIR),
        ('("mapping_type", "email_address")', _THE_PAIR),
        ('(mapping_type COLLATE "C", email_address COLLATE "C")', _THE_PAIR),
        ("(mapping_type varchar_pattern_ops, email_address text_ops)", _THE_PAIR),
        ("(mapping_type DESC, email_address NULLS LAST)", _THE_PAIR),
        ("(mapping_type ASC NULLS FIRST, email_address DESC)", _THE_PAIR),
        ("(\n  mapping_type,\n  email_address\n)", _THE_PAIR),
        ("(\tmapping_type ,\temail_address )", _THE_PAIR),
        ("(mapping_type)", frozenset({"mapping_type"})),
        (
            "(mapping_type, email_address, label)",
            frozenset({"mapping_type", "email_address", "label"}),
        ),
        ("(email_address, deleted_at)", frozenset({"email_address", "deleted_at"})),
        ("mapping_type, email_address)", None),
        ("(mapping_type, email_address", None),
        (" (mapping_type, email_address)", None),
        ("()", frozenset()),
    ],
)
def test_key_columns_normalises(key_list: str, expected: frozenset[str] | None) -> None:
    """Assert the normaliser directly.

    Round 4: on a green run _key_columns() only ever sees ONE key list --
    idx_email_mappings_active's plain, lower-case, in-order pair -- so every
    branch making it tolerant of order, case, quoting and per-column modifiers
    was unasserted. Replacing its body with `return None`, gutting name(), or
    corrupting _PAIR each left all 338 tests passing. The helper that three
    rounds of findings all route through had no test of its own.
    """
    assert _key_columns(key_list) == expected


def _guard(ddl: str, at: int, what: str) -> None:
    """Fail unless the statement starting at `at` is predicated on live rows.

    Bound to the STATEMENT -- `ddl` up to the next `;` -- not to a character
    count. An earlier version used a fixed 220-char window; a mutation put the
    next WHERE at offset 227, so it was close enough to going vacuous that the
    bound had to stop being a guess.

    The `;` split is deliberately naive, and two limits are worth stating
    honestly rather than papering over:

    * It does not understand quoting. ensure_config_schema() contains three
      `$$ ... $$` plpgsql bodies full of semicolons, so a future trigger body
      that merely QUOTES this shape would truncate the window and fail on
      benign SQL. That direction is safe (loud, not silent) but it is a real
      limit, not the absence of one.
    * It relies on _executed_ddl() joining captured statements with `;`.
      Inert while that function issues a single statement, as it does today;
      once there is more than one, a statement with no trailing semicolon
      merges into the next and borrows its WHERE -- measured under a
      two-cursor injection, where an unterminated ALTER ... ADD CONSTRAINT
      passed on a `\n` join and was caught on a `;` join. Keep the two in
      step.
    """
    statement = ddl[at:].split(";", 1)[0]
    assert re.search(r"WHERE\s+deleted_at\s+IS\s+NULL", statement, re.I), (
        f"{what} -- unconditional uniqueness over (mapping_type, "
        "email_address) blocks recreating a soft-deleted mapping and shadows "
        f"idx_email_mappings_active: {' '.join(statement.split())[:110]!r}"
    )


# Hoisted so a test can read it. Round 6: while it was an inline literal,
# nothing asserted WHICH statements the walk selects -- only what happens to
# the ones it picks. Replacing the whole pattern with one that never matches
# left all 363 tests passing, and so did re-tightening the index-name group,
# which would have silently reverted a fix made in this very PR (cortex-migc
# item 2). The verdict half (_guard) and the normaliser half (_key_columns)
# were asserted in rounds 5 and 4; this is the selector half.
_INDEX_SELECTOR = re.compile(
    r"CREATE\s+UNIQUE\s+INDEX"
    r"(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?"
    r'(?:\s+"?(\w+)"?)?\s+ON\s+(?:"?\w+"?\.)?"?triage_email_mappings"?'
    r"(?:\s+USING\s+\w+)?\s*(?=\()",
    re.I,
)

# Shapes the selector must select (M1 is the base case; the rest are the
# tolerances it claims), plus near-misses it must reject.
_SELECTOR_MUST_MATCH = [
    "CREATE UNIQUE INDEX i ON triage_email_mappings(a, b)",
    "CREATE UNIQUE INDEX IF NOT EXISTS i ON triage_email_mappings(a, b)",
    "CREATE UNIQUE INDEX CONCURRENTLY i ON triage_email_mappings(a, b)",
    "CREATE UNIQUE INDEX ON triage_email_mappings(a, b)",
    'CREATE UNIQUE INDEX "i" ON "triage_email_mappings"(a, b)',
    "CREATE UNIQUE INDEX i ON public.triage_email_mappings(a, b)",
    "CREATE UNIQUE INDEX i ON triage_email_mappings USING btree (a, b)",
    "create unique index i on triage_email_mappings (a, b)",
]
_SELECTOR_MUST_NOT_MATCH = [
    "CREATE INDEX i ON triage_email_mappings(a, b)",
    "CREATE UNIQUE INDEX i ON triage_email_mappings_history(a, b)",
    "CREATE UNIQUE INDEX i ON other_table(a, b)",
]


@pytest.mark.parametrize("stmt", _SELECTOR_MUST_MATCH)
def test_index_selector_matches_every_shape_it_claims(stmt: str) -> None:
    m = _INDEX_SELECTOR.search(stmt)
    assert m, f"selector missed a shape it claims: {stmt!r}"
    # WHERE the match ends is half the contract, and the half the rest of the
    # code depends on: the walk does _key_columns(ddl[m.end():]), which
    # hard-requires a "(" at that offset. Turning the trailing lookahead into a
    # consuming group left all 374 tests passing while every _key_columns()
    # returned None and _guard never ran -- the guard silently vacuous.
    assert stmt[m.end() :].startswith("("), (
        f"match must end immediately before the key list, not at "
        f"{stmt[m.end() : m.end() + 12]!r}"
    )


@pytest.mark.parametrize("stmt", _SELECTOR_MUST_NOT_MATCH)
def test_index_selector_rejects_near_misses(stmt: str) -> None:
    assert not _INDEX_SELECTOR.search(stmt), f"selector over-matched: {stmt!r}"


def test_no_unconditional_unique_constraint_on_the_mapping_columns(
    ddl: str,
) -> None:
    """A UNIQUE *constraint* over the pair, under any name or spelling.

    Covers the inline `CONSTRAINT x UNIQUE (...)` form this PR removed and the
    `ALTER TABLE ... ADD [CONSTRAINT x] UNIQUE (...)` form, in either column
    order. Index forms are a different grammar and are covered by the test
    below.
    """
    assert "CONSTRAINT unique_email_mapping" not in ddl, (
        "unique_email_mapping is being recreated on fresh databases; "
        "migration 004 drops it precisely because it blocks recreating a "
        "soft-deleted mapping and shadows idx_email_mappings_active"
    )
    for m in re.finditer(r"UNIQUE\s*(?=\()", ddl, re.I):
        if _key_columns(ddl[m.end() :]) == _PAIR:
            _guard(ddl, m.start(), "UNIQUE constraint")


def test_no_unconditional_unique_index_on_the_mapping_columns(ddl: str) -> None:
    """The same defect as an INDEX rather than a constraint.

    There is no CONSTRAINT keyword in this form, so the name check above never
    sees it -- and migration 004 drops it explicitly, which is the evidence it
    occurs in the wild. Every variation below was mutation-verified as passing
    before the checks that catch it existed:

    * a different index NAME (the docstring used to claim "whatever it is
      called" while the check was pinned to unique_email_mapping)
    * REVERSED columns -- Postgres infers an arbiter by the SET of key
      columns, so the order carries no meaning; migration 004 sorts by name
      for this reason
    * UPPERCASE or "quoted" column names, which fold to the same identifiers
    * CONCURRENTLY, and an explicit `USING btree`
    * a per-column COLLATE, opclass, DESC/ASC or NULLS FIRST/LAST -- these
      sit inside the key list and used to make the column set stop matching,
      so the guard never ran. COLLATE "C" is the realistic one: the sibling
      incident here was glibc collation corrupting these exact text btrees
      (cortex-rj2b), so pinning a collation on them is a plausible fix that
      would quietly restore the defect. Whitespace-delimited -- the space-free
      `"col"COLLATE"C"` spelling is on cortex-migc, not covered here.
    * no index name at all (Postgres auto-names it), and a schema-qualified
      table (public.triage_email_mappings)
    """
    for m in _INDEX_SELECTOR.finditer(ddl):
        if _key_columns(ddl[m.end() :]) == _PAIR:
            _guard(ddl, m.start(), f"unique index {m.group(1) or '<unnamed>'!r}")


def test_the_partial_unique_index_is_still_created(ddl: str) -> None:
    """Removing the constraint must not take the real arbiter with it."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX(\s+IF\s+NOT\s+EXISTS)?\s+idx_email_mappings_active"
        r"[^;]*?\(\s*mapping_type\s*,\s*email_address\s*\)[^;]*?"
        r"WHERE\s+deleted_at\s+IS\s+NULL",
        ddl,
        re.I | re.S,
    ), "idx_email_mappings_active is not created with the shape the upsert infers"
