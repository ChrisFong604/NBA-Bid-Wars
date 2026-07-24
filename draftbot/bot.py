"""Discord wiring: client, sessions, dispatch, timers, effect renderer,
slash commands, and crash recovery.

Every state change funnels through ``DraftBot.dispatch`` — engine.apply under
the session lock, snapshot at meaningful boundaries, then render the effects
outside the critical section. Persistent messages are always edited by stored
message id, never via interaction tokens (which die after 15 minutes).
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands

from . import dataset, engine, sim, store, ui
from .models import (
    SLOTS,
    ArmTimerFx,
    AutoFilledFx,
    AutopilotFx,
    Bid,
    BoardFx,
    Cancel,
    CancelledFx,
    CancelTimerFx,
    CompleteFx,
    Config,
    DraftState,
    Effect,
    ErrorFx,
    ForceAssignedFx,
    FreePickFx,
    Join,
    Kick,
    Leave,
    LobbyFx,
    LotOpened,
    PassedFx,
    Pause,
    PausedFx,
    Pick,
    PickedFx,
    Resume,
    ResumedFx,
    SoldFx,
    Start,
    Swap,
    TimerExpired,
)

log = logging.getLogger("draftbot")

SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"
BOARD_DEBOUNCE_SECONDS = 2.0
SIM_GAME_BEAT_SECONDS = 1.5

# Snapshot on lot boundaries / phase changes / roster changes — never on a
# bare BidPlaced. LobbyFx/BoardFx are included so lobby membership and swaps
# survive a restart (DESIGN §3 lists "created" and "swap" as snapshot points).
SNAPSHOT_EFFECTS = (
    LotOpened, SoldFx, PassedFx, ForceAssignedFx, FreePickFx, PickedFx,
    AutoFilledFx, CompleteFx, CancelledFx, PausedFx, ResumedFx, AutopilotFx,
    LobbyFx, BoardFx,
)


_reply_ephemeral = ui.reply_ephemeral


def _admin_user_id(
    session: "DraftSession", interaction: discord.Interaction
) -> int:
    """Rule #18: the commissioner is the creator; Manage Server permission
    is the fallback. Returns the commissioner id when the caller is
    authorized — so commissioner-gated events pass the engine's pure check —
    and the caller's own id otherwise (which the engine rejects)."""
    uid = interaction.user.id
    if uid == session.state.commissioner_id:
        return uid
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms is not None and perms.manage_guild:
        return session.state.commissioner_id
    return uid


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed", exc_info=exc)


@dataclass
class DraftSession:
    """Mutable per-thread holder. The immutable ``DraftState`` inside is the
    only game truth; everything else is Discord bookkeeping."""

    state: DraftState
    thread: discord.Thread
    channel_id: int
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: Optional[asyncio.Task] = None
    board_task: Optional[asyncio.Task] = None
    sim_task: Optional[asyncio.Task] = None
    lobby_message_id: Optional[int] = None
    board_message_id: Optional[int] = None
    lot_message_id: Optional[int] = None
    pick_message_id: Optional[int] = None

    @property
    def thread_id(self) -> int:
        return self.thread.id


class DraftBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False
            ),
        )
        self.tree = app_commands.CommandTree(self)
        self.sessions: dict[int, DraftSession] = {}
        self.rng = random.Random()
        self._startup_done = False
        register_commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(*ui.DYNAMIC_ITEMS)

    async def on_ready(self) -> None:
        if self._startup_done:  # on_ready re-fires on reconnect
            return
        await self._recover_snapshots()
        try:
            await self.tree.sync()
            test_guild = os.environ.get("TEST_GUILD_ID")
            if test_guild:
                guild = discord.Object(id=int(test_guild))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
        except discord.HTTPException:
            log.exception("command sync failed")
        self._startup_done = True
        log.info("ready as %s", self.user)

    def missing_session_message(self) -> str:
        return (
            "There's no active draft in this thread." if self._startup_done
            else "Still starting up — give me a few seconds and try again."
        )

    # ------------------------------------------------------------ dispatch

    async def dispatch(self, session: DraftSession, event: Any) -> list[Effect]:
        """The only path for state changes: apply + commit + snapshot under
        the lock. engine.apply is pure and synchronous — no awaits between
        read and commit. Timer effects are processed here at commit time —
        arming in the renderer, after awaited Discord I/O, would let a slow
        earlier transition clobber a newer transition's fresh timer and
        leave the lot with no live timer at all."""
        async with session.lock:
            new_state, effects = engine.apply(session.state, event, self.rng)
            session.state = new_state
            effects = self._apply_timer_effects(session, effects)
            if any(isinstance(fx, SNAPSHOT_EFFECTS) for fx in effects):
                self._save(session)
        return effects

    def _apply_timer_effects(
        self, session: DraftSession, effects: list[Effect]
    ) -> list[Effect]:
        """Arm/cancel timers immediately (pure bookkeeping, no Discord I/O)
        and strip the timer effects from what the renderer sees. Must be
        called under ``session.lock``, in commit order."""
        remaining: list[Effect] = []
        for fx in effects:
            if isinstance(fx, ArmTimerFx):
                self._arm_timer(session, fx.kind, fx.lot_seq, fx.deadline)
            elif isinstance(fx, CancelTimerFx):
                self._cancel_timer(session)
            else:
                remaining.append(fx)
        return remaining

    def _snapshot_path(self, thread_id: int) -> Path:
        return SNAPSHOT_DIR / f"{thread_id}.json"

    def _meta(self, session: DraftSession) -> dict[str, Any]:
        return {
            "thread_id": session.thread_id,
            "channel_id": session.channel_id,
            "lobby_message_id": session.lobby_message_id,
            "board_message_id": session.board_message_id,
            "lot_message_id": session.lot_message_id,
            "pick_message_id": session.pick_message_id,
        }

    def _save(self, session: DraftSession) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            store.save_snapshot(
                self._snapshot_path(session.thread_id),
                session.state,
                self._meta(session),
            )
        except OSError:
            log.exception("snapshot save failed for %s", session.thread_id)

    def _delete_snapshot(self, thread_id: int) -> None:
        self._snapshot_path(thread_id).unlink(missing_ok=True)

    # -------------------------------------------------------------- timers

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(_log_task_result)
        return task

    def _arm_timer(
        self, session: DraftSession, kind: str, lot_seq: int, deadline: float
    ) -> None:
        old = session.timer_task
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        session.timer_task = self._spawn(
            self._timer(session, kind, lot_seq, deadline)
        )

    def _cancel_timer(self, session: DraftSession) -> None:
        old = session.timer_task
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        session.timer_task = None

    async def _timer(
        self, session: DraftSession, kind: str, lot_seq: int, deadline: float
    ) -> None:
        await asyncio.sleep(max(0.0, deadline - time.time()))
        event = TimerExpired(
            kind=kind, lot_seq=lot_seq, deadline=deadline, now=time.time()
        )
        effects = await self.dispatch(session, event)
        await self.render(session, effects)

    # --------------------------------------------------------------- board

    def _schedule_board(self, session: DraftSession) -> None:
        """Trailing 2s debounce; rapid sales coalesce into one edit reading
        the latest state at fire time."""
        if session.board_task is not None and not session.board_task.done():
            return
        session.board_task = self._spawn(self._board_later(session))

    async def _board_later(self, session: DraftSession) -> None:
        await asyncio.sleep(BOARD_DEBOUNCE_SECONDS)
        if session.board_message_id is None:
            return
        try:
            await session.thread.get_partial_message(
                session.board_message_id
            ).edit(embed=ui.board_embed(session.state))
        except discord.HTTPException:
            log.exception("board edit failed for %s", session.thread_id)

    # ------------------------------------------------------------ renderer

    async def render(
        self,
        session: DraftSession,
        effects: list[Effect],
        interaction: Optional[discord.Interaction] = None,
    ) -> None:
        await ui.render_effects(self, session, effects, interaction)

    # ----------------------------------------------------------------- sim

    async def _run_sim(self, session: DraftSession) -> None:
        await session.thread.send("🤖 Simulating the tournament…")
        try:
            tournament = await sim.run_tournament(ui.teams_for_sim(session.state))
        except sim.SimError as exc:
            await session.thread.send(f"⚠️ {exc} Run /simulate to retry.")
            return
        except Exception:
            log.exception("tournament sim crashed")
            await session.thread.send(
                "⚠️ The tournament sim crashed — run /simulate to retry."
            )
            return
        for game in tournament.games:
            await session.thread.send(embed=ui.game_embed(game))
            await asyncio.sleep(SIM_GAME_BEAT_SECONDS)
        await session.thread.send(embed=ui.champion_embed(tournament))

    # ---------------------------------------------------- component entry

    async def handle_join(
        self, interaction: discord.Interaction, thread_id: int
    ) -> None:
        session = self.sessions.get(thread_id)
        if session is None:
            await _reply_ephemeral(interaction, self.missing_session_message())
            return
        effects = await self.dispatch(
            session, Join(interaction.user.id, interaction.user.display_name)
        )
        await self.render(session, effects, interaction)
        if interaction.response.is_done():
            return
        if any(isinstance(fx, BoardFx) for fx in effects):  # mid-draft reclaim
            await interaction.response.send_message(
                "👋 Welcome back — you've reclaimed your team.", ephemeral=True
            )
        else:
            await interaction.response.defer()

    async def handle_leave(
        self, interaction: discord.Interaction, thread_id: int
    ) -> None:
        session = self.sessions.get(thread_id)
        if session is None:
            await _reply_ephemeral(interaction, self.missing_session_message())
            return
        effects = await self.dispatch(session, Leave(interaction.user.id))
        await self.render(session, effects, interaction)
        if interaction.response.is_done():
            return
        if any(isinstance(fx, AutopilotFx) for fx in effects):
            await interaction.response.send_message(
                "You've left — your team is on autopilot. Hit Join to reclaim it.",
                ephemeral=True,
            )
        else:
            await interaction.response.defer()

    async def handle_bid(
        self,
        interaction: discord.Interaction,
        thread_id: int,
        lot_seq: int,
        *,
        increment: Optional[int] = None,
        amount: Optional[int] = None,
        confirmed: bool = False,
    ) -> None:
        session = self.sessions.get(thread_id)
        if session is None:
            await _reply_ephemeral(interaction, self.missing_session_message())
            return
        if not confirmed:
            preview = ui.confirm_preview(
                session.state, interaction.user.id, lot_seq, increment, amount
            )
            if preview is not None:
                lot = session.state.lot
                assert lot is not None
                await interaction.response.send_message(
                    f"⚠️ Confirm **${preview}** on **{lot.player.name}**? "
                    "That's at least half your remaining budget.",
                    view=ui.confirm_view(thread_id, lot_seq, preview),
                    ephemeral=True,
                )
                return
        event = Bid(
            user_id=interaction.user.id,
            lot_seq=lot_seq,
            now=time.time(),
            increment=None if amount is not None else increment,
            amount=amount,
        )
        effects = await self.dispatch(session, event)
        await self.render(session, effects, interaction)
        if interaction.response.is_done():
            return
        if confirmed:  # kill the ephemeral confirm button
            await interaction.response.edit_message(
                content=f"✅ Bid placed — ${amount}.", view=None
            )
        else:
            await interaction.response.defer()

    async def handle_cancel_confirm(
        self, interaction: discord.Interaction, thread_id: int
    ) -> None:
        session = self.sessions.get(thread_id)
        if session is None:
            await _reply_ephemeral(interaction, self.missing_session_message())
            return
        effects = await self.dispatch(
            session, Cancel(_admin_user_id(session, interaction))
        )
        if any(isinstance(fx, ErrorFx) for fx in effects):
            await self.render(session, effects, interaction)
            return
        await interaction.response.edit_message(
            content="🛑 Draft cancelled.", view=None
        )
        await self.render(session, effects)

    # ------------------------------------------------------------ recovery

    async def _recover_snapshots(self) -> None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        for path in sorted(SNAPSHOT_DIR.glob("*.json")):
            try:
                state, meta = store.load_snapshot(path)
            except Exception:
                log.exception("unreadable snapshot %s — skipping", path)
                continue
            if state.phase == "cancelled":
                path.unlink(missing_ok=True)
                continue
            # phase == "complete" falls through: the snapshot only survives
            # completion if the bot died before _render_complete announced
            # it (that render deletes the snapshot) — announce it now.
            thread_id = int(meta["thread_id"])
            try:
                channel = await self.fetch_channel(thread_id)
            except discord.NotFound:
                log.warning("thread %s is gone — dropping snapshot", thread_id)
                path.unlink(missing_ok=True)
                continue
            except discord.HTTPException:
                log.exception("couldn't fetch thread %s — keeping snapshot", thread_id)
                continue
            if not isinstance(channel, discord.Thread):
                path.unlink(missing_ok=True)
                continue
            session = DraftSession(
                state=state,
                thread=channel,
                channel_id=int(meta.get("channel_id") or channel.parent_id or 0),
                lobby_message_id=meta.get("lobby_message_id"),
                board_message_id=meta.get("board_message_id"),
                lot_message_id=meta.get("lot_message_id"),
                pick_message_id=meta.get("pick_message_id"),
            )
            self.sessions[thread_id] = session
            try:
                await self._resume_session(session)
            except discord.HTTPException:
                log.exception("recovery render failed for %s", thread_id)

    async def _resume_session(self, session: DraftSession) -> None:
        state = session.state
        now = time.time()
        if state.phase == "complete":
            # The bot died between committing completion and announcing it.
            # Re-run the completion render: final rosters, sim, and the
            # snapshot delete all happen in _render_complete.
            await session.thread.send(
                "♻️ Bot restarted — the draft had just finished."
            )
            await self.render(session, [CompleteFx()])
            return
        if state.paused:
            # Old lot card + buttons still work (DynamicItems); the engine
            # rejects bids while paused. Resume recomputes the deadline.
            await session.thread.send(
                "♻️ Bot restarted while the draft was paused — "
                "/draft resume to continue."
            )
            return
        if state.phase == "auction":
            async with session.lock:
                current = session.state
                if current.lot is not None:
                    current = replace(
                        current,
                        queue=(current.lot.player,) + current.queue,
                        lot=None,
                    )
                new_state, effects = engine.redeal(current, now, self.rng)
                session.state = new_state
                effects = self._apply_timer_effects(session, effects)
                self._save(session)
            await session.thread.send(
                "♻️ Bot restarted — re-opening the current player."
            )
            await self.render(session, effects)
        elif state.phase == "free_pick":
            deadline = now + state.config.free_pick_seconds
            async with session.lock:
                session.state = replace(session.state, pick_deadline=deadline)
                self._arm_timer(session, "pick", -1, deadline)
                self._save(session)
            actives = session.state.active_managers
            if actives:
                message = await session.thread.send(
                    "♻️ Bot restarted — "
                    + ui.pick_prompt(actives[0].user_id, deadline)
                )
                session.pick_message_id = message.id
        # lobby phase: nothing to re-arm; the lobby buttons are DynamicItems.


# ------------------------------------------------------------------ commands


ERA_DECADES: tuple[int, ...] = tuple(range(1960, 2030, 10))


def register_commands(bot: DraftBot) -> None:  # noqa: PLR0915 - command defs
    tree = bot.tree
    slot_choices = [app_commands.Choice(name=s, value=s) for s in SLOTS]
    era_choices = [
        app_commands.Choice(name=f"{d}s", value=d) for d in ERA_DECADES
    ]

    def thread_session(interaction: discord.Interaction) -> Optional[DraftSession]:
        if interaction.channel_id is None:
            return None
        return bot.sessions.get(interaction.channel_id)

    async def require_session(
        interaction: discord.Interaction,
    ) -> Optional[DraftSession]:
        session = thread_session(interaction)
        if session is None:
            await _reply_ephemeral(interaction, bot.missing_session_message())
        return session

    draft = app_commands.Group(name="draft", description="Run an NBA auction draft")

    @draft.command(name="create", description="Open a draft lobby in a new thread")
    @app_commands.describe(
        budget="Starting budget per manager (default $20)",
        open_seconds="Opening window per player (default 20s)",
        hammer_seconds="Hammer timer after a bid (default 10s)",
        era_from="Earliest era in the player pool (default 1960s)",
        era_to="Latest era in the player pool (default 2020s)",
        sim="Simulate a tournament between the teams afterwards (default on)",
    )
    @app_commands.choices(era_from=era_choices, era_to=era_choices)
    async def draft_create(
        interaction: discord.Interaction,
        budget: app_commands.Range[int, 1, 1000] = 20,
        open_seconds: app_commands.Range[int, 5, 600] = 20,
        hammer_seconds: app_commands.Range[int, 3, 120] = 10,
        era_from: int = 1960,
        era_to: int = 2020,
        sim: bool = True,  # shadows the sim module only inside this closure
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await _reply_ephemeral(
                interaction, "Run this in a regular text channel, not a thread."
            )
            return
        if era_from > era_to:
            await _reply_ephemeral(
                interaction,
                f"Era range is backwards — the {era_from}s come after the "
                f"{era_to}s. Pick era_from at or before era_to.",
            )
            return
        await interaction.response.defer()
        try:
            thread = await channel.create_thread(
                name=f"🏀 Draft — {date.today().isoformat()}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
        except discord.HTTPException:
            log.exception("thread creation failed")
            await interaction.followup.send(
                "I couldn't create a thread here — check my permissions "
                "(Create Public Threads)."
            )
            return
        config = Config(
            budget=budget,
            open_seconds=open_seconds,
            hammer_seconds=hammer_seconds,
            era_start=era_from,
            era_end=era_to,
            sim=sim,
        )
        state = DraftState(config=config, commissioner_id=interaction.user.id)
        state, _ = engine.apply(  # auto-join the commissioner
            state,
            Join(interaction.user.id, interaction.user.display_name),
            bot.rng,
        )
        session = DraftSession(state=state, thread=thread, channel_id=channel.id)
        lobby = await thread.send(
            embed=ui.lobby_embed(state), view=ui.lobby_view(thread.id)
        )
        session.lobby_message_id = lobby.id
        bot.sessions[thread.id] = session
        async with session.lock:
            bot._save(session)
        try:
            await thread.add_user(interaction.user)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            f"🏀 Draft lobby open — head to {thread.mention} and hit Join!"
        )

    @draft.command(name="start", description="Start the draft (commissioner)")
    async def draft_start(interaction: discord.Interaction) -> None:
        if (session := await require_session(interaction)) is None:
            return
        try:
            players = dataset.load_players()
        except ValueError as exc:
            await _reply_ephemeral(interaction, f"Dataset error: {exc}")
            return
        config = session.state.config
        players = dataset.filter_by_era(
            players, config.era_start, config.era_end
        )
        try:
            effects = await bot.dispatch(
                session,
                Start(_admin_user_id(session, interaction), players, time.time()),
            )
        except ValueError as exc:  # pool build failed (era pool too small)
            eras = ui.era_label(config.era_start, config.era_end)
            await _reply_ephemeral(
                interaction,
                f"Can't build a pool from the **{eras}** eras for "
                f"**{len(session.state.managers)}** managers ({exc}). "
                "Widen the era range or shrink the lobby.",
            )
            return
        if any(isinstance(fx, ErrorFx) for fx in effects):
            await bot.render(session, effects, interaction)
            return
        await interaction.response.send_message(
            "🏀 The draft is live — first player coming up!"
        )
        board = await session.thread.send(embed=ui.board_embed(session.state))
        session.board_message_id = board.id
        try:
            await board.pin()
        except discord.HTTPException:
            log.warning("couldn't pin the board (need Manage Messages)")
        async with session.lock:
            bot._save(session)
        await bot.render(session, effects)

    async def _pause_or_resume(
        interaction: discord.Interaction, event_cls: type, ack: str
    ) -> None:
        if (session := await require_session(interaction)) is None:
            return
        effects = await bot.dispatch(
            session, event_cls(_admin_user_id(session, interaction), time.time())
        )
        await bot.render(session, effects, interaction)
        if not interaction.response.is_done():
            await interaction.response.send_message(ack, ephemeral=True)

    @draft.command(name="pause", description="Pause the draft (commissioner)")
    async def draft_pause(interaction: discord.Interaction) -> None:
        await _pause_or_resume(interaction, Pause, "⏸️ Paused.")

    @draft.command(name="resume", description="Resume a paused draft (commissioner)")
    async def draft_resume(interaction: discord.Interaction) -> None:
        await _pause_or_resume(interaction, Resume, "▶️ Resumed.")

    @draft.command(
        name="addtime", description="Add seconds to the live timer (commissioner)"
    )
    @app_commands.describe(seconds="Seconds to add to the current deadline")
    async def draft_addtime(
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 1, 3600],
    ) -> None:
        if (session := await require_session(interaction)) is None:
            return
        # Not an engine event: extend the live deadline in place under the
        # lock (same shape as a Resume) and re-arm the timer.
        error: Optional[str] = None
        arm: Optional[tuple[str, int, float]] = None
        async with session.lock:
            state = session.state
            if _admin_user_id(session, interaction) != state.commissioner_id:
                error = "Only the commissioner can add time."
            elif state.paused:
                error = "The draft is paused — resume first."
            elif state.phase == "auction" and state.lot is not None:
                lot = replace(state.lot, deadline=state.lot.deadline + seconds)
                session.state = replace(state, lot=lot)
                arm = ("lot", lot.seq, lot.deadline)
                bot._arm_timer(session, *arm)  # commit-time, under the lock
                bot._save(session)
            elif state.phase == "free_pick":
                deadline = state.pick_deadline + seconds
                session.state = replace(state, pick_deadline=deadline)
                arm = ("pick", -1, deadline)
                bot._arm_timer(session, *arm)  # commit-time, under the lock
                bot._save(session)
            else:
                error = "There's no live timer to extend."
        if error is not None:
            await _reply_ephemeral(interaction, error)
            return
        assert arm is not None
        state = session.state
        if arm[0] == "lot" and state.lot is not None:
            await ui.edit_lot(
                session,
                ui.lot_embed(state.lot, 1 + len(state.queue)),
                clear=False,
            )
        elif arm[0] == "pick" and session.pick_message_id is not None:
            actives = state.active_managers
            if actives:
                await session.thread.get_partial_message(
                    session.pick_message_id
                ).edit(content=ui.pick_prompt(actives[0].user_id, arm[2]))
        await interaction.response.send_message(
            f"⏱️ Added {seconds}s to the clock."
        )

    @draft.command(name="kick", description="Kick a manager (commissioner)")
    @app_commands.describe(
        user="Manager to kick",
        replacement="Optional replacement — inherits roster and budget",
    )
    async def draft_kick(
        interaction: discord.Interaction,
        user: discord.User,
        replacement: Optional[discord.User] = None,
    ) -> None:
        if (session := await require_session(interaction)) is None:
            return
        event = Kick(
            user_id=_admin_user_id(session, interaction),
            target_id=user.id,
            now=time.time(),
            replacement_id=replacement.id if replacement else None,
            replacement_name=replacement.display_name if replacement else None,
        )
        effects = await bot.dispatch(session, event)
        await bot.render(session, effects, interaction)
        if interaction.response.is_done():
            return
        suffix = (
            f" — {replacement.display_name} takes over the team."
            if replacement
            else " — the team is on autopilot."
        )
        await interaction.response.send_message(
            f"👢 {user.display_name} kicked{suffix}"
        )

    @draft.command(name="cancel", description="Cancel the draft (commissioner)")
    async def draft_cancel(interaction: discord.Interaction) -> None:
        if (session := await require_session(interaction)) is None:
            return
        if _admin_user_id(session, interaction) != session.state.commissioner_id:
            await _reply_ephemeral(interaction, "Only the commissioner can cancel.")
            return
        await interaction.response.send_message(
            "This ends the draft for everyone — are you sure?",
            view=ui.CancelConfirmView(session.thread_id),
            ephemeral=True,
        )

    tree.add_command(draft)

    @tree.command(name="swap", description="Swap two of your lineup slots")
    @app_commands.choices(slot_a=slot_choices, slot_b=slot_choices)
    async def swap_command(
        interaction: discord.Interaction, slot_a: str, slot_b: str
    ) -> None:
        if (session := await require_session(interaction)) is None:
            return
        effects = await bot.dispatch(
            session, Swap(interaction.user.id, slot_a, slot_b)
        )
        await bot.render(session, effects, interaction)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"🔁 Swapped {slot_a} and {slot_b}.", ephemeral=True
            )

    @tree.command(
        name="status", description="Your roster, budget, and the pool count"
    )
    async def status_command(interaction: discord.Interaction) -> None:
        if (session := await require_session(interaction)) is None:
            return
        manager = session.state.manager(interaction.user.id)
        if manager is None:
            await _reply_ephemeral(interaction, "You're not a manager in this draft.")
            return
        await interaction.response.send_message(
            embed=ui.status_embed(session.state, manager), ephemeral=True
        )

    async def pick_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        session = thread_session(interaction)
        if session is None or session.state.phase != "free_pick":
            return []
        actives = session.state.active_managers
        if not actives or actives[0].user_id != interaction.user.id:
            return []
        needle = current.lower()
        choices: list[app_commands.Choice[str]] = []
        for p in session.state.queue:
            if needle in p.name.lower():
                label = (
                    f"{p.name} — {p.pos} · {p.team} · "
                    f"{ui.decade_tag(p.decade)} · {p.stars}⭐"
                )
                choices.append(app_commands.Choice(name=label[:100], value=p.id))
                if len(choices) == 25:
                    break
        return choices

    @tree.command(
        name="pick", description="Free-pick phase: take any player from the pool"
    )
    @app_commands.describe(player="Player to take, free of charge")
    @app_commands.autocomplete(player=pick_autocomplete)
    async def pick_command(
        interaction: discord.Interaction, player: str
    ) -> None:
        if (session := await require_session(interaction)) is None:
            return
        effects = await bot.dispatch(
            session, Pick(interaction.user.id, player, time.time())
        )
        await bot.render(session, effects, interaction)
        if not interaction.response.is_done():
            await interaction.response.send_message("🎯 Locked in.", ephemeral=True)

    @tree.command(
        name="simulate",
        description="Run or re-run the tournament sim (commissioner)",
    )
    async def simulate_command(interaction: discord.Interaction) -> None:
        if (session := await require_session(interaction)) is None:
            return
        state = session.state
        if _admin_user_id(session, interaction) != state.commissioner_id:
            await _reply_ephemeral(interaction, "Only the commissioner can simulate.")
            return
        if state.phase != "complete":
            await _reply_ephemeral(
                interaction, "The sim runs after the draft completes."
            )
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            await _reply_ephemeral(
                interaction, "Set ANTHROPIC_API_KEY to run the tournament sim."
            )
            return
        if session.sim_task is not None and not session.sim_task.done():
            await _reply_ephemeral(interaction, "A sim is already running.")
            return
        session.sim_task = bot._spawn(bot._run_sim(session))
        await interaction.response.send_message(
            "🤖 Tournament sim started.", ephemeral=True
        )

    @tree.error
    async def on_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        log.exception("app command failed", exc_info=error)
        try:
            await _reply_ephemeral(interaction, "Something went wrong — try again.")
        except discord.HTTPException:
            pass
