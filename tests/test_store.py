"""Snapshot persistence: roundtrip fidelity and atomic writes."""
from __future__ import annotations

import dataclasses
import json

import pytest

from draftbot import engine
from draftbot.models import Bid, Config, DraftState, Lottery, Swap, TimerExpired
from draftbot.store import load_snapshot, save_snapshot, state_to_dict
from helpers import make_manager, start_draft


def rich_state():
    """A mid-draft state exercising every field: log, passed_ids, filled
    spots, a live lot with a leader, and a swap."""
    state, _, rng = start_draft(3, seed=7)
    state, _ = engine.apply(state, Bid(2, 1, 1001.0, amount=5), rng)
    state, _ = engine.apply(
        state, TimerExpired("lot", 1, state.lot.deadline, state.lot.deadline), rng
    )
    # Pass lot 2 to populate passed_ids.
    state, _ = engine.apply(
        state, TimerExpired("lot", 2, state.lot.deadline, state.lot.deadline), rng
    )
    state, _ = engine.apply(state, Swap(2, "PG", "C"), rng)
    state, _ = engine.apply(state, Bid(3, 3, state.lot.deadline, amount=2), rng)
    assert state.passed_ids and state.log and state.lot.leader_id == 3
    return state


def test_roundtrip_save_load_equality(tmp_path):
    state = rich_state()
    path = tmp_path / "snap.json"
    meta = {"thread_id": 42, "board_message_id": None, "note": "mid-lot"}
    save_snapshot(path, state, meta)
    loaded, loaded_meta = load_snapshot(path)
    assert loaded == state
    assert loaded_meta == meta


def test_overwrite_leaves_no_tmp_files(tmp_path):
    state = rich_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"v": 1})
    save_snapshot(path, state, {"v": 2})
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]
    _, meta = load_snapshot(path)
    assert meta == {"v": 2}


def test_old_style_config_snapshot_loads(tmp_path):
    """Regression: pre-flat-clock snapshots (like the live lobby snapshot on
    the production VM) carry open_seconds/hammer_seconds/pool_extra, omit
    lot_seconds, and store ``sim`` as a bool. The loader must drop the dead
    keys, default lot_seconds, and coerce sim True->"ai" / False->"off".
    Pre-lineup-phase snapshots also omit lineup_seconds/lineup_deadline —
    defaults must fill both."""
    state = rich_state()
    payload = {"state": state_to_dict(state), "meta": {"thread_id": 3}}
    cfg = payload["state"]["config"]
    del cfg["lot_seconds"]
    del cfg["lineup_seconds"]
    del payload["state"]["lineup_deadline"]
    cfg.update(open_seconds=20, hammer_seconds=10, pool_extra=10, sim=True)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, meta = load_snapshot(path)
    assert meta == {"thread_id": 3}
    assert loaded.config.lot_seconds == 30  # default fills the missing key
    assert loaded.config.lineup_seconds == 60  # default fills the missing key
    assert loaded.lineup_deadline == 0.0  # default fills the missing key
    assert loaded.config.sim == "ai"  # True -> "ai"
    assert not hasattr(loaded.config, "pool_extra")
    # everything else survives untouched (sim True coerces to "ai", which
    # differs from the fresh-Config default of "prompt")
    expected = dataclasses.replace(
        state, config=dataclasses.replace(state.config, sim="ai")
    )
    assert loaded == expected
    # sim False coerces to "off".
    cfg["sim"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded2, _ = load_snapshot(path)
    assert loaded2.config.sim == "off"


def test_lineup_deadline_roundtrips(tmp_path):
    """asdict serializes the new field automatically; a nonzero value must
    survive save/load bit-for-bit."""
    state = dataclasses.replace(rich_state(), lineup_deadline=4321.5)
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"thread_id": 9})
    loaded, _ = load_snapshot(path)
    assert loaded.lineup_deadline == 4321.5
    assert loaded == state


def test_lineup_phase_snapshot_roundtrips(tmp_path):
    """A snapshot taken mid-lineup-window (phase="lineup", nonzero
    lineup_deadline, full rosters, empty queue) must roundtrip exactly —
    this is the state _resume_session's lineup branch recovers from."""
    cfg = Config()
    state = DraftState(
        config=cfg,
        commissioner_id=1,
        phase="lineup",
        managers=(
            make_manager(1, cfg, filled=5),
            make_manager(2, cfg, filled=5),
        ),
        lot_seq=9,
        lineup_deadline=5000.0,
    )
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"thread_id": 7})
    loaded, meta = load_snapshot(path)
    assert loaded == state
    assert loaded.phase == "lineup" and loaded.lineup_deadline == 5000.0
    assert meta == {"thread_id": 7}


def test_cpu_flag_roundtrips_and_defaults_false_on_old_snapshots(tmp_path):
    """Manager.cpu must survive save/load (recovery restarts the CPU driver
    off this flag, and ui.display renders CPUs plain off it); snapshots
    written before the CPU feature omit the key -> human manager."""
    cfg = Config()
    state = DraftState(
        config=cfg,
        commissioner_id=1,
        managers=(
            make_manager(1, cfg),
            dataclasses.replace(
                make_manager(-1, cfg), name="CPU 1", cpu=True
            ),
        ),
    )
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"thread_id": 1})
    loaded, _ = load_snapshot(path)
    assert loaded == state
    assert [m.cpu for m in loaded.managers] == [False, True]
    # Old snapshot: no "cpu" key on managers at all.
    payload = {"state": state_to_dict(state), "meta": {"v": 1}}
    for m in payload["state"]["managers"]:
        del m["cpu"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    old, _ = load_snapshot(path)
    assert all(not m.cpu for m in old.managers)


def lottery_state():
    """rich_state's live lot (leader 3 at $2) with a showdown in flight."""
    state = rich_state()
    lottery = Lottery(participants=(3, 1), guesses=((3, 7), (1, 99)))
    return dataclasses.replace(
        state, lot=dataclasses.replace(state.lot, lottery=lottery)
    )


def test_live_lottery_snapshot_roundtrips(tmp_path):
    state = lottery_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"thread_id": 1})
    loaded, _ = load_snapshot(path)
    assert loaded == state
    lot = loaded.lot
    assert lot.lottery.participants == (3, 1)
    # JSON delivers guesses as 2-lists; the loader coerces to int pairs.
    assert lot.lottery.guesses == ((3, 7), (1, 99))
    assert all(isinstance(g, tuple) for g in lot.lottery.guesses)


def test_old_shape_lot_dict_defaults_lottery_none(tmp_path):
    """Snapshots written before the showdown feature have no ``lottery`` key
    on the lot dict — the loader must default it to None."""
    state = rich_state()
    payload = {"state": state_to_dict(state), "meta": {"v": 1}}
    del payload["state"]["lot"]["lottery"]
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, _ = load_snapshot(path)
    assert loaded.lot.lottery is None
    assert loaded == state


def test_recovered_snapshot_redeal_drops_mid_flight_lottery(tmp_path):
    """Bot crash recovery re-deals the interrupted lot; a mid-flight showdown
    is dropped by design (v1) — the fresh lot must come back clean."""
    state = lottery_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {})
    loaded, _ = load_snapshot(path)
    assert loaded.lot.lottery is not None
    # Mirror the bot: interrupted lot's player back to the queue head, redeal.
    recovered = dataclasses.replace(
        loaded, queue=(loaded.lot.player,) + loaded.queue
    )
    state2, _ = engine.redeal(recovered, 9_000.0)
    assert state2.lot.lottery is None
    assert state2.lot.player == state.lot.player
    assert state2.lot.current_bid == 0 and state2.lot.leader_id is None


def test_failed_save_cleans_tmp_and_preserves_old_snapshot(tmp_path):
    state = rich_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"v": 1})
    with pytest.raises(TypeError):
        save_snapshot(path, state, {"bad": {1, 2, 3}})  # set isn't JSON
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]
    loaded, meta = load_snapshot(path)
    assert loaded == state and meta == {"v": 1}
