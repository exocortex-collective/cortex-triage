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
        # re.I: docker instruction keywords are case-insensitive, so a
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
            # Leading RUN flags (--mount=...) sit before the command itself.
            segment = re.sub(r"^\s*(?:--\S+\s+)*", "", segment)
            if re.match(r"uv\s+sync\b", segment):
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
