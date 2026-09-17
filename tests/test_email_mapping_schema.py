"""A fresh database must not regain the constraint migration 004 drops.

cortex-apd6: `triage_email_mappings` carried an unconditional
`UNIQUE (mapping_type, email_address)` named `unique_email_mapping`. Being
unconditional it counts soft-deleted rows, so a mapping that was deleted could
never be recreated, and it SHADOWS the partial index the importer's upsert is
written to infer:

    ON CONFLICT (mapping_type, email_address) WHERE deleted_at IS NULL

postmark migration 004 drops it. But `_ensure_schema` here created the table
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

import contextlib
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
    cur = MagicMock()
    cur.execute.side_effect = lambda sql, *a, **k: statements.append(str(sql))
    cur.fetchone.return_value = (0,)
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cur

    mod = importlib.import_module("triage.services.worker")
    cfg = MagicMock()
    with (
        patch.object(mod.psycopg2, "connect", return_value=conn),
        contextlib.suppress(Exception),
    ):
        mod.ensure_config_schema(cfg)
    return "\n".join(statements)


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
    )
    return sql


def test_no_unconditional_unique_on_the_mapping_columns(ddl: str) -> None:
    """The exact shape migration 004 exists to remove."""
    assert "CONSTRAINT unique_email_mapping" not in ddl, (
        "unique_email_mapping is being recreated on fresh databases; "
        "migration 004 drops it precisely because it blocks recreating a "
        "soft-deleted mapping and shadows idx_email_mappings_active"
    )
    # ...and not under any other name, either: an unconditional UNIQUE over
    # those two columns is the defect, whatever it is called.
    for m in re.finditer(
        r"UNIQUE\s*\(\s*mapping_type\s*,\s*email_address\s*\)", ddl, re.I
    ):
        # Bound to the STATEMENT, not a character count. A 220-char window was
        # measured at 7 characters from going vacuous -- under one mutation the
        # next WHERE sat at offset 227, so slightly different indentation would
        # have let an unconditional, differently-named UNIQUE pass silently.
        # This regex only matches inline table constraints, so truncating at the
        # semicolon cannot produce a false positive.
        window = ddl[m.start() :].split(";", 1)[0]
        assert re.search(
            r"WHERE\s+deleted_at\s+IS\s+NULL", window, re.I
        ), f"unconditional UNIQUE on (mapping_type, email_address): {window[:120]!r}"


def test_no_unconditional_unique_under_a_reordered_or_renamed_form(ddl: str) -> None:
    """Two shapes of the same defect the first test does not see.

    * REVERSED COLUMNS. Postgres infers an arbiter by the SET of key columns,
      so UNIQUE (email_address, mapping_type) shadows the partial index just as
      effectively. postmark migration 004 sorts by name for exactly this
      reason, and its assertions were fixed for it in review.
    * A BARE UNIQUE INDEX reusing the name. There is no CONSTRAINT keyword, so
      `"CONSTRAINT unique_email_mapping" not in ddl` does not see it. Migration
      004 drops that form explicitly, which is the evidence it occurs.

    Both were mutation-verified as passing before this test existed.
    """
    both_orders = (
        r"UNIQUE\s*\(\s*(?:mapping_type\s*,\s*email_address"
        r"|email_address\s*,\s*mapping_type)\s*\)"
    )
    for m in re.finditer(both_orders, ddl, re.I):
        statement = ddl[m.start() :].split(";", 1)[0]
        assert re.search(r"WHERE\s+deleted_at\s+IS\s+NULL", statement, re.I), (
            "unconditional unique over those two columns, in either order, "
            f"shadows idx_email_mappings_active: {' '.join(statement.split())[:110]!r}"
        )

    # Any unconditional unique INDEX on those columns, under ANY name -- not
    # just one reusing unique_email_mapping. Review noted the docstring
    # claimed "whatever it is called" while the check was name-pinned, so a
    # CREATE UNIQUE INDEX some_other_name on the same two columns slipped
    # through. Closing that is cheaper than softening the claim.
    for m in re.finditer(
        r"CREATE\s+UNIQUE\s+INDEX(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)\s+"
        r"ON\s+triage_email_mappings\s*\(",
        ddl,
        re.I,
    ):
        statement = ddl[m.start() :].split(";", 1)[0]
        cols = statement[statement.index("(") :]
        on_the_pair = re.match(
            r"\(\s*(?:mapping_type\s*,\s*email_address"
            r"|email_address\s*,\s*mapping_type)\s*\)",
            cols,
        )
        if not on_the_pair:
            continue
        assert re.search(r"WHERE\s+deleted_at\s+IS\s+NULL", statement, re.I), (
            f"unconditional unique index {m.group(1)!r} on those two columns "
            "shadows idx_email_mappings_active just as a constraint would"
        )


def test_the_partial_unique_index_is_still_created(ddl: str) -> None:
    """Removing the constraint must not take the real arbiter with it."""
    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX(\s+IF\s+NOT\s+EXISTS)?\s+idx_email_mappings_active"
        r"[^;]*?\(\s*mapping_type\s*,\s*email_address\s*\)[^;]*?"
        r"WHERE\s+deleted_at\s+IS\s+NULL",
        ddl,
        re.I | re.S,
    ), "idx_email_mappings_active is not created with the shape the upsert infers"
