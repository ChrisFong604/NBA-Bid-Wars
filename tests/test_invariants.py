"""Random-agent simulation harness (DESIGN.md §3 "Simulation test").

Plays 300+ full drafts across N=2..8 with random — often invalid — bids,
picks, leaves, rejoins, pauses, and timer fires, asserting the core
invariants after EVERY apply.
"""
from __future__ import annotations

import random

from draftbot import engine
from draftbot.models import (
    Bid,
    Config,
    DraftState,
    Join,
    Leave,
    LotteryGuess,
    Pause,
    Pick,
    Resume,
    Start,
    Swap,
    TimerExpired,
)
from helpers import make_players

PLAYERS = make_players(10)  # 10 per position covers N=2..8 (pool = 5N exactly)


def assert_invariants(state: DraftState) -> None:
    cfg = state.config
    seen: set[str] = set()
    for m in state.managers:
        assert m.budget >= 0, "budget overdrawn"
        assert len(m.spots) == len(cfg.slots)
        for s in m.spots:
            if s.player is not None:
                assert s.player.id not in seen, "player assigned twice"
                seen.add(s.player.id)
    for p in state.queue:
        assert p.id not in seen, "queued player also on a roster"
        seen.add(p.id)
    if state.lot is not None:
        assert state.lot.player.id not in seen, "lot player duplicated"
    if state.phase in ("auction", "free_pick"):
        empties = sum(m.empty_slots for m in state.managers)
        remaining = len(state.queue) + (1 if state.lot is not None else 0)
        assert remaining == empties, (
            "players remaining (queue + live lot + free-pick pool) != "
            "total empty slots across all managers"
        )
    if state.phase == "auction":
        assert state.lot is not None
    if state.lot is not None and state.lot.lottery is not None:
        lottery = state.lot.lottery
        assert state.phase == "auction", "showdown outside the auction"
        assert len(lottery.participants) >= 2, "showdown needs 2+ entrants"
        assert len(set(lottery.participants)) == len(lottery.participants)
        assert lottery.participants[0] == state.lot.leader_id
        for uid in lottery.participants:
            m = state.manager(uid)
            assert m is not None
            if uid == lottery.participants[0]:  # the dragged-in leader may
                assert m.budget >= state.lot.current_bid > 0  # hold spare cash
            else:  # every matcher is all-in at the tie
                assert m.budget == state.lot.current_bid > 0, (
                    "showdown matcher not all-in at the tied amount"
                )
        guess_ids = [u for u, _ in lottery.guesses]
        assert len(set(guess_ids)) == len(guess_ids), "duplicate guess entry"
        assert set(guess_ids) <= set(lottery.participants), "outsider guess"
        assert all(1 <= g <= 100 for _, g in lottery.guesses)
    if state.phase == "free_pick":
        assert len(state.active_managers) <= 1, "free_pick with 2+ actives"
    if state.phase == "lineup":
        assert all(m.full for m in state.managers), "lineup with empty slots"
        assert not state.queue and state.lot is None, (
            "leftover pool players in the lineup window"
        )
        assert state.lineup_deadline > 0.0
    if state.phase == "complete":
        assert all(m.full for m in state.managers)
        assert not state.queue and state.lot is None, (
            "leftover pool players at completion — pool must be exactly 5N"
        )


def play_draft(seed: int, n: int) -> bool:
    """Returns True when the draft entered at least one all-in showdown."""
    eng_rng = random.Random(seed)
    agent = random.Random(seed ^ 0xA5A5A5)
    cfg = Config()
    state = DraftState(config=cfg, commissioner_id=1)
    for uid in range(1, n + 1):
        state, _ = engine.apply(state, Join(uid, f"M{uid}"))
    now = 1_000.0
    saw_lottery = False

    def step(event) -> None:
        nonlocal state, saw_lottery
        state, _ = engine.apply(state, event, eng_rng)
        if state.lot is not None and state.lot.lottery is not None:
            saw_lottery = True
        assert_invariants(state)

    step(Start(1, PLAYERS, now))
    max_lots = 2 * (len(cfg.slots) * n)
    guard = 0
    while state.phase in ("auction", "free_pick", "lineup"):
        guard += 1
        assert guard < 5_000, "draft did not terminate"
        now += 1.0
        if state.phase == "auction":
            for _ in range(agent.randrange(4)):
                uid = agent.choice([*range(1, n + 1), 999])
                seq = state.lot.seq if agent.random() < 0.9 else state.lot.seq - 1
                t = state.lot.deadline + 1.0 if agent.random() < 0.05 else now
                if agent.random() < 0.5:
                    step(Bid(uid, seq, t, increment=agent.choice(cfg.quick_bids)))
                else:
                    step(Bid(uid, seq, t, amount=agent.randrange(-2, 31)))
                now += 0.1
            if agent.random() < 0.02:
                step(Leave(agent.randrange(1, n + 1)))
            if agent.random() < 0.02:
                step(Join(agent.randrange(1, n + 1), "back"))
            if agent.random() < 0.03:
                step(Pause(1, now))
                now += agent.random() * 30
                step(Resume(1, now))
            if agent.random() < 0.1:  # stale-deadline fire, must be a no-op
                step(TimerExpired("lot", state.lot.seq, state.lot.deadline - 5.0, now))
            # All-in tie? Exact stacks sometimes pile into the showdown —
            # the leader needn't be all-in (one-sided open, rule #19).
            lot = state.lot
            if lot.lottery is None and lot.leader_id is not None:
                if agent.random() < 0.6:
                    for m in state.managers:
                        if m.budget == lot.current_bid and agent.random() < 0.8:
                            step(Bid(m.user_id, lot.seq, now, amount=lot.current_bid))
                            now += 0.1
            # ...then guess (participants and gatecrashers, valid and not).
            if state.lot.lottery is not None:
                for _ in range(agent.randrange(4)):
                    uid = agent.choice([*range(1, n + 1), 999])
                    step(LotteryGuess(uid, state.lot.seq, agent.randrange(-3, 105), now))
                    now += 0.1
                if agent.random() < 0.15:  # a richer stack sometimes cancels
                    uid = agent.randrange(1, n + 1)
                    step(Bid(uid, state.lot.seq, now, amount=state.lot.current_bid + 1))
            deadline = state.lot.deadline
            now = max(now, deadline)
            step(TimerExpired("lot", state.lot.seq, deadline, now))
        elif state.phase == "free_pick":
            if agent.random() < 0.1:  # reclaim attempts must never add actives
                step(Join(agent.randrange(1, n + 1), "back"))
            actives = state.active_managers
            if not actives or agent.random() < 0.15:
                now = max(now, state.pick_deadline)
                step(TimerExpired("pick", -1, state.pick_deadline, now))
            elif agent.random() < 0.15:
                step(Pick(999, "nope", now))  # not the picker
            elif agent.random() < 0.15:
                step(Pick(actives[0].user_id, "not-a-player", now))
            else:
                step(Pick(actives[0].user_id, agent.choice(state.queue).id, now))
        else:  # lineup — swaps allowed, pause rejected, then the window locks
            if agent.random() < 0.5:
                uid = agent.choice([*range(1, n + 1), 999])
                a, b = agent.sample(cfg.slots, 2)
                step(Swap(uid, a, b))
            if agent.random() < 0.2:  # pause is rejected, state untouched
                step(Pause(1, now))
            if agent.random() < 0.2:  # stale-deadline fire must be a no-op
                step(TimerExpired("lineup", -1, state.lineup_deadline - 5.0, now))
            now = max(now, state.lineup_deadline)
            step(TimerExpired("lineup", -1, state.lineup_deadline, now))
    assert state.phase == "complete"
    assert all(m.full for m in state.managers)
    assert state.lot_seq <= max_lots, "reveal bound exceeded"
    return saw_lottery


def test_random_agent_drafts():
    drafts = 0
    lotteries = 0
    for round_ in range(43):  # 43 rounds x 7 sizes = 301 drafts
        for n in range(2, 9):
            lotteries += play_draft(round_ * 1_000 + n, n)
            drafts += 1
    assert drafts >= 300
    # The driver joins tied all-in stacks, so showdowns actually happen.
    assert lotteries > 0, "no draft ever entered an all-in showdown"
