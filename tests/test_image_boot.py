"""The container must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needs working DNS --
and a redeploy, or an ofelia job with `delete: true`, makes fresh ones.

SCOPE, because it is narrower than it looks: this covers the IMAGE DEFAULT
only. A compose `command:` OVERRIDES the image CMD, and nine of the eleven
cortex services in Portainer stack 48 pass their own `uv run python -m ...`,
so the image being correct does not make production correct. Tracked in
cortex-4da6.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# The one correct start command. Asserted whole rather than probed for
# properties: there is exactly one right answer here, so equality is both the
# simplest assertion and the strongest. It rejects a boot-time `pip install`,
# a `python -c` that shells out, and a `/app/.venv/bin/../../..` path that
# escapes the venv, without needing to name any of them.
EXPECTED_START = ["/app/.venv/bin/python", "-m", "triage.services.worker"]


def _effective_argv(dockerfile: str) -> list[str]:
    """The argv docker would start the container with.

    Four rules, each measured against docker rather than read off the docs:

    1. Only the LAST ENTRYPOINT and the LAST CMD count; an earlier one of
       either is dead text. Collecting every match reads a stale line as live,
       which both passes a Dockerfile whose final CMD is wrong and fails one
       whose final CMD is right.
    2. Only the final build stage ships, so an earlier stage's CMD is not ours.
    3. SHELL form runs through the shell, so it becomes /bin/sh -c <string>.
    4. A SHELL-form ENTRYPOINT makes docker DROP the CMD entirely.

    Rules 3 and 4 are the ones that matter for this test: without them an
    exec-form ENTRYPOINT with a shell-form CMD, and a shell-form ENTRYPOINT
    with an exec-form CMD, both produce the expected argv here while docker
    starts something else.
    """
    # A command split across lines is ONE command; a per-line scan reads its
    # halves as unrelated.
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    joined = joined[joined.rfind("\nFROM ") + 1 :] if "\nFROM " in joined else joined

    def last(kind: str) -> str | None:
        # re.IGNORECASE: docker instruction keywords are case-insensitive, so a
        # lowercase `cmd` is a real CMD.
        found = re.findall(rf"^\s*{kind}\s+(.+)$", joined, re.MULTILINE | re.IGNORECASE)
        return found[-1].strip() if found else None

    def parse(raw: str) -> list[str]:
        if raw.startswith("["):
            # raw_decode, so a trailing comment after the JSON array is
            # ignored rather than fatal.
            return json.JSONDecoder().raw_decode(raw)[0]
        return ["/bin/sh", "-c", raw]

    entrypoint, cmd = last("ENTRYPOINT"), last("CMD")
    if entrypoint is None:
        return parse(cmd) if cmd is not None else []
    if not entrypoint.startswith("["):
        return parse(entrypoint)  # rule 4: the CMD is discarded
    # CMD supplies ARGUMENTS to an exec-form ENTRYPOINT, hence this order.
    return parse(entrypoint) + (parse(cmd) if cmd is not None else [])


def _uv_syncs(dockerfile: str) -> list[str]:
    """Every `uv sync` that actually runs, with its flags.

    Taken per shell command rather than per line, so `RUN echo uv sync` is not
    a sync while `RUN cd /app && uv sync` and
    `RUN --mount=type=cache,... uv sync` are.
    """
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    syncs = []
    for body in re.findall(r"^\s*RUN\s+(.+)$", joined, re.MULTILINE | re.IGNORECASE):
        for segment in re.split(r"&&|\|\||;|\|", body):
            # RUN flags (--mount=...) and env assignments
            # (UV_COMPILE_BYTECODE=1, which uv's own Docker guidance uses) sit
            # before the command itself.
            segment = re.sub(r"^\s*(?:(?:--\S+|\w+=\S*)\s+)*", "", segment)
            # `uv --no-cache sync` puts a flag before the subcommand.
            if re.match(r"uv\s+(?:-\S+\s+)*sync\b", segment):
                syncs.append(segment)
    return syncs


def test_the_container_starts_with_the_baked_venv():
    """The whole point: the start command must be the interpreter already
    built into the image, so nothing is resolved or fetched at boot."""
    argv = _effective_argv((REPO / "Dockerfile").read_text())
    assert argv, "the Dockerfile declares no CMD or ENTRYPOINT"
    assert argv == EXPECTED_START, (
        f"the container's start command is not the baked venv, so it resolves "
        f"the project at boot.\n  expected: {EXPECTED_START}\n  found:    {argv}"
    )


def test_a_uv_sync_installs_the_project_itself():
    """The other half of the bug: pointing at the baked venv only works if the
    project is IN it. A sync that skips installing the project leaves no .pth,
    so the boot fails with ModuleNotFoundError rather than a network error --
    a different symptom, the same outage.

    A STATIC APPROXIMATION of a build-time property, and nothing in this
    repo's CI boots the image, so this is all there is. It cannot see a later
    `RUN rm -rf /app/.venv`, nor `ENV UV_NO_INSTALL_PROJECT=1`, which is a
    real uv variable (triage-0520). Only a built image could.
    """
    syncs = _uv_syncs((REPO / "Dockerfile").read_text())
    assert syncs, "the Dockerfile never runs `uv sync`, so no venv is built"
    # Any --no-install-* flag disables it, not just --no-install-project:
    # --no-install-package and --no-install-workspace do too.
    assert [s for s in syncs if "--no-install-" not in s], (
        "every `uv sync` carries a --no-install-* flag, so the project itself "
        "is never installed and the baked venv cannot import it"
    )


def _installs_the_project(dockerfile: str) -> bool:
    """Whether some `uv sync` puts the project itself into the venv."""
    return any("--no-install-" not in s for s in _uv_syncs(dockerfile))


# The rules above are exercised by exactly ONE branch of the real Dockerfile,
# so mutating any of the others away leaves every test green. These cases are
# the regression test for the rules themselves: synthetic Dockerfiles, derived
# from EXPECTED_START so they hold for whichever service this repo builds.
_RIGHT = json.dumps(EXPECTED_START)
_WRONG = json.dumps(["python"] + EXPECTED_START[1:])
_VIA_UV = json.dumps(["uv", "run"] + EXPECTED_START)

START_CASES = [
    ("the exec-form start command", f"CMD {_RIGHT}", True),
    (
        "a stale earlier CMD with a correct final one",
        f"CMD {_VIA_UV}\nCMD {_RIGHT}",
        True,
    ),
    ("an ENTRYPOINT [] reset", f"ENTRYPOINT []\nCMD {_RIGHT}", True),
    ("a lowercase cmd keyword", f"cmd {_RIGHT}", True),
    (
        "an earlier stage's CMD, which is dead",
        f"FROM busybox AS build\nCMD {_WRONG}\nFROM python:3.11-slim\nCMD {_RIGHT}",
        True,
    ),
    ("the correct CMD followed by a wrong one", f"CMD {_RIGHT}\nCMD {_WRONG}", False),
    ("the command run through uv", f"CMD {_VIA_UV}", False),
    ("a bare interpreter from PATH", f"CMD {_WRONG}", False),
    (
        "a shell wrapper hiding an install",
        'CMD ["sh", "-c", "pip install -e . && x"]',
        False,
    ),
    (
        "a shell-form ENTRYPOINT, which discards the CMD",
        f"ENTRYPOINT {EXPECTED_START[0]}\nCMD {_RIGHT}",
        False,
    ),
    (
        "an exec ENTRYPOINT with a shell-form CMD",
        f'ENTRYPOINT ["{EXPECTED_START[0]}"]\nCMD {" ".join(EXPECTED_START[1:])}',
        False,
    ),
    ("a lone shell-form CMD", "CMD " + " ".join(EXPECTED_START), False),
]

SYNC_CASES = [
    ("a plain sync", "RUN uv sync --frozen --no-dev", True),
    ("a sync after a cd", "RUN cd /app && uv sync --frozen", True),
    (
        "a sync behind a cache mount",
        "RUN --mount=type=cache,target=/r uv sync --frozen",
        True,
    ),
    (
        "a sync behind an env assignment",
        "RUN UV_COMPILE_BYTECODE=1 uv sync --frozen",
        True,
    ),
    ("a flag before the subcommand", "RUN uv --no-cache sync --frozen", True),
    (
        "a flagged sync and a clean one",
        "RUN uv sync --no-install-project\nRUN uv sync --frozen",
        True,
    ),
    ("no sync at all", "RUN pip install .", False),
    ("only a decoy", "RUN echo uv sync", False),
    ("--no-install-project", "RUN uv sync --frozen --no-install-project", False),
    ("--no-install-package", "RUN uv sync --frozen --no-install-package x", False),
    ("--no-install-workspace", "RUN uv sync --frozen --no-install-workspace", False),
    (
        "a decoy beside a flagged sync",
        "RUN echo uv sync\nRUN uv sync --no-install-project",
        False,
    ),
]


@pytest.mark.parametrize(
    "body,is_baked",
    [(b, ok) for _, b, ok in START_CASES],
    ids=[n for n, _, _ in START_CASES],
)
def test_the_start_command_rules(body: str, is_baked: bool):
    dockerfile = "FROM python:3.11-slim\n" + body + "\n"
    assert (_effective_argv(dockerfile) == EXPECTED_START) is is_baked


@pytest.mark.parametrize(
    "body,installs",
    [(b, ok) for _, b, ok in SYNC_CASES],
    ids=[n for n, _, _ in SYNC_CASES],
)
def test_the_uv_sync_rules(body: str, installs: bool):
    dockerfile = "FROM python:3.11-slim\n" + body + "\n"
    assert _installs_the_project(dockerfile) is installs


# Rules 1-4 as a docker MODEL, asserted on the resolved argv itself. The
# boolean cases above cannot see rule 4: rule 3 already turns shell form into
# /bin/sh -c, so dropping rule 4 can never produce something EQUAL to
# EXPECTED_START, only something differently wrong. Each expectation below was
# measured against docker with a printf stub as the entrypoint.
RESOLUTION_CASES = [
    ("only the last CMD counts", 'CMD ["a"]\nCMD ["b"]', ["b"]),
    (
        "only the last ENTRYPOINT counts",
        'ENTRYPOINT ["a"]\nENTRYPOINT ["b"]\nCMD ["c"]',
        ["b", "c"],
    ),
    (
        "CMD supplies arguments to an exec ENTRYPOINT",
        'ENTRYPOINT ["a"]\nCMD ["b"]',
        ["a", "b"],
    ),
    (
        "a shell-form ENTRYPOINT discards the CMD",
        'ENTRYPOINT a b\nCMD ["c"]',
        ["/bin/sh", "-c", "a b"],
    ),
    ("a shell-form CMD runs through the shell", "CMD a b", ["/bin/sh", "-c", "a b"]),
    (
        "only the final build stage counts",
        'FROM x\nCMD ["a"]\nFROM y\nCMD ["b"]',
        ["b"],
    ),
    # The case that actually NEEDS rule 2: with a CMD only in an earlier
    # stage, "the last CMD wins" alone would resurrect it.
    (
        "an earlier stage's CMD does not carry over",
        'FROM x\nCMD ["a"]\nFROM y\nRUN true',
        [],
    ),
    ("a trailing comment after the JSON", 'CMD ["a"] # note', ["a"]),
    ("neither instruction", "RUN true", []),
]


@pytest.mark.parametrize(
    "body,argv",
    [(b, a) for _, b, a in RESOLUTION_CASES],
    ids=[n for n, _, _ in RESOLUTION_CASES],
)
def test_the_resolution_rules(body: str, argv: list[str]):
    assert _effective_argv("FROM python:3.11-slim\n" + body + "\n") == argv
