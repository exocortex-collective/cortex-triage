"""The image must be able to start without the network.

`uv run` re-resolves the project on a container's first start and fetches
hatchling from pypi to do it, so every fresh container needs working DNS --
and a redeploy, or an ofelia job with `delete: true`, makes fresh ones.

Measured from a git-archive build, so the file modes are the ones CI sees,
with --network none:

    pre-fix image   -> rc=2, dns error fetching hatchling
    fixed image     -> reaches the worker's own env validation

SCOPE, because it is narrower than it looks: this covers the IMAGE DEFAULT
only. A compose `command:` OVERRIDES the image CMD, and in Portainer stack 48
nine of twelve cortex services pass their own `uv run python -m ...`, so the
image being correct does not make production correct. The stack commands are
tracked in cortex-4da6 and have to change too.
"""

from __future__ import annotations

import json
import pathlib
import re
import shlex

REPO = pathlib.Path(__file__).resolve().parent.parent

# The venv's bin DIRECTORY, not `python`: a console script baked into that venv
# needs no resolution at boot either, and pinning an interpreter name would
# refuse a legitimate /app/.venv/bin/<script>.
VENV_BIN = "/app/.venv/bin/"


def _start_argv(dockerfile: str) -> list[str]:
    """The argv a container starts with: ENTRYPOINT then CMD, PARSED.

    Parsed rather than matched as text. A substring check passes on an argv
    that merely MENTIONS the venv -- ["sh", "-c", "... /app/.venv/bin/python
    ..."], or a trailing "# /app/.venv/bin/" comment -- and both of those
    still resolve the project at boot.
    """
    # A command split across lines is ONE command; a per-line scan reads its
    # halves as unrelated.
    joined = re.sub(r"\\\n\s*", " ", dockerfile)
    argv: list[str] = []
    for kind in ("ENTRYPOINT", "CMD"):
        for m in re.finditer(rf"^\s*{kind}\s+(.+)$", joined, re.M | re.I):
            raw = m.group(1).strip()
            if raw.startswith("["):
                # raw_decode, so a trailing comment after the JSON is ignored
                # rather than making this blow up.
                argv += json.JSONDecoder().raw_decode(raw)[0]
            else:
                argv += shlex.split(raw, comments=True)
    return argv


def test_the_start_command_is_the_baked_venv():
    """Asserts the PROPERTY -- the container STARTS with the baked venv --
    rather than forbidding a spelling. Blacklisting "uv run" does not work:
    uvx, "uv  run", a tab between the words, and a bare python all walk past
    it."""
    argv = _start_argv((REPO / "Dockerfile").read_text())
    assert argv, "the Dockerfile declares no CMD or ENTRYPOINT"

    # The FIRST token. A substring check over the whole command is what this
    # replaced, and it passed on an sh -c wrapper and on a trailing comment.
    assert argv[0].startswith(VENV_BIN), (
        f"the container must START with the baked venv's absolute path, or it "
        f"resolves the project at boot: {argv}"
    )
    # \buvx?\b, not the substring "uv": that would refuse uvicorn.
    assert not re.search(
        r"\buvx?\b", " ".join(argv)
    ), f"the start command goes through uv, so it needs pypi at boot: {argv}"


def test_the_venv_contains_the_project_at_build_time():
    """The other half of the bug, and why the start command alone is not
    enough: pointing at the baked venv only works if the project is IN it.

    A `uv sync` carrying --no-install-project leaves no .pth, so the boot
    fails with ModuleNotFoundError instead of a network error -- a different
    symptom, the same outage.
    """
    joined = re.sub(r"\\\n\s*", " ", (REPO / "Dockerfile").read_text())
    syncs = re.findall(r"^\s*RUN\s+.*\buv\s+sync\b.*$", joined, re.M)
    assert syncs, "the Dockerfile never runs `uv sync`, so no venv is built"
    assert [s for s in syncs if "--no-install-project" not in s], (
        "every `uv sync` carries --no-install-project, so the project itself "
        "is never installed and the baked venv cannot import it"
    )
