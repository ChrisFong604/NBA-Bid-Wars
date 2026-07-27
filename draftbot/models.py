"""Data contract for the draft engine.

Pure data — no Discord, no I/O, no timers. Every type is a frozen dataclass;
transitions build new objects (never mutate). ``engine.apply(state, event,
rng)`` is the only way state changes; it returns ``(new_state, [effects])``
where effects are *descriptions* of what the outside world should do.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SLOTS: tuple[str, ...] = ("PG", "SG", "SF", "PF", "C")


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


@dataclass(frozen=True)
class Config:
    budget: int = 20
    slots: tuple[str, ...] = SLOTS
    era_start: int = 1960  # decade anchors, inclusive; host picks in
    era_end: int = 2020  # 10-year increments on /draft create
    lot_seconds: int = 30  # flat clock per lot — bids never extend it
    quick_bids: tuple[int, ...] = (1, 2, 5)
    min_managers: int = 2
    max_managers: int = 10
    pass_rule: str = "pass_once"  # or "recycle_forever"
    afk_lots: int = 10
    free_pick_seconds: int = 60
    sim: str = "prompt"  # "prompt" (copy-paste prompt for your own LLM, default)
    # | "off" | "stats" (pure stat ranking) | "ai" (stats + weighted LLM ranking)


@dataclass(frozen=True)
class Spot:
    slot: str  # "PG" etc. — the lineup slot, not the player's natural pos
    player: Player | None = None
    price: int = 0  # 0 for free assignments (picks / auto-fill)


@dataclass(frozen=True)
class Manager:
    user_id: int
    name: str
    budget: int
    spots: tuple[Spot, ...]
    autopilot: bool = False
    last_action_lot: int = 0

    @property
    def empty_slots(self) -> int:
        return sum(1 for s in self.spots if s.player is None)

    @property
    def full(self) -> bool:
        return self.empty_slots == 0


@dataclass(frozen=True)
class Lot:
    seq: int
    player: Player
    last_call: bool  # second appearance — sells or force-assigns, never recycles
    current_bid: int = 0  # 0 = no opening bid yet (opening window)
    leader_id: int | None = None
    deadline: float = 0.0  # epoch seconds; authoritative over any client render


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
    phase: str = "lobby"  # lobby | auction | free_pick | complete | cancelled
    managers: tuple[Manager, ...] = ()
    queue: tuple[Player, ...] = ()  # hidden upcoming players; head is next up
    passed_ids: frozenset = field(default_factory=frozenset)  # passed once
    lot: Lot | None = None
    lot_seq: int = 0  # last dealt lot number (1-based)
    pick_deadline: float = 0.0  # free-pick phase only
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

    kind: str  # "lot" | "pick"
    lot_seq: int  # -1 for kind="pick"
    deadline: float
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
class Cancel:
    user_id: int


Event = (
    Join | Leave | Start | Bid | TimerExpired | Pick | Swap | Pause | Resume
    | Kick | Cancel
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
class CompleteFx:
    pass


@dataclass(frozen=True)
class ArmTimerFx:
    kind: str  # "lot" | "pick"
    lot_seq: int  # -1 for kind="pick"
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
    | PickedFx | AutoFilledFx | CompleteFx | ArmTimerFx | CancelTimerFx
    | BoardFx | ErrorFx | AutopilotFx | PausedFx | ResumedFx | CancelledFx
    | LobbyFx
)
