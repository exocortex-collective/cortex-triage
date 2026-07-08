"""Teach bot: presents learned rule proposals in Discord for confirmation.

Part of teach-by-labeling (cortex-uo9b.3). Runs as its own container from the
triage image::

    python -m triage.services.teach_bot

On an interval it rolls new ``learning_opportunities`` into ``rule_proposals``
(cortex-utils), then posts any not-yet-presented pending proposal to a Discord
channel with ✅ / ❌ reactions. A reaction resolves the proposal
(approve / reject). Committing an approved proposal to triage config is uo9b.4.

The proposal↔message link lives in the DB, so reactions still resolve after a
restart and proposals are never double-posted.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import discord
import httpx
import psycopg2
from cortex_utils.learning import (
    APPLY_QUEUE,
    REMOVE,
    ApplyJob,
    RuleProposal,
    active_label_prefix,
    approve_proposal,
    claim_proposal_apply_jobs,
    complete_apply_job,
    enqueue_proposal_apply,
    ensure_learning_schema,
    ensure_proposals_schema,
    get_proposal_by_id,
    get_proposal_by_message_id,
    mark_proposal_applied,
    mark_proposal_deferred,
    mark_proposal_failed,
    mark_proposal_posted,
    propose_from_opportunities,
    reject_proposal,
    unposted_pending_proposals,
)
from cortex_utils.logging import configure_logging, get_logger
from cortex_utils.metrics import start_metrics_server
from cortex_utils.queue.retry import fail_or_retry
from discord.ext import commands, tasks

configure_logging(service_name="teach-bot", level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger(__name__)

APPROVE = "✅"
REJECT = "❌"

# Actor recorded on mappings the bot creates/updates (Gateway audit headers).
APPLY_ACTOR = "teach-bot"
# Mappings are keyed on a specific sender, so they belong in the priority list
# (O(1), overrides rules) — the "direct email filter" path, not a rule sweep.
APPLY_MAPPING_TYPE = "priority"


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} environment variable not set")
    return value


@dataclass
class TeachConfig:
    token: str
    channel_id: int
    db_params: dict[str, str]
    poll_interval_seconds: int
    gateway_url: str
    apply_interval_seconds: int
    apply_batch_limit: int

    @classmethod
    def from_env(cls) -> TeachConfig:
        # Kwargs (not a DSN string) so special characters in the password can't
        # break connection-string parsing.
        db_params = {
            "host": _require("POSTGRES_HOST"),
            "port": os.getenv("POSTGRES_PORT", "5432"),
            "dbname": _require("POSTGRES_DB"),
            "user": _require("POSTGRES_USER"),
            "password": _require("POSTGRES_PASSWORD"),
        }
        return cls(
            token=_require("DISCORD_TEACH_BOT_TOKEN"),
            channel_id=int(_require("DISCORD_TEACH_CHANNEL_ID")),
            db_params=db_params,
            poll_interval_seconds=int(os.getenv("TEACH_POLL_INTERVAL_SECONDS", "60")),
            # Base URL of the Cortex Gateway (mappings API). Required — no
            # default, to avoid baking a homelab host into the image.
            gateway_url=_require("GATEWAY_URL").rstrip("/"),
            apply_interval_seconds=int(os.getenv("TEACH_APPLY_INTERVAL_SECONDS", "30")),
            apply_batch_limit=int(os.getenv("TEACH_APPLY_BATCH_LIMIT", "20")),
        )


def _strip_prefix(label: str, prefix: str | None) -> str:
    """Drop the managed ``prefix/`` from a label — mapping labels are stored
    raw and re-prefixed at triage-eval time. ``Cortex/Home/Redfin`` -> ``Home/
    Redfin``."""
    if not prefix:
        return label
    head = f"{prefix}/"
    return label[len(head) :] if label.startswith(head) else label


def format_proposal(proposal: RuleProposal) -> str:
    """Human-readable prompt for a proposal, with ✅ / ❌ instructions."""
    sender = discord.utils.escape_markdown(proposal.sender)
    if proposal.direction == "add":
        action = f"Label mail from **{sender}** as `{proposal.label}`?"
    else:
        action = f"Stop labeling mail from **{sender}** as `{proposal.label}`?"
    seen = (
        f" _(seen {proposal.opportunity_count}×)_"
        if proposal.opportunity_count > 1
        else ""
    )
    return f"**Rule proposal**{seen}\n{action}\n{APPROVE} yes · {REJECT} no"


class TeachBot(commands.Bot):
    """Discord bot that presents rule proposals and records decisions."""

    def __init__(self, config: TeachConfig) -> None:
        intents = discord.Intents.default()
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.poll_task.change_interval(seconds=config.poll_interval_seconds)
        self.apply_task.change_interval(seconds=config.apply_interval_seconds)
        # Sync client; apply runs in a worker thread via asyncio.to_thread.
        self._http = httpx.Client(timeout=30.0)

    # --- DB (blocking; called via asyncio.to_thread from async contexts) ---

    @contextmanager
    def _db_connection(self) -> Iterator[psycopg2.extensions.connection]:
        """A connection that commits/rolls back the transaction and always
        closes — psycopg2's ``with conn`` only manages the transaction."""
        conn = psycopg2.connect(**self.config.db_params)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._db_connection() as conn:
            ensure_learning_schema(conn)
            ensure_proposals_schema(conn)

    def _promote_and_fetch(self) -> list[RuleProposal]:
        """Roll opportunities into proposals, return the ones still unposted."""
        with self._db_connection() as conn:
            propose_from_opportunities(conn)
            return unposted_pending_proposals(conn)

    def _mark_posted(self, proposal_id: int, message_id: str) -> None:
        with self._db_connection() as conn:
            mark_proposal_posted(conn, proposal_id, message_id)

    def _decide(self, message_id: str, approved: bool) -> RuleProposal | None:
        """Resolve the proposal a reacted-to message presents. Returns it if
        the reaction actually transitioned a pending proposal, else None.

        Approving also enqueues a ``proposal_apply`` job in the same
        transaction, so the approval and the work to commit it land atomically —
        the apply loop then picks it up.
        """
        with self._db_connection() as conn:
            proposal = get_proposal_by_message_id(conn, message_id)
            if proposal is None:
                return None
            if approved:
                changed = approve_proposal(conn, proposal.id)
                if changed:
                    enqueue_proposal_apply(conn, proposal.id)
            else:
                changed = reject_proposal(conn, proposal.id)
            return proposal if changed else None

    # --- Discord lifecycle ---

    async def setup_hook(self) -> None:
        # setup_hook runs exactly once on startup; on_ready can fire on every
        # reconnect, so schema + the poll loop belong here.
        await asyncio.to_thread(self._ensure_schema)
        self.poll_task.start()
        self.apply_task.start()

    async def on_ready(self) -> None:
        logger.info(f"Teach bot ready as {self.user}")

    async def close(self) -> None:
        self.poll_task.cancel()
        self.apply_task.cancel()
        self._http.close()
        await super().close()

    @tasks.loop(seconds=60.0)
    async def poll_task(self) -> None:
        try:
            channel = self.get_channel(self.config.channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.config.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                logger.error(f"Channel {self.config.channel_id} is not messageable")
                return
            proposals = await asyncio.to_thread(self._promote_and_fetch)
            for proposal in proposals:
                try:
                    await self._post_proposal(channel, proposal)
                except Exception:
                    logger.error(
                        f"Failed to post proposal {proposal.id}", exc_info=True
                    )
        except Exception:
            logger.error("Teach poll failed", exc_info=True)

    @poll_task.before_loop
    async def _before_poll(self) -> None:
        # Don't poll until the gateway connection is ready.
        await self.wait_until_ready()

    # --- Apply loop: commit approved proposals as mappings (uo9b.4) ---

    @tasks.loop(seconds=30.0)
    async def apply_task(self) -> None:
        try:
            applied = await asyncio.to_thread(self._drain_apply_jobs)
            for proposal_id, outcome in applied:
                logger.info(f"Apply job for proposal {proposal_id}: {outcome}")
        except Exception:
            logger.error("Teach apply loop failed", exc_info=True)

    @apply_task.before_loop
    async def _before_apply(self) -> None:
        await self.wait_until_ready()

    def _drain_apply_jobs(self) -> list[tuple[int, str]]:
        """Claim ready apply jobs and process each. Returns (proposal_id,
        outcome) pairs for logging."""
        with self._db_connection() as conn:
            self._reclaim_stale_apply_jobs(conn)
            jobs = claim_proposal_apply_jobs(conn, limit=self.config.apply_batch_limit)
        return [(job.proposal_id, self._apply_one(job)) for job in jobs]

    def _reclaim_stale_apply_jobs(self, conn: psycopg2.extensions.connection) -> None:
        """Return apply jobs stuck in 'processing' (bot crashed mid-apply) to
        'pending' so they retry. The apply itself is idempotent (POST→409→PUT)."""
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE queue SET status = 'pending', claimed_at = NULL "
                "WHERE queue_name = %s AND status = 'processing' "
                "AND claimed_at < NOW() - INTERVAL '10 minutes'",
                (APPLY_QUEUE,),
            )

    def _apply_one(self, job: ApplyJob) -> str:
        """Process one apply job in its own transaction. On a transient failure
        the job is retried (or finally failed) via ``fail_or_retry``."""
        with self._db_connection() as conn:
            proposal = get_proposal_by_id(conn, job.proposal_id)
        # Only approved proposals have work to do; anything else (already
        # applied/deferred by a duplicate job, or rejected) just closes the job.
        if proposal is None or proposal.status != "approved":
            with self._db_connection() as conn:
                complete_apply_job(conn, job.job_id)
            return "no-op (not approved)"

        try:
            if proposal.direction == REMOVE:
                # A "stop labeling as X" signal has no clean auto-action; record
                # it for manual handling. (Archiving is captured on the add side
                # via the mapping's archive flag.)
                with self._db_connection() as conn:
                    mark_proposal_deferred(
                        conn, proposal.id, "remove — recorded for manual handling"
                    )
                    complete_apply_job(conn, job.job_id)
                return "deferred"

            self._apply_add(proposal)
            with self._db_connection() as conn:
                mark_proposal_applied(conn, proposal.id)
                complete_apply_job(conn, job.job_id)
            return "applied"
        except Exception as exc:
            logger.error(
                f"Failed to apply proposal {proposal.id} "
                f"({proposal.sender} -> {proposal.label})",
                exc_info=True,
            )
            with self._db_connection() as conn:
                result = fail_or_retry(conn, job.job_id, exc, job.max_attempts)
                if result == "failed":
                    mark_proposal_failed(conn, proposal.id, str(exc)[:1000])
            return result

    def _apply_add(self, proposal: RuleProposal) -> None:
        """Upsert the sender's priority mapping (label only).

        Archive is deliberately NOT inferred from the taught mail's inbox state:
        that would turn any manual archive into a standing auto-archive rule.
        Archiving stays a separate, explicit decision.
        """
        with self._db_connection() as conn:
            prefix = active_label_prefix(conn)
        label = _strip_prefix(proposal.label, prefix)
        self._upsert_mapping(proposal.sender, label, archive=False)

    def _upsert_mapping(self, email: str, label: str, archive: bool) -> None:
        """POST a new priority mapping; on 409 (already exists) move it via PUT.
        Both paths re-enqueue the sender's mail into the triage queue."""
        base = self.config.gateway_url
        body: dict[str, object] = {
            "type": APPLY_MAPPING_TYPE,
            "email": email,
            "label": label,
        }
        if archive:
            body["archive"] = True  # only ever set archive on; never clear it
        resp = self._http.post(
            f"{base}/mappings", json=body, headers={"X-Created-By": APPLY_ACTOR}
        )
        if resp.status_code == 201:
            return
        if resp.status_code == 409:
            self._move_existing_mapping(email, label, archive)
            return
        raise RuntimeError(
            f"POST /mappings for {email} -> {resp.status_code}: {resp.text[:200]}"
        )

    def _move_existing_mapping(self, email: str, label: str, archive: bool) -> None:
        """Update the existing priority mapping's label (and archive, if the
        taught mail was archived) — moving it between the label / label+archive
        lists."""
        base = self.config.gateway_url
        listing = self._http.get(
            f"{base}/mappings", params={"type": APPLY_MAPPING_TYPE, "limit": 1000}
        )
        listing.raise_for_status()
        match = next(
            (
                m
                for m in listing.json().get("mappings", [])
                if m.get("email_address") == email
            ),
            None,
        )
        if match is None:
            raise RuntimeError(f"mapping for {email} vanished between POST 409 and GET")
        body: dict[str, object] = {"label": label}
        if archive:
            body["archive"] = True
        resp = self._http.put(
            f"{base}/mappings/{match['id']}",
            json=body,
            headers={"X-Updated-By": APPLY_ACTOR},
        )
        resp.raise_for_status()

    async def _post_proposal(
        self, channel: discord.abc.Messageable, proposal: RuleProposal
    ) -> None:
        message = await channel.send(format_proposal(proposal))
        # Record the link immediately so a slow/failed reaction add can't cause
        # a re-post on the next poll. If the DB write fails, delete the message
        # so it doesn't become an orphan that re-posts as a duplicate.
        try:
            await asyncio.to_thread(self._mark_posted, proposal.id, str(message.id))
        except Exception:
            logger.error(
                f"Failed to record proposal {proposal.id} as posted; "
                f"deleting message {message.id} to avoid a duplicate",
                exc_info=True,
            )
            try:
                await message.delete()
            except Exception:
                logger.error(
                    f"Failed to delete orphan message {message.id}", exc_info=True
                )
            raise
        for emoji in (APPROVE, REJECT):
            try:
                await message.add_reaction(emoji)
            except Exception:
                logger.warning(
                    f"Failed to add reaction {emoji} to message {message.id}",
                    exc_info=True,
                )
        logger.info(
            f"Posted proposal {proposal.id} ({proposal.sender} -> "
            f"{proposal.label}, {proposal.direction}) as message {message.id}"
        )

    async def on_raw_reaction_add(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        # Only reactions in the teach channel, not from any bot, and only the
        # ✅ / ❌ emojis.
        if payload.channel_id != self.config.channel_id:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return
        user = payload.member or self.get_user(payload.user_id)
        if user is not None and user.bot:
            return  # never let another bot approve/reject
        emoji = str(payload.emoji)
        if emoji not in (APPROVE, REJECT):
            return
        try:
            proposal = await asyncio.to_thread(
                self._decide, str(payload.message_id), emoji == APPROVE
            )
        except Exception:
            logger.error(
                f"Failed to resolve reaction on message {payload.message_id}",
                exc_info=True,
            )
            return
        if proposal is not None:
            verb = "approved" if emoji == APPROVE else "rejected"
            logger.info(
                f"Proposal {proposal.id} {verb} ({proposal.sender} -> "
                f"{proposal.label})"
            )


def main() -> None:
    start_metrics_server(port=int(os.getenv("METRICS_PORT", "8096")))
    config = TeachConfig.from_env()
    bot = TeachBot(config)
    bot.run(config.token)


if __name__ == "__main__":
    main()
