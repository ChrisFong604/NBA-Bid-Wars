"""FastAPI app: room CRUD, one WebSocket per client, static file serving.

Run: ``uv run uvicorn webapp.server:app``. The server only maps HTTP/WS
messages to engine events — every rule lives in ``draftbot.engine``.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from draftbot import dataset
from draftbot.models import (
    Bid,
    Cancel,
    Config,
    Event,
    Join,
    Kick,
    Leave,
    LotteryGuess,
    Pause,
    Pick,
    Resume,
    Start,
    Swap,
)

from . import views
from .rooms import JoinError, Room, RoomRegistry

log = logging.getLogger("webapp")

app = FastAPI(title="NBA draft rooms")
registry = RoomRegistry()

SIM_MODES = ("prompt", "off", "stats", "ai")
MAX_NAME_LENGTH = 32


# ------------------------------------------------------------- validation


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=message)


def _room_or_404(code: str) -> Room:
    room = registry.get(code)
    if room is None:
        raise HTTPException(status_code=404, detail="Room not found.")
    return room


def _clean_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad("A display name is required.")
    return value.strip()[:MAX_NAME_LENGTH]


def _int_option(body: dict, key: str, default: int, lo: int, hi: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _bad(f"{key} must be an integer between {lo} and {hi}.")
    return value


def _era_option(body: dict, key: str, default: int) -> int:
    value = body.get(key, default)
    if isinstance(value, bool) or value not in dataset.DECADES:
        anchors = ", ".join(str(d) for d in dataset.DECADES)
        raise _bad(f"{key} must be one of the decade anchors: {anchors}.")
    return value


def _era_label(era_start: int, era_end: int) -> str:
    if era_start == era_end:
        return f"{era_start}s"
    return f"{era_start}s–{era_end}s"


# ------------------------------------------------------------------ routes


@app.post("/api/rooms")
async def create_room(body: dict = Body(...)) -> dict[str, Any]:
    name = _clean_name(body.get("name"))
    budget = _int_option(body, "budget", 20, 1, 1000)
    clock = _int_option(body, "clock", 30, 15, 300)
    lineup = _int_option(body, "lineup", 60, 0, 300)
    era_from = _era_option(body, "era_from", 1960)
    era_to = _era_option(body, "era_to", 2020)
    if era_from > era_to:
        raise _bad(
            f"Era range is backwards — the {era_from}s come after the "
            f"{era_to}s. Pick era_from at or before era_to."
        )
    sim_mode = body.get("sim", "prompt")
    if sim_mode not in SIM_MODES:
        raise _bad(f"sim must be one of: {', '.join(SIM_MODES)}.")
    config = Config(
        budget=budget,
        lot_seconds=clock,
        lineup_seconds=lineup,
        era_start=era_from,
        era_end=era_to,
        sim=sim_mode,
    )
    room, token, user_id = registry.create_room(config, name)
    return {"room": room.code, "token": token, "user_id": user_id}


@app.post("/api/rooms/{code}/join")
async def join_room(code: str, body: dict = Body(...)) -> dict[str, Any]:
    room = _room_or_404(code)
    token = body.get("token")
    if isinstance(token, str) and token in room.tokens:
        # Reclaim: same identity; the engine decides whether a mid-draft
        # rejoin wakes an autopilot team (Join-reclaim rules, not ours).
        user_id = room.tokens[token]
        manager = room.state.manager(user_id)
        if manager is not None and manager.autopilot:
            await registry.dispatch(room, Join(user_id, manager.name))
        return {"room": room.code, "token": token, "user_id": user_id}
    name = _clean_name(body.get("name"))
    try:
        token, user_id = await registry.join(room, name)
    except JoinError as exc:
        raise _bad(str(exc)) from exc
    return {"room": room.code, "token": token, "user_id": user_id}


@app.get("/api/rooms/{code}")
async def room_summary(code: str) -> dict[str, Any]:
    room = _room_or_404(code)
    return {
        "exists": True,
        "phase": room.state.phase,
        "managers": len(room.state.managers),
    }


@app.post("/api/rooms/{code}/simulate")
async def simulate(code: str, body: dict = Body(...)) -> dict[str, Any]:
    """/simulate parity: complete drafts only, draft members only, re-runs
    per the room's configured mode."""
    room = _room_or_404(code)
    token = body.get("token")
    if not isinstance(token, str) or token not in room.tokens:
        raise HTTPException(status_code=403, detail="Only draft members can simulate.")
    state = room.state
    if state.phase != "complete":
        raise _bad("The sim runs after the draft completes.")
    if state.config.sim == "off":
        raise _bad("The tournament sim is off for this draft.")
    if room.sim_task is not None and not room.sim_task.done():
        raise HTTPException(status_code=409, detail="A sim is already running.")
    registry.start_sim(room)
    return {"ok": True}


# --------------------------------------------------------------- websocket


async def _ws_error(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "message": message})


async def _start_draft(room: Room, ws: WebSocket, actor: int, now: float) -> None:
    """Commissioner's start: load + era-filter the dataset, fire Start.
    Mirrors the bot's draft_start, including the friendly pool-too-small
    error (build_pool raises before any state commit)."""
    try:
        players = dataset.load_players()
    except ValueError as exc:
        await _ws_error(ws, f"Dataset error: {exc}")
        return
    config = room.state.config
    players = dataset.filter_by_era(players, config.era_start, config.era_end)
    try:
        await registry.dispatch(room, Start(actor, players, now))
    except ValueError as exc:  # pool build failed (era pool too small)
        eras = _era_label(config.era_start, config.era_end)
        await _ws_error(
            ws,
            f"Can't build a pool from the {eras} eras for "
            f"{len(room.state.managers)} managers ({exc}). "
            "Widen the era range or shrink the lobby.",
        )


def _build_event(
    action: object, actor: int, now: float, data: dict, room: Room
) -> Event | None:
    """Map a client action to an engine event verbatim; None = malformed.
    The engine validates everything that matters."""
    if action == "bid":
        amount = data.get("amount")
        increment = data.get("increment")
        if amount is not None and not isinstance(amount, int):
            return None
        if amount is None and not isinstance(increment, int):
            return None
        raw_seq = data.get("lot_seq")
        lot = room.state.lot
        lot_seq = (
            raw_seq
            if isinstance(raw_seq, int)
            else (lot.seq if lot is not None else -1)
        )
        return Bid(
            user_id=actor,
            lot_seq=lot_seq,
            now=now,
            increment=None if amount is not None else increment,
            amount=amount,
        )
    if action == "guess":  # all-in showdown pick — engine gates participants
        number = data.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            return None
        raw_seq = data.get("lot_seq")
        lot = room.state.lot
        lot_seq = (
            raw_seq
            if isinstance(raw_seq, int)
            else (lot.seq if lot is not None else -1)
        )
        return LotteryGuess(user_id=actor, lot_seq=lot_seq, guess=number, now=now)
    if action == "pick":
        player_id = data.get("player_id")
        if not isinstance(player_id, str):
            return None
        return Pick(actor, player_id, now)
    if action == "swap":
        slot_a, slot_b = data.get("a"), data.get("b")
        if not isinstance(slot_a, str) or not isinstance(slot_b, str):
            return None
        return Swap(actor, slot_a, slot_b)
    if action == "pause":
        return Pause(actor, now)
    if action == "resume":
        return Resume(actor, now)
    if action == "kick":
        target = data.get("target")
        if isinstance(target, bool) or not isinstance(target, int):
            return None
        return Kick(user_id=actor, target_id=target, now=now)
    if action == "cancel":
        return Cancel(actor)
    if action == "leave":
        return Leave(actor)
    return None


async def _handle_action(
    room: Room, ws: WebSocket, viewer_id: int | None, data: dict
) -> None:
    action = data.get("action")
    actor = viewer_id if viewer_id is not None else 0  # 0 = spectator; engine rejects
    now = time.time()
    if action == "start":
        await _start_draft(room, ws, actor, now)
        return
    if action == "addtime":
        seconds = data.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 1 <= seconds <= 3600
        ):
            await _ws_error(ws, "addtime needs seconds between 1 and 3600.")
            return
        error = await registry.add_time(room, actor, float(seconds))
        if error is not None:
            await _ws_error(ws, error)
        return
    event = _build_event(action, actor, now, data, room)
    if event is None:
        await _ws_error(ws, "Unknown or malformed action.")
        return
    await registry.dispatch(room, event)


@app.websocket("/ws/{code}")
async def ws_room(ws: WebSocket, code: str) -> None:
    room = registry.get(code)
    if room is None:
        await ws.accept()
        await _ws_error(ws, "Room not found.")
        await ws.close()
        return
    token = ws.query_params.get("token")
    viewer_id = room.tokens.get(token) if token else None
    await ws.accept()
    room.sockets[ws] = viewer_id
    room.last_active = time.time()
    try:
        await ws.send_json(
            {"type": "state", "state": views.state_view(room.state, viewer_id)}
        )
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except ValueError:
                await _ws_error(ws, "Malformed message — send a JSON object.")
                continue
            if not isinstance(data, dict):
                await _ws_error(ws, "Malformed message — send a JSON object.")
                continue
            try:
                await _handle_action(room, ws, viewer_id, data)
            except WebSocketDisconnect:
                raise
            except Exception:
                log.exception("action failed in room %s: %r", room.code, data)
                await _ws_error(ws, "Something went wrong — try again.")
    except WebSocketDisconnect:
        pass
    finally:
        room.sockets.pop(ws, None)


# ------------------------------------------------------------------ static

_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
