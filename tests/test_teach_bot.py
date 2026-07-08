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
        token="t",
        channel_id=1,
        db_params={"host": "db", "dbname": "cortex"},
        poll_interval_seconds=60,
        gateway_url="http://gw:8097",
        apply_interval_seconds=30,
        apply_batch_limit=20,
    )
    bot._http = MagicMock()
    return bot


def patch_conn(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.__exit__.return_value = False
    monkeypatch.setattr(tb.psycopg2, "connect", lambda *a, **k: conn)
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
        "GATEWAY_URL": "http://gw:8097/",
    }.items():
        monkeypatch.setenv(k, v)
    config = TeachConfig.from_env()
    assert config.token == "tok"
    assert config.channel_id == 12345
    assert config.poll_interval_seconds == 30
    assert config.db_params["dbname"] == "cortex"
    assert config.db_params["host"] == "db"
    assert config.gateway_url == "http://gw:8097"  # trailing slash stripped


def test_config_from_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set everything, then drop only the token, so the error is about it.
    for k, v in {
        "DISCORD_TEACH_BOT_TOKEN": "tok",
        "DISCORD_TEACH_CHANNEL_ID": "1",
        "POSTGRES_HOST": "db",
        "POSTGRES_DB": "cortex",
        "POSTGRES_USER": "cortex",
        "POSTGRES_PASSWORD": "secret",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DISCORD_TEACH_BOT_TOKEN")
    with pytest.raises(ValueError, match="DISCORD_TEACH_BOT_TOKEN"):
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


def test_decide_approve_returns_proposal_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=5)
    monkeypatch.setattr(tb, "get_proposal_by_message_id", lambda conn, mid: proposal)
    approve = MagicMock(return_value=True)
    enqueue = MagicMock()
    monkeypatch.setattr(tb, "approve_proposal", approve)
    monkeypatch.setattr(tb, "enqueue_proposal_apply", enqueue)
    monkeypatch.setattr(tb, "reject_proposal", MagicMock())
    result = make_bot()._decide("msg-1", approved=True)
    assert result is proposal
    approve.assert_called_once()
    # An approved proposal enqueues its apply job atomically.
    assert enqueue.call_args.args[1] == 5


def test_decide_approve_noop_does_not_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the approve was a no-op (already decided), no apply job is enqueued.
    patch_conn(monkeypatch)
    monkeypatch.setattr(
        tb, "get_proposal_by_message_id", lambda conn, mid: make_proposal()
    )
    monkeypatch.setattr(tb, "approve_proposal", lambda conn, pid: False)
    enqueue = MagicMock()
    monkeypatch.setattr(tb, "enqueue_proposal_apply", enqueue)
    assert make_bot()._decide("msg-1", approved=True) is None
    enqueue.assert_not_called()


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


# --- _strip_prefix (pure) ---


def test_strip_prefix_removes_managed_prefix() -> None:
    assert tb._strip_prefix("Cortex/Home/Redfin", "Cortex") == "Home/Redfin"


def test_strip_prefix_leaves_unprefixed_label() -> None:
    assert tb._strip_prefix("Home/Redfin", "Cortex") == "Home/Redfin"


def test_strip_prefix_no_prefix_configured() -> None:
    assert tb._strip_prefix("Cortex/Foo", None) == "Cortex/Foo"


# --- _upsert_mapping (Gateway client) ---


def _resp(status: int, json_body: object = None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.json.return_value = json_body
    return r


def test_upsert_mapping_posts_new_priority_mapping() -> None:
    bot = make_bot()
    bot._http.post.return_value = _resp(201)
    bot._upsert_mapping("bob@x.com", "Home/Redfin", archive=True)
    url, kwargs = bot._http.post.call_args.args[0], bot._http.post.call_args.kwargs
    assert url == "http://gw:8097/mappings"
    assert kwargs["json"] == {
        "type": "priority",
        "email": "bob@x.com",
        "label": "Home/Redfin",
        "archive": True,
    }
    assert kwargs["headers"]["X-Created-By"] == "teach-bot"


def test_upsert_mapping_omits_archive_when_not_archived() -> None:
    bot = make_bot()
    bot._http.post.return_value = _resp(201)
    bot._upsert_mapping("bob@x.com", "Home/Redfin", archive=False)
    assert "archive" not in bot._http.post.call_args.kwargs["json"]


def test_upsert_mapping_on_conflict_puts_existing() -> None:
    bot = make_bot()
    bot._http.post.return_value = _resp(409, text="exists")
    bot._http.get.return_value = _resp(
        200, {"mappings": [{"id": 42, "email_address": "bob@x.com"}]}
    )
    bot._http.put.return_value = _resp(200)
    bot._upsert_mapping("bob@x.com", "Home/Redfin", archive=True)
    put_url = bot._http.put.call_args.args[0]
    assert put_url == "http://gw:8097/mappings/42"
    assert bot._http.put.call_args.kwargs["json"] == {
        "label": "Home/Redfin",
        "archive": True,
    }
    assert bot._http.put.call_args.kwargs["headers"]["X-Updated-By"] == "teach-bot"


def test_upsert_mapping_raises_on_unexpected_status() -> None:
    bot = make_bot()
    bot._http.post.return_value = _resp(500, text="boom")
    with pytest.raises(RuntimeError, match="500"):
        bot._upsert_mapping("bob@x.com", "Home/Redfin", archive=False)


def test_move_existing_raises_when_mapping_absent() -> None:
    bot = make_bot()
    bot._http.get.return_value = _resp(200, {"mappings": []})
    with pytest.raises(RuntimeError, match="vanished"):
        bot._move_existing_mapping("bob@x.com", "Home/Redfin", archive=False)


# --- _apply_add wiring ---


def test_apply_add_strips_prefix_and_passes_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_conn(monkeypatch)
    monkeypatch.setattr(tb, "active_label_prefix", lambda conn: "Cortex")
    monkeypatch.setattr(tb, "taught_mail_archived", lambda conn, sender, label: True)
    bot = make_bot()
    calls = []
    bot._upsert_mapping = lambda email, label, archive: calls.append(  # type: ignore[method-assign]
        (email, label, archive)
    )
    bot._apply_add(make_proposal(sender="bob@x.com", label="Cortex/Home/Redfin"))
    assert calls == [("bob@x.com", "Home/Redfin", True)]


# --- _apply_one outcomes ---


def test_apply_one_defers_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=9, direction="remove", status="approved")
    monkeypatch.setattr(tb, "get_proposal_by_id", lambda conn, pid: proposal)
    deferred = MagicMock(return_value=True)
    monkeypatch.setattr(tb, "mark_proposal_deferred", deferred)
    monkeypatch.setattr(tb, "complete_apply_job", MagicMock())
    job = tb.ApplyJob(job_id=1, proposal_id=9, attempts=0, max_attempts=3)
    assert make_bot()._apply_one(job) == "deferred"
    deferred.assert_called_once()


def test_apply_one_applies_add(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=6, direction="add", status="approved")
    monkeypatch.setattr(tb, "get_proposal_by_id", lambda conn, pid: proposal)
    applied = MagicMock(return_value=True)
    monkeypatch.setattr(tb, "mark_proposal_applied", applied)
    monkeypatch.setattr(tb, "complete_apply_job", MagicMock())
    bot = make_bot()
    bot._apply_add = MagicMock()  # type: ignore[method-assign]
    job = tb.ApplyJob(job_id=2, proposal_id=6, attempts=0, max_attempts=3)
    assert bot._apply_one(job) == "applied"
    bot._apply_add.assert_called_once_with(proposal)
    applied.assert_called_once()


def test_apply_one_noop_when_not_approved(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=6, status="applied")  # already terminal
    monkeypatch.setattr(tb, "get_proposal_by_id", lambda conn, pid: proposal)
    complete = MagicMock()
    monkeypatch.setattr(tb, "complete_apply_job", complete)
    job = tb.ApplyJob(job_id=3, proposal_id=6, attempts=0, max_attempts=3)
    assert make_bot()._apply_one(job) == "no-op (not approved)"
    complete.assert_called_once()


def test_apply_one_retries_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_conn(monkeypatch)
    proposal = make_proposal(id=6, direction="add", status="approved")
    monkeypatch.setattr(tb, "get_proposal_by_id", lambda conn, pid: proposal)
    monkeypatch.setattr(tb, "complete_apply_job", MagicMock())
    monkeypatch.setattr(tb, "mark_proposal_failed", MagicMock())
    monkeypatch.setattr(tb, "fail_or_retry", lambda conn, jid, exc, maxa: "retrying")
    bot = make_bot()

    def boom(_proposal: object) -> None:
        raise RuntimeError("gateway down")

    bot._apply_add = boom  # type: ignore[method-assign]
    job = tb.ApplyJob(job_id=4, proposal_id=6, attempts=0, max_attempts=3)
    assert bot._apply_one(job) == "retrying"
