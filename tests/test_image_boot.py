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
import shlex

REPO = pathlib.Path(__file__).resolve().parent.parent

# The one correct start command. Asserted whole rather than probed for
# properties: there is exactly one right answer here, so equality is both the
# simplest assertion and the strongest. It rejects a boot-time `pip install`,
# a `python -c` that shells out, and a `/app/.venv/bin/../../..` path that
# escapes the venv -- none of which a first-token check refuses, and none of
# which needs naming to exclude.
EXPECTED_START = ["/app/.venv/bin/python", "-m", "triage.services.worker"]


def _effective_argv(dockerfile: str) -> list[str]:
    """The argv docker would actually start, as docker resolves it.

    Only the LAST ENTRYPOINT and the LAST CMD count -- an earlier one of
    either is dead text. Collecting every match reads a stale line as live,
    which both passes a Dockerfile whose final CMD is wrong and fails one
    whose final CMD is right.
    """
    # A command split across lines is ONE command; a per-line scan reads its
    # halves as unrelated.
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    # Only the final build stage ships, so earlier stages' CMDs are not ours.
    joined = joined[joined.rfind("\nFROM ") + 1 :] if "\nFROM " in joined else joined

    def last(kind: str) -> list[str]:
        found = re.findall(rf"^\s*{kind}\s+(.+)$", joined, re.M)
        if not found:
            return []
        raw = found[-1].strip()
        if raw.startswith("["):
            # raw_decode, so a trailing comment after the JSON array is
            # ignored rather than fatal.
            return json.JSONDecoder().raw_decode(raw)[0]
        return shlex.split(raw, comments=True)

    # CMD supplies ARGUMENTS to an exec-form ENTRYPOINT, hence this order.
    return last("ENTRYPOINT") + last("CMD")


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

    A STATIC APPROXIMATION of a build-time property. It cannot see a later
    `RUN rm -rf /app/.venv`, and only a built image could. The CI smoke in
    cortex-utils is the real check; this is what is affordable in a repo whose
    CI has no docker.
    """
    joined = re.sub(r"\\\n\s*", " ", (REPO / "Dockerfile").read_text())
    # `RUN uv sync`, anchored: `RUN echo uv sync` is not a sync.
    syncs = re.findall(r"^\s*RUN\s+uv\s+sync\b(.*)$", joined, re.M)
    assert syncs, "the Dockerfile never runs `uv sync`, so no venv is built"
    # Any --no-install-* flag disables it, not just --no-install-project:
    # --no-install-package and --no-install-workspace do too.
    assert [s for s in syncs if "--no-install-" not in s], (
        "every `uv sync` carries a --no-install-* flag, so the project itself "
        "is never installed and the baked venv cannot import it"
    )
