"""Snapshot persistence: roundtrip fidelity and atomic writes."""
from __future__ import annotations

import dataclasses
import json

import pytest

from draftbot import engine
from draftbot.models import Bid, Swap, TimerExpired
from draftbot.store import load_snapshot, save_snapshot, state_to_dict
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


def test_old_style_config_snapshot_loads(tmp_path):
    """Regression: pre-flat-clock snapshots (like the live lobby snapshot on
    the production VM) carry open_seconds/hammer_seconds/pool_extra, omit
    lot_seconds, and store ``sim`` as a bool. The loader must drop the dead
    keys, default lot_seconds, and coerce sim True->"ai" / False->"off"."""
    state = rich_state()
    payload = {"state": state_to_dict(state), "meta": {"thread_id": 3}}
    cfg = payload["state"]["config"]
    del cfg["lot_seconds"]
    cfg.update(open_seconds=20, hammer_seconds=10, pool_extra=10, sim=True)
    path = tmp_path / "old.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, meta = load_snapshot(path)
    assert meta == {"thread_id": 3}
    assert loaded.config.lot_seconds == 30  # default fills the missing key
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


def test_failed_save_cleans_tmp_and_preserves_old_snapshot(tmp_path):
    state = rich_state()
    path = tmp_path / "snap.json"
    save_snapshot(path, state, {"v": 1})
    with pytest.raises(TypeError):
        save_snapshot(path, state, {"bad": {1, 2, 3}})  # set isn't JSON
    assert [p.name for p in tmp_path.iterdir()] == ["snap.json"]
    loaded, meta = load_snapshot(path)
    assert loaded == state and meta == {"v": 1}
