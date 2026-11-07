"""The image must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needs working DNS --
and a redeploy, or an ofelia job with `delete: true`, makes fresh ones.

Measured from a git-archive build, so the file modes are the ones CI sees,
with --network none:

    pre-fix image   -> rc=2, dns error fetching hatchling
    fixed image     -> reaches the worker's own env validation

Unlike cortex-utils, this image needs no change to its `uv sync`: the sync
runs against a src/triage/__init__.py stub and installs the project EDITABLE,
so the venv's .pth points at /app/src, which the later `COPY src/` fills in.
Only the start command had to change. Verified by running
`/app/.venv/bin/python -c "import triage.services.worker"` in the pre-fix
image with --network none, which succeeds.
"""

from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parent.parent

# The venv's bin DIRECTORY, not `python`: a console script baked into that venv
# needs no resolution at boot either, and pinning an interpreter name would
# refuse a legitimate /app/.venv/bin/<script>.
VENV_BIN = "/app/.venv/bin/"


def test_the_start_command_is_the_baked_venv():
    """Asserts the PROPERTY -- the start command IS the baked venv -- rather
    than forbidding a spelling. Blacklisting "uv run" does not work: `uvx`,
    `uv  run`, `uv<TAB>run` and a bare `python` all walk past it."""
    # Line-continuations joined first: a command split across lines is ONE
    # command, and a per-line scan reads its halves as unrelated.
    joined = re.sub(r"\\\n\s*", " ", (REPO / "Dockerfile").read_text())
    cmds = re.findall(r"^\s*(?:CMD|ENTRYPOINT)\s+(.+)$", joined, re.M | re.I)
    assert cmds, "the Dockerfile declares no CMD or ENTRYPOINT"
    # Joined, so an ENTRYPOINT holding the interpreter and a CMD holding only
    # its arguments are judged as the one command they combine into.
    start = " ".join(cmds)
    assert VENV_BIN in start, (
        f"the start command must invoke the baked venv by its absolute path, "
        f"or it is resolving the project at boot: {start}"
    )
    # `\buvx?\b`, not the substring "uv": that would refuse `uvicorn`.
    assert not re.search(
        r"\buvx?\b", start
    ), f"the start command goes through uv, so it needs pypi at boot: {start}"
