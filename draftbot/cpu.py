"""Pure CPU-manager brain: ``decide(state, cpu_id, now) -> (event, delay)``.

Zero Discord, zero I/O, zero clock reads — the caller supplies ``now`` and
feeds any returned event through the normal engine dispatch, so every rule
(bidding, soft close, all-in showdowns, going broke, free pick) applies to
CPUs unchanged, and a stale decision is rejected by the engine like any
late human click.

Deterministic: all "personality" randomness is seeded from
``(cpu_id, lot.seq)``, so the same snapshot always yields the same move.
"""
from __future__ import annotations

import random

from .models import (
    Bid,
    DraftState,
    Event,
    LotteryGuess,
    Manager,
    Pick,
    Player,
    Swap,
)

IDLE_DELAY = 3.0  # nothing to do — poll again soon (keeps showdown entry live)
REACT_DELAY = 1.0  # just acted — check back quickly
MAX_DELAY = 10.0  # cap every requested sleep; deadlines can move under us
PICK_THINK_SECONDS = 2.0  # free-pick "thinking" pause after the window opens

# Star rating -> multiplier on the fair per-slot budget share.
_STAR_MULT = {1: 0.4, 2: 0.8, 3: 1.2, 4: 1.8, 5: 2.6}


def decide(
    state: DraftState, cpu_id: int, now: float
) -> tuple[Event | None, float]:
    """The CPU's next move: at most one event, plus seconds until the next
    call. Pure — safe to call with a state snapshot outside any lock."""
    event, delay = _decide(state, cpu_id, now)
    return event, min(delay, MAX_DELAY)


def _decide(
    state: DraftState, cpu_id: int, now: float
) -> tuple[Event | None, float]:
    m = state.manager(cpu_id)
    if m is None or not m.cpu or m.autopilot or state.paused:
        return None, IDLE_DELAY
    if state.phase == "lineup":
        return _lineup_move(state, m)
    if m.full:
        return None, IDLE_DELAY
    if state.phase == "auction" and state.lot is not None:
        return _auction_move(state, m, now)
    if state.phase == "free_pick":
        return _free_pick_move(state, m, now)
    return None, IDLE_DELAY


def _limit(m: Manager, player: Player) -> int:
    """Max price this CPU pays: a star-scaled slice of its fair per-slot
    share, keeping $1 per other empty slot unless the player is a 5-star."""
    share = m.budget / m.empty_slots
    reserve = 0 if player.stars == 5 else m.empty_slots - 1
    return max(0, min(round(share * _STAR_MULT[player.stars]), m.budget - reserve))


def _auction_move(
    state: DraftState, m: Manager, now: float
) -> tuple[Event | None, float]:
    lot = state.lot
    assert lot is not None
    if now > lot.deadline:
        return None, REACT_DELAY  # resolution imminent — wait for the next lot
    rng = random.Random(m.user_id * 1_000_003 + lot.seq)
    if lot.lottery is not None:
        # Showdown participant with no locked number yet -> pick one.
        if m.user_id in lot.lottery.participants and all(
            uid != m.user_id for uid, _ in lot.lottery.guesses
        ):
            return (
                LotteryGuess(m.user_id, lot.seq, rng.randint(1, 100), now),
                REACT_DELAY,
            )
        return None, IDLE_DELAY
    if lot.leader_id == m.user_id:
        return None, IDLE_DELAY
    limit = _limit(m, lot.player)
    raise_to = 1 if lot.current_bid == 0 else lot.current_bid + 1
    if raise_to <= min(limit, m.budget):
        # Soft-close timing: act inside the snipe window, at a jittered
        # per-(cpu, lot) moment so CPUs don't move in lockstep.
        act_at = lot.deadline - state.config.snipe_window * (
            0.4 + 0.5 * rng.random()
        )
        if now < act_at:
            return None, act_at - now
        return Bid(m.user_id, lot.seq, now, amount=raise_to), REACT_DELAY
    if _all_in_match(state, m, limit):
        # Rule #19: tie the all-in stack — join (or open) the showdown.
        return Bid(m.user_id, lot.seq, now, amount=m.budget), REACT_DELAY
    return None, IDLE_DELAY


def _all_in_match(state: DraftState, m: Manager, limit: int) -> bool:
    """True when bidding our exact all-in stack joins/opens a showdown the
    engine will accept, and the player is worth our whole budget."""
    lot = state.lot
    assert lot is not None
    if lot.current_bid < 1 or m.budget != lot.current_bid or limit < m.budget:
        return False
    if lot.lottery is not None:
        return m.user_id not in lot.lottery.participants
    leader = state.manager(lot.leader_id) if lot.leader_id is not None else None
    return leader is not None and leader.budget == lot.current_bid


def _lineup_move(state: DraftState, m: Manager) -> tuple[Event | None, float]:
    """Arrange the roster: best players claim their natural slot; displaced
    ones slide to the nearest open slot (a 4★ C behind a 5★ C plays PF, a
    4★ PG behind a 5★ PG plays SG). One Swap per call; each swap parks at
    least one player in their target slot, so it settles in <=4 swaps."""
    slots = state.config.slots
    idx = {s: i for i, s in enumerate(slots)}
    players = sorted(
        (s.player for s in m.spots if s.player is not None),
        key=lambda p: (-p.stars, -(p.ppg + p.rpg + p.apg), p.id),
    )
    free = list(slots)
    target: dict[str, str] = {}
    for p in players:
        best = min(free, key=lambda s: (abs(idx[s] - idx[p.pos]), idx[s]))
        free.remove(best)
        target[p.id] = best
    for spot in m.spots:
        if spot.player is not None and target[spot.player.id] != spot.slot:
            return Swap(m.user_id, spot.slot, target[spot.player.id]), REACT_DELAY
    return None, IDLE_DELAY


def _free_pick_move(
    state: DraftState, m: Manager, now: float
) -> tuple[Event | None, float]:
    actives = state.active_managers
    if len(actives) != 1 or actives[0].user_id != m.user_id or not state.queue:
        return None, IDLE_DELAY
    act_at = (
        state.pick_deadline - state.config.free_pick_seconds + PICK_THINK_SECONDS
    )
    if now < act_at:
        return None, act_at - now
    best = max(state.queue, key=lambda p: (p.stars, p.ppg + p.rpg + p.apg))
    return Pick(m.user_id, best.id, now), REACT_DELAY
