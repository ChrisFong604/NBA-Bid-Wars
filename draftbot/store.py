"""Atomic JSON snapshot persistence for draft state.

One human-readable file per draft: ``{"state": ..., "meta": ...}``. Writes go
to a temp file in the same directory and land via ``os.replace`` so a crash
never leaves a torn snapshot.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from typing import Any

from .models import (
    Config,
    DraftState,
    LogEntry,
    Lot,
    Manager,
    Player,
    Spot,
)


# -------------------------------------------------------------- serialization


def state_to_dict(state: DraftState) -> dict[str, Any]:
    d = asdict(state)
    d["passed_ids"] = sorted(state.passed_ids)  # frozenset isn't JSON
    return d


def _player(d: dict[str, Any] | None) -> Player | None:
    # Pre-era snapshots omit decade/prime; dataclass defaults fill them in.
    return None if d is None else Player(**d)


def _spot(d: dict[str, Any]) -> Spot:
    return Spot(slot=d["slot"], player=_player(d["player"]), price=d["price"])


def _manager(d: dict[str, Any]) -> Manager:
    return Manager(
        user_id=d["user_id"],
        name=d["name"],
        budget=d["budget"],
        spots=tuple(_spot(s) for s in d["spots"]),
        autopilot=d["autopilot"],
        last_action_lot=d["last_action_lot"],
    )


def _lot(d: dict[str, Any] | None) -> Lot | None:
    if d is None:
        return None
    player = _player(d["player"])
    assert player is not None
    return Lot(
        seq=d["seq"],
        player=player,
        last_call=d["last_call"],
        current_bid=d["current_bid"],
        leader_id=d["leader_id"],
        deadline=d["deadline"],
    )


def _log_entry(d: dict[str, Any]) -> LogEntry:
    player = _player(d["player"])
    assert player is not None
    return LogEntry(
        kind=d["kind"],
        player=player,
        manager_id=d["manager_id"],
        price=d["price"],
    )


def _config(d: dict[str, Any]) -> Config:
    # Pre-era snapshots omit era_start/era_end; defaults (1960/2020) apply.
    return Config(
        **{**d, "slots": tuple(d["slots"]), "quick_bids": tuple(d["quick_bids"])}
    )


def state_from_dict(d: dict[str, Any]) -> DraftState:
    return DraftState(
        config=_config(d["config"]),
        commissioner_id=d["commissioner_id"],
        phase=d["phase"],
        managers=tuple(_manager(m) for m in d["managers"]),
        queue=tuple(_player(p) for p in d["queue"]),  # type: ignore[misc]
        passed_ids=frozenset(d["passed_ids"]),
        lot=_lot(d["lot"]),
        lot_seq=d["lot_seq"],
        pick_deadline=d["pick_deadline"],
        log=tuple(_log_entry(e) for e in d["log"]),
        paused=d["paused"],
        pause_remaining=d["pause_remaining"],
    )


# --------------------------------------------------------------- persistence


def save_snapshot(
    path: str | os.PathLike[str], state: DraftState, meta: dict[str, Any]
) -> None:
    target = os.fspath(path)
    payload = {"state": state_to_dict(state), "meta": meta}
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(target) + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_snapshot(path: str | os.PathLike[str]) -> tuple[DraftState, dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return state_from_dict(payload["state"]), payload["meta"]
