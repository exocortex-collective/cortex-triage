"""Tests for the teach bot's pure + DB logic (cortex-uo9b.3)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from cortex_utils.learning import RuleProposal

import triage.services.teach_bot as tb
from triage.services.teach_bot import (
    APPROVE,
    REJECT,
    TeachBot,
    TeachConfig,
    format_proposal,
)


def make_proposal(**kw: object) -> RuleProposal:
    base = dict(
        id=1,
        sender="bob@x.com",
        label="Cortex/Foo",
        direction="add",
        status="pending",
        source="teach",
        opportunity_count=1,
        discord_message_id=None,
    )
    base.update(kw)
    return RuleProposal(**base)  # type: ignore[arg-type]


def make_bot() -> TeachBot:
    bot = TeachBot.__new__(TeachBot)
    bot.config = TeachConfig(
        token="t", channel_id=1, dsn="dsn", poll_interval_seconds=60
    )
    return bot


def patch_conn(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    monkeypatch.setattr(tb.psycopg2, "connect", lambda dsn: conn)
    return conn


# --- format_proposal (pure) ---


def test_format_add_proposal() -> None:
    text = format_proposal(make_proposal(direction="add"))
    assert "Label mail from **bob@x.com** as `Cortex/Foo`?" in text
    assert APPROVE in text and REJECT in text


def test_format_remove_proposal() -> None:
    text = format_proposal(make_proposal(direction="remove"))
    assert "Stop labeling mail from **bob@x.com** as `Cortex/Foo`?" in text


def test_format_shows_repeat_count() -> None:
    assert "seen 4×" in format_proposal(make_proposal(opportunity_count=4))
    assert "seen" not in format_proposal(make_proposal(opportunity_count=1))


# --- TeachConfig.from_env ---


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in {
        "DISCORD_TEACH_BOT_TOKEN": "tok",
        "DISCORD_TEACH_CHANNEL_ID": "12345",
        "POSTGRES_HOST": "db",
        "POSTGRES_DB": "cortex",
        "POSTGRES_USER": "cortex",
        "POSTGRES_PASSWORD": "secret",
        "TEACH_POLL_INTERVAL_SECONDS": "30",
    }.items():
        monkeypatch.setenv(k, v)
    config = TeachConfig.from_env()
    assert config.token == "tok"
    assert config.channel_id == 12345
    assert config.poll_interval_seconds == 30
    assert "dbname=cortex" in config.dsn and "host=db" in config.dsn


def test_config_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TEACH_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_TEACH_CHANNEL_ID", "1")
    with pytest.raises(ValueError):
        TeachConfig.from_env()


# --- _promote_and_fetch ---


def test_promote_and_fetch_returns_unposted(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposals = [make_proposal(id=7)]
    promote = MagicMock()
    monkeypatch.setattr(tb, "propose_from_opportunities", promote)
    monkeypatch.setattr(tb, "unposted_pending_proposals", lambda conn: proposals)
    result = make_bot()._promote_and_fetch()
    assert result == proposals
    promote.assert_called_once()


# --- _decide ---


def test_decide_approve_returns_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=5)
    monkeypatch.setattr(tb, "get_proposal_by_message_id", lambda conn, mid: proposal)
    approve = MagicMock(return_value=True)
    monkeypatch.setattr(tb, "approve_proposal", approve)
    monkeypatch.setattr(tb, "reject_proposal", MagicMock())
    result = make_bot()._decide("msg-1", approved=True)
    assert result is proposal
    approve.assert_called_once()


def test_decide_reject_uses_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=5)
    monkeypatch.setattr(tb, "get_proposal_by_message_id", lambda conn, mid: proposal)
    reject = MagicMock(return_value=True)
    monkeypatch.setattr(tb, "approve_proposal", MagicMock())
    monkeypatch.setattr(tb, "reject_proposal", reject)
    assert make_bot()._decide("msg-1", approved=False) is proposal
    reject.assert_called_once()


def test_decide_unknown_message_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    monkeypatch.setattr(tb, "get_proposal_by_message_id", lambda conn, mid: None)
    assert make_bot()._decide("msg-x", approved=True) is None


def test_decide_already_decided_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proposal exists but the transition was a no-op (already decided).
    patch_conn(monkeypatch)
    monkeypatch.setattr(
        tb, "get_proposal_by_message_id", lambda conn, mid: make_proposal()
    )
    monkeypatch.setattr(tb, "approve_proposal", lambda conn, pid: False)
    assert make_bot()._decide("msg-1", approved=True) is None
