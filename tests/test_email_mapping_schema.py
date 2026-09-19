"""A fresh database must not regain the constraint migration 004 drops.

`triage_email_mappings` carried an unconditional
`UNIQUE (mapping_type, email_address)` named `unique_email_mapping`. Being
unconditional it counts soft-deleted rows, so a deleted mapping could never be
recreated, and it shadows the partial index the importer's upsert infers:

    ON CONFLICT (mapping_type, email_address) WHERE deleted_at IS NULL

postmark migration 004 drops it, but `ensure_config_schema` created the table
WITH it inline and runs on every worker start, so every fresh database got the
bug back (cortex-qvnz). `CREATE TABLE IF NOT EXISTS` no-ops on an existing
table, which is why production never showed it.

These assert on the DDL the worker actually EXECUTES, captured from a mocked
connection, rather than on the source text -- a grep would also match a comment
about the constraint, including the one this fix added.

Most of the file is the capture and its own tests. That is deliberate: the
capture decides what the three real assertions at the end can see, and every
defect found while writing this was in the capture rather than in the fix.
"""

from __future__ import annotations

import importlib
import re
from unittest.mock import MagicMock, patch

import psycopg2.sql
import pytest

# The two worlds a probe inside ensure_config_schema can be answered from.
# "Empty" is the fresh database this file is about -- but ensure_config_schema
# CREATES the mappings table itself, so any probe issued after that point sees
# a table that EXISTS on a fresh server. Pinning only the empty answers made
# every "if it is already there, alter it" step invisible.
_EMPTY: dict[str, object] = {
    "fetchone": None,
    "fetchall": [],
    "fetchmany": [],
    "rowcount": 0,
    "description": None,
}
_POPULATED: dict[str, object] = {
    "fetchone": (1,),
    "fetchall": [(1,)],
    "fetchmany": [(1,)],
    "rowcount": 1,
    "description": [("x",)],
}


class _EverythingOff:
    """A Config whose every attribute is falsey.

    MagicMock answers every attribute with a truthy Mock, so a config-gated
    statement -- `if not config.some_flag: <create it>` -- took the skip branch
    under capture and the create branch on any deployment where that flag is
    off or absent. Config is a dataclass with boolean fields already
    (enable_convergence), and ensure_config_schema is handed the whole object.
    """

    def __getattr__(self, name: str) -> None:
        return None


class _Strict(MagicMock):
    """A mock whose unpinned attributes cannot be read as present OR absent.

    A plain MagicMock answers every attribute truthily; `spec=[...]` makes an
    undeclared one raise AttributeError, which `hasattr()`, `getattr(x, n, default)`
    and `except AttributeError:` all turn back into a falsey answer. Either way a
    migration gated on an unpinned attribute takes one branch here and the other on
    a real server.

    AssertionError closes both readings, because hasattr swallows only
    AttributeError. The leading-underscore guard is required: mock's own __init__
    probes `_mock_methods` via getattr.

    It is not airtight -- `except Exception:` swallows this too. See cortex-5uyd.
    """

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        try:
            return super().__getattr__(name)
        except AttributeError as exc:
            if name.startswith("_"):
                raise
            raise AssertionError(
                f"{name!r} is not pinned on this recorder. Add it to _CONN_PINS "
                "with the value psycopg2 documents -- an unpinned attribute "
                "answers neither truthily nor absently, because both readings "
                "have sent a guarded migration down the wrong branch."
            ) from exc


# An allowlist, not a bare MagicMock. Any attribute not named below raises
# (see _Strict), because an unpinned one answers truthily and a migration
# gated on it takes the skip branch here and the create branch on a real
# server. Enumerating the attributes that matter does not converge -- thirteen
# were still open after four rounds of pinning instances.
#
# The spec lists are DERIVED from _CONN_PINS so a name cannot be added without
# its value; a flat list of names would restore the truthy mock. spec=[names]
# rather than spec=psycopg2.extensions.connection, which rejects only
# FICTIONAL attributes and so closes none of these.
_CONN_PINS: dict[str, object] = {
    "autocommit": False,  # psycopg2 opens in transaction mode
    "closed": 0,  # int, not bool
    "readonly": None,
    "deferrable": None,
    "notices": [],
}
# Callables only. A DATA attribute added here would answer with a truthy Mock
# again -- it belongs in _CONN_PINS, which forces a value.
_CONN_CALLS = ("cursor", "commit", "__enter__", "__exit__")
_CUR_CALLS = (
    "execute",
    "executemany",
    "fetchone",
    "fetchall",
    "fetchmany",
    "__enter__",
    "__exit__",
    "__iter__",
)
# cur's data attributes come from the world dict below, plus its own two pins.
_CUR_DATA = ("rowcount", "description", "closed", "connection")


def _recording_conn(
    answers: dict[str, object] | None = None,
) -> tuple[MagicMock, list[str]]:
    """A connection whose every SQL path lands in the returned list.

    Extracted from _executed_ddl so its REACH is assertable. Round 8: both of
    round 2's widenings could be reverted with the suite green -- deleting the
    executemany recorder, or narrowing conn.cursor back to binding only
    __enter__ -- and DDL routed through either escaped unseen. The capture was
    correct and unguarded, so nothing noticed it narrowing again.
    """
    statements: list[str] = []

    def record(sql: object, *a: object, **k: object) -> None:
        # str() of a non-string is a REPR, and the guards below would match
        # the repr instead of the statement. psycopg2 accepts
        # psycopg2.sql.Composed, whose str() contains no literal constraint
        # name, so a Composed ALTER shipped with the suite green. The tree
        # builds DDL this way already (postmark gmail_sync.py:180, :207).
        assert isinstance(sql, str | bytes), (
            f"SQL reached the recorder as {type(sql).__name__}, which str() "
            "renders as a repr -- the guards below would match the repr, not "
            "the statement"
        )
        statements.append(str(sql))

    cur = _Strict(spec=[*_CUR_CALLS, *_CUR_DATA])
    # BOTH execute paths. executemany is not exotic -- it is the natural way to
    # write a parameterised ALTER, and DDL sent through it was invisible here.
    cur.execute.side_effect = record
    cur.executemany.side_effect = record
    # Every accessor that answers "is this database empty?" must answer empty.
    # Unpinned, MagicMock returns a truthy Mock, so both spellings of the
    # ordinary guard -- `if not cur.<probe>():` and `if cur.rowcount == 0:` --
    # take the "already exists, skip" branch under capture and the "create it"
    # branch on a real fresh server, and the ALTER is never recorded.
    #
    # Pinned as a class rather than per accessor: rowcount is read directly at
    # worker.py:539 and compared at seven sites across three repos, and
    # fetchmany has no uses today but is the same question.
    world = _EMPTY if answers is None else answers
    cur.fetchone.return_value = world["fetchone"]
    cur.fetchall.return_value = world["fetchall"]
    cur.fetchmany.return_value = world["fetchmany"]
    cur.rowcount = world["rowcount"]
    cur.description = world["description"]
    # `for (name,) in cur:` is the same read one step shorter than
    # `.fetchall()`. The tree writes both -- the long form on a migration path
    # at postmark gmail_sync.py:197, the short one at scripts/parse_test.py:179
    # -- so it is house style, just never written inside ensure_config_schema.
    # side_effect, not return_value: a return_value iterator is exhausted after
    # the first loop, which would reopen the hole for the second read.
    cur.__iter__.side_effect = lambda: iter(world["fetchall"])
    # `cur` must be its OWN context manager, and conn.cursor() must return it
    # directly. Binding only conn.cursor().__enter__ meant a bare
    # `cur2 = conn.cursor()` -- no `with` -- handed back a fresh unrecorded
    # MagicMock that swallowed its SQL silently.
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn = _Strict(spec=[*_CONN_PINS, *_CONN_CALLS])
    conn.__enter__.return_value = conn
    # Pinned for the same reason cur's is: MagicMock.__exit__ happens to
    # default falsey today, so an unpinned outer exit is correct by accident.
    conn.__exit__.return_value = False
    conn.cursor.return_value = cur
    # `conn` itself, not just the cursor. Every pinned value is FALSEY on a
    # real psycopg2 connection, so `if not conn.autocommit:` and its siblings
    # fire in production and are skipped under capture. autocommit has a live
    # motive: CREATE INDEX CONCURRENTLY requires it, and this DDL builds the
    # partial unique index the file is about.
    #
    # Pins, not worlds -- psycopg2 documents one value for each, so there is no
    # second branch to explore.
    for _name, _value in _CONN_PINS.items():
        setattr(conn, _name, _value)
    cur.closed = False
    # cur.connection.<anything> then resolves to the pins above rather than to
    # a fresh truthy Mock.
    cur.connection = conn
    # conn.status is absent on purpose, so a gate on it RAISES. It is
    # genuinely two-valued inside the function (READY, then BEGIN), so no
    # single pin is right and _Strict's generic "add it to _CONN_PINS" is the
    # wrong advice here -- pinning either value makes the error go away and the
    # test wrong. Decide it when something needs to gate on status.
    return conn, statements


@pytest.mark.parametrize(
    ("label", "drive", "expected"),
    [
        ("with conn.cursor() as c", lambda c: _drive_with(c), ["SENTINEL 0;"]),
        (
            "bare c = conn.cursor()",
            lambda c: c.cursor().execute("SENTINEL 1;"),
            ["SENTINEL 1;"],
        ),
        (
            "executemany",
            lambda c: c.cursor().executemany("SENTINEL 2;", [()]),
            ["SENTINEL 2;"],
        ),
        (
            "nested with, twice",
            lambda c: _drive_twice(c),
            ["SENTINEL 3;", "SENTINEL 4;"],
        ),
    ],
)
def test_the_capture_reaches_every_sql_path(
    label: str, drive: object, expected: list[str]
) -> None:
    """Every way the worker can hand SQL to a cursor must land in the capture.

    execute, executemany, a bare `conn.cursor()` without `with`, and a nested
    `with` all reach it; anything missed is DDL the assertions cannot see.
    """
    conn, statements = _recording_conn()
    drive(conn)  # type: ignore[operator]
    assert statements == expected, (
        f"SQL sent via {label} did not reach the recorder intact: "
        f"{statements!r} != {expected!r}"
    )


def test_the_recorder_answers_as_an_empty_database() -> None:
    """Every accessor answers as a FRESH database, not as a truthy Mock.

    An unpinned rowcount is truthy, so `== 0` and `not rowcount` are both False and
    a guarded ALTER takes the skip branch here and the create branch in production.
    """
    conn, _ = _recording_conn()
    with conn.cursor() as cur:
        # None, not (0,): "no row" is true of an existence probe and false of
        # an aggregate, where a fresh server returns (0,) -- which is a row.
        # None fails LOUDLY (row[0] raises) where (0,) would be right by
        # accident under one idiom and wrong under the other. The populated
        # case is _POPULATED's job, not a relaxation of this one.
        assert cur.fetchone() is None, "the empty world returns no row"
        assert cur.fetchall() == [], "a fresh database returns no rows"
        assert cur.fetchmany() == [], "a fresh database returns no rows"
        # Both forms, because both are idioms in this tree and an unpinned
        # rowcount satisfies NEITHER: it is a truthy Mock, so `== 0` is False
        # and `not rowcount` is False -- a guarded ALTER takes the skip branch
        # under capture and the create branch on a real server.
        assert cur.rowcount == 0, "a fresh database affected no rows"
        assert not cur.rowcount, "an unpinned rowcount is truthy and skips the guard"


def test_the_other_world_is_really_the_other_one() -> None:
    """_POPULATED and _EverythingOff have to differ from the defaults.

    Both are only ever consumed by the ddl fixture, so degrading either to
    match the empty/truthy default reopens the escape with the suite green:
    measured at 391, an ALTER ... ADD CONSTRAINT gated on `if cur.fetchone()
    is not None:` or on `if not config.some_flag:` shipped untouched.
    """
    conn, _ = _recording_conn(_POPULATED)
    with conn.cursor() as cur:
        assert cur.fetchone() is not None, "the populated world must return a row"
        assert cur.fetchall() and cur.fetchmany(), "...and rows"
        assert cur.rowcount == 1, "...and a non-zero rowcount"
        assert cur.description, "...and a row description"
        # Fresh iterator per call, so a second `for row in cur:` sees rows too.
        assert list(cur) == [(1,)], "the populated world must iterate rows"
        assert list(cur) == [(1,)], "...and must still iterate on the next read"
    assert not _EverythingOff().any_flag_at_all, "the falsey config must be falsey"


def test_the_recorder_refuses_an_attribute_nobody_pinned() -> None:
    """An attribute nobody wrote down answers nothing -- it raises.

    Checked through every reading that could turn a raise back into a falsey
    answer: direct, hasattr, getattr-with-default, and a swallowing try/except.
    """
    conn, _ = _recording_conn()
    for attribute in ("notifies", "cursor_factory", "isolation_level", "info"):
        with pytest.raises(AssertionError, match=attribute):
            getattr(conn, attribute)
    with conn.cursor() as cur:
        for attribute in ("name", "query", "rownumber", "statusmessage"):
            with pytest.raises(AssertionError, match=attribute):
                getattr(cur, attribute)

    # A raise is only loud if the caller does not turn it back into an answer,
    # and all three idioms below do exactly that for AttributeError. hasattr on
    # a real psycopg2 connection is True for every one of these names, so a
    # gate phrased this way fires in production.
    for reading in (
        lambda: hasattr(conn, "info"),
        lambda: getattr(conn, "server_version", None),
        lambda: conn.encoding,
    ):
        with pytest.raises(AssertionError):
            reading()

    def swallowing_probe() -> bool:
        """`try: conn.x / except AttributeError:` must not become False either."""
        try:
            return bool(conn.notifies)
        except AttributeError:
            return False

    with pytest.raises(AssertionError):
        swallowing_probe()


def test_the_allowlist_cannot_be_widened_without_a_value() -> None:
    """A name may only enter the allowlist by way of a value.

    A flat `spec=[...]` rots silently: adding just the name quietens the raise and
    restores the truthy mock. Deriving it from _CONN_PINS forces a value, so
    widening strengthens the test.
    """
    assert set(_CONN_PINS).isdisjoint(
        _CONN_CALLS
    ), "a name in both is ambiguous: the pin sets a value, the call list does not"
    assert set(_CONN_PINS).isdisjoint(_CUR_CALLS)
    # Every pinned name must actually be readable on the recorder -- otherwise
    # the pin dict and the spec list have drifted apart.
    conn, _ = _recording_conn()
    for name, value in _CONN_PINS.items():
        assert getattr(conn, name) == value


def test_the_worlds_are_four_DIFFERENT_worlds() -> None:
    """`len(_WORLDS) == 4` is not the property; crossing both axes is.

    Four identical worlds keep the length, satisfy the fixture's count and explore
    one branch. isinstance, not `type(c) is MagicMock` -- mock gives each instance
    its own subclass.
    """
    covered = {
        ("empty" if answers is _EMPTY else "populated", isinstance(config, MagicMock))
        for answers, config in _WORLDS
    }
    assert covered == {
        ("empty", True),
        ("empty", False),
        ("populated", True),
        ("populated", False),
    }, f"_WORLDS no longer crosses both axes: {covered}"
    assert len(_WORLDS) == 4


def test_the_connection_answers_as_a_live_psycopg2_connection() -> None:
    """The third axis: `conn` itself, which was a bare truthy MagicMock.

    Every one of these is falsey on a real connection, so `if not <x>:` fires
    in production and was skipped under capture. Each shipped an unconditional
    UNIQUE at 393 green before these pins existed.
    """
    conn, _ = _recording_conn()
    assert conn.autocommit is False, "psycopg2 opens in transaction mode"
    assert not conn.closed, "an open connection is falsey-closed (int 0)"
    assert conn.readonly is None
    assert conn.deferrable is None
    assert conn.notices == []
    with conn.cursor() as cur:
        assert not cur.closed, "a live cursor is not closed"
        # The class-level pin: the cursor's connection is THE connection, so
        # cur.connection.<anything> inherits every pin above.
        assert cur.connection is conn
        assert cur.connection.autocommit is False


def test_the_recorder_refuses_sql_it_cannot_render_as_text() -> None:
    """A Composed statement must be loud here, not silently reprd."""
    composed = psycopg2.sql.SQL(
        "ALTER TABLE {} ADD CONSTRAINT {} UNIQUE ({}, {})"
    ).format(
        *map(
            psycopg2.sql.Identifier,
            ("triage_email_mappings", "uem2", "mapping_type", "email_address"),
        )
    )
    assert "CONSTRAINT uem2" not in str(composed), "str() is not the statement"
    conn, _ = _recording_conn()
    with pytest.raises(AssertionError), conn.cursor() as cur:
        cur.execute(composed)


def test_the_cursor_context_manager_does_not_suppress() -> None:
    """A mock __exit__ returning True swallows everything raised in the block.

    ensure_config_schema runs its DDL inside
    `with (psycopg2.connect(...) as conn, conn.cursor() as cur)`, so both context
    managers sit on the exception path of the function under capture. A swallow
    there turns a real failure into a clean-looking capture with the constraint
    simply absent.
    """
    conn, _ = _recording_conn()
    boom = RuntimeError("raised inside the cursor context")
    # BOTH context managers, in the order production opens them. conn.__exit__
    # is the OUTER one and sees the exception LAST, so it can suppress even
    # when cur.__exit__ correctly declines to -- and the first version of this
    # test entered only conn.cursor(), leaving that half untested while this
    # docstring claimed both.
    with pytest.raises(RuntimeError) as caught, conn as c2, c2.cursor() as cur:
        cur.execute("SENTINEL;")
        raise boom
    assert caught.value is boom, "cur.__exit__ is suppressing the real exception"


def _drive_with(conn: MagicMock) -> None:
    with conn.cursor() as c:
        c.execute("SENTINEL 0;")


def _drive_twice(conn: MagicMock) -> None:
    with conn.cursor() as c:
        c.execute("SENTINEL 3;")
    with conn.cursor() as c:
        c.executemany("SENTINEL 4;", [()])


def _executed_ddl(
    answers: dict[str, object] | None = None, config: object | None = None
) -> str:
    """Every statement ensure_config_schema() hands to the cursor.

    NOT TriageWorker._ensure_schema, which ensures classifications and never
    touches the mapping tables.
    """
    conn, statements = _recording_conn(answers)
    mod = importlib.import_module("triage.services.worker")
    cfg = MagicMock() if config is None else config
    # Deliberately NOT wrapped in try/except. ensure_config_schema issues all
    # its DDL in ONE execute() -- a 13,155-character blob of 44 statements --
    # so the hazard is not a truncated prefix but a SECOND call that never
    # reaches the recorder. psycopg2.extras.execute_batch and execute_values
    # both bypass the side_effect above and carry their DDL away with them, by
    # different routes and raising different exceptions;
    # test_the_swallow_stays_removed is parametrised over both.
    #
    # With an ALTER routed through execute_batch the capture is byte-identical
    # to a clean run and the constraint is simply absent. The raised exception
    # is the only evidence there is, so swallowing it destroys it.
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
    """Stripping must remove comments and nothing else.

    A `--` inside a quoted string is not a comment, and removing a whole line that
    merely contains one would delete real DDL.
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


# Every combination of the two axes the capture used to fix silently: what a
# probe is answered with, and what the config says. A statement reachable in
# ANY of them is a statement a fresh database can get.
_WORLDS = [
    (answers, config)
    for answers in (_EMPTY, _POPULATED)
    for config in (MagicMock(), _EverythingOff())
]


@pytest.fixture(scope="module")
def ddl() -> str:
    sql = _without_comments(
        ";\n".join(_executed_ddl(answers, config) for answers, config in _WORLDS)
    )
    # The exact CREATE, not a substring: "triage_email_mappings" is also a
    # prefix of triage_email_mappings_history, so the loose form was satisfied
    # by the history table alone -- with the mappings DDL deleted entirely,
    # this guard passed and the constraint test passed vacuously.
    assert sql.count("CREATE TABLE IF NOT EXISTS triage_email_mappings (") == len(
        _WORLDS
    ), (
        "the mappings DDL was not executed once per world -- either it did not "
        "run at all, or the fixture stopped capturing every branch and this "
        "test is not looking at what it thinks it is"
    )
    # ...and that the fixture USES the stripper. A correct helper is worthless
    # if the fixture stops calling it: dropping the call leaves a real
    # unconditional UNIQUE green, because a commented-out one then reads as
    # live DDL and the comment below it supplies the rescuing predicate.
    #
    # Asserts the PROPERTY, not a phrase quoted from worker.py -- keying on
    # prose this test does not own means a reword reopens the defect.
    return sql


@pytest.mark.parametrize(
    ("exc", "helper"),
    [
        (TypeError("sequence item 0: expected bytes, MagicMock"), "execute_batch"),
        (KeyError("<MagicMock name='mock.connection.encoding'>"), "execute_values"),
    ],
)
def test_the_swallow_stays_removed(exc: Exception, helper: str) -> None:
    """_executed_ddl must not catch. A swallowed exception is a silent pass.

    The fixture is module-scoped, so a raise here surfaces as fixture errors on
    every consumer rather than as one failure.
    """
    mod = importlib.import_module("triage.services.worker")
    # NOT by patching psycopg2.connect -- _executed_ddl() patches that itself,
    # so an outer patch is simply overridden and the test passes vacuously.
    # Raise from the function under capture instead.
    with (
        patch.object(mod, "ensure_config_schema", side_effect=exc),
        pytest.raises(type(exc)) as caught,
    ):
        _executed_ddl()
    # The exact instance, not merely the type. Widening this from
    # match="sequence item 0" to a bare type check bought the KeyError case and
    # sold propagation fidelity: `raise type(e)("schema setup failed") from e`
    # kept the type, discarded the message, and shipped at 389 green. The
    # message is what identifies WHICH helper carried the DDL away, so losing
    # it destroys the same evidence a swallow would.
    assert caught.value is exc, "the exception must propagate unaltered"


# The two columns whose uniqueness is supposed to hold over LIVE rows only.
_PAIR = frozenset({"mapping_type", "email_address"})


def _key_columns(text: str) -> frozenset[str] | None:
    """The parenthesised key list at `at`, normalised to a set of bare names.

    Order carries no meaning -- Postgres infers an arbiter by the SET of columns --
    so the comparison must be order-insensitive, and quoting and case must be
    stripped.
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
# go vacuous. The literal is kept for both because one spelling reads
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
    """True if the statement at `at` is scoped to live rows.

    Looks only as far as the next `;`, so it cannot borrow a predicate from the
    statement after it.
    """
    statement = ddl[at:].split(";", 1)[0]
    assert re.search(r"WHERE\s+deleted_at\s+IS\s+NULL", statement, re.I), (
        f"{what} -- unconditional uniqueness over (mapping_type, "
        "email_address) blocks recreating a soft-deleted mapping and shadows "
        f"idx_email_mappings_active: {' '.join(statement.split())[:110]!r}"
    )


# The constraint walk's selector. Round 8: this one was never hoisted, and on a
# green run it matches ZERO times -- the DDL's two bare UNIQUEs are
# `version INTEGER NOT NULL UNIQUE` (worker.py:1667) and
# `worker_id TEXT NOT NULL UNIQUE` (worker.py:1907), and neither has a key
# list, so the lookahead rejects both. So gutting it left
# 375 green, and gutting it alongside an unnamed or reversed inline UNIQUE also
# left 375 green. A selector that never fires on healthy input is invisible
# until the day it needed to fire.
_CONSTRAINT_SELECTOR = re.compile(r"UNIQUE\s*(?=\()", re.I)

_CONSTRAINT_MUST_MATCH = [
    "CONSTRAINT x UNIQUE (mapping_type, email_address)",
    "UNIQUE (mapping_type, email_address)",
    "ALTER TABLE t ADD CONSTRAINT x UNIQUE (a, b)",
    "ALTER TABLE t ADD UNIQUE (a, b)",
    "unique (a, b)",
    "UNIQUE(a, b)",
]
_CONSTRAINT_MUST_NOT_MATCH = [
    "worker_id TEXT NOT NULL UNIQUE",
    "CREATE UNIQUE INDEX i ON t(a, b)",
    # Not about comments -- those are stripped upstream. This is rejected
    # because nothing follows UNIQUE before a "(", which is what a
    # lookahead-free selector would get wrong.
    "the word UNIQUE with no key list",
]


@pytest.mark.parametrize("stmt", _CONSTRAINT_MUST_MATCH)
def test_constraint_selector_matches_every_shape_it_claims(stmt: str) -> None:
    m = _CONSTRAINT_SELECTOR.search(stmt)
    assert m, f"constraint selector missed: {stmt!r}"
    assert stmt[m.end() :].startswith("("), "match must end right before the key list"


@pytest.mark.parametrize("stmt", _CONSTRAINT_MUST_NOT_MATCH)
def test_constraint_selector_rejects_near_misses(stmt: str) -> None:
    assert not _CONSTRAINT_SELECTOR.search(stmt), f"over-matched: {stmt!r}"


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
    for m in _CONSTRAINT_SELECTOR.finditer(ddl):
        if _key_columns(ddl[m.end() :]) == _PAIR:
            _guard(ddl, m.start(), "UNIQUE constraint")


def test_no_unconditional_unique_index_on_the_mapping_columns(ddl: str) -> None:
    """The same defect as an INDEX rather than a constraint.

    There is no CONSTRAINT keyword in this form, so the name check above never sees
    it. Migration 004 drops this shape defensively -- its comment says the name
    "may exist as a bare index" -- while the constraint form is the one it records
    as observed. Guarded anyway: the cost is a regex and the failure is silent.
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
