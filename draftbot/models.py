"""Data contract for the draft engine.

Pure data — no Discord, no I/O, no timers. Every type is a frozen dataclass;
transitions build new objects (never mutate). ``engine.apply(state, event,
rng)`` is the only way state changes; it returns ``(new_state, [effects])``
where effects are *descriptions* of what the outside world should do.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SLOTS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")

SNAKE_BUDGET: int = 15  # hard-coded snake-mode budget: exactly one player
# from every tier ($5+$4+$3+$2+$1) — stacking $5 stars forces bargain picks.


# ---------------------------------------------------------------- core data


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    team: str  # franchise of their prime era
    pos: str  # natural position, one of SLOTS (modern 5-position mapping)
    ppg: float  # prime-years per-game stats — the only stats recorded in
    rpg: float  # every era since 1960, so no nulls across decades
    apg: float
    stars: int  # 1-5, era-relative editorial rating of their prime
    decade: int = 2020  # anchor decade of their prime: 1960..2020
    prime: str = ""  # display range, e.g. "1989–1993"
    rank: int = 10  # 1-10 caliber rank within decade+position (1 = best)


def snake_price(p: Player) -> int:
    """Snake-mode sticker price: the 1-5 star tier IS the dollar tier."""
    return p.stars


@dataclass(frozen=True)
class Config:
    budget: int = 20
    slots: tuple[str, ...] = SLOTS
    era_start: int = 2000  # decade anchors, inclusive; host picks in
    era_end: int = 2020  # 10-year increments on /draft create
    pool_depth: str = "legends"  # "legends" (rank<=4) | "household" (<=7) | "deep" (all)
    lot_seconds: int = 30  # clock per lot; only late bids extend it (soft close)
    snipe_window: float = 10.0  # a bid this close to the deadline...
    snipe_extend: float = 5.0  # ...pushes the deadline out by this much
    lottery_seconds: float = 15.0  # all-in showdown countdown
    quick_bids: tuple[int, ...] = (1, 2, 5)
    min_managers: int = 2
    max_managers: int = 10
    pass_rule: str = "pass_once"  # or "recycle_forever"
    afk_lots: int = 10
    free_pick_seconds: int = 60
    lineup_seconds: int = 60  # arrange-your-lineup window before completion; 0 skips it
    sim: str = "prompt"  # "prompt" (copy-paste prompt for your own LLM, default)
    # | "off" | "stats" (pure stat ranking) | "ai" (stats + weighted LLM ranking)
    mode: str = "auction"  # | "blind" (auction, names hidden until rostered)
    # | "snake" (open pool, $15 budget, $1-$5 tier prices, snaking turn order)


@dataclass(frozen=True)
class Spot:
    slot: str  # "PG" etc. — the lineup slot, not the player's natural pos
    player: Player | None = None
    price: int = 0  # 0 for free assignments (picks / auto-fill)


@dataclass(frozen=True)
class Manager:
    user_id: int  # Discord/web ids are positive; CPU managers are negative
    name: str
    budget: int
    spots: tuple[Spot, ...]
    autopilot: bool = False
    last_action_lot: int = 0
    cpu: bool = False  # computer-driven manager (draftbot/cpu.py decides)

    @property
    def empty_slots(self) -> int:
        return sum(1 for s in self.spots if s.player is None)

    @property
    def full(self) -> bool:
        return self.empty_slots == 0


@dataclass(frozen=True)
class Lottery:
    """All-in showdown: 2+ managers tied at their entire remaining budget.

    Participants pick 1-100; at the deadline the engine draws a mystery
    number and the closest guess buys the player at the tied amount. The
    leader is always participants[0]. Guesses are (user_id, number) pairs —
    private until the reveal. The countdown lives in ``Lot.deadline`` so
    pause/addtime/stale-timer machinery works unchanged."""

    participants: tuple[int, ...]
    guesses: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class Lot:
    seq: int
    player: Player
    last_call: bool  # second appearance — sells or force-assigns, never recycles
    current_bid: int = 0  # 0 = no opening bid yet (opening window)
    leader_id: int | None = None
    deadline: float = 0.0  # epoch seconds; authoritative over any client render
    lottery: Lottery | None = None  # live all-in showdown, else None


@dataclass(frozen=True)
class LogEntry:
    kind: str  # sold | force | passed | pick | autofill
    player: Player
    manager_id: int | None  # None for a first-pass recycle
    price: int


@dataclass(frozen=True)
class DraftState:
    config: Config
    commissioner_id: int
    phase: str = "lobby"  # lobby | auction | snake | free_pick | lineup
    # | complete | cancelled ("blind" mode runs the auction phase)
    managers: tuple[Manager, ...] = ()
    queue: tuple[Player, ...] = ()  # hidden upcoming players; head is next up
    passed_ids: frozenset = field(default_factory=frozenset)  # passed once
    lot: Lot | None = None
    lot_seq: int = 0  # last dealt lot number (1-based)
    pick_deadline: float = 0.0  # free-pick phase only
    lineup_deadline: float = 0.0  # lineup phase only
    log: tuple[LogEntry, ...] = ()
    paused: bool = False
    pause_remaining: float = 0.0

    def manager(self, user_id: int) -> Manager | None:
        return next((m for m in self.managers if m.user_id == user_id), None)

    @property
    def active_managers(self) -> tuple[Manager, ...]:
        """Active = empty slots + at least $1 + not autopilot (DESIGN #12)."""
        return tuple(
            m for m in self.managers
            if not m.autopilot and m.budget >= 1 and not m.full
        )


# ------------------------------------------------------------------- events
# Events that arm or check timers carry ``now`` (epoch seconds) so the engine
# never reads the clock itself.


@dataclass(frozen=True)
class Join:
    user_id: int
    name: str


@dataclass(frozen=True)
class Leave:
    user_id: int


@dataclass(frozen=True)
class Start:
    user_id: int  # must be commissioner
    players: tuple[Player, ...]  # full dataset; engine builds the pool
    now: float


@dataclass(frozen=True)
class Bid:
    """Exactly one of ``increment`` (quick button, relative to the live bid)
    or ``amount`` (modal, absolute) is set. An increment on a lot with no
    opening bid opens at ``increment``."""

    user_id: int
    lot_seq: int
    now: float
    increment: int | None = None
    amount: int | None = None


@dataclass(frozen=True)
class TimerExpired:
    """Fired by the bot's timer task. ``deadline`` echoes the value the timer
    was armed with — the engine ignores the event unless it matches current
    state (stale-timer guard)."""

    kind: str  # "lot" | "pick" | "snake" | "lineup"
    lot_seq: int  # -1 for kind="pick" / "snake" / "lineup"
    deadline: float
    now: float


@dataclass(frozen=True)
class LotteryGuess:
    """A showdown participant locks in (or overwrites) their 1-100 number."""

    user_id: int
    lot_seq: int
    guess: int
    now: float


@dataclass(frozen=True)
class Pick:
    user_id: int
    player_id: str
    now: float


@dataclass(frozen=True)
class Swap:
    user_id: int
    slot_a: str
    slot_b: str


@dataclass(frozen=True)
class Pause:
    user_id: int
    now: float


@dataclass(frozen=True)
class Resume:
    user_id: int
    now: float


@dataclass(frozen=True)
class Kick:
    user_id: int  # commissioner issuing the kick
    target_id: int
    now: float
    replacement_id: int | None = None
    replacement_name: str | None = None


@dataclass(frozen=True)
class AddCpu:
    """Commissioner adds computer opponents to the lobby (ids go negative)."""

    user_id: int
    count: int = 1


@dataclass(frozen=True)
class RemoveCpu:
    """Commissioner removes a CPU manager from the lobby."""

    user_id: int
    cpu_id: int


@dataclass(frozen=True)
class Cancel:
    user_id: int


Event = (
    Join | Leave | Start | Bid | TimerExpired | LotteryGuess | Pick | Swap
    | Pause | Resume | Kick | AddCpu | RemoveCpu | Cancel
)


# ------------------------------------------------------------------ effects
# Effects are render/side-effect instructions for the Discord layer. The
# engine emits them in the order they should happen.


@dataclass(frozen=True)
class LotOpened:
    lot: Lot
    pool_left: int  # players remaining in pool incl. this one


@dataclass(frozen=True)
class BidPlaced:
    lot: Lot  # updated lot: new price / leader / deadline


@dataclass(frozen=True)
class SoldFx:
    player: Player
    manager_id: int
    price: int


@dataclass(frozen=True)
class PassedFx:  # first pass only — recycled to the back of the queue
    player: Player


@dataclass(frozen=True)
class ForceAssignedFx:  # LAST CALL went unsold
    player: Player
    manager_id: int


@dataclass(frozen=True)
class FreePickFx:  # exactly one active manager left; pool is revealed
    manager_id: int
    pool: tuple[Player, ...]
    deadline: float


@dataclass(frozen=True)
class PickedFx:
    player: Player
    manager_id: int


@dataclass(frozen=True)
class AutoFilledFx:
    assignments: tuple[tuple[int, Player], ...]  # (manager_id, player)


@dataclass(frozen=True)
class LotteryOpenedFx:  # all-in tie — showdown countdown started
    lot: Lot  # lot.lottery set; lot.deadline is the showdown deadline


@dataclass(frozen=True)
class LotteryJoinedFx:  # another tied all-in manager entered the showdown
    lot: Lot
    manager_id: int


@dataclass(frozen=True)
class LotteryGuessedFx:  # participant locked in a number (value stays hidden)
    manager_id: int


@dataclass(frozen=True)
class LotteryCancelledFx:  # a richer manager outbid the tied stacks
    manager_id: int  # who cancelled it (the new leader)


@dataclass(frozen=True)
class LotteryRevealFx:  # showdown resolved — followed by a SoldFx
    mystery: int
    guesses: tuple[tuple[int, int], ...]  # (manager_id, number), all filled
    winner_id: int


@dataclass(frozen=True)
class SnakeTurnFx:  # snake mode — this manager is now on the clock
    manager_id: int
    deadline: float


@dataclass(frozen=True)
class LineupPhaseFx:  # all rosters full — arrange-lineup window is open
    deadline: float


@dataclass(frozen=True)
class CompleteFx:
    pass


@dataclass(frozen=True)
class ArmTimerFx:
    kind: str  # "lot" | "pick" | "lineup"
    lot_seq: int  # -1 for kind="pick" / "lineup"
    deadline: float


@dataclass(frozen=True)
class CancelTimerFx:
    pass


@dataclass(frozen=True)
class BoardFx:  # budgets/rosters changed; re-render the pinned board
    pass


@dataclass(frozen=True)
class ErrorFx:  # ephemeral rejection back to one user
    user_id: int
    message: str


@dataclass(frozen=True)
class AutopilotFx:
    manager_id: int


@dataclass(frozen=True)
class PausedFx:
    pass


@dataclass(frozen=True)
class ResumedFx:
    lot: Lot | None  # carries the recomputed deadline


@dataclass(frozen=True)
class CancelledFx:
    pass


@dataclass(frozen=True)
class LobbyFx:  # lobby membership changed; re-render the lobby message
    pass


Effect = (
    LotOpened | BidPlaced | SoldFx | PassedFx | ForceAssignedFx | FreePickFx
    | PickedFx | AutoFilledFx | LotteryOpenedFx | LotteryJoinedFx
    | LotteryGuessedFx | LotteryCancelledFx | LotteryRevealFx | SnakeTurnFx
    | LineupPhaseFx
    | CompleteFx | ArmTimerFx | CancelTimerFx | BoardFx | ErrorFx
    | AutopilotFx | PausedFx | ResumedFx | CancelledFx | LobbyFx
)
