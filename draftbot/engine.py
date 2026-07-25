"""Pure draft engine: ``apply(state, event, rng) -> (new_state, effects)``.

Zero Discord, zero I/O, zero clock reads — events carry ``now`` and effects
are descriptions for the outside world. DESIGN.md rules #1-#18 are the
source of truth for every transition here.
"""
from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace

from .models import (
    SLOTS,
    ArmTimerFx,
    AutoFilledFx,
    AutopilotFx,
    Bid,
    BidPlaced,
    BoardFx,
    Cancel,
    CancelledFx,
    CancelTimerFx,
    CompleteFx,
    Config,
    DraftState,
    Effect,
    ErrorFx,
    Event,
    ForceAssignedFx,
    FreePickFx,
    Join,
    Kick,
    Leave,
    LobbyFx,
    LogEntry,
    Lot,
    LotOpened,
    Manager,
    PassedFx,
    Pause,
    PausedFx,
    Pick,
    PickedFx,
    Player,
    Resume,
    ResumedFx,
    SoldFx,
    Spot,
    Start,
    Swap,
    TimerExpired,
)

Transition = tuple[DraftState, list[Effect]]


# ------------------------------------------------------------------- public


def apply(
    state: DraftState, event: Event, rng: random.Random | None = None
) -> Transition:
    r = rng if rng is not None else random.Random()
    if isinstance(event, Join):
        return _join(state, event)
    if isinstance(event, Leave):
        return _leave(state, event)
    if isinstance(event, Start):
        return _start(state, event, r)
    if isinstance(event, Bid):
        return _bid(state, event)
    if isinstance(event, TimerExpired):
        if event.kind == "lot":
            return _lot_expired(state, event, r)
        if event.kind == "pick":
            return _pick_expired(state, event, r)
        return state, []
    if isinstance(event, Pick):
        return _pick(state, event, r)
    if isinstance(event, Swap):
        return _swap(state, event)
    if isinstance(event, Pause):
        return _pause(state, event)
    if isinstance(event, Resume):
        return _resume(state, event)
    if isinstance(event, Kick):
        return _kick(state, event)
    if isinstance(event, Cancel):
        return _cancel(state, event)
    raise TypeError(f"unknown event: {event!r}")


def build_pool(
    players: Sequence[Player],
    n_managers: int,
    config: Config,
    rng: random.Random,
) -> tuple[Player, ...]:
    """Exactly ``5 * n_managers`` players — one per roster spot, shuffled.

    Stratified best-effort: aim for ``n_managers`` per natural position; when
    the era-filtered dataset lacks a position, the shortfall is filled with
    random players from other positions (any player can occupy any slot,
    rule #9). Zero leftovers: every pool player ends on a roster.
    """
    total = len(config.slots) * n_managers
    if len(players) < total:
        raise ValueError(
            f"not enough players: need {total} for {n_managers} managers, "
            f"have {len(players)}"
        )
    chosen: list[Player] = []
    for pos in SLOTS:
        bucket = [p for p in players if p.pos == pos]
        chosen.extend(rng.sample(bucket, min(n_managers, len(bucket))))
    chosen_ids = {p.id for p in chosen}
    leftovers = [p for p in players if p.id not in chosen_ids]
    chosen.extend(rng.sample(leftovers, total - len(chosen)))
    rng.shuffle(chosen)
    return tuple(chosen)


def redeal(
    state: DraftState, now: float, rng: random.Random | None = None
) -> Transition:
    """Crash recovery. The caller has already pushed the interrupted lot's
    player back to the queue head; deal it again as a fresh lot (new seq, so
    pre-crash button clicks are stale by construction)."""
    return _deal_lot(replace(state, lot=None), now)


# ---------------------------------------------------------------- internals


def _err(state: DraftState, user_id: int, message: str) -> Transition:
    return state, [ErrorFx(user_id, message)]


def _with_manager(state: DraftState, m: Manager) -> DraftState:
    managers = tuple(m if x.user_id == m.user_id else x for x in state.managers)
    return replace(state, managers=managers)


def _assign(m: Manager, player: Player, price: int) -> Manager:
    """Place ``player`` into the first empty spot and charge ``price``."""
    idx = next(i for i, s in enumerate(m.spots) if s.player is None)
    spot = replace(m.spots[idx], player=player, price=price)
    spots = m.spots[:idx] + (spot,) + m.spots[idx + 1 :]
    return replace(m, spots=spots, budget=m.budget - price)


# ---------------------------------------------------------------- lobby


def _join(state: DraftState, ev: Join) -> Transition:
    existing = state.manager(ev.user_id)
    if state.phase in ("auction", "free_pick") and existing and existing.autopilot:
        if (
            state.phase == "free_pick"
            and not existing.full
            and existing.budget >= 1
        ):
            # Rules #13/#16 conflict: waking this team now would create a
            # second active manager mid-free-pick, so the sole survivor's
            # pick would brick. The reward stands; this team auto-fills.
            return _err(
                state,
                ev.user_id,
                "The free-pick phase has started — your empty slots fill "
                "automatically at the end.",
            )
        # Rule #16: rejoining reclaims the team.
        m = replace(existing, autopilot=False, last_action_lot=state.lot_seq)
        return _with_manager(state, m), [BoardFx()]
    if state.phase != "lobby":
        return _err(state, ev.user_id, "The draft has already started.")
    if existing is not None:
        return _err(state, ev.user_id, "You're already in this draft.")
    if len(state.managers) >= state.config.max_managers:
        return _err(state, ev.user_id, "The lobby is full.")
    m = Manager(
        user_id=ev.user_id,
        name=ev.name,
        budget=state.config.budget,
        spots=tuple(Spot(slot=s) for s in state.config.slots),
    )
    return replace(state, managers=state.managers + (m,)), [LobbyFx()]


def _leave(state: DraftState, ev: Leave) -> Transition:
    m = state.manager(ev.user_id)
    if m is None:
        return _err(state, ev.user_id, "You're not in this draft.")
    if state.phase == "lobby":
        managers = tuple(x for x in state.managers if x.user_id != ev.user_id)
        return replace(state, managers=managers), [LobbyFx()]
    if state.phase in ("auction", "free_pick"):
        if m.autopilot:
            return _err(state, ev.user_id, "You've already left this draft.")
        # Takes effect at the next lot resolution; a standing bid still pays.
        m2 = replace(m, autopilot=True)
        return _with_manager(state, m2), [AutopilotFx(ev.user_id), BoardFx()]
    return _err(state, ev.user_id, "The draft is over.")


def _start(state: DraftState, ev: Start, rng: random.Random) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can start the draft.")
    if state.phase != "lobby":
        return _err(state, ev.user_id, "The draft has already started.")
    if len(state.managers) < state.config.min_managers:
        return _err(
            state,
            ev.user_id,
            f"Need at least {state.config.min_managers} managers to start.",
        )
    pool = build_pool(ev.players, len(state.managers), state.config, rng)
    managers = tuple(replace(m, last_action_lot=0) for m in state.managers)
    state2 = replace(state, phase="auction", managers=managers, queue=pool)
    return _deal_lot(state2, ev.now)


# ---------------------------------------------------------------- auction


def _deal_lot(state: DraftState, now: float) -> Transition:
    player, rest = state.queue[0], state.queue[1:]
    seq = state.lot_seq + 1
    lot = Lot(
        seq=seq,
        player=player,
        last_call=(
            state.config.pass_rule == "pass_once"
            and player.id in state.passed_ids
        ),
        # Flat clock (rule #5): armed once per lot; bids never extend it.
        deadline=now + state.config.lot_seconds,
    )
    state2 = replace(state, queue=rest, lot=lot, lot_seq=seq)
    fx: list[Effect] = [
        LotOpened(lot, pool_left=1 + len(rest)),
        ArmTimerFx("lot", seq, lot.deadline),
    ]
    return state2, fx


def _bid(state: DraftState, ev: Bid) -> Transition:
    uid = ev.user_id
    if state.paused:
        return _err(state, uid, "The draft is paused.")
    if state.phase != "auction" or state.lot is None:
        return _err(state, uid, "There's no auction running right now.")
    lot = state.lot
    if ev.lot_seq != lot.seq:
        return _err(state, uid, "That auction already closed.")
    if ev.now > lot.deadline:
        return _err(state, uid, "That auction already closed.")
    m = state.manager(uid)
    if m is None or m.autopilot:
        return _err(state, uid, "You're not drafting in this auction.")
    if m.full:
        return _err(state, uid, "Your roster is already full.")
    if lot.leader_id == uid:
        return _err(state, uid, "You're already the high bidder.")
    if ev.amount is not None:
        effective = ev.amount
    else:
        effective = lot.current_bid + (ev.increment or 0)
    if effective < 1 or effective <= lot.current_bid:
        return _err(state, uid, f"Bid must beat the current ${lot.current_bid}.")
    if effective > m.budget:
        return _err(state, uid, f"You've only got ${m.budget} left.")
    # Flat clock: the deadline set at deal time stands — no re-arm on bids.
    new_lot = replace(lot, current_bid=effective, leader_id=uid)
    state2 = _with_manager(
        replace(state, lot=new_lot), replace(m, last_action_lot=lot.seq)
    )
    return state2, [BidPlaced(new_lot)]


def _lot_expired(
    state: DraftState, ev: TimerExpired, rng: random.Random
) -> Transition:
    lot = state.lot
    if (
        state.paused
        or state.phase != "auction"
        or lot is None
        or ev.lot_seq != lot.seq
        or ev.deadline != lot.deadline  # stale guard: addtime/resume re-arm
    ):
        return state, []
    if lot.current_bid > 0:  # SOLD
        winner = state.manager(lot.leader_id)
        assert winner is not None
        entry = LogEntry("sold", lot.player, winner.user_id, lot.current_bid)
        state2 = replace(
            _with_manager(state, _assign(winner, lot.player, lot.current_bid)),
            lot=None,
            log=state.log + (entry,),
        )
        fx: list[Effect] = [
            SoldFx(lot.player, winner.user_id, lot.current_bid),
            BoardFx(),
        ]
        return _resolve_next(state2, ev.now, rng, fx)
    if not lot.last_call or not state.active_managers:
        # PASSED — recycled. The no-actives last-call case also recycles:
        # nobody can be charged $1, and resolve_next auto-fills for free.
        passed = state.passed_ids
        if state.config.pass_rule == "pass_once":
            passed = passed | {lot.player.id}
        entry = LogEntry("passed", lot.player, None, 0)
        state2 = replace(
            state,
            lot=None,
            queue=state.queue + (lot.player,),
            passed_ids=passed,
            log=state.log + (entry,),
        )
        return _resolve_next(state2, ev.now, rng, [PassedFx(lot.player)])
    # LAST CALL — force-assign at $1 to an active team with most open slots.
    actives = state.active_managers
    most = max(m.empty_slots for m in actives)
    target = rng.choice([m for m in actives if m.empty_slots == most])
    entry = LogEntry("force", lot.player, target.user_id, 1)
    state2 = replace(
        _with_manager(state, _assign(target, lot.player, 1)),
        lot=None,
        log=state.log + (entry,),
    )
    fx = [ForceAssignedFx(lot.player, target.user_id), BoardFx()]
    return _resolve_next(state2, ev.now, rng, fx)


def _resolve_next(
    state: DraftState, now: float, rng: random.Random, fx: list[Effect]
) -> Transition:
    # (a) AFK sweep (rule #16) — evaluated only at lot resolution.
    managers = []
    for m in state.managers:
        if (
            not m.autopilot
            and not m.full
            and state.lot_seq - m.last_action_lot >= state.config.afk_lots
        ):
            m = replace(m, autopilot=True)
            fx.append(AutopilotFx(m.user_id))
        managers.append(m)
    state = replace(state, managers=tuple(managers))
    # (b) everyone full -> done.
    if all(m.full for m in state.managers):
        return replace(state, phase="complete"), fx + [CompleteFx()]
    # (c) phase by active count (rules #12-#14).
    actives = state.active_managers
    if len(actives) >= 2:
        state2, fx2 = _deal_lot(state, now)
        return state2, fx + fx2
    if len(actives) == 1:
        deadline = now + state.config.free_pick_seconds
        state2 = replace(state, phase="free_pick", pick_deadline=deadline)
        fx2: list[Effect] = [
            FreePickFx(actives[0].user_id, state.queue, deadline),
            ArmTimerFx("pick", -1, deadline),
        ]
        return state2, fx + fx2
    return _auto_fill(state, rng, fx)


def _auto_fill(
    state: DraftState, rng: random.Random, fx: list[Effect]
) -> Transition:
    """Fill every empty slot with random pool players, free (rule #14)."""
    order = list(state.managers)
    rng.shuffle(order)
    queue = list(state.queue)
    by_id = {m.user_id: m for m in state.managers}
    assignments: list[tuple[int, Player]] = []
    log = list(state.log)
    for m in order:
        cur = by_id[m.user_id]
        while not cur.full:
            player = queue.pop(rng.randrange(len(queue)))
            cur = _assign(cur, player, 0)
            assignments.append((cur.user_id, player))
            log.append(LogEntry("autofill", player, cur.user_id, 0))
        by_id[m.user_id] = cur
    state2 = replace(
        state,
        phase="complete",
        managers=tuple(by_id[m.user_id] for m in state.managers),
        queue=tuple(queue),
        log=tuple(log),
        pick_deadline=0.0,
    )
    return state2, fx + [AutoFilledFx(tuple(assignments)), BoardFx(), CompleteFx()]


# ---------------------------------------------------------------- free pick


def _pick(state: DraftState, ev: Pick, rng: random.Random) -> Transition:
    uid = ev.user_id
    if state.phase != "free_pick":
        return _err(state, uid, "There's no free-pick phase running.")
    if state.paused:
        return _err(state, uid, "The draft is paused.")
    actives = state.active_managers
    if len(actives) != 1 or actives[0].user_id != uid:
        return _err(state, uid, "It's not your pick.")
    player = next((p for p in state.queue if p.id == ev.player_id), None)
    if player is None:
        return _err(state, uid, "That player isn't in the pool.")
    m = replace(_assign(actives[0], player, 0), last_action_lot=state.lot_seq)
    state2 = replace(
        _with_manager(state, m),
        queue=tuple(p for p in state.queue if p.id != ev.player_id),
        log=state.log + (LogEntry("pick", player, uid, 0),),
    )
    fx: list[Effect] = [PickedFx(player, uid), BoardFx()]
    if not m.full:
        deadline = ev.now + state.config.free_pick_seconds
        state3 = replace(state2, pick_deadline=deadline)
        return state3, fx + [ArmTimerFx("pick", -1, deadline)]
    if all(x.full for x in state2.managers):
        state3 = replace(state2, phase="complete", pick_deadline=0.0)
        return state3, fx + [CompleteFx()]
    return _auto_fill(state2, rng, fx)


def _pick_expired(
    state: DraftState, ev: TimerExpired, rng: random.Random
) -> Transition:
    if (
        state.phase != "free_pick"
        or state.paused
        or ev.deadline != state.pick_deadline
    ):
        return state, []
    fx: list[Effect] = []
    managers = []
    for m in state.managers:  # the idling picker flips to autopilot
        if not m.autopilot and not m.full and m.budget >= 1:
            m = replace(m, autopilot=True)
            fx.append(AutopilotFx(m.user_id))
        managers.append(m)
    return _auto_fill(replace(state, managers=tuple(managers)), rng, fx)


# ---------------------------------------------------------------- utilities


def _swap(state: DraftState, ev: Swap) -> Transition:
    m = state.manager(ev.user_id)
    if m is None:
        return _err(state, ev.user_id, "You're not in this draft.")
    if state.phase not in ("auction", "free_pick"):
        return _err(state, ev.user_id, "You can only swap during the draft.")
    slots = state.config.slots
    if ev.slot_a not in slots or ev.slot_b not in slots or ev.slot_a == ev.slot_b:
        return _err(state, ev.user_id, "Pick two different slots to swap.")
    ia = next(i for i, s in enumerate(m.spots) if s.slot == ev.slot_a)
    ib = next(i for i, s in enumerate(m.spots) if s.slot == ev.slot_b)
    sa, sb = m.spots[ia], m.spots[ib]
    new_spots = list(m.spots)
    new_spots[ia] = replace(sa, player=sb.player, price=sb.price)
    new_spots[ib] = replace(sb, player=sa.player, price=sa.price)
    m2 = replace(m, spots=tuple(new_spots), last_action_lot=state.lot_seq)
    return _with_manager(state, m2), [BoardFx()]


# ------------------------------------------------------------- commissioner


def _pause(state: DraftState, ev: Pause) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can pause.")
    if state.phase not in ("auction", "free_pick"):
        return _err(state, ev.user_id, "There's nothing to pause.")
    if state.paused:
        return _err(state, ev.user_id, "The draft is already paused.")
    base = (
        state.lot.deadline
        if state.phase == "auction" and state.lot is not None
        else state.pick_deadline
    )
    remaining = max(0.0, base - ev.now)
    state2 = replace(state, paused=True, pause_remaining=remaining)
    return state2, [CancelTimerFx(), PausedFx()]


def _resume(state: DraftState, ev: Resume) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can resume.")
    if not state.paused:
        return _err(state, ev.user_id, "The draft isn't paused.")
    deadline = ev.now + state.pause_remaining
    if state.phase == "auction" and state.lot is not None:
        lot = replace(state.lot, deadline=deadline)
        state2 = replace(state, paused=False, pause_remaining=0.0, lot=lot)
        return state2, [ResumedFx(lot), ArmTimerFx("lot", lot.seq, deadline)]
    state2 = replace(
        state, paused=False, pause_remaining=0.0, pick_deadline=deadline
    )
    return state2, [ResumedFx(None), ArmTimerFx("pick", -1, deadline)]


def _kick(state: DraftState, ev: Kick) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can kick.")
    target = state.manager(ev.target_id)
    if target is None:
        return _err(state, ev.user_id, "That user isn't a manager here.")
    if ev.replacement_id is not None:
        if state.manager(ev.replacement_id) is not None:
            return _err(state, ev.user_id, "The replacement is already a manager.")
        # Replacement inherits roster and budget verbatim (rule #16). During
        # free_pick a replacement for an autopilot team must stay on
        # autopilot — waking it would create a second active manager and
        # brick the sole survivor's pick (rule #13).
        keep_autopilot = (
            state.phase == "free_pick"
            and target.autopilot
            and not target.full
            and target.budget >= 1
        )
        m2 = replace(
            target,
            user_id=ev.replacement_id,
            name=ev.replacement_name or target.name,
            autopilot=keep_autopilot,
            last_action_lot=state.lot_seq,
        )
        managers = tuple(
            m2 if x.user_id == ev.target_id else x for x in state.managers
        )
        state2 = replace(state, managers=managers)
        if state2.lot is not None and state2.lot.leader_id == ev.target_id:
            # A standing bid stands — it now belongs to the replacement.
            state2 = replace(
                state2, lot=replace(state2.lot, leader_id=ev.replacement_id)
            )
        return state2, [BoardFx()]
    m2 = replace(target, autopilot=True)
    state2 = _with_manager(state, m2)
    return state2, [AutopilotFx(ev.target_id), BoardFx()]


def _cancel(state: DraftState, ev: Cancel) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can cancel.")
    state2 = replace(state, phase="cancelled", paused=False)
    return state2, [CancelTimerFx(), CancelledFx()]
