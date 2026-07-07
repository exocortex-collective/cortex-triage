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
import psycopg2
from cortex_utils.learning import (
    RuleProposal,
    approve_proposal,
    ensure_learning_schema,
    ensure_proposals_schema,
    get_proposal_by_message_id,
    mark_proposal_posted,
    propose_from_opportunities,
    reject_proposal,
    unposted_pending_proposals,
)
from cortex_utils.logging import configure_logging, get_logger
from cortex_utils.metrics import start_metrics_server
from discord.ext import commands, tasks

configure_logging(service_name="teach-bot", level=os.getenv("LOG_LEVEL", "INFO"))
logger = get_logger(__name__)

APPROVE = "✅"
REJECT = "❌"


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
        )


def format_proposal(proposal: RuleProposal) -> str:
    """Human-readable prompt for a proposal, with ✅ / ❌ instructions."""
    if proposal.direction == "add":
        action = f"Label mail from **{proposal.sender}** as `{proposal.label}`?"
    else:
        action = f"Stop labeling mail from **{proposal.sender}** as `{proposal.label}`?"
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
        the reaction actually transitioned a pending proposal, else None."""
        with self._db_connection() as conn:
            proposal = get_proposal_by_message_id(conn, message_id)
            if proposal is None:
                return None
            changed = (
                approve_proposal(conn, proposal.id)
                if approved
                else reject_proposal(conn, proposal.id)
            )
            return proposal if changed else None

    # --- Discord lifecycle ---

    async def setup_hook(self) -> None:
        # setup_hook runs exactly once on startup; on_ready can fire on every
        # reconnect, so schema + the poll loop belong here.
        await asyncio.to_thread(self._ensure_schema)
        self.poll_task.start()

    async def on_ready(self) -> None:
        logger.info(f"Teach bot ready as {self.user}")

    async def close(self) -> None:
        self.poll_task.cancel()
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
        try:
            await message.add_reaction(APPROVE)
            await message.add_reaction(REJECT)
        except Exception:
            logger.warning(
                f"Failed to add reactions to message {message.id}", exc_info=True
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
        if payload.member is not None and payload.member.bot:
            return  # never let another bot approve/reject
        emoji = str(payload.emoji)
        if emoji not in (APPROVE, REJECT):
            return
        proposal = await asyncio.to_thread(
            self._decide, str(payload.message_id), emoji == APPROVE
        )
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
