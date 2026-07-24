"""Snapshot persistence: roundtrip fidelity and atomic writes."""
from __future__ import annotations

import pytest

from draftbot import engine
from draftbot.models import Bid, Swap, TimerExpired
from draftbot.store import load_snapshot, save_snapshot
from helpers import start_draft


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


def test_failed_save_cleans_tmp_and_preserves_old_snapshot(tmp_path):
    state = rich_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"v": 1})
    with pytest.raises(TypeError):
        save_snapshot(path, state, {"bad": {1, 2, 3}})  # set isn't JSON
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]
    loaded, meta = load_snapshot(path)
    assert loaded == state and meta == {"v": 1}
