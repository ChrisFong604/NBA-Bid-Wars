"""Discord presentation layer: embeds, persistent components, modals, and
the effect renderer.

No state transitions here — component callbacks delegate every change to the
``DraftBot`` client (``interaction.client``), and ``render_effects`` turns
engine effects into Discord I/O. All buttons on persistent messages are
``DynamicItem``s keyed by ``custom_id``, so they keep working across
restarts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

import discord

from .models import (
    ArmTimerFx,
    AutoFilledFx,
    AutopilotFx,
    BidPlaced,
    BoardFx,
    CancelledFx,
    CancelTimerFx,
    CompleteFx,
    DraftState,
    Effect,
    ErrorFx,
    ForceAssignedFx,
    FreePickFx,
    LineupPhaseFx,
    LobbyFx,
    Lot,
    LotOpened,
    LotteryCancelledFx,
    LotteryGuessedFx,
    LotteryJoinedFx,
    LotteryOpenedFx,
    LotteryRevealFx,
    Manager,
    PassedFx,
    PausedFx,
    PickedFx,
    Player,
    ResumedFx,
    SoldFx,
    Swap,
)
from .sim import LLM_WEIGHT, SimResult, TeamDict, share_prompt

if TYPE_CHECKING:
    from .bot import DraftBot, DraftSession

log = logging.getLogger("draftbot.ui")

BLUE = 0x5865F2
GREEN = 0x57F287
GREY = 0x95A5A6
ORANGE = 0xE67E22
YELLOW = 0xFEE75C
RED = 0xED4245
GOLD = 0xF1C40F

FINAL_WARN_SECONDS = 10.0  # lot-card warning edit for non-ticking mobile clients
POOL_LINES_PER_EMBED = 25
MAX_EMBEDS_PER_MESSAGE = 10  # Discord hard cap per message
EMBED_CHAR_BUDGET = 5500  # headroom under Discord's 6000-char embed total
CLOSE_BEAT_SECONDS = 2.0
LOT_FOOTER = (
    "late bids add +5s (no sniping) · bid up to your full remaining "
    "budget; hit $0 and you're done bidding"
)
SIM_LABELS = {
    "prompt": "prompt for your own LLM",
    "off": "off",
    "stats": "stats only",
    "ai": "AI + stats",
}
POOL_LABELS = {
    "legends": "legends only",
    "household": "household names",
    "deep": "deep (stars + role players)",
}
SHARE_PROMPT_HEADER = (
    "🎟️ Draft complete — paste this into your favorite LLM to run the tournament:"
)
PROMPT_MESSAGE_LIMIT = 1900  # headroom under Discord's 2000-char cap
_PROMPT_OVERHEAD = 30  # "(part i/N)" marker line + the two fence lines
LAST_CALL_WARNING = "LAST CALL — passes again and they're force-assigned for $1"

_NAME_SUFFIXES = frozenset({"Jr.", "Sr.", "II", "III", "IV", "V"})


def _bot(interaction: discord.Interaction) -> DraftBot:
    from .bot import DraftBot  # runtime import — no cycle at module load

    client = interaction.client
    assert isinstance(client, DraftBot)
    return client


async def reply_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ------------------------------------------------------------------ helpers


def short_name(full_name: str) -> str:
    """Board-friendly surname: 'Jaren Jackson Jr.' -> 'Jackson'."""
    tokens = full_name.split()
    if len(tokens) >= 2 and tokens[-1] in _NAME_SUFFIXES:
        return tokens[-2]
    return tokens[-1]


def display(m: Manager) -> str:
    """CPU managers have no Discord account — render their name plain."""
    return f"🤖 {m.name}" if m.cpu else f"<@{m.user_id}>"


def display_id(manager_id: int) -> str:
    """Id-only render sites: CPU ids are negative and name deterministically
    ("CPU 1" is -1), so no state lookup is needed."""
    return f"🤖 CPU {-manager_id}" if manager_id < 0 else f"<@{manager_id}>"


def rel(deadline: float) -> str:
    """Discord relative timestamp — the client ticks it, no edit spam."""
    return f"<t:{int(deadline)}:R>"


def era_label(era_start: int, era_end: int) -> str:
    """'1960s–2020s', or '2000s' when the range is a single decade."""
    if era_start == era_end:
        return f"{era_start}s"
    return f"{era_start}s–{era_end}s"


def decade_tag(decade: int) -> str:
    """Short era tag for player lines: 1990 -> \"'90s\", 2000 -> \"'00s\"."""
    return f"'{decade % 100:02d}s"


def _stat_line(p: Player) -> str:
    """One-line lot flavor: CHI · prime 1989–1993 ('90s) · 31.5/6.3/5.5 · ⭐⭐⭐⭐⭐.
    Pre-era snapshots may carry players with no prime — skip the era chunk."""
    era = f"prime {p.prime} ({decade_tag(p.decade)}) · " if p.prime else ""
    return f"{p.team} · {era}{p.ppg:g}/{p.rpg:g}/{p.apg:g} · {'⭐' * p.stars}"


def _roster_lines(m: Manager) -> str:
    lines = []
    for s in m.spots:
        if s.player is None:
            lines.append(f"{s.slot} —")
            continue
        tag = decade_tag(s.player.decade)
        price = f"${s.price}" if s.price > 0 else "free"
        lines.append(f"{s.slot} {short_name(s.player.name)} {tag} ({price})")
    return "\n".join(lines)


def pool_count(state: DraftState) -> int:
    return len(state.queue) + (1 if state.lot is not None else 0)


def prompt_messages(text: str) -> list[str]:
    """Fence ``text`` for Discord, splitting on line boundaries so every
    message stays under the 2000-char cap. Multi-part messages start with a
    ``(part i/N)`` marker line above the fence; single-part is just the
    fenced block."""
    budget = PROMPT_MESSAGE_LIMIT - _PROMPT_OVERHEAD
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > budget:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    if len(chunks) == 1:
        return [f"```\n{chunks[0]}\n```"]
    return [
        f"(part {i}/{len(chunks)})\n```\n{chunk}\n```"
        for i, chunk in enumerate(chunks, 1)
    ]


async def send_share_prompt(session: DraftSession, state: DraftState) -> None:
    """Post the copy-pastable tournament prompt (sim mode ``prompt``)."""
    await session.thread.send(SHARE_PROMPT_HEADER)
    for message in prompt_messages(share_prompt(teams_for_sim(state))):
        await session.thread.send(message)


def teams_for_sim(state: DraftState) -> list[TeamDict]:
    """Sim input per sim.run_stats/run_ai; manager names deduped defensively."""
    seen: set[str] = set()
    teams: list[TeamDict] = []
    for m in state.managers:
        name = m.name if m.name not in seen else f"{m.name} ({m.user_id % 1000})"
        seen.add(name)
        teams.append(
            {
                "manager": name,
                "players": [
                    {
                        "slot": s.slot,
                        "name": s.player.name,
                        "pos": s.player.pos,
                        "ppg": s.player.ppg,
                        "rpg": s.player.rpg,
                        "apg": s.player.apg,
                        "stars": s.player.stars,
                        "decade": s.player.decade,
                        "prime": s.player.prime,
                    }
                    for s in m.spots
                    if s.player is not None
                ],
            }
        )
    return teams


# ------------------------------------------------------------------- embeds


def lobby_embed(state: DraftState) -> discord.Embed:
    cfg = state.config
    embed = discord.Embed(
        title="🏀 NBA Auction Draft — Lobby",
        color=BLUE,
        description=(
            "Blind auction: players come off a hidden shuffled queue one at a "
            "time, you bid live with the buttons, and the last solvent manager "
            "gets free picks from the revealed pool. Join up — the "
            "commissioner starts the draft when everyone's in."
        ),
    )
    managers = "\n".join(
        f"{i}. {display(m)}" for i, m in enumerate(state.managers, 1)
    )
    embed.add_field(
        name=f"Managers ({len(state.managers)}/{cfg.max_managers})",
        value=managers or "*nobody yet*",
        inline=False,
    )
    embed.add_field(
        name="Config",
        value=(
            f"Budget **${cfg.budget}** · clock **{cfg.lot_seconds}s flat** "
            "(a bid in the last 10s adds 5s) · Eras: "
            f"**{era_label(cfg.era_start, cfg.era_end)}** · pool "
            f"**{POOL_LABELS.get(cfg.pool_depth, cfg.pool_depth)}** · "
            f"tournament sim **{SIM_LABELS.get(cfg.sim, cfg.sim)}**"
        ),
        inline=False,
    )
    embed.set_footer(text="Commissioner: run /draft start when ready")
    return embed


def lot_embed(
    lot: Lot, pool_left: int, paused: bool = False, final_seconds: bool = False
) -> discord.Embed:
    p = lot.player
    title = f"🏀 {p.name} — {p.pos}"
    description = _stat_line(p)
    if lot.last_call:
        title = f"🔔 {title}"
        description = f"**{LAST_CALL_WARNING}**\n{description}"
    if final_seconds:  # mobile clients don't tick <t:..:R> — this edit is
        color = ORANGE  # their loud signal that the clock is nearly done
    elif lot.last_call:
        color = YELLOW
    else:
        color = BLUE
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_author(name=f"Lot #{lot.seq} - {pool_left} players left in the pool")
    if lot.current_bid == 0:
        embed.add_field(name="Current Bid", value="$1 opening — no bids yet")
        embed.add_field(name="Leader", value="—")
        status = f"Passes {rel(lot.deadline)} if nobody bids"
    else:
        embed.add_field(name="Current Bid", value=f"${lot.current_bid}")
        embed.add_field(name="Leader", value=display_id(lot.leader_id))
        status = f"Sells {rel(lot.deadline)}"
    if lot.lottery is not None:
        # Showdown status owns the card; the FINAL SECONDS warning edit is
        # skipped while a lottery is live (bot._timer) — simpler than
        # restyling the warning around it.
        status = f"🎰 Showdown — locks {rel(lot.deadline)}"
    elif final_seconds:
        status = f"⏳ **FINAL SECONDS** — {status[0].lower()}{status[1:]}"
    embed.add_field(name="Status", value="⏸️ Paused" if paused else status)
    embed.set_footer(text=LOT_FOOTER)
    return embed


def sold_embed(player: Player, winner: Manager, price: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"✅ SOLD ${price} — {player.name} ({player.pos})",
        description=f"{display(winner)} lands **{player.name}**.",
        color=GREEN,
    )
    embed.set_footer(
        text=f"{winner.name}: ${winner.budget} left · {winner.empty_slots} slots open"
    )
    return embed


def passed_embed(player: Player) -> discord.Embed:
    return discord.Embed(
        title=f"➖ PASSED — {player.name} ({player.pos})",
        description="No opening bid — shuffled back into the pool",
        color=GREY,
    )


def force_embed(player: Player, target: Manager) -> discord.Embed:
    return discord.Embed(
        title=(
            f"🔔 FORCE-ASSIGNED $1 — {player.name} ({player.pos}) "
            f"→ {target.name}"
        ),
        description=f"Second pass with no bid — {display(target)} eats the $1.",
        color=ORANGE,
    )


def cancelled_lot_embed(lot: Lot) -> discord.Embed:
    return discord.Embed(
        title=f"🛑 CANCELLED — {lot.player.name} ({lot.player.pos})",
        description="The draft was cancelled with this player on the block.",
        color=RED,
    )


# ------------------------------------------------------- all-in showdown
# Pure builders — every public showdown surface lives here so tests can
# assert guess values never leak before the reveal.


def showdown_open_text(lot: Lot) -> str:
    assert lot.lottery is not None
    names = " and ".join(display_id(p) for p in lot.lottery.participants)
    return (
        f"🎰 **ALL-IN SHOWDOWN** — {names} are all-in at "
        f"${lot.current_bid} on **{lot.player.name}**!"
    )


def showdown_embed(lot: Lot) -> discord.Embed:
    # Both timestamp styles: :R ticks on desktop, :T is the absolute time
    # for mobile clients that don't tick relative stamps.
    return discord.Embed(
        title="🎰 All-in showdown — closest number wins",
        description=(
            "Every stack is on the table, so luck breaks the tie. Tap "
            "**🎲 Pick my number** and choose a number from **1 to 100** — "
            "resubmitting overwrites, and picks stay secret until the "
            "reveal. When the clock hits zero I draw a mystery number and "
            f"the closest pick buys **{lot.player.name}** for "
            f"**${lot.current_bid}**. No pick? I'll roll one for you.\n"
            "A manager with a bigger budget can still break this up by "
            "bidding higher on the lot card.\n\n"
            f"🔒 Locks {rel(lot.deadline)} (at <t:{int(lot.deadline)}:T>)"
        ),
        color=GOLD,
    )


def lottery_joined_text(manager_id: int) -> str:
    return (
        f"🎰 {display_id(manager_id)} shoves their stack in — "
        "they're in the showdown!"
    )


def lottery_guessed_text(name: str) -> str:
    return f"**{name}** locked in a number 🎲"


def lottery_cancelled_text(name: str) -> str:
    return f"💥 **{name}** broke up the showdown with a higher bid!"


def lottery_reveal_embed(
    fx: LotteryRevealFx, state: DraftState
) -> discord.Embed:
    lines = []
    for mid, guess in fx.guesses:
        manager = state.manager(mid)
        name = manager.name if manager is not None else display_id(mid)
        line = f"{name} picked {guess} — off by {abs(guess - fx.mystery)}"
        if mid == fx.winner_id:
            line = f"🏆 **{line}**"
        lines.append(line)
    return discord.Embed(
        title=f"🎰 The mystery number is… {fx.mystery}!",
        description="\n".join(lines),
        color=GOLD,
    )


def _manager_field_name(m: Manager) -> str:
    if m.full:
        return f"✅ {m.name} — ${m.budget} left"
    if m.autopilot:
        return f"🤖 {m.name} — ${m.budget} left"
    if m.budget == 0:
        return f"💸 {m.name} — BROKE"
    return f"🟢 {m.name} — ${m.budget} left"


def board_embed(state: DraftState) -> discord.Embed:
    embed = discord.Embed(title="📋 Draft Board", color=BLUE)
    for m in state.managers:
        embed.add_field(name=_manager_field_name(m), value=_roster_lines(m))
    return embed


def pool_embeds(pool: tuple[Player, ...]) -> list[discord.Embed]:
    """The full remaining pool, chunked to stay inside embed limits."""
    lines = [
        f"{'⭐' * p.stars} {p.pos} **{p.name}** — {p.team} · "
        f"{decade_tag(p.decade)} · {p.ppg:g}/{p.rpg:g}/{p.apg:g}"
        for p in pool
    ]
    embeds = []
    for i in range(0, len(lines), POOL_LINES_PER_EMBED):
        embed = discord.Embed(
            description="\n".join(lines[i : i + POOL_LINES_PER_EMBED]),
            color=BLUE,
        )
        if i == 0:
            embed.title = f"🔓 The pool is revealed — {len(pool)} players left"
        embeds.append(embed)
    return embeds


def pick_prompt(picker_id: int, deadline: float) -> str:
    return (
        f"{display_id(picker_id)} the floor is yours — "
        f"/pick anyone, {rel(deadline)}"
    )


def picked_embed(player: Player, picker: Manager) -> discord.Embed:
    return discord.Embed(
        title=f"🎯 {picker.name} picks {player.name} ({player.pos})",
        description="Free of charge.",
        color=GREEN,
    )


def autofill_embed(assignments: tuple[tuple[int, Player], ...]) -> discord.Embed:
    lines = [f"{display_id(mid)} ← **{p.name}** ({p.pos})" for mid, p in assignments]
    return discord.Embed(
        title="🎲 Auto-fill — empty slots filled from the pool, free",
        description="\n".join(lines) or "*nothing to fill*",
        color=GREY,
    )


def lineup_open_embed(deadline: float) -> discord.Embed:
    # Both timestamp styles: :R ticks on desktop, :T is the absolute time
    # for mobile clients that don't tick relative stamps.
    return discord.Embed(
        title="🔀 Lineups open — all rosters are full!",
        description=(
            "Every manager can rearrange who plays which slot — tap "
            "**🔀 Arrange my lineup** below or use /swap. Lineups lock "
            f"{rel(deadline)} (at <t:{int(deadline)}:T>), then the draft "
            "wraps with your final lineups."
        ),
        color=BLUE,
    )


def lineup_panel_embed(m: Manager, deadline: float) -> discord.Embed:
    lines = [
        f"**{s.slot}** — {s.player.name} (natural {s.player.pos}, "
        f"{decade_tag(s.player.decade)})"
        for s in m.spots
        if s.player is not None
    ]
    # Timestamps only render in descriptions/fields, never footers; :T is the
    # mobile-accurate form since mobile clients don't tick :R.
    lines.append(f"\n🔒 Lineups lock {rel(deadline)} (at <t:{int(deadline)}:T>)")
    return discord.Embed(
        title=f"🔀 {m.name} — arrange your lineup",
        description="\n".join(lines),
        color=BLUE,
    )


def complete_embed(state: DraftState) -> discord.Embed:
    embed = discord.Embed(title="🏁 Draft complete — final rosters", color=GOLD)
    for m in state.managers:
        spent = state.config.budget - m.budget
        stars = sum(s.player.stars for s in m.spots if s.player is not None)
        embed.add_field(
            name=f"{m.name} — spent ${spent} · {stars}⭐",
            value=_roster_lines(m),
        )
    return embed


def status_embed(state: DraftState, m: Manager) -> discord.Embed:
    embed = discord.Embed(title=f"📋 {m.name}", color=BLUE)
    embed.add_field(name=f"Budget — ${m.budget}", value=_roster_lines(m))
    embed.add_field(
        name="Pool", value=f"{pool_count(state)} players left", inline=False
    )
    return embed


def sim_results_embed(result: SimResult) -> discord.Embed:
    standings = "\n".join(
        f"`{rank}.` **{name}** — {score:.1f}"
        for rank, (name, score) in enumerate(result.standings, 1)
    )
    embed = discord.Embed(
        title=f"🏆 {result.champion} wins the tournament!",
        description=standings,
        color=GOLD,
    )
    if result.summary:  # non-empty iff the standings blend in the LLM ranking
        embed.add_field(name="How it played out", value=result.summary, inline=False)
        embed.set_footer(
            text=(
                f"Standings: {round((1 - LLM_WEIGHT) * 100)}% stats / "
                f"{round(LLM_WEIGHT * 100)}% LLM"
            )
        )
    return embed


# --------------------------------------------------------- dynamic buttons


def _btn(label: str, style: discord.ButtonStyle, custom_id: str) -> discord.ui.Button:
    return discord.ui.Button(label=label, style=style, custom_id=custom_id)


class _FromIntGroups:
    """Shared DynamicItem reconstruction: every custom_id group is an int
    and the groups appear in constructor-argument order."""

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item,
        match: re.Match[str], /,
    ):
        return cls(*(int(g) for g in match.groups()))


class JoinButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:join:(?P<thread>[0-9]+)",
):
    def __init__(self, thread_id: int) -> None:
        super().__init__(
            _btn("Join", discord.ButtonStyle.success, f"nba:join:{thread_id}")
        )
        self.thread_id = thread_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _bot(interaction).handle_join(interaction, self.thread_id)


class LeaveButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:leavelobby:(?P<thread>[0-9]+)",
):
    def __init__(self, thread_id: int) -> None:
        super().__init__(
            _btn("Leave", discord.ButtonStyle.secondary, f"nba:leavelobby:{thread_id}")
        )
        self.thread_id = thread_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _bot(interaction).handle_leave(interaction, self.thread_id)


class QuickBidButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:bid:(?P<thread>[0-9]+):(?P<lot>[0-9]+):(?P<inc>[0-9]+)",
):
    def __init__(self, thread_id: int, lot_seq: int, increment: int) -> None:
        super().__init__(_btn(
            f"+${increment}", discord.ButtonStyle.primary,
            f"nba:bid:{thread_id}:{lot_seq}:{increment}",
        ))
        self.thread_id = thread_id
        self.lot_seq = lot_seq
        self.increment = increment

    async def callback(self, interaction: discord.Interaction) -> None:
        await _bot(interaction).handle_bid(
            interaction, self.thread_id, self.lot_seq, increment=self.increment
        )


class CustomBidButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:bidc:(?P<thread>[0-9]+):(?P<lot>[0-9]+)",
):
    def __init__(self, thread_id: int, lot_seq: int) -> None:
        super().__init__(_btn(
            "Custom…", discord.ButtonStyle.secondary,
            f"nba:bidc:{thread_id}:{lot_seq}",
        ))
        self.thread_id = thread_id
        self.lot_seq = lot_seq

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = _bot(interaction)
        session = bot.sessions.get(self.thread_id)
        if session is None:
            await reply_ephemeral(interaction, bot.missing_session_message())
            return
        state = session.state
        if (
            state.phase != "auction"
            or state.lot is None
            or state.lot.seq != self.lot_seq
        ):
            await reply_ephemeral(interaction, "That auction already closed.")
            return
        if state.paused:
            await reply_ephemeral(interaction, "The draft is paused.")
            return
        # send_modal must be the FIRST response to this interaction.
        await interaction.response.send_modal(
            CustomBidModal(self.thread_id, self.lot_seq)
        )


class LotteryPickButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:lotto:(?P<thread>[0-9]+):(?P<lot>[0-9]+)",
):
    def __init__(self, thread_id: int, lot_seq: int) -> None:
        super().__init__(_btn(
            "🎲 Pick my number", discord.ButtonStyle.primary,
            f"nba:lotto:{thread_id}:{lot_seq}",
        ))
        self.thread_id = thread_id
        self.lot_seq = lot_seq

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = _bot(interaction)
        session = bot.sessions.get(self.thread_id)
        if session is None:
            await reply_ephemeral(interaction, bot.missing_session_message())
            return
        state = session.state
        if (
            state.phase != "auction"
            or state.lot is None
            or state.lot.seq != self.lot_seq
            or state.lot.lottery is None
        ):
            await reply_ephemeral(interaction, "That showdown already closed.")
            return
        if state.paused:
            await reply_ephemeral(interaction, "The draft is paused.")
            return
        # Participants only (the engine enforces this too).
        if interaction.user.id not in state.lot.lottery.participants:
            await reply_ephemeral(interaction, "You're not in this showdown.")
            return
        # send_modal must be the FIRST response to this interaction.
        await interaction.response.send_modal(
            LotteryGuessModal(self.thread_id, self.lot_seq)
        )


class LineupButton(
    _FromIntGroups,
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nba:lineup:(?P<thread>[0-9]+)",
):
    def __init__(self, thread_id: int) -> None:
        super().__init__(_btn(
            "🔀 Arrange my lineup", discord.ButtonStyle.primary,
            f"nba:lineup:{thread_id}",
        ))
        self.thread_id = thread_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await _bot(interaction).handle_lineup_panel(interaction, self.thread_id)


DYNAMIC_ITEMS: tuple[type, ...] = (
    JoinButton,
    LeaveButton,
    QuickBidButton,
    CustomBidButton,
    LotteryPickButton,
    LineupButton,
)


# -------------------------------------------------------------------- views


def lobby_view(thread_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(JoinButton(thread_id))
    view.add_item(LeaveButton(thread_id))
    return view


def bid_view(
    thread_id: int, lot_seq: int, quick_bids: tuple[int, ...]
) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for increment in quick_bids:
        view.add_item(QuickBidButton(thread_id, lot_seq, increment))
    view.add_item(CustomBidButton(thread_id, lot_seq))
    return view


def lineup_view(thread_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LineupButton(thread_id))
    return view


def lottery_view(thread_id: int, lot_seq: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LotteryPickButton(thread_id, lot_seq))
    return view


class LineupPanelView(discord.ui.View):
    """Ephemeral per-manager lineup panel — short-lived, NOT persistent.
    Two selects; once both are chosen the Swap dispatches through the same
    apply_event/render pipeline as /swap, then the panel is rebuilt fresh
    (refreshed lineup, reset selects) so the manager can keep rearranging."""

    def __init__(self, thread_id: int, user_id: int, manager: Manager) -> None:
        super().__init__(timeout=90)
        self.thread_id = thread_id
        self.user_id = user_id
        self.slot_a: str | None = None
        self.slot_b: str | None = None
        self.move_select: discord.ui.Select = discord.ui.Select(
            placeholder="Move this player",
            options=[
                discord.SelectOption(
                    label=f"{short_name(s.player.name)} (natural {s.player.pos})",
                    value=s.slot,
                )
                for s in manager.spots
                if s.player is not None
            ],
        )
        self.into_select: discord.ui.Select = discord.ui.Select(
            placeholder="…into this slot",
            options=[
                discord.SelectOption(label=s.slot, value=s.slot)
                for s in manager.spots
            ],
        )
        self.move_select.callback = self._on_move  # type: ignore[method-assign]
        self.into_select.callback = self._on_into  # type: ignore[method-assign]
        self.add_item(self.move_select)
        self.add_item(self.into_select)

    async def _on_move(self, interaction: discord.Interaction) -> None:
        self.slot_a = self.move_select.values[0]
        await self._maybe_swap(interaction)

    async def _on_into(self, interaction: discord.Interaction) -> None:
        self.slot_b = self.into_select.values[0]
        await self._maybe_swap(interaction)

    async def _maybe_swap(self, interaction: discord.Interaction) -> None:
        if self.slot_a is None or self.slot_b is None:
            await interaction.response.defer()  # wait for the other select
            return
        bot = _bot(interaction)
        session = bot.sessions.get(self.thread_id)
        if session is None:
            await reply_ephemeral(interaction, bot.missing_session_message())
            return
        if session.state.phase != "lineup":
            await reply_ephemeral(interaction, "The lineup window is closed.")
            return
        effects = await bot.apply_event(
            session, Swap(self.user_id, self.slot_a, self.slot_b)
        )
        await bot.render(session, effects, interaction)
        if interaction.response.is_done():
            return  # an ErrorFx already replied; the lineup didn't change
        manager = session.state.manager(self.user_id)
        if manager is None:  # kicked+replaced mid-panel — nothing to show
            await interaction.response.defer()
            return
        await interaction.response.edit_message(
            embed=lineup_panel_embed(manager, session.state.lineup_deadline),
            view=LineupPanelView(self.thread_id, self.user_id, manager),
        )
        self.stop()


class CancelConfirmView(discord.ui.View):
    """Ephemeral confirm for /draft cancel — short-lived, not persistent."""

    def __init__(self, thread_id: int) -> None:
        super().__init__(timeout=60)
        self.thread_id = thread_id

    @discord.ui.button(
        label="Yes, cancel the draft", style=discord.ButtonStyle.danger
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await _bot(interaction).handle_cancel_confirm(interaction, self.thread_id)


# -------------------------------------------------------------------- modal


class CustomBidModal(discord.ui.Modal, title="Custom bid"):
    amount: discord.ui.TextInput = discord.ui.TextInput(
        label="Amount in dollars",
        placeholder="e.g. 7",
        min_length=1,
        max_length=4,
    )

    def __init__(self, thread_id: int, lot_seq: int) -> None:
        super().__init__()
        self.thread_id = thread_id
        self.lot_seq = lot_seq

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.amount.value.strip().lstrip("$")
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole dollar amount, like 7.", ephemeral=True
            )
            return
        await _bot(interaction).handle_bid(
            interaction, self.thread_id, self.lot_seq, amount=value
        )


class LotteryGuessModal(discord.ui.Modal, title="All-in showdown"):
    number: discord.ui.TextInput = discord.ui.TextInput(
        label="Your number (1-100)",
        placeholder="e.g. 42",
        min_length=1,
        max_length=3,
    )

    def __init__(self, thread_id: int, lot_seq: int) -> None:
        super().__init__()
        self.thread_id = thread_id
        self.lot_seq = lot_seq

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(self.number.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Enter a whole number from 1 to 100.", ephemeral=True
            )
            return
        await _bot(interaction).handle_lottery_guess(
            interaction, self.thread_id, self.lot_seq, value
        )


# ---------------------------------------------------------- effect renderer


async def render_effects(
    bot: DraftBot,
    session: DraftSession,
    effects: list[Effect],
    interaction: discord.Interaction | None = None,
) -> None:
    """Turn engine effects into Discord I/O, in order. Runs outside the
    session lock; the state committed by dispatch is already final. The
    state is captured once so the whole batch renders the snapshot it
    belongs to, even if a concurrent commit swaps ``session.state``."""
    after_close = False
    state = session.state
    for fx in effects:
        try:
            after_close = await _render_one(
                bot, session, state, fx, interaction, after_close
            )
        except discord.HTTPException:
            log.exception("render failed for %r", fx)
        except Exception:
            log.exception("unexpected render failure for %r", fx)
    if any(isinstance(fx, (LotOpened, FreePickFx)) for fx in effects):
        # New message ids were minted; refresh the snapshot's meta.
        async with session.lock:
            await bot._save(session)


async def _render_one(
    bot: DraftBot,
    session: DraftSession,
    state: DraftState,
    fx: Effect,
    interaction: discord.Interaction | None,
    after_close: bool,
) -> bool:
    if isinstance(fx, LotOpened):
        if after_close:  # rate-bucket-friendly beat between lots
            await asyncio.sleep(CLOSE_BEAT_SECONDS)
        view = bid_view(session.thread_id, fx.lot.seq, state.config.quick_bids)
        message = await session.thread.send(
            embed=lot_embed(fx.lot, fx.pool_left), view=view
        )
        session.lot_message_id = message.id
        return False
    if isinstance(fx, BidPlaced):
        await _render_bid(session, state, fx, interaction)
    elif isinstance(fx, SoldFx):
        winner = state.manager(fx.manager_id)
        assert winner is not None
        await edit_lot(session, sold_embed(fx.player, winner, fx.price), clear=True)
        return True
    elif isinstance(fx, PassedFx):
        await edit_lot(session, passed_embed(fx.player), clear=True)
        return True
    elif isinstance(fx, ForceAssignedFx):
        target = state.manager(fx.manager_id)
        assert target is not None
        await edit_lot(session, force_embed(fx.player, target), clear=True)
        return True
    elif isinstance(fx, BoardFx):
        bot._schedule_board(session)
    elif isinstance(fx, ArmTimerFx):
        bot._arm_timer(session, fx.kind, fx.lot_seq, fx.deadline)
    elif isinstance(fx, CancelTimerFx):
        bot._cancel_timer(session)
    elif isinstance(fx, ErrorFx):
        await _render_error(fx, interaction)
    elif isinstance(fx, AutopilotFx):
        manager = state.manager(fx.manager_id)
        assert manager is not None
        await session.thread.send(
            f"🤖 **{manager.name}** is on autopilot — no more bids; their "
            "empty slots fill automatically at the end."
        )
    elif isinstance(fx, FreePickFx):
        await _render_free_pick(session, fx)
    elif isinstance(fx, PickedFx):
        picker = state.manager(fx.manager_id)
        assert picker is not None
        await session.thread.send(embed=picked_embed(fx.player, picker))
        if state.phase == "free_pick" and session.pick_message_id is not None:
            # Each pick re-arms a fresh deadline; keep the prompt's <t:R>
            # countdown authoritative for picks 2-5 too.
            await session.thread.get_partial_message(
                session.pick_message_id
            ).edit(content=pick_prompt(fx.manager_id, state.pick_deadline))
    elif isinstance(fx, AutoFilledFx):
        await session.thread.send(embed=autofill_embed(fx.assignments))
    elif isinstance(fx, LotteryOpenedFx):
        # New deadline + showdown status on the card, then the announcement
        # with its persistent pick button. The lot card keeps its bid
        # buttons — that's how a richer manager cancels the showdown.
        await edit_lot(
            session, lot_embed(fx.lot, 1 + len(state.queue)), clear=False
        )
        await session.thread.send(
            content=showdown_open_text(fx.lot),
            embed=showdown_embed(fx.lot),
            view=lottery_view(session.thread_id, fx.lot.seq),
        )
    elif isinstance(fx, LotteryJoinedFx):
        await session.thread.send(lottery_joined_text(fx.manager_id))
    elif isinstance(fx, LotteryGuessedFx):
        guesser = state.manager(fx.manager_id)
        assert guesser is not None
        await session.thread.send(lottery_guessed_text(guesser.name))
    elif isinstance(fx, LotteryCancelledFx):
        # The BidPlaced right behind this in the batch re-edits the lot
        # card (new leader/price/deadline) — only the announcement is here.
        canceller = state.manager(fx.manager_id)
        assert canceller is not None
        await session.thread.send(lottery_cancelled_text(canceller.name))
    elif isinstance(fx, LotteryRevealFx):
        # The engine emits this immediately before the SoldFx of the same
        # batch, so the reveal always lands right above the sale.
        await session.thread.send(embed=lottery_reveal_embed(fx, state))
    elif isinstance(fx, LineupPhaseFx):
        # Fire-and-forget: the button is stateless via its custom_id, so no
        # message id needs tracking — it keeps working across restarts.
        await session.thread.send(
            embed=lineup_open_embed(fx.deadline),
            view=lineup_view(session.thread_id),
        )
    elif isinstance(fx, CompleteFx):
        await _render_complete(bot, session, state)
    elif isinstance(fx, PausedFx):
        await _render_paused(session, state)
    elif isinstance(fx, ResumedFx):
        await _render_resumed(session, state, fx)
    elif isinstance(fx, CancelledFx):
        await _render_cancelled(bot, session, state)
    elif isinstance(fx, LobbyFx):
        await _render_lobby(session, state, interaction)
    return after_close


async def _edit_via_interaction_or_id(
    session: DraftSession,
    interaction: discord.Interaction | None,
    message_id: int | None,
    embed: discord.Embed,
) -> None:
    """Edit by stored message id; when the triggering component sits on that
    very message, ack + edit atomically via the interaction instead."""
    if (
        interaction is not None
        and interaction.message is not None
        and interaction.message.id == message_id
        and not interaction.response.is_done()
    ):
        await interaction.response.edit_message(embed=embed)
    elif message_id is not None:
        await session.thread.get_partial_message(message_id).edit(embed=embed)


async def _render_bid(
    session: DraftSession,
    state: DraftState,
    fx: BidPlaced,
    interaction: discord.Interaction | None,
) -> None:
    # A bid inside the warning window must not wipe the FINAL SECONDS styling
    # the timer task painted (mobile clients rely on it).
    embed = lot_embed(
        fx.lot,
        pool_left=1 + len(state.queue),
        final_seconds=fx.lot.deadline - time.time() <= FINAL_WARN_SECONDS,
    )
    await _edit_via_interaction_or_id(
        session, interaction, session.lot_message_id, embed
    )


async def edit_lot(
    session: DraftSession, embed: discord.Embed, clear: bool
) -> None:
    if session.lot_message_id is None:
        return
    partial = session.thread.get_partial_message(session.lot_message_id)
    if clear:
        await partial.edit(embed=embed, view=None)
    else:
        await partial.edit(embed=embed)


async def _render_error(
    fx: ErrorFx, interaction: discord.Interaction | None
) -> None:
    if interaction is None or interaction.user.id != fx.user_id:
        log.warning("undeliverable ErrorFx: %s", fx.message)
        return
    await reply_ephemeral(interaction, fx.message)


async def _render_lobby(
    session: DraftSession,
    state: DraftState,
    interaction: discord.Interaction | None,
) -> None:
    await _edit_via_interaction_or_id(
        session, interaction, session.lobby_message_id, lobby_embed(state)
    )


async def _render_free_pick(session: DraftSession, fx: FreePickFx) -> None:
    # Discord caps a message at 10 embeds AND 6000 chars across all embeds.
    # Reachable pools (≤ 5N = 50 players → ≤ 2 embeds) fit in one message;
    # the char budget guards any future config that grows the pool.
    batch: list[discord.Embed] = []
    batch_chars = 0
    for embed in pool_embeds(fx.pool):
        chars = len(embed.description or "") + len(embed.title or "")
        if batch and (
            len(batch) == MAX_EMBEDS_PER_MESSAGE
            or batch_chars + chars > EMBED_CHAR_BUDGET
        ):
            await session.thread.send(embeds=batch)
            batch, batch_chars = [], 0
        batch.append(embed)
        batch_chars += chars
    if batch:
        await session.thread.send(embeds=batch)
    message = await session.thread.send(pick_prompt(fx.manager_id, fx.deadline))
    session.pick_message_id = message.id


async def _render_paused(session: DraftSession, state: DraftState) -> None:
    if state.lot is not None:
        await edit_lot(
            session,
            lot_embed(state.lot, 1 + len(state.queue), paused=True),
            clear=False,
        )
    await session.thread.send("⏸️ Draft paused — the clock stops where it stood.")


async def _render_resumed(
    session: DraftSession, state: DraftState, fx: ResumedFx
) -> None:
    if fx.lot is not None:
        await edit_lot(session, lot_embed(fx.lot, 1 + len(state.queue)), clear=False)
        await session.thread.send("▶️ Draft resumed — clock's running.")
        return
    actives = state.active_managers
    if actives:
        message = await session.thread.send(
            "▶️ Resumed — " + pick_prompt(actives[0].user_id, state.pick_deadline)
        )
        session.pick_message_id = message.id
    else:
        await session.thread.send("▶️ Draft resumed.")


async def _render_complete(
    bot: DraftBot, session: DraftSession, state: DraftState
) -> None:
    bot._cancel_timer(session)
    if session.board_task is not None:
        session.board_task.cancel()  # a debounced repost would land after this
    if session.cpu_task is not None:
        session.cpu_task.cancel()  # CPUs are done once the draft wraps
    bot._delete_snapshot(session.thread_id)
    # Unconditional final board — reposted fresh so it sits at the bottom.
    # (repost_board deliberately reads live session.state.)
    try:
        await bot.repost_board(session)
    except discord.HTTPException:
        log.exception("final board repost failed for %s", session.thread_id)
    await session.thread.send(embed=complete_embed(state))
    if state.config.sim == "off":
        return
    if state.config.sim == "prompt":
        await send_share_prompt(session, state)
        return
    session.sim_task = bot._spawn(bot._run_sim(session))


async def _render_cancelled(
    bot: DraftBot, session: DraftSession, state: DraftState
) -> None:
    bot._cancel_timer(session)
    if session.board_task is not None:
        session.board_task.cancel()
    if session.cpu_task is not None:
        session.cpu_task.cancel()
    if state.lot is not None:
        await edit_lot(session, cancelled_lot_embed(state.lot), clear=True)
    if session.lobby_message_id is not None:
        try:
            await session.thread.get_partial_message(
                session.lobby_message_id
            ).edit(view=None)
        except discord.HTTPException:
            pass
    await session.thread.send("🛑 Draft cancelled by the commissioner.")
    bot._delete_snapshot(session.thread_id)
    bot.sessions.pop(session.thread_id, None)
