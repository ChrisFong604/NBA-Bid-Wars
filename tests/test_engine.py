"""Unit tests for the pure draft engine (DESIGN.md rules #1-#18)."""
from __future__ import annotations

import random

import pytest

from draftbot import engine
from draftbot.models import (
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
    ErrorFx,
    ForceAssignedFx,
    FreePickFx,
    Join,
    Kick,
    Leave,
    LineupPhaseFx,
    LobbyFx,
    Lot,
    LotOpened,
    PassedFx,
    Pause,
    PausedFx,
    Pick,
    PickedFx,
    Player,
    Resume,
    ResumedFx,
    SoldFx,
    Start,
    Swap,
    TimerExpired,
)
from helpers import (
    auction_state,
    fx_of,
    make_manager,
    make_players,
    start_draft,
)

CFG = Config()


def px(pid: str = "x", pos: str = "PG") -> Player:
    return Player(
        id=pid, name=f"P {pid}", team="TST", pos=pos,
        ppg=20.0, rpg=5.0, apg=5.0, stars=4,
    )


# ------------------------------------------------------------- lobby / start


def test_join_adds_manager_with_budget_and_spots():
    state = DraftState(config=CFG, commissioner_id=1)
    state2, fx = engine.apply(state, Join(1, "Chris"))
    assert fx == [LobbyFx()]
    m = state2.manager(1)
    assert m is not None
    assert m.budget == CFG.budget
    assert tuple(s.slot for s in m.spots) == CFG.slots
    assert all(s.player is None for s in m.spots)
    assert m.last_action_lot == 0 and not m.autopilot


def test_join_duplicate_rejected():
    state = DraftState(config=CFG, commissioner_id=1)
    state, _ = engine.apply(state, Join(1, "Chris"))
    state2, fx = engine.apply(state, Join(1, "Chris"))
    assert state2 is state
    assert isinstance(fx[0], ErrorFx) and fx[0].user_id == 1


def test_join_lobby_full_rejected():
    cfg = Config(max_managers=2)
    state = DraftState(config=cfg, commissioner_id=1)
    state, _ = engine.apply(state, Join(1, "A"))
    state, _ = engine.apply(state, Join(2, "B"))
    state2, fx = engine.apply(state, Join(3, "C"))
    assert state2 is state and isinstance(fx[0], ErrorFx)


def test_join_after_start_rejected_for_new_user():
    state, _, _ = start_draft(2)
    state2, fx = engine.apply(state, Join(9, "Late"))
    assert state2 is state and isinstance(fx[0], ErrorFx)


def test_join_reclaims_autopilot_team_mid_draft():
    state, _, _ = start_draft(2)
    state, _ = engine.apply(state, Leave(2))
    assert state.manager(2).autopilot
    state2, fx = engine.apply(state, Join(2, "M2"))
    m = state2.manager(2)
    assert not m.autopilot
    assert m.last_action_lot == state2.lot_seq  # not instantly AFK-swept again
    assert fx == [BoardFx()]


def test_leave_lobby_removes_manager():
    state = DraftState(config=CFG, commissioner_id=1)
    state, _ = engine.apply(state, Join(1, "A"))
    state, _ = engine.apply(state, Join(2, "B"))
    state2, fx = engine.apply(state, Leave(2))
    assert state2.manager(2) is None
    assert fx == [LobbyFx()]


def test_leave_mid_auction_flips_autopilot_and_standing_bid_pays():
    state, _, rng = start_draft(2)
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=7), rng)
    state, fx = engine.apply(state, Leave(2), rng)
    assert fx_of(fx, AutopilotFx) and fx_of(fx, BoardFx)
    # Mid-lot leave doesn't touch the lot; the bid stands.
    assert state.lot.leader_id == 2 and state.lot.current_bid == 7
    state2, fx2 = engine.apply(
        state, TimerExpired("lot", 1, state.lot.deadline, state.lot.deadline), rng
    )
    sold = fx_of(fx2, SoldFx)[0]
    assert sold.manager_id == 2 and sold.price == 7
    assert state2.manager(2).budget == CFG.budget - 7


def test_start_requires_commissioner_and_min_managers():
    state = DraftState(config=CFG, commissioner_id=1)
    state, _ = engine.apply(state, Join(1, "A"))
    _, fx = engine.apply(state, Start(2, make_players(), 0.0), random.Random(0))
    assert isinstance(fx[0], ErrorFx)
    _, fx = engine.apply(state, Start(1, make_players(), 0.0), random.Random(0))
    assert isinstance(fx[0], ErrorFx)  # 1 < min_managers


def test_start_deals_first_lot():
    state, fx, _ = start_draft(3)
    assert state.phase == "auction"
    assert state.lot is not None and state.lot.seq == 1
    pool_size = 5 * 3  # exactly 5N, zero leftovers
    assert len(state.queue) == pool_size - 1
    opened = fx_of(fx, LotOpened)[0]
    assert opened.pool_left == pool_size
    timer = fx_of(fx, ArmTimerFx)[0]
    assert timer.kind == "lot" and timer.lot_seq == 1
    assert timer.deadline == 1000.0 + CFG.lot_seconds
    assert state.lot.current_bid == 0 and state.lot.leader_id is None
    assert not state.lot.last_call


# --------------------------------------------------------------- build_pool


def test_build_pool_short_position_fills_from_other_positions():
    """Stratified best-effort: a short position bucket no longer raises —
    the shortfall comes from other positions (any player fits any slot)."""
    players = [p for p in make_players(3) if p.pos != "C"] + [px("c1", "C")]
    pool = engine.build_pool(players, 2, CFG, random.Random(0))
    assert len(pool) == 5 * 2
    assert len({p.id for p in pool}) == len(pool)
    assert sum(1 for p in pool if p.pos == "C") == 1  # only C available


def test_build_pool_raises_when_era_pool_smaller_than_5n():
    players = make_players(3)[:9]  # 9 < 5 * 2
    with pytest.raises(ValueError):
        engine.build_pool(players, 2, CFG, random.Random(0))


def test_build_pool_composition():
    rng = random.Random(5)
    pool = engine.build_pool(make_players(10), 4, CFG, rng)
    assert len(pool) == 5 * 4  # exactly 5N, zero leftovers
    assert len({p.id for p in pool}) == len(pool)
    for pos in CFG.slots:  # deep buckets -> exactly N per position
        assert sum(1 for p in pool if p.pos == pos) == 4


# ------------------------------------------------------------------ bidding


def test_all_in_bid_legal_and_does_not_move_deadline():
    state, _, rng = start_draft(2)
    deadline0 = state.lot.deadline
    state2, fx = engine.apply(state, Bid(2, 1, 1001.0, amount=CFG.budget), rng)
    placed = fx_of(fx, BidPlaced)[0]
    assert placed.lot.current_bid == CFG.budget and placed.lot.leader_id == 2
    # Flat clock: the bid must NOT move the deadline, and no timer re-arms.
    assert placed.lot.deadline == deadline0
    assert state2.lot.deadline == deadline0
    assert fx_of(fx, ArmTimerFx) == []
    assert state2.manager(2).last_action_lot == 1


def test_soft_close_late_bid_extends_early_bid_does_not():
    """Early bids never move the clock; a bid inside snipe_window pushes the
    deadline out by snipe_extend and re-arms the timer (anti-sniping)."""
    state, _, rng = start_draft(2)
    deadline = state.lot.deadline
    assert deadline == 1000.0 + CFG.lot_seconds
    # Early bid (outside the window): deadline untouched, no re-arm.
    state, fx = engine.apply(state, Bid(2, 1, 1001.0, amount=2), rng)
    assert state.lot.deadline == deadline
    assert fx_of(fx, ArmTimerFx) == []
    # Last-half-second snipe: +snipe_extend, timer re-armed with the echo.
    state, fx = engine.apply(state, Bid(1, 1, deadline - 0.5, amount=3), rng)
    extended = deadline + CFG.snipe_extend
    assert state.lot.deadline == extended
    assert fx_of(fx, ArmTimerFx) == [ArmTimerFx("lot", 1, extended)]
    # The old deadline's timer fire is stale — ignored.
    state2, fx = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert state2 is state and fx == []
    # The extended deadline sells to the sniper's counter-bidder... i.e. leader.
    state2, fx = engine.apply(
        state, TimerExpired("lot", 1, extended, extended), rng
    )
    sold = fx_of(fx, SoldFx)[0]
    assert sold.manager_id == 1 and sold.price == 3
    assert state2.manager(1).budget == CFG.budget - 3


def test_bid_over_budget_rejected():
    state, _, rng = start_draft(2)
    state2, fx = engine.apply(state, Bid(2, 1, 1001.0, amount=CFG.budget + 1), rng)
    assert state2 is state
    assert fx == [ErrorFx(2, f"You've only got ${CFG.budget} left.")]


def test_self_raise_rejected():
    state, _, rng = start_draft(2)
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=5), rng)
    state2, fx = engine.apply(state, Bid(2, 1, 1002.0, amount=6), rng)
    assert state2 is state
    assert fx == [ErrorFx(2, "You're already the high bidder.")]


def test_stale_lot_seq_bid_rejected():
    state, _, rng = start_draft(2)
    state2, fx = engine.apply(state, Bid(2, 99, 1001.0, amount=5), rng)
    assert state2 is state
    assert fx == [ErrorFx(2, "That auction already closed.")]


def test_bid_after_deadline_rejected():
    state, _, rng = start_draft(2)
    late = state.lot.deadline + 0.5
    state2, fx = engine.apply(state, Bid(2, 1, late, amount=5), rng)
    assert state2 is state and isinstance(fx[0], ErrorFx)


def test_broke_and_full_and_outsider_cannot_bid():
    a = make_manager(1, CFG, budget=0)
    b = make_manager(2, CFG, filled=5)
    lot = Lot(seq=1, player=px(), last_call=False, deadline=1020.0)
    state = auction_state(CFG, (a, b), make_players(2)[:5], lot)
    _, fx = engine.apply(state, Bid(1, 1, 1001.0, amount=1))
    assert fx == [ErrorFx(1, "You've only got $0 left.")]
    _, fx = engine.apply(state, Bid(2, 1, 1001.0, amount=1))
    assert isinstance(fx[0], ErrorFx)  # roster full
    _, fx = engine.apply(state, Bid(9, 1, 1001.0, amount=1))
    assert isinstance(fx[0], ErrorFx)  # not a manager
    _, fx = engine.apply(state, Bid(1, 1, 1001.0, amount=0))
    assert isinstance(fx[0], ErrorFx)  # bids start at $1


def test_quick_increment_raises_live_bid():
    state, _, rng = start_draft(2)
    state, fx = engine.apply(state, Bid(2, 1, 1001.0, increment=1), rng)
    assert state.lot.current_bid == 1  # opens at the increment
    state, fx = engine.apply(state, Bid(1, 1, 1002.0, increment=5), rng)
    assert state.lot.current_bid == 6 and state.lot.leader_id == 1


# ----------------------------------------------------------- lot resolution


def test_stale_deadline_timer_ignored():
    state, _, rng = start_draft(2)
    ev = TimerExpired("lot", 1, state.lot.deadline - 1.0, 2000.0)
    state2, fx = engine.apply(state, ev, rng)
    assert state2 is state and fx == []
    ev = TimerExpired("lot", 99, state.lot.deadline, 2000.0)
    state2, fx = engine.apply(state, ev, rng)
    assert state2 is state and fx == []


def test_no_bid_at_flat_deadline_recycles_to_back():
    state, _, rng = start_draft(2)
    first = state.lot.player
    deadline = state.lot.deadline
    state2, fx = engine.apply(
        state, TimerExpired("lot", 1, deadline, deadline), rng
    )
    assert fx_of(fx, PassedFx) == [PassedFx(first)]
    assert state2.queue[-1] == first
    assert first.id in state2.passed_ids
    assert state2.log[-1].kind == "passed" and state2.log[-1].manager_id is None
    assert state2.lot.seq == 2  # next lot dealt immediately
    assert fx_of(fx, LotOpened)


def test_recycle_forever_never_marks_passed():
    cfg = Config(pass_rule="recycle_forever")
    state, _, rng = start_draft(2, cfg=cfg)
    deadline = state.lot.deadline
    state2, _ = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert state2.passed_ids == frozenset()


def test_last_call_force_assign_targets_active_with_most_empties():
    a = make_manager(1, CFG)                       # 5 empties, active
    b = make_manager(2, CFG, filled=2)             # 3 empties, active
    c = make_manager(3, CFG, autopilot=True)       # excluded: autopilot
    d = make_manager(4, CFG, budget=0)             # excluded: broke
    player = px("lastcall")
    lot = Lot(seq=5, player=player, last_call=True, deadline=1000.0)
    state = auction_state(
        CFG, (a, b, c, d), make_players(2)[:8], lot,
        passed_ids=frozenset({"lastcall"}),
    )
    state2, fx = engine.apply(
        state, TimerExpired("lot", 5, 1000.0, 1000.0), random.Random(0)
    )
    force = fx_of(fx, ForceAssignedFx)[0]
    assert force == ForceAssignedFx(player, 1)  # only max-empties active team
    m1 = state2.manager(1)
    assert m1.budget == CFG.budget - 1
    assert m1.spots[0].player == player and m1.spots[0].price == 1
    entry = next(e for e in state2.log if e.kind == "force")
    assert entry.manager_id == 1 and entry.price == 1
    assert state2.lot.seq == 6  # two actives remain -> next lot dealt


def test_sold_places_in_first_empty_spot_with_budget_math():
    state, _, rng = start_draft(3)
    player = state.lot.player
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=7), rng)
    deadline = state.lot.deadline
    state2, fx = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert fx[0] == SoldFx(player, 2, 7)
    assert isinstance(fx[1], BoardFx)
    m = state2.manager(2)
    assert m.budget == CFG.budget - 7
    assert m.spots[0].player == player and m.spots[0].price == 7
    assert state2.log[-1].kind == "sold" and state2.log[-1].price == 7
    assert state2.lot.seq == 2  # 3 actives -> auction continues


def test_one_active_manager_enters_free_pick():
    state, _, rng = start_draft(2)
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=3), rng)
    state, _ = engine.apply(state, Leave(1), rng)  # only manager 2 stays active
    deadline = state.lot.deadline
    state2, fx = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert state2.phase == "free_pick"
    free = fx_of(fx, FreePickFx)[0]
    assert free.manager_id == 2
    assert free.pool == state2.queue
    assert free.deadline == deadline + CFG.free_pick_seconds
    assert state2.pick_deadline == free.deadline
    assert ArmTimerFx("pick", -1, free.deadline) in fx


def test_zero_active_managers_auto_fills_then_lineup_then_complete():
    a = make_manager(1, CFG, filled=4, budget=5)       # buys last slot below
    b = make_manager(2, CFG, autopilot=True)           # 5 empties
    player = px("onblock")
    lot = Lot(
        seq=3, player=player, last_call=False,
        current_bid=5, leader_id=1, deadline=2000.0,
    )
    state = auction_state(CFG, (a, b), make_players(2)[:8], lot)
    state2, fx = engine.apply(
        state, TimerExpired("lot", 3, 2000.0, 2000.0), random.Random(1)
    )
    assert state2.phase == "lineup"  # full rosters open the lineup window
    assert all(m.full for m in state2.managers)
    assert state2.manager(1).budget == 0
    filled = fx_of(fx, AutoFilledFx)[0]
    assert len(filled.assignments) == 5
    assert all(mid == 2 for mid, _ in filled.assignments)
    deadline = 2000.0 + CFG.lineup_seconds
    assert fx_of(fx, LineupPhaseFx) == [LineupPhaseFx(deadline)]
    assert ArmTimerFx("lineup", -1, deadline) in fx
    assert not fx_of(fx, CompleteFx)  # completion waits for the window
    assert state2.lineup_deadline == deadline
    assert len(state2.queue) == 8 - 5
    assert sum(1 for e in state2.log if e.kind == "autofill") == 5
    state3, fx3 = engine.apply(
        state2, TimerExpired("lineup", -1, deadline, deadline), random.Random(1)
    )
    assert state3.phase == "complete" and state3.lineup_deadline == 0.0
    assert fx3 == [CompleteFx()]


# ---------------------------------------------------------------- free pick


def free_pick_state(picker_filled: int = 3, other_filled: int = 4) -> DraftState:
    a = make_manager(1, CFG, filled=picker_filled)
    b = make_manager(2, CFG, autopilot=True, filled=other_filled)
    return DraftState(
        config=CFG,
        commissioner_id=1,
        phase="free_pick",
        managers=(a, b),
        queue=make_players(2)[:9],
        lot_seq=7,
        pick_deadline=5000.0,
    )


def test_pick_flow_valid_and_invalid():
    state = free_pick_state()
    _, fx = engine.apply(state, Pick(2, state.queue[0].id, 4000.0))
    assert isinstance(fx[0], ErrorFx)  # not the picker
    _, fx = engine.apply(state, Pick(1, "not-a-player", 4000.0))
    assert isinstance(fx[0], ErrorFx)
    target = state.queue[3]
    state2, fx = engine.apply(state, Pick(1, target.id, 4000.0))
    assert fx[0] == PickedFx(target, 1) and isinstance(fx[1], BoardFx)
    m = state2.manager(1)
    assert m.spots[3].player == target and m.spots[3].price == 0
    assert m.budget == CFG.budget  # free
    assert m.last_action_lot == 7
    assert target.id not in {p.id for p in state2.queue}
    assert state2.pick_deadline == 4000.0 + CFG.free_pick_seconds
    assert ArmTimerFx("pick", -1, state2.pick_deadline) in fx
    assert state2.log[-1].kind == "pick"


def test_pick_until_full_auto_fills_everyone_else():
    state = free_pick_state(picker_filled=4, other_filled=4)
    state2, fx = engine.apply(
        state, Pick(1, state.queue[0].id, 4000.0), random.Random(2)
    )
    assert state2.phase == "lineup"
    assert all(m.full for m in state2.managers)
    filled = fx_of(fx, AutoFilledFx)[0]
    assert [mid for mid, _ in filled.assignments] == [2]
    deadline = 4000.0 + CFG.lineup_seconds
    assert fx_of(fx, LineupPhaseFx) == [LineupPhaseFx(deadline)]
    assert ArmTimerFx("lineup", -1, deadline) in fx
    assert not fx_of(fx, CompleteFx)
    assert state2.pick_deadline == 0.0
    state3, fx3 = engine.apply(
        state2, TimerExpired("lineup", -1, deadline, deadline)
    )
    assert state3.phase == "complete" and fx3 == [CompleteFx()]


def test_free_pick_reclaim_that_would_add_second_active_rejected():
    """Rules #13/#16: Join-reclaim during free_pick must not create a second
    active manager — that bricked the sole survivor's pick (regression)."""
    state = free_pick_state()  # manager 2: autopilot, empty slot, full budget
    state2, fx = engine.apply(state, Join(2, "M2"))
    assert state2 is state
    assert isinstance(fx[0], ErrorFx) and fx[0].user_id == 2
    assert state2.manager(2).autopilot
    # The sole survivor's pick still works after the rejected reclaim.
    target = state.queue[0]
    state3, fx3 = engine.apply(state2, Pick(1, target.id, 4000.0))
    assert fx3[0] == PickedFx(target, 1)
    assert len(state3.active_managers) == 1


def test_free_pick_reclaim_of_full_or_broke_team_still_allowed():
    # Full team: reclaim clears autopilot but can't add an active manager.
    state = free_pick_state(other_filled=5)
    state2, fx = engine.apply(state, Join(2, "M2"))
    assert not state2.manager(2).autopilot
    assert fx == [BoardFx()]
    assert len(state2.active_managers) == 1  # still just the picker
    # Broke team ($0, empty slots): reclaim can't make it active either.
    a = make_manager(1, CFG, filled=3)
    b = make_manager(2, CFG, autopilot=True, filled=4, budget=0)
    state = DraftState(
        config=CFG, commissioner_id=1, phase="free_pick",
        managers=(a, b), queue=make_players(2)[:9], lot_seq=7,
        pick_deadline=5000.0,
    )
    state2, fx = engine.apply(state, Join(2, "M2"))
    assert not state2.manager(2).autopilot
    assert len(state2.active_managers) == 1


def test_free_pick_kick_replacement_stays_autopilot():
    """Rule #16 replacement during free_pick inherits the team but must not
    wake it — same second-active brick as Join-reclaim (regression)."""
    state = free_pick_state()
    state2, fx = engine.apply(
        state,
        Kick(1, target_id=2, now=4000.0, replacement_id=99,
             replacement_name="Sub"),
    )
    sub = state2.manager(99)
    assert sub is not None and sub.autopilot
    assert sub.budget == CFG.budget  # roster and budget inherited verbatim
    assert len(state2.active_managers) == 1
    assert state2.active_managers[0].user_id == 1
    # The picker is undisturbed.
    target = state2.queue[0]
    _, fx3 = engine.apply(state2, Pick(1, target.id, 4000.0))
    assert fx3[0] == PickedFx(target, 1)


def test_pick_timeout_flips_picker_and_auto_fills():
    state = free_pick_state()
    # Stale deadline -> ignored.
    state2, fx = engine.apply(state, TimerExpired("pick", -1, 4999.0, 6000.0))
    assert state2 is state and fx == []
    state2, fx = engine.apply(
        state, TimerExpired("pick", -1, 5000.0, 6000.0), random.Random(3)
    )
    assert AutopilotFx(1) in fx
    assert state2.phase == "lineup"
    assert all(m.full for m in state2.managers)
    assert state2.manager(1).autopilot
    filled = fx_of(fx, AutoFilledFx)[0]
    assert len(filled.assignments) == 2 + 1  # picker's 2 empties + other's 1
    assert fx_of(fx, LineupPhaseFx) == [LineupPhaseFx(6000.0 + CFG.lineup_seconds)]
    state3, _ = engine.apply(
        state2,
        TimerExpired("lineup", -1, state2.lineup_deadline, state2.lineup_deadline),
    )
    assert state3.phase == "complete"


# ------------------------------------------------------------------- lineup


def lineup_state() -> DraftState:
    a = make_manager(1, CFG, filled=5)
    b = make_manager(2, CFG, filled=5)
    return DraftState(
        config=CFG, commissioner_id=1, phase="lineup",
        managers=(a, b), lot_seq=9, lineup_deadline=5000.0,
    )


def test_last_sale_enters_lineup_phase():
    a = make_manager(1, CFG, filled=4)
    b = make_manager(2, CFG, filled=5)
    player = px("last")
    lot = Lot(
        seq=9, player=player, last_call=False,
        current_bid=3, leader_id=1, deadline=2000.0,
    )
    state = auction_state(CFG, (a, b), (), lot)
    state2, fx = engine.apply(state, TimerExpired("lot", 9, 2000.0, 2000.0))
    assert state2.phase == "lineup"
    deadline = 2000.0 + CFG.lineup_seconds
    assert state2.lineup_deadline == deadline
    assert fx == [
        SoldFx(player, 1, 3), BoardFx(),
        LineupPhaseFx(deadline), ArmTimerFx("lineup", -1, deadline),
    ]


def test_final_pick_enters_lineup_phase():
    state = free_pick_state(picker_filled=4, other_filled=5)
    target = state.queue[0]
    state2, fx = engine.apply(state, Pick(1, target.id, 4000.0))
    assert state2.phase == "lineup"
    assert state2.pick_deadline == 0.0
    deadline = 4000.0 + CFG.lineup_seconds
    assert state2.lineup_deadline == deadline
    assert fx == [
        PickedFx(target, 1), BoardFx(),
        LineupPhaseFx(deadline), ArmTimerFx("lineup", -1, deadline),
    ]


def test_lineup_seconds_zero_skips_the_window():
    cfg = Config(lineup_seconds=0)
    a = make_manager(1, cfg, filled=4)
    b = make_manager(2, cfg, filled=5)
    player = px("last")
    lot = Lot(
        seq=9, player=player, last_call=False,
        current_bid=3, leader_id=1, deadline=2000.0,
    )
    state = auction_state(cfg, (a, b), (), lot)
    state2, fx = engine.apply(state, TimerExpired("lot", 9, 2000.0, 2000.0))
    assert state2.phase == "complete" and state2.lineup_deadline == 0.0
    assert fx == [SoldFx(player, 1, 3), BoardFx(), CompleteFx()]


def test_lineup_timer_fires_completes_draft():
    state = lineup_state()
    state2, fx = engine.apply(state, TimerExpired("lineup", -1, 5000.0, 5000.0))
    assert state2.phase == "complete" and state2.lineup_deadline == 0.0
    assert fx == [CompleteFx()]


def test_stale_or_post_complete_lineup_timer_ignored():
    state = lineup_state()
    state2, fx = engine.apply(state, TimerExpired("lineup", -1, 4999.0, 6000.0))
    assert state2 is state and fx == []  # deadline mismatch
    done, _ = engine.apply(state, TimerExpired("lineup", -1, 5000.0, 5000.0))
    done2, fx2 = engine.apply(done, TimerExpired("lineup", -1, 5000.0, 5001.0))
    assert done2 is done and fx2 == []  # phase already complete


def test_swap_works_during_lineup_and_preserves_players():
    state = lineup_state()
    before = sorted(s.player.id for s in state.manager(1).spots)
    state2, fx = engine.apply(state, Swap(1, "PG", "C"))
    assert fx == [BoardFx()]
    m = state2.manager(1)
    assert sorted(s.player.id for s in m.spots) == before  # same multiset
    assert m.spots[0].player.id == "own-1-4"
    assert m.spots[4].player.id == "own-1-0"


def test_bid_and_pick_rejected_during_lineup():
    state = lineup_state()
    _, fx = engine.apply(state, Bid(1, 9, 4500.0, amount=5))
    assert fx == [ErrorFx(1, "There's no auction running right now.")]
    _, fx = engine.apply(state, Pick(1, "own-2-0", 4500.0))
    assert fx == [ErrorFx(1, "There's no free-pick phase running.")]


def test_pause_and_resume_rejected_during_lineup():
    state = lineup_state()
    msg = "The draft is wrapping up — lineups lock shortly."
    state2, fx = engine.apply(state, Pause(1, 4500.0))
    assert state2 is state and fx == [ErrorFx(1, msg)]
    state2, fx = engine.apply(state, Resume(1, 4500.0))
    assert state2 is state and fx == [ErrorFx(1, msg)]


def test_cancel_during_lineup_cancels_everything():
    state = lineup_state()
    state2, fx = engine.apply(state, Cancel(1))
    assert state2.phase == "cancelled"
    assert fx == [CancelTimerFx(), CancelledFx()]


def test_kick_during_lineup_documented_behavior():
    # Kick keeps working in the lineup window: without a replacement it flags
    # the (already full) team autopilot — harmless, nothing left to bid on —
    # and the phase/deadline are untouched.
    state = lineup_state()
    state2, fx = engine.apply(state, Kick(1, target_id=2, now=4500.0))
    assert state2.phase == "lineup"
    assert state2.lineup_deadline == 5000.0
    assert state2.manager(2).autopilot
    assert AutopilotFx(2) in fx and fx_of(fx, BoardFx)


# --------------------------------------------------------------------- swap


def test_swap_swaps_payload_keeps_slot_labels():
    state, _, rng = start_draft(2)
    player = state.lot.player
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=4), rng)
    deadline = state.lot.deadline
    state, _ = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert state.manager(2).spots[0].player == player  # in PG
    state2, fx = engine.apply(state, Swap(2, "PG", "C"), rng)
    assert fx == [BoardFx()]
    m = state2.manager(2)
    assert tuple(s.slot for s in m.spots) == CFG.slots  # labels stay put
    assert m.spots[0].player is None and m.spots[0].price == 0
    assert m.spots[4].player == player and m.spots[4].price == 4
    assert m.last_action_lot == state2.lot_seq


def test_swap_invalid_slots_rejected():
    state, _, rng = start_draft(2)
    _, fx = engine.apply(state, Swap(2, "PG", "PG"), rng)
    assert isinstance(fx[0], ErrorFx)
    _, fx = engine.apply(state, Swap(2, "PG", "XX"), rng)
    assert isinstance(fx[0], ErrorFx)
    _, fx = engine.apply(state, Swap(9, "PG", "SG"), rng)
    assert isinstance(fx[0], ErrorFx)  # not a manager


# ------------------------------------------------------------ pause / resume


def test_pause_resume_deadline_math_auction():
    state, _, rng = start_draft(2)
    assert state.lot.deadline == 1000.0 + CFG.lot_seconds
    state, fx = engine.apply(state, Pause(1, 1010.0), rng)
    assert state.paused and state.pause_remaining == CFG.lot_seconds - 10.0
    assert fx == [CancelTimerFx(), PausedFx()]
    # Paused: bids rejected, timers ignored.
    _, fx = engine.apply(state, Bid(2, 1, 1011.0, amount=3), rng)
    assert isinstance(fx[0], ErrorFx)
    state2, fx = engine.apply(
        state,
        TimerExpired("lot", 1, 1000.0 + CFG.lot_seconds, 1000.0 + CFG.lot_seconds),
        rng,
    )
    assert state2 is state and fx == []
    state, fx = engine.apply(state, Resume(1, 2000.0), rng)
    assert not state.paused and state.pause_remaining == 0.0
    assert state.lot.deadline == 2000.0 + CFG.lot_seconds - 10.0  # pause/resume DOES shift the clock
    assert fx == [ResumedFx(state.lot), ArmTimerFx("lot", 1, 2000.0 + CFG.lot_seconds - 10.0)]


def test_pause_resume_deadline_math_free_pick():
    state = free_pick_state()  # pick_deadline 5000
    state, fx = engine.apply(state, Pause(1, 4970.0))
    assert state.pause_remaining == 30.0
    state, fx = engine.apply(state, Resume(1, 9000.0))
    assert state.pick_deadline == 9030.0
    assert fx == [ResumedFx(None), ArmTimerFx("pick", -1, 9030.0)]


# --------------------------------------------------------------------- kick


def test_kick_with_replacement_inherits_team_and_standing_bid():
    state, _, rng = start_draft(2)
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=6), rng)
    state2, fx = engine.apply(
        state, Kick(1, target_id=2, now=1002.0, replacement_id=99,
                    replacement_name="Sub"), rng
    )
    assert fx == [BoardFx()]
    assert state2.manager(2) is None
    sub = state2.manager(99)
    assert sub.name == "Sub" and not sub.autopilot
    assert sub.budget == CFG.budget and sub.spots == state.manager(2).spots
    assert state2.lot.leader_id == 99  # the bid stands, now theirs


def test_kick_without_replacement_flips_autopilot():
    state, _, rng = start_draft(2)
    state2, fx = engine.apply(state, Kick(1, target_id=2, now=1002.0), rng)
    assert state2.manager(2).autopilot
    assert AutopilotFx(2) in fx and fx_of(fx, BoardFx)
    _, fx = engine.apply(state, Kick(1, target_id=42, now=1002.0), rng)
    assert isinstance(fx[0], ErrorFx)  # not a manager


# ---------------------------------------------------------- admin / cancel


def test_admin_events_require_commissioner():
    state, _, rng = start_draft(2)
    events = [
        Start(2, make_players(), 1002.0),
        Pause(2, 1002.0),
        Resume(2, 1002.0),
        Kick(2, target_id=1, now=1002.0),
        Cancel(2),
    ]
    for ev in events:
        state2, fx = engine.apply(state, ev, rng)
        assert state2 is state
        assert len(fx) == 1 and isinstance(fx[0], ErrorFx)


def test_cancel_sets_phase_and_effects():
    state, _, rng = start_draft(2)
    state2, fx = engine.apply(state, Cancel(1), rng)
    assert state2.phase == "cancelled"
    assert fx == [CancelTimerFx(), CancelledFx()]


# ---------------------------------------------------------------- AFK sweep


def test_afk_sweep_flags_idle_managers_at_resolution():
    cfg = Config(afk_lots=2)
    a = make_manager(1, cfg, last_action_lot=2)  # acted on this lot
    b = make_manager(2, cfg)                     # idle since lot 0
    c = make_manager(3, cfg)                     # idle since lot 0
    lot = Lot(
        seq=2, player=px("onblock"), last_call=False,
        current_bid=3, leader_id=1, deadline=3000.0,
    )
    state = auction_state(cfg, (a, b, c), make_players(3)[:14], lot)
    state2, fx = engine.apply(
        state, TimerExpired("lot", 2, 3000.0, 3000.0), random.Random(4)
    )
    assert AutopilotFx(2) in fx and AutopilotFx(3) in fx
    assert state2.manager(2).autopilot and state2.manager(3).autopilot
    assert not state2.manager(1).autopilot
    assert state2.phase == "free_pick"  # only manager 1 left active
    assert fx_of(fx, FreePickFx)[0].manager_id == 1


# ------------------------------------------------------------------- redeal


def test_redeal_deals_same_player_as_fresh_lot():
    a = make_manager(1, CFG)
    b = make_manager(2, CFG)
    player = px("interrupted")
    others = make_players(2)[:4]
    lot = Lot(
        seq=3, player=player, last_call=False,
        current_bid=2, leader_id=2, deadline=1500.0,
    )
    # Crash recovery: the caller already pushed the player to the queue head.
    state = auction_state(CFG, (a, b), (player,) + others, lot)
    state2, fx = engine.redeal(state, 3000.0)
    assert state2.lot.seq == 4 and state2.lot_seq == 4
    assert state2.lot.player == player
    assert state2.lot.current_bid == 0 and state2.lot.leader_id is None
    assert state2.lot.deadline == 3000.0 + CFG.lot_seconds
    assert state2.queue == others
    opened = fx_of(fx, LotOpened)[0]
    assert opened.pool_left == 5
    assert ArmTimerFx("lot", 4, state2.lot.deadline) in fx
