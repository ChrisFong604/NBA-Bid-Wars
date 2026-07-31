"""Headless end-to-end drafts against the REAL shipped dataset.

Drives ``engine.apply`` directly (no Discord) with scripted and seeded-random
events through completion, asserting the DESIGN.md invariants after every
transition and full log/roster/budget consistency at the end.
"""
from __future__ import annotations

import json
import random
from collections import Counter

from draftbot import dataset, engine, store
from draftbot.models import (
    SLOTS,
    AutoFilledFx,
    ArmTimerFx,
    Bid,
    BidPlaced,
    CancelTimerFx,
    CompleteFx,
    Config,
    DraftState,
    ErrorFx,
    ForceAssignedFx,
    FreePickFx,
    Join,
    LineupPhaseFx,
    LotteryGuess,
    LotteryGuessedFx,
    LotteryJoinedFx,
    LotteryOpenedFx,
    LotteryRevealFx,
    PassedFx,
    Pause,
    Pick,
    PickedFx,
    Player,
    Resume,
    SoldFx,
    Start,
    Swap,
    TimerExpired,
)

PLAYERS = dataset.load_players()
N_MANAGERS = 4
PLACEMENT_KINDS = ("sold", "force", "pick", "autofill")


# ------------------------------------------------------------------ helpers


def fresh_draft(
    rng: random.Random, config: Config | None = None, now: float = 1_000.0
) -> DraftState:
    state = DraftState(config=config or Config(), commissioner_id=1)
    for uid in range(1, N_MANAGERS + 1):
        state, _ = engine.apply(state, Join(uid, f"M{uid}"), rng)
    state, _ = engine.apply(state, Start(1, PLAYERS, now), rng)
    assert state.phase == "auction"
    return state


def assert_invariants(state: DraftState) -> None:
    seen: set[str] = set()
    for m in state.managers:
        assert m.budget >= 0, "budget overdrawn"
        for s in m.spots:
            if s.player is not None:
                assert s.player.id not in seen, "player placed twice"
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
    if state.phase == "lineup":
        assert all(m.full for m in state.managers), "lineup with empty slots"
        assert not state.queue and state.lot is None, (
            "leftover pool players in the lineup window"
        )


def assert_complete_and_consistent(state: DraftState) -> None:
    assert state.phase == "complete"
    # Zero leftovers: the pool is exactly 5N, so every player ends on a roster.
    assert not state.queue and state.lot is None, "leftover pool players"
    placed: dict[str, int] = {}
    for m in state.managers:
        assert m.budget >= 0
        assert m.full, f"{m.name} has empty slots at completion"
        for s in m.spots:
            placed[s.player.id] = m.user_id  # type: ignore[union-attr]
    # Every roster spot's player appears exactly once across placement kinds.
    entries = [e for e in state.log if e.kind in PLACEMENT_KINDS]
    ids = [e.player.id for e in entries]
    assert len(ids) == len(set(ids)), "player placed twice in the log"
    assert set(ids) == set(placed), "log placements don't match rosters"
    spend: Counter[int] = Counter()
    for e in entries:
        assert e.manager_id is not None
        assert placed[e.player.id] == e.manager_id, "log manager != roster owner"
        spend[e.manager_id] += e.price
    for m in state.managers:
        assert m.budget == state.config.budget - spend[m.user_id]


# ------------------------------------------------- seeded random full drafts


def run_random_draft(seed: int) -> DraftState:
    rng = random.Random(seed)
    now = 1_000.0
    state = fresh_draft(rng, now=now)
    assert_invariants(state)
    steps = 0
    while state.phase not in ("complete", "cancelled"):
        steps += 1
        assert steps < 2_000, "draft failed to terminate"
        if state.phase == "auction":
            for _ in range(rng.randrange(0, 4)):  # 0 bids => the lot passes
                m = rng.choice(state.managers)
                now += rng.uniform(0.05, 1.5)
                if rng.random() < 0.5:  # quick button vs. custom modal
                    ev = Bid(
                        m.user_id, state.lot.seq, now,
                        increment=rng.choice(state.config.quick_bids),
                    )
                else:  # often-invalid absolute amounts; the engine rejects
                    ev = Bid(
                        m.user_id, state.lot.seq, now,
                        amount=rng.randrange(1, state.config.budget + 2),
                    )
                state, _ = engine.apply(state, ev, rng)
                assert_invariants(state)
            if rng.random() < 0.05:  # occasional pause/resume mid-lot
                state, _ = engine.apply(state, Pause(1, now), rng)
                now += rng.uniform(1.0, 30.0)
                state, _ = engine.apply(state, Resume(1, now), rng)
                assert_invariants(state)
            lot = state.lot
            now = max(now, lot.deadline)
            state, _ = engine.apply(
                state, TimerExpired("lot", lot.seq, lot.deadline, now), rng
            )
        elif state.phase == "free_pick":
            if rng.random() < 0.8:
                picker = state.active_managers[0]
                pid = rng.choice(state.queue).id
                now += rng.uniform(0.5, 10.0)
                state, _ = engine.apply(state, Pick(picker.user_id, pid, now), rng)
            else:  # idle out -> autopilot -> auto-fill
                now = max(now, state.pick_deadline)
                state, _ = engine.apply(
                    state,
                    TimerExpired("pick", -1, state.pick_deadline, now),
                    rng,
                )
        else:  # lineup — a swap or two, then the window locks
            if rng.random() < 0.7:
                m = rng.choice(state.managers)
                slot_a, slot_b = rng.sample(SLOTS, 2)
                state, _ = engine.apply(
                    state, Swap(m.user_id, slot_a, slot_b), rng
                )
                assert_invariants(state)
            now = max(now, state.lineup_deadline)
            state, _ = engine.apply(
                state,
                TimerExpired("lineup", -1, state.lineup_deadline, now),
                rng,
            )
        assert_invariants(state)
    return state


def test_random_drafts_complete_with_consistent_logs():
    kinds_seen: Counter[str] = Counter()
    for seed in range(6):
        state = run_random_draft(seed)
        assert_complete_and_consistent(state)
        kinds_seen.update(e.kind for e in state.log)
    # Across the seed set the major paths all get exercised.
    assert {"sold", "passed", "pick", "autofill"} <= set(kinds_seen)


# ------------------------------------------------------------ scripted path


def test_scripted_broke_freepick_pause_resume():
    """One deterministic draft: a pass/recycle, an all-in that goes broke,
    broke-manager lockout, pause/resume mid-lot, sole-survivor free picks,
    and auto-fill to completion."""
    rng = random.Random(7)
    state = fresh_draft(rng)
    assert 1 + len(state.queue) == 5 * N_MANAGERS  # pool = 5N exactly

    # Lot 1 passes: recycled to the back and flagged for LAST CALL.
    lot1 = state.lot
    state, fx = engine.apply(
        state, TimerExpired("lot", lot1.seq, lot1.deadline, lot1.deadline), rng
    )
    assert any(isinstance(f, PassedFx) for f in fx)
    assert state.queue[-1].id == lot1.player.id
    assert lot1.player.id in state.passed_ids
    assert state.lot.seq == 2

    # Lot 2: manager 2 goes all-in — legal (rule #5) — and ends up broke.
    lot2 = state.lot
    state, fx = engine.apply(
        state, Bid(2, lot2.seq, lot2.deadline - 1.0, amount=20), rng
    )
    assert any(isinstance(f, BidPlaced) for f in fx)
    lot2 = state.lot
    assert (lot2.current_bid, lot2.leader_id) == (20, 2)
    state, fx = engine.apply(
        state, TimerExpired("lot", lot2.seq, lot2.deadline, lot2.deadline), rng
    )
    assert any(isinstance(f, SoldFx) for f in fx)
    m2 = state.manager(2)
    assert (m2.budget, m2.empty_slots) == (0, 4)

    # Broke = spectator (rule #11): m2's bids bounce.
    lot3 = state.lot
    state, fx = engine.apply(
        state, Bid(2, lot3.seq, lot3.deadline - 15.0, amount=1), rng
    )
    assert any(isinstance(f, ErrorFx) for f in fx)

    # Pause mid-lot: clock freezes, bids bounce, the armed timer is void.
    pause_at = lot3.deadline - 5.0
    state, fx = engine.apply(state, Pause(1, pause_at), rng)
    assert state.paused
    assert any(isinstance(f, CancelTimerFx) for f in fx)
    state, fx = engine.apply(
        state, Bid(1, lot3.seq, pause_at + 1.0, amount=3), rng
    )
    assert any(isinstance(f, ErrorFx) for f in fx)
    state, fx = engine.apply(  # a stale fire during the pause is a no-op
        state, TimerExpired("lot", lot3.seq, lot3.deadline, lot3.deadline), rng
    )
    assert state.lot.seq == lot3.seq and fx == []
    resume_at = lot3.deadline + 100.0
    state, fx = engine.apply(state, Resume(1, resume_at), rng)
    assert not state.paused
    assert state.lot.deadline == resume_at + 5.0  # the frozen 5s came back
    arm = next(f for f in fx if isinstance(f, ArmTimerFx))
    assert (arm.kind, arm.lot_seq, arm.deadline) == (
        "lot", lot3.seq, state.lot.deadline
    )

    # m1 then m3 also go all-in and broke; m4 never spends a dollar.
    for uid in (1, 3):
        lot = state.lot
        state, _ = engine.apply(
            state, Bid(uid, lot.seq, lot.deadline - 1.0, amount=20), rng
        )
        lot = state.lot
        state, fx = engine.apply(
            state, TimerExpired("lot", lot.seq, lot.deadline, lot.deadline), rng
        )
        assert any(isinstance(f, SoldFx) for f in fx)

    # Exactly one active manager left -> free-pick phase, pool revealed.
    assert state.phase == "free_pick"
    free = next(f for f in fx if isinstance(f, FreePickFx))
    assert free.manager_id == 4
    assert free.pool == state.queue  # the whole remaining queue is revealed
    assert len(free.pool) == 5 * N_MANAGERS - 3  # 3 sold so far

    # m4 free-picks a full roster; each pick re-arms the 60s pick timer.
    now = state.pick_deadline - 30.0
    for i in range(5):
        pid = state.queue[i].id  # any player, including never-revealed ones
        state, fx = engine.apply(state, Pick(4, pid, now), rng)
        assert any(isinstance(f, PickedFx) for f in fx)
        if i < 4:
            assert state.phase == "free_pick"
            assert any(
                isinstance(f, ArmTimerFx) and f.kind == "pick" for f in fx
            )
        now += 10.0

    # Roster full -> broke teams auto-fill free -> lineup window opens.
    filled = next(f for f in fx if isinstance(f, AutoFilledFx))
    assert len(filled.assignments) == 3 * 4  # m1/m2/m3, 4 empty slots each
    assert state.phase == "lineup"
    assert not any(isinstance(f, CompleteFx) for f in fx)  # not complete yet
    lineup = next(f for f in fx if isinstance(f, LineupPhaseFx))
    arm = next(f for f in fx if isinstance(f, ArmTimerFx))
    assert (arm.kind, arm.lot_seq, arm.deadline) == ("lineup", -1, lineup.deadline)
    assert state.lineup_deadline == lineup.deadline
    assert len(state.queue) == 0  # zero leftovers: every pool player placed

    # Managers can rearrange slots during the window; players are preserved.
    before = Counter(s.player.id for s in state.manager(4).spots)
    state, fx = engine.apply(state, Swap(4, "PG", "C"), rng)
    assert Counter(s.player.id for s in state.manager(4).spots) == before

    # The window closes -> complete, with the final (swapped) lineups.
    state, fx = engine.apply(
        state,
        TimerExpired(
            "lineup", -1, state.lineup_deadline, state.lineup_deadline
        ),
        rng,
    )
    assert any(isinstance(f, CompleteFx) for f in fx)
    assert_complete_and_consistent(state)
    assert [state.manager(u).budget for u in (1, 2, 3, 4)] == [0, 0, 0, 20]
    assert Counter(e.kind for e in state.log) == Counter(
        {"passed": 1, "sold": 3, "pick": 5, "autofill": 12}
    )


def test_scripted_all_in_showdown_resolves_and_draft_completes():
    """Three managers go all-in at the full $20 on lot 1: tie -> showdown ->
    guesses (one overwrite, one missing) -> seeded reveal -> winner charged
    their whole budget -> the draft then runs to a consistent completion."""
    rng = random.Random(3)
    state = fresh_draft(rng)
    player = state.lot.player

    # m1 all-in $20; m2 matches -> showdown opens with a 15s countdown.
    state, _ = engine.apply(state, Bid(1, 1, 1001.0, amount=20), rng)
    state, fx = engine.apply(state, Bid(2, 1, 1002.0, amount=20), rng)
    opened = next(f for f in fx if isinstance(f, LotteryOpenedFx))
    assert opened.lot.lottery.participants == (1, 2)
    assert state.lot.deadline == 1002.0 + state.config.lottery_seconds
    assert any(isinstance(f, ArmTimerFx) for f in fx)

    # m3 joins late; the countdown does NOT reset.
    state, fx = engine.apply(state, Bid(3, 1, 1003.0, amount=20), rng)
    joined = next(f for f in fx if isinstance(f, LotteryJoinedFx))
    assert joined.manager_id == 3
    assert state.lot.lottery.participants == (1, 2, 3)
    assert state.lot.deadline == 1002.0 + state.config.lottery_seconds

    # Guesses: m1 locks 50, m2 locks 10 then overwrites to 80, m3 fumbles.
    state, fx = engine.apply(state, LotteryGuess(1, 1, 50, 1004.0), rng)
    assert fx == [LotteryGuessedFx(1)]
    state, _ = engine.apply(state, LotteryGuess(2, 1, 10, 1004.5), rng)
    state, _ = engine.apply(state, LotteryGuess(2, 1, 80, 1005.0), rng)
    assert dict(state.lot.lottery.guesses) == {1: 50, 2: 80}
    _, fx = engine.apply(state, LotteryGuess(4, 1, 42, 1005.5), rng)
    assert any(isinstance(f, ErrorFx) for f in fx)  # m4 never went all-in

    # Predict the resolution by cloning the engine rng: m3's missing guess
    # fills first, then the mystery draw, then the (possibly tied) choice.
    clone = random.Random()
    clone.setstate(rng.getstate())
    fill3 = clone.randint(1, 100)
    mystery = clone.randint(1, 100)
    dist = {1: abs(50 - mystery), 2: abs(80 - mystery), 3: abs(fill3 - mystery)}
    best = min(dist.values())
    winner = clone.choice([u for u in (1, 2, 3) if dist[u] == best])

    deadline = state.lot.deadline
    state, fx = engine.apply(state, TimerExpired("lot", 1, deadline, deadline), rng)
    assert fx[0] == LotteryRevealFx(mystery, ((1, 50), (2, 80), (3, fill3)), winner)
    assert fx[1] == SoldFx(player, winner, 20)
    assert state.manager(winner).budget == 0
    assert any(
        s.player is not None and s.player.id == player.id and s.price == 20
        for s in state.manager(winner).spots
    )
    assert_invariants(state)
    assert state.phase == "auction" and state.lot.seq == 2  # flow continues

    # Nobody else bids: pass / LAST CALL / force / free-pick to completion.
    guard = 0
    while state.phase == "auction":
        guard += 1
        assert guard < 200, "draft failed to terminate"
        lot = state.lot
        state, _ = engine.apply(
            state, TimerExpired("lot", lot.seq, lot.deadline, lot.deadline), rng
        )
        assert_invariants(state)
    while state.phase == "free_pick":
        picker = state.active_managers[0]
        state, _ = engine.apply(
            state,
            Pick(picker.user_id, state.queue[0].id, state.pick_deadline - 1.0),
            rng,
        )
        assert_invariants(state)
    if state.phase == "lineup":
        state, _ = engine.apply(
            state,
            TimerExpired("lineup", -1, state.lineup_deadline, state.lineup_deadline),
            rng,
        )
    assert_complete_and_consistent(state)
    sold = next(e for e in state.log if e.kind == "sold")
    assert (sold.manager_id, sold.price) == (winner, 20)


# ---------------------------------------------------------- era-range pool


def _synthetic_era_players() -> tuple[Player, ...]:
    """Three decades × 8 per position: enough for a 4-manager pool inside
    any single decade, plus out-of-era players that must never leak in.
    Synthetic so this file stays green while the dataset is regenerated."""
    players: list[Player] = []
    for decade in (1960, 1990, 2020):
        for pos in SLOTS:
            for i in range(8):
                players.append(
                    Player(
                        id=f"syn-{decade}-{pos}-{i}",
                        name=f"Syn {pos}{i} {decade}",
                        team="SYN",
                        pos=pos,
                        ppg=10.0 + i,
                        rpg=5.0,
                        apg=3.0,
                        stars=(i % 5) + 1,
                        decade=decade,
                        prime=f"{decade + 1}–{decade + 5}",
                    )
                )
    return tuple(players)


def test_narrow_era_draft_completes_with_only_in_era_players():
    """Config(era_start=1990, era_end=1990) with the pool pre-filtered the
    way /draft start does (inclusive decade anchors): the draft runs to
    completion and every roster spot holds a 1990s player."""
    rng = random.Random(5)
    # lineup_seconds=0 also covers the skip-the-window fast path end to end.
    config = Config(era_start=1990, era_end=1990, afk_lots=1_000, lineup_seconds=0)
    eligible = tuple(
        p
        for p in _synthetic_era_players()
        if config.era_start <= p.decade <= config.era_end
    )
    state = DraftState(config=config, commissioner_id=1)
    for uid in range(1, N_MANAGERS + 1):
        state, _ = engine.apply(state, Join(uid, f"M{uid}"), rng)
    state, _ = engine.apply(state, Start(1, eligible, 1_000.0), rng)
    assert state.phase == "auction"
    pool = (state.lot.player,) + state.queue
    assert len(pool) == 5 * N_MANAGERS
    assert all(p.decade == 1990 for p in pool)
    while state.phase == "auction":  # nobody bids: pass → LAST CALL → force
        lot = state.lot
        state, _ = engine.apply(
            state, TimerExpired("lot", lot.seq, lot.deadline, lot.deadline), rng
        )
        assert state.lot_seq <= 2 * len(pool), "reveal bound exceeded"
        assert_invariants(state)
    while state.phase == "free_pick":
        picker = state.active_managers[0]
        state, _ = engine.apply(
            state,
            Pick(picker.user_id, state.queue[0].id, state.pick_deadline - 1.0),
            rng,
        )
        assert_invariants(state)
    assert_complete_and_consistent(state)
    for m in state.managers:
        assert all(s.player.decade == 1990 for s in m.spots)


# --------------------------------------------------- snapshot compatibility


def _era_draft_state(rng: random.Random, config: Config) -> DraftState:
    state = DraftState(config=config, commissioner_id=1)
    for uid in range(1, N_MANAGERS + 1):
        state, _ = engine.apply(state, Join(uid, f"M{uid}"), rng)
    state, _ = engine.apply(state, Start(1, _synthetic_era_players(), 1_000.0), rng)
    assert state.phase == "auction"
    return state


def test_snapshot_roundtrip_preserves_era_fields(tmp_path):
    state = _era_draft_state(
        random.Random(9), Config(era_start=1960, era_end=2020)
    )
    path = tmp_path / "snap.json"
    store.save_snapshot(path, state, {"thread_id": 1})
    loaded, meta = store.load_snapshot(path)
    assert loaded == state  # decade/prime/era_start/era_end all survive
    assert meta == {"thread_id": 1}


def _strip_era_keys(obj):
    """Rewrite a snapshot payload to the pre-era shape: no era_start/era_end
    on the config, no decade/prime on any player."""
    if isinstance(obj, dict):
        return {
            k: _strip_era_keys(v)
            for k, v in obj.items()
            if k not in ("era_start", "era_end", "decade", "prime")
        }
    if isinstance(obj, (list, tuple)):  # asdict keeps tuples as tuples
        return [_strip_era_keys(v) for v in obj]
    return obj


def test_pre_era_snapshot_loads_with_dataclass_defaults(tmp_path):
    state = _era_draft_state(random.Random(2), Config())
    payload = _strip_era_keys(
        {"state": store.state_to_dict(state), "meta": {"thread_id": 7}}
    )
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, meta = store.load_snapshot(path)
    assert meta == {"thread_id": 7}
    assert (loaded.config.era_start, loaded.config.era_end) == (2000, 2020)
    players = (loaded.lot.player,) + loaded.queue
    assert all(p.decade == 2020 and p.prime == "" for p in players)
    # everything else survives untouched
    assert loaded.phase == "auction"
    assert loaded.lot.seq == state.lot.seq
    assert [m.user_id for m in loaded.managers] == list(range(1, N_MANAGERS + 1))
    assert loaded.config.budget == state.config.budget


def test_all_pass_draft_force_assigns_within_reveal_bound():
    """Nobody ever bids: every player passes once, comes back as LAST CALL,
    and force-assigns at $1 until all rosters fill — inside the 2*5N
    reveal bound (rule #6). afk_lots is raised so the AFK sweep doesn't
    flip everyone to autopilot first."""
    rng = random.Random(11)
    state = fresh_draft(rng, config=Config(afk_lots=1_000))
    pool_size = 5 * N_MANAGERS
    while state.phase == "auction":
        lot = state.lot
        state, fx = engine.apply(
            state, TimerExpired("lot", lot.seq, lot.deadline, lot.deadline), rng
        )
        assert state.lot_seq <= 2 * pool_size, "reveal bound exceeded"
        assert_invariants(state)
        if lot.last_call:
            assert any(isinstance(f, ForceAssignedFx) for f in fx)
    # Forces spread evenly (most-open-slots rule), so the 19th force leaves
    # exactly one active manager with one empty slot -> free-pick (rule #13).
    assert state.phase == "free_pick"
    picker = state.active_managers[0]
    assert picker.empty_slots == 1
    state, fx = engine.apply(
        state, Pick(picker.user_id, state.queue[0].id, state.pick_deadline - 1.0),
        rng,
    )
    # The final pick opens the lineup window; completion follows the timer.
    assert any(isinstance(f, LineupPhaseFx) for f in fx)
    assert state.phase == "lineup"
    state, fx = engine.apply(
        state,
        TimerExpired("lineup", -1, state.lineup_deadline, state.lineup_deadline),
        rng,
    )
    assert any(isinstance(f, CompleteFx) for f in fx)
    assert_complete_and_consistent(state)
    kinds = Counter(e.kind for e in state.log)
    assert kinds["passed"] == pool_size  # every player passed exactly once
    assert kinds["force"] == 5 * N_MANAGERS - 1  # LAST CALL filled the rest
    assert kinds["pick"] == 1
    for m in state.managers:  # each team ate $1 per forced slot
        forced = sum(1 for s in m.spots if s.price == 1)
        assert m.budget == state.config.budget - forced
