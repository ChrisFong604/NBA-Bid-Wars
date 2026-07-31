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
    SNAKE_BUDGET,
    AddCpu,
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
    LineupPhaseFx,
    LobbyFx,
    LogEntry,
    Lot,
    LotOpened,
    Lottery,
    LotteryCancelledFx,
    LotteryGuess,
    LotteryGuessedFx,
    LotteryJoinedFx,
    LotteryOpenedFx,
    LotteryRevealFx,
    Manager,
    PassedFx,
    Pause,
    PausedFx,
    Pick,
    PickedFx,
    Player,
    RemoveCpu,
    Resume,
    ResumedFx,
    SnakeTurnFx,
    SoldFx,
    Spot,
    Start,
    Swap,
    TimerExpired,
    snake_price,
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
        if event.kind == "snake":
            return _snake_expired(state, event)
        if event.kind == "pick":
            return _pick_expired(state, event, r)
        if event.kind == "lineup":
            return _lineup_expired(state, event)
        return state, []
    if isinstance(event, LotteryGuess):
        return _lottery_guess(state, event)
    if isinstance(event, Pick):
        if state.phase == "snake":
            return _snake_pick(state, event)
        return _pick(state, event, r)
    if isinstance(event, Swap):
        return _swap(state, event)
    if isinstance(event, Pause):
        return _pause(state, event)
    if isinstance(event, Resume):
        return _resume(state, event)
    if isinstance(event, Kick):
        return _kick(state, event)
    if isinstance(event, AddCpu):
        return _add_cpu(state, event)
    if isinstance(event, RemoveCpu):
        return _remove_cpu(state, event)
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
    pre-crash button clicks are stale by construction).

    Note: this silently drops any mid-flight showdown lottery (the fresh lot
    has ``lottery=None``) — acceptable v1."""
    return _deal_lot(replace(state, lot=None), now)


# ---------------------------------------------------------------- internals


def _err(state: DraftState, user_id: int, message: str) -> Transition:
    return state, [ErrorFx(user_id, message)]


def _with_manager(state: DraftState, m: Manager) -> DraftState:
    managers = tuple(m if x.user_id == m.user_id else x for x in state.managers)
    return replace(state, managers=managers)


def _finish(state: DraftState, now: float, fx: list[Effect]) -> Transition:
    """All rosters just filled. Open the arrange-your-lineup window (rule:
    swaps only, then the draft locks), or complete instantly when
    ``lineup_seconds`` is 0."""
    if state.config.lineup_seconds > 0:
        deadline = now + state.config.lineup_seconds
        state2 = replace(
            state, phase="lineup", pick_deadline=0.0, lineup_deadline=deadline
        )
        return state2, fx + [
            LineupPhaseFx(deadline),
            ArmTimerFx("lineup", -1, deadline),
        ]
    state2 = replace(state, phase="complete", pick_deadline=0.0)
    return state2, fx + [CompleteFx()]


def _assign(m: Manager, player: Player, price: int) -> Manager:
    """Place ``player`` into the first empty spot and charge ``price``."""
    idx = next(i for i, s in enumerate(m.spots) if s.player is None)
    spot = replace(m.spots[idx], player=player, price=price)
    spots = m.spots[:idx] + (spot,) + m.spots[idx + 1 :]
    return replace(m, spots=spots, budget=m.budget - price)


# ---------------------------------------------------------------- lobby


def _join(state: DraftState, ev: Join) -> Transition:
    existing = state.manager(ev.user_id)
    if (
        state.phase in ("auction", "snake", "free_pick")
        and existing
        and existing.autopilot
    ):
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
    if state.phase in ("auction", "snake", "free_pick"):
        if m.autopilot:
            return _err(state, ev.user_id, "You've already left this draft.")
        # Takes effect at the next lot resolution; a standing bid still pays
        # (and a showdown entry stays in — their team pays if it wins).
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
    if state.config.mode == "snake":
        # Snake ignores config.budget: everyone gets the fixed $15 stack —
        # spent evenly it buys exactly one player from every $1-$5 tier. The
        # queue IS the open pool here (redaction is a view concern).
        managers = tuple(replace(m, budget=SNAKE_BUDGET) for m in managers)
        state2 = replace(state, phase="snake", managers=managers, queue=pool)
        return _snake_advance(state2, ev.now, [BoardFx()])
    # "blind" runs the auction flow untouched — hiding names is a view concern.
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
        # Flat clock (rule #5): armed once per lot; only a bid inside the
        # snipe window extends it (soft close, _bid).
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
    if lot.leader_id == uid and lot.lottery is None:
        # During a showdown the leader MAY bid: raising cancels the lottery.
        return _err(state, uid, "You're already the high bidder.")
    if ev.amount is not None:
        effective = ev.amount
    else:
        effective = lot.current_bid + (ev.increment or 0)
    # ALL-IN SHOWDOWN: a bid landing exactly on the live amount, from a
    # manager whose entire budget IS that amount, joins (or opens) a guess
    # lottery instead of the usual "must beat" rejection. The leader needn't
    # be all-in themselves — they ride the showdown as a participant, or
    # raise their own bid to cancel it.
    if lot.current_bid >= 1 and effective == m.budget == lot.current_bid:
        if lot.lottery is not None:
            if uid in lot.lottery.participants:
                return _err(state, uid, "You're already in the showdown.")
            new_lot = replace(
                lot,
                lottery=replace(
                    lot.lottery,
                    participants=lot.lottery.participants + (uid,),
                ),
            )
            state2 = _with_manager(
                replace(state, lot=new_lot), replace(m, last_action_lot=lot.seq)
            )
            # Deadline UNCHANGED: joiners ride the running countdown.
            return state2, [LotteryJoinedFx(new_lot, uid)]
        leader = state.manager(lot.leader_id) if lot.leader_id is not None else None
        if leader is not None:
            deadline = ev.now + state.config.lottery_seconds
            new_lot = replace(
                lot,
                deadline=deadline,
                lottery=Lottery(participants=(leader.user_id, uid)),
            )
            state2 = _with_manager(
                replace(state, lot=new_lot), replace(m, last_action_lot=lot.seq)
            )
            return state2, [
                LotteryOpenedFx(new_lot),
                ArmTimerFx("lot", lot.seq, deadline),
            ]
        # No leader (current_bid >= 1 makes this unreachable) — fall through.
    if effective < 1 or effective <= lot.current_bid:
        return _err(state, uid, f"Bid must beat the current ${lot.current_bid}.")
    if effective > m.budget:
        return _err(state, uid, f"You've only got ${m.budget} left.")
    if lot.lottery is not None:
        # A richer bid cancels the showdown: normal leader/price update plus
        # a fresh snipe_window of bidding (ordinary soft-close afterwards).
        deadline = ev.now + state.config.snipe_window
        new_lot = replace(
            lot,
            current_bid=effective,
            leader_id=uid,
            deadline=deadline,
            lottery=None,
        )
        state2 = _with_manager(
            replace(state, lot=new_lot), replace(m, last_action_lot=lot.seq)
        )
        return state2, [
            LotteryCancelledFx(uid),
            BidPlaced(new_lot),
            ArmTimerFx("lot", lot.seq, deadline),
        ]
    # Soft close: early bids never move the deadline, but a bid inside the
    # snipe window pushes it out so nobody wins on a last-half-second snipe.
    deadline = lot.deadline
    if deadline - ev.now <= state.config.snipe_window:
        deadline += state.config.snipe_extend
    new_lot = replace(lot, current_bid=effective, leader_id=uid, deadline=deadline)
    state2 = _with_manager(
        replace(state, lot=new_lot), replace(m, last_action_lot=lot.seq)
    )
    fx: list[Effect] = [BidPlaced(new_lot)]
    if deadline != lot.deadline:
        fx.append(ArmTimerFx("lot", lot.seq, deadline))
    return state2, fx


def _lottery_guess(state: DraftState, ev: LotteryGuess) -> Transition:
    uid = ev.user_id
    if state.paused:
        return _err(state, uid, "The draft is paused.")
    lot = state.lot
    if state.phase != "auction" or lot is None or lot.lottery is None:
        return _err(state, uid, "There's no showdown running right now.")
    if ev.lot_seq != lot.seq or ev.now > lot.deadline:
        return _err(state, uid, "That showdown already closed.")
    if uid not in lot.lottery.participants:
        return _err(state, uid, "You're not in this showdown.")
    if not 1 <= ev.guess <= 100:
        return _err(state, uid, "Pick a number from 1 to 100.")
    # Resubmission overwrites; the value stays private until the reveal.
    guesses = tuple(g for g in lot.lottery.guesses if g[0] != uid)
    guesses += ((uid, ev.guess),)
    new_lot = replace(lot, lottery=replace(lot.lottery, guesses=guesses))
    return replace(state, lot=new_lot), [LotteryGuessedFx(uid)]


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
    if lot.lottery is not None:  # ALL-IN SHOWDOWN — closest guess buys it
        guessed = dict(lot.lottery.guesses)
        filled = tuple(  # nobody forfeits to a fumbled UI: missing -> random
            (p, guessed[p] if p in guessed else rng.randint(1, 100))
            for p in lot.lottery.participants
        )
        mystery = rng.randint(1, 100)
        best = min(abs(g - mystery) for _, g in filled)
        winner_id = rng.choice([p for p, g in filled if abs(g - mystery) == best])
        winner = state.manager(winner_id)
        assert winner is not None
        entry = LogEntry("sold", lot.player, winner_id, lot.current_bid)
        state2 = replace(
            _with_manager(state, _assign(winner, lot.player, lot.current_bid)),
            lot=None,
            log=state.log + (entry,),
        )
        reveal_fx: list[Effect] = [
            LotteryRevealFx(mystery, filled, winner_id),
            SoldFx(lot.player, winner_id, lot.current_bid),
            BoardFx(),
        ]
        return _resolve_next(state2, ev.now, rng, reveal_fx)
    if lot.current_bid > 0:  # SOLD
        assert lot.leader_id is not None
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
    # (b) everyone full -> lineup window (or instant complete).
    if all(m.full for m in state.managers):
        return _finish(state, now, fx)
    # (c) phase by active count (rules #12-#14).
    actives = state.active_managers
    if len(actives) >= 2:
        state2, fx2 = _deal_lot(state, now)
        return state2, fx + fx2
    if len(actives) == 1:
        deadline = now + state.config.free_pick_seconds
        state2 = replace(state, phase="free_pick", pick_deadline=deadline)
        pick_fx: list[Effect] = [
            FreePickFx(actives[0].user_id, state.queue, deadline),
            ArmTimerFx("pick", -1, deadline),
        ]
        return state2, fx + pick_fx
    return _auto_fill(state, now, rng, fx)


def _auto_fill(
    state: DraftState, now: float, rng: random.Random, fx: list[Effect]
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
        managers=tuple(by_id[m.user_id] for m in state.managers),
        queue=tuple(queue),
        log=tuple(log),
    )
    return _finish(state2, now, fx + [AutoFilledFx(tuple(assignments)), BoardFx()])


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
        return _finish(state2, ev.now, fx)
    return _auto_fill(state2, ev.now, rng, fx)


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
    return _auto_fill(replace(state, managers=tuple(managers)), ev.now, rng, fx)


# -------------------------------------------------------------------- snake


def _snake_on_turn(state: DraftState) -> Manager:
    """Turn order: managers tuple order, snaking back on odd rounds."""
    n = len(state.managers)
    picks_made = sum(
        1 for m in state.managers for s in m.spots if s.player is not None
    )
    r, i = divmod(picks_made, n)
    return state.managers[i] if r % 2 == 0 else state.managers[n - 1 - i]


def _snake_value(p: Player) -> tuple[int, float]:
    """Autopick ranking: stars first, then combined prime stat line."""
    return (p.stars, p.ppg + p.rpg + p.apg)


def _snake_feasible(m: Manager, p: Player) -> bool:
    """Affordable AND leaves $1 for every other still-empty slot."""
    price = snake_price(p)
    return price <= m.budget and m.budget - price >= m.empty_slots - 1


def _snake_resolve(state: DraftState, m: Manager) -> Transition:
    """Resolve ``m``'s turn without them: best feasible pick, else a forced
    bargain (cheapest tier, charged only what's left) — never a deadlock."""
    feasible = [p for p in state.queue if _snake_feasible(m, p)]
    if feasible:
        player = max(feasible, key=_snake_value)
        price = snake_price(player)
        kind = "pick"
        fx: list[Effect] = [PickedFx(player, m.user_id), BoardFx()]
    else:
        cheapest = min(snake_price(p) for p in state.queue)
        bargains = [p for p in state.queue if snake_price(p) == cheapest]
        player = max(bargains, key=_snake_value)
        price = min(cheapest, m.budget)  # a stub budget pays what it can
        kind = "force"
        fx = [ForceAssignedFx(player, m.user_id), BoardFx()]
    state2 = replace(
        _with_manager(state, _assign(m, player, price)),
        queue=tuple(p for p in state.queue if p.id != player.id),
        log=state.log + (LogEntry(kind, player, m.user_id, price),),
    )
    return state2, fx


def _snake_advance(
    state: DraftState, now: float, fx: list[Effect]
) -> Transition:
    """After every resolved pick: burn through managers who can't (or won't)
    choose — autopilot teams and teams with no feasible pick resolve on the
    spot — then arm the next live manager's clock, or finish on empty pool."""
    while state.queue:
        m = _snake_on_turn(state)
        if not m.autopilot and any(_snake_feasible(m, p) for p in state.queue):
            deadline = now + state.config.lot_seconds
            state2 = replace(state, pick_deadline=deadline)
            return state2, fx + [
                SnakeTurnFx(m.user_id, deadline),
                ArmTimerFx("snake", -1, deadline),
            ]
        state, more = _snake_resolve(state, m)
        fx = fx + more
    return _finish(state, now, fx)


def _snake_pick(state: DraftState, ev: Pick) -> Transition:
    uid = ev.user_id
    if state.paused:
        return _err(state, uid, "The draft is paused.")
    m = _snake_on_turn(state)
    if m.user_id != uid:
        return _err(state, uid, "It's not your pick.")
    if m.autopilot:  # left/kicked while on the clock — mirror _bid's gate
        return _err(state, uid, "You're on autopilot — reclaim your team to pick.")
    player = next((p for p in state.queue if p.id == ev.player_id), None)
    if player is None:
        return _err(state, uid, "That player isn't in the pool.")
    price = snake_price(player)
    if price > m.budget:
        return _err(
            state, uid, f"That's a ${price} player — you've only got ${m.budget}."
        )
    reserve = m.empty_slots - 1
    if m.budget - price < reserve:
        return _err(
            state,
            uid,
            f"${price} would leave you ${m.budget - price}, but you need to "
            f"keep $1 for each of your {reserve} other empty slot(s).",
        )
    m2 = replace(_assign(m, player, price), last_action_lot=state.lot_seq)
    state2 = replace(
        _with_manager(state, m2),
        queue=tuple(p for p in state.queue if p.id != ev.player_id),
        log=state.log + (LogEntry("pick", player, uid, price),),
    )
    return _snake_advance(state2, ev.now, [PickedFx(player, uid), BoardFx()])


def _snake_expired(state: DraftState, ev: TimerExpired) -> Transition:
    # Stale guard, same shape as "pick": phase + deadline echo must match.
    if (
        state.phase != "snake"
        or state.paused
        or ev.deadline != state.pick_deadline
    ):
        return state, []
    # The idler keeps their team (no autopilot flip) — this turn autopicks.
    state2, fx = _snake_resolve(state, _snake_on_turn(state))
    return _snake_advance(state2, ev.now, fx)


# ------------------------------------------------------------------- lineup


def _lineup_expired(state: DraftState, ev: TimerExpired) -> Transition:
    # Stale guard, same shape as "pick": phase + deadline echo must match.
    if state.phase != "lineup" or ev.deadline != state.lineup_deadline:
        return state, []
    state2 = replace(state, phase="complete", lineup_deadline=0.0)
    # No BoardFx needed: the complete render reposts the final board itself.
    return state2, [CompleteFx()]


# ---------------------------------------------------------------- utilities


def _swap(state: DraftState, ev: Swap) -> Transition:
    m = state.manager(ev.user_id)
    if m is None:
        return _err(state, ev.user_id, "You're not in this draft.")
    if state.phase not in ("auction", "snake", "free_pick", "lineup"):
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
    if state.phase == "lineup":
        return _err(
            state, ev.user_id, "The draft is wrapping up — lineups lock shortly."
        )
    if state.phase not in ("auction", "snake", "free_pick"):
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
    if state.phase == "lineup":
        return _err(
            state, ev.user_id, "The draft is wrapping up — lineups lock shortly."
        )
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
    kind = "snake" if state.phase == "snake" else "pick"
    return state2, [ResumedFx(None), ArmTimerFx(kind, -1, deadline)]


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
        lot = state2.lot
        if (
            lot is not None
            and lot.lottery is not None
            and ev.target_id in lot.lottery.participants
        ):
            # A showdown entry stands too: remap it (participants + any
            # locked guess) or resolution would hit a ghost user_id.
            lottery = replace(
                lot.lottery,
                participants=tuple(
                    ev.replacement_id if p == ev.target_id else p
                    for p in lot.lottery.participants
                ),
                guesses=tuple(
                    (ev.replacement_id if u == ev.target_id else u, g)
                    for u, g in lot.lottery.guesses
                ),
            )
            state2 = replace(state2, lot=replace(lot, lottery=lottery))
        return state2, [BoardFx()]
    m2 = replace(target, autopilot=True)
    state2 = _with_manager(state, m2)
    return state2, [AutopilotFx(ev.target_id), BoardFx()]


def _add_cpu(state: DraftState, ev: AddCpu) -> Transition:
    """Seat computer opponents (rule #20): lobby-only, commissioner-only.
    CPU n gets user_id -n; numbering fills the lowest free slot."""
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can add CPUs.")
    if state.phase != "lobby":
        return _err(state, ev.user_id, "The draft has already started.")
    if ev.count < 1:
        return _err(state, ev.user_id, "Add at least one CPU.")
    room = state.config.max_managers - len(state.managers)
    if ev.count > room:
        return _err(
            state, ev.user_id,
            f"Not enough room — only {room} seat(s) left in the lobby.",
        )
    used = {-m.user_id for m in state.managers if m.user_id < 0}
    new: list[Manager] = []
    n = 1
    for _ in range(ev.count):
        while n in used:
            n += 1
        used.add(n)
        new.append(
            Manager(
                user_id=-n,
                name=f"CPU {n}",
                budget=state.config.budget,
                spots=tuple(Spot(slot=s) for s in state.config.slots),
                cpu=True,
            )
        )
    return replace(state, managers=state.managers + tuple(new)), [LobbyFx()]


def _remove_cpu(state: DraftState, ev: RemoveCpu) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can remove CPUs.")
    if state.phase != "lobby":
        return _err(state, ev.user_id, "The draft has already started.")
    target = state.manager(ev.cpu_id)
    if target is None or not target.cpu:
        return _err(state, ev.user_id, "That's not a CPU manager.")
    managers = tuple(m for m in state.managers if m.user_id != ev.cpu_id)
    return replace(state, managers=managers), [LobbyFx()]


def _cancel(state: DraftState, ev: Cancel) -> Transition:
    if ev.user_id != state.commissioner_id:
        return _err(state, ev.user_id, "Only the commissioner can cancel.")
    state2 = replace(state, phase="cancelled", paused=False)
    return state2, [CancelTimerFx(), CancelledFx()]
