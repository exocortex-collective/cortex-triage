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
    # suppress() is inert against the current single-execute implementation,
    # but if the DDL is ever split across statements it would let this return
    # whatever was emitted BEFORE a failure and assert happily on the prefix.
    # Keep swallowing (the mock cannot satisfy every driver call) and record
    # what was swallowed, so the vacuity guard can say why it is short.
    swallowed: list[BaseException] = []
    with patch.object(mod.psycopg2, "connect", return_value=conn):
        try:
            mod.ensure_config_schema(cfg)
        except Exception as exc:  # noqa: BLE001 -- see above
            swallowed.append(exc)
    _LAST_SWALLOWED[:] = swallowed
    # ";" is what _guard splits on, so it must be what separates captured
    # statements. Joining on "\n" alone let an unterminated statement run
    # into the next one and borrow its WHERE -- mutation-verified: an
    # ALTER ... ADD CONSTRAINT with no trailing ";" passed.
    return ";\n".join(statements)


# Populated by _executed_ddl(); read only by the vacuity guard's message.
_LAST_SWALLOWED: list[BaseException] = []


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
        + (f" (swallowed: {_LAST_SWALLOWED[0]!r})" if _LAST_SWALLOWED else "")
    )
    return sql


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
    return frozenset(
        c.strip().strip('"').lower() for c in inner.split(",") if c.strip()
    )


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
    * It relies on _executed_ddl() joining captured statements with `;`. Left
      to `\n` alone, a statement with no trailing semicolon merged into the
      next and borrowed its WHERE -- an unterminated ALTER ... ADD CONSTRAINT
      passed. Keep those two in step.
    """
    statement = ddl[at:].split(";", 1)[0]
    assert re.search(r"WHERE\s+deleted_at\s+IS\s+NULL", statement, re.I), (
        f"{what} -- unconditional uniqueness over (mapping_type, "
        "email_address) blocks recreating a soft-deleted mapping and shadows "
        f"idx_email_mappings_active: {' '.join(statement.split())[:110]!r}"
    )


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
    """
    for m in re.finditer(
        r"CREATE\s+UNIQUE\s+INDEX"
        r"(?:\s+CONCURRENTLY)?(?:\s+IF\s+NOT\s+EXISTS)?"
        r'\s+"?(\w+)"?\s+ON\s+"?triage_email_mappings"?'
        r"(?:\s+USING\s+\w+)?\s*(?=\()",
        ddl,
        re.I,
    ):
        if _key_columns(ddl[m.end() :]) == _PAIR:
            _guard(ddl, m.start(), f"unique index {m.group(1)!r}")


def test_the_partial_unique_index_is_still_created(ddl: str) -> None:
    """Removing the constraint must not take the real arbiter with it."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX(\s+IF\s+NOT\s+EXISTS)?\s+idx_email_mappings_active"
        r"[^;]*?\(\s*mapping_type\s*,\s*email_address\s*\)[^;]*?"
        r"WHERE\s+deleted_at\s+IS\s+NULL",
        ddl,
        re.I | re.S,
    ), "idx_email_mappings_active is not created with the shape the upsert infers"
