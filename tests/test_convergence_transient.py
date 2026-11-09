"""Which convergence LLM failures leave mail behind (triage-6c9f)."""

from __future__ import annotations

import httpx
import pytest
from cortex_utils.llm import LLMError, LLMHTTPError

from triage.services.worker import is_transient


def _caused_by(cause: BaseException) -> LLMError:
    try:
        raise LLMError("LLM network error") from cause
    except LLMError as e:
        return e


@pytest.mark.parametrize(
    "error, transient",
    [
        (_caused_by(httpx.ReadTimeout("timed out")), True),
        (_caused_by(httpx.ConnectError("refused")), True),
        (LLMHTTPError("503", 503), True),
        (LLMHTTPError("429", 429), True),
        (LLMHTTPError("400", 400), False),
        (_caused_by(ValueError("bad json")), False),
        (LLMError("LLM returned unexpected response format"), False),
    ],
)
def test_is_transient(error: LLMError, transient: bool) -> None:
    assert is_transient(error) is transient
