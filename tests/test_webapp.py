"""Web app backend tests: rooms API, redaction, timers, and a full
WebSocket-driven draft. Timers ACTUALLY fire: rooms are built directly
through the RoomRegistry with sub-second clocks, and the asyncio timer
tasks (running on the TestClient's portal event loop) drive lot expiry,
free-pick, and lineup transitions for real."""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from draftbot.models import Config
from helpers import make_manager
from webapp.rooms import ROOM_CODE_ALPHABET, RoomRegistry
from webapp.server import app, registry


@pytest.fixture()
def client():
    registry.rooms.clear()
    with TestClient(app) as c:
        yield c
    registry.rooms.clear()


# ------------------------------------------------------------ ws helpers


def _recv_until(ws, pred, seen=None, limit=400):
    for _ in range(limit):
        msg = ws.receive_json()
        if seen is not None:
            seen.append(msg)
        if pred(msg):
            return msg
    raise AssertionError("expected message never arrived")


def _state(msg):
    return msg.get("state", {})


def _is_phase(msg, phase):
    return msg.get("type") == "state" and _state(msg).get("phase") == phase


def _bid_at(msg, n):
    return (
        msg.get("type") == "state"
        and (_state(msg).get("lot") or {}).get("current_bid") == n
    )


def _fx_kinds(msg):
    return [f["kind"] for f in msg.get("fx", [])] if msg.get("type") == "fx" else []


def _fx(msg, kind):
    return next(f for f in msg["fx"] if f["kind"] == kind)


# ------------------------------------------------------------- rooms API


def test_create_room_and_summary(client):
    resp = client.post("/api/rooms", json={"name": "Alice", "budget": 25})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["room"]) == 4
    assert all(c in ROOM_CODE_ALPHABET for c in body["room"])
    assert body["user_id"] == 1
    assert isinstance(body["token"], str) and body["token"]

    summary = client.get(f"/api/rooms/{body['room']}")
    assert summary.status_code == 200
    assert summary.json() == {"exists": True, "phase": "lobby", "managers": 1}

    assert client.get("/api/rooms/ZZZZ").status_code == 404


def test_create_room_validation(client):
    bad_bodies = [
        {},  # missing name
        {"name": "   "},  # blank name
        {"name": "A", "budget": 0},
        {"name": "A", "budget": 1001},
        {"name": "A", "clock": 5},
        {"name": "A", "clock": 301},
        {"name": "A", "lineup": -1},
        {"name": "A", "era_from": 1955},
        {"name": "A", "era_to": 2025},
        {"name": "A", "era_from": 2020, "era_to": 1960},  # backwards
        {"name": "A", "sim": "vibes"},
    ]
    for body in bad_bodies:
        assert client.post("/api/rooms", json=body).status_code == 400, body


def test_join_and_full_lobby(client):
    room, _, _ = registry.create_room(Config(max_managers=2), "Alice")
    resp = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"})
    assert resp.status_code == 200
    assert resp.json()["user_id"] == 2  # sequential ints from 1

    full = client.post(f"/api/rooms/{room.code}/join", json={"name": "Carol"})
    assert full.status_code == 400
    assert full.json()["detail"] == "The lobby is full."

    assert (
        client.post("/api/rooms/ZZZZ/join", json={"name": "Dan"}).status_code == 404
    )


def test_token_reclaim(client):
    room, _, _ = registry.create_room(Config(), "Alice")
    first = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    again = client.post(
        f"/api/rooms/{room.code}/join", json={"token": first["token"]}
    )
    assert again.status_code == 200
    assert again.json() == first  # same identity, no new manager
    assert len(room.state.managers) == 2


def test_room_eviction():
    reg = RoomRegistry()
    stale, _, _ = reg.create_room(Config(), "Alice")
    stale.last_active = time.time() - 3700  # idle past the 1h TTL
    fresh, _, _ = reg.create_room(Config(), "Bob")  # create sweeps
    assert reg.get(stale.code) is None
    assert reg.get(fresh.code) is fresh


# ------------------------------------------------------- websocket basics


def test_ws_unknown_room(client):
    with client.websocket_connect("/ws/ZZZZ") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["message"] == "Room not found."


def test_ws_malformed_and_unknown_actions(client):
    room, token, _ = registry.create_room(Config(), "Alice")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        assert ws.receive_json()["type"] == "state"
        ws.send_text("{not json")
        assert ws.receive_json()["type"] == "error"
        ws.send_json([1, 2, 3])
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "explode"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "bid", "increment": "lots"})
        assert ws.receive_json()["type"] == "error"


def test_state_redaction_in_auction(client):
    room, token, _ = registry.create_room(Config(), "Alice")
    client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"})
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        first = ws.receive_json()
        assert first["type"] == "state" and _state(first)["phase"] == "lobby"
        assert _state(first)["you"] == 1
        ws.send_json({"action": "start"})
        st = _recv_until(ws, lambda m: _is_phase(m, "auction"))
        # Queue contents are unreachable; only the count is visible.
        assert '"queue"' not in json.dumps(st)
        assert _state(st)["queue_count"] == 9  # 10-player pool, 1 on the block
        assert _state(st)["lot"]["player"]["name"]
        assert _state(st)["free_pick"] is None


def test_error_routed_only_to_acting_socket(client):
    room, token_a, _ = registry.create_room(Config(), "Alice")
    joined = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={joined['token']}") as ws_b:
        ws_a.receive_json()
        ws_b.receive_json()
        ws_a.send_json({"action": "start"})
        _recv_until(ws_a, lambda m: _is_phase(m, "auction"))
        _recv_until(ws_b, lambda m: _is_phase(m, "auction"))

        ws_b.send_json({"action": "bid", "amount": 999})  # over budget
        err = ws_b.receive_json()
        assert err["type"] == "error"
        assert "$20" in err["message"]

        ws_a.send_json({"action": "bid", "increment": 1})
        # A's very next message is the bid state — the error never reached A.
        a_next = ws_a.receive_json()
        assert a_next["type"] == "state"
        assert _state(a_next)["lot"]["current_bid"] == 1
        _recv_until(ws_b, lambda m: _bid_at(m, 1))


def test_addtime_extends_live_deadline(client):
    room, token_a, _ = registry.create_room(Config(), "Alice")
    joined = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={joined['token']}") as ws_b:
        ws_a.receive_json()
        ws_b.receive_json()
        ws_a.send_json({"action": "start"})
        st = _recv_until(ws_a, lambda m: _is_phase(m, "auction"))
        _recv_until(ws_b, lambda m: _is_phase(m, "auction"))
        before = _state(st)["lot"]["deadline"]

        ws_b.send_json({"action": "addtime", "seconds": 60})  # not commissioner
        err = ws_b.receive_json()
        assert err["type"] == "error"
        assert err["message"] == "Only the commissioner can add time."

        ws_a.send_json({"action": "addtime", "seconds": 60})
        st2 = _recv_until(
            ws_a,
            lambda m: m.get("type") == "state"
            and (_state(m).get("lot") or {}).get("deadline", 0) > before + 30,
        )
        assert _state(st2)["lot"]["deadline"] == pytest.approx(before + 60)


# ------------------------------------------------- adversarial redaction check


def _wire_player_ids(node):
    """Every player-shaped dict (has 'pos' + 'id') reachable in a wire payload."""
    found = set()
    if isinstance(node, dict):
        if "pos" in node and "id" in node:
            found.add(node["id"])
        for value in node.values():
            found |= _wire_player_ids(value)
    elif isinstance(node, list):
        for value in node:
            found |= _wire_player_ids(value)
    return found


def test_hidden_queue_players_unreachable_for_manager_and_spectator(client):
    """The ONLY player object in any auction-phase state payload is the lot
    player — compared structurally against the room's real hidden queue, for
    a manager socket and a tokenless spectator socket alike."""
    room, token, _ = registry.create_room(Config(), "Alice")
    client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"})
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws_m, \
            client.websocket_connect(f"/ws/{room.code}") as ws_s:
        assert _state(ws_m.receive_json())["you"] == 1
        spec_lobby = ws_s.receive_json()
        assert _state(spec_lobby)["you"] is None  # spectator identity
        ws_m.send_json({"action": "start"})
        st_m = _recv_until(ws_m, lambda m: _is_phase(m, "auction"))
        st_s = _recv_until(ws_s, lambda m: _is_phase(m, "auction"))
        hidden_ids = {p.id for p in room.state.queue}
        lot_id = room.state.lot.player.id
        for st in (st_m, st_s):
            dump = json.dumps(st)
            assert '"queue"' not in dump
            visible = _wire_player_ids(st)
            assert visible == {lot_id}  # nothing but the on-block player
            assert not visible & hidden_ids
            assert _state(st)["log"] == []  # no name leaks via the log either


# ---------------------------------------------------- the full scripted draft


def test_full_draft_over_websockets(client):
    """Create → join → start → bid war (sold) → passes → force-assigns →
    free pick (pool revealed) → lineup swap → completion + sim prompt.
    Every phase transition is driven by real asyncio timers firing."""
    cfg = Config(lot_seconds=0.4, lineup_seconds=0.6, afk_lots=99, sim="prompt")
    room, token_a, uid_a = registry.create_room(cfg, "Alice")
    joined = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    token_b, uid_b = joined["token"], joined["user_id"]

    seen_a: list[dict] = []
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={token_b}") as ws_b:
        seen_a.append(ws_a.receive_json())
        ws_b.receive_json()

        ws_a.send_json({"action": "start"})
        st = _recv_until(ws_a, lambda m: _is_phase(m, "auction"), seen_a)
        assert _state(st)["queue_count"] == 9
        _recv_until(ws_b, lambda m: _is_phase(m, "auction"))

        # --- bid war on lot 1: A opens $1, B raises to $2, A takes it at $3
        ws_a.send_json({"action": "bid", "increment": 1})
        _recv_until(ws_b, lambda m: _bid_at(m, 1))
        ws_b.send_json({"action": "bid", "amount": 2})
        _recv_until(ws_a, lambda m: _bid_at(m, 2), seen_a)
        ws_a.send_json({"action": "bid", "amount": 3})
        _recv_until(ws_a, lambda m: _bid_at(m, 3), seen_a)

        # --- lot timer fires: SOLD to A at $3
        sold_msg = _recv_until(ws_a, lambda m: "sold" in _fx_kinds(m), seen_a)
        sold = _fx(sold_msg, "sold")
        assert sold["manager"] == uid_a and sold["price"] == 3

        # --- lots 2-10 expire unbid: passed and recycled
        _recv_until(ws_a, lambda m: "passed" in _fx_kinds(m), seen_a)

        # --- lot 11 is the first LAST CALL: force-assigned at $1 to B
        #     (5 empty slots vs A's 4)
        force_msg = _recv_until(ws_a, lambda m: "force" in _fx_kinds(m), seen_a)
        assert _fx(force_msg, "force")["manager"] == uid_b

        # --- B leaves: team flips to autopilot
        _recv_until(ws_b, lambda m: "force" in _fx_kinds(m))
        ws_b.send_json({"action": "leave"})
        _recv_until(ws_a, lambda m: "autopilot" in _fx_kinds(m), seen_a)

        # --- lot 12 force-assigns to A (sole active), then FREE PICK:
        #     A is the single active manager and the pool is revealed
        force2 = _recv_until(ws_a, lambda m: "force" in _fx_kinds(m), seen_a)
        assert _fx(force2, "force")["manager"] == uid_a
        # the free_pick state broadcast precedes the force fx (state first,
        # then fx) — it is already in the collected stream
        fp = next(m for m in reversed(seen_a) if _is_phase(m, "free_pick"))
        pool = _state(fp)["free_pick"]["pool"]
        assert _state(fp)["free_pick"]["picker"] == uid_a
        assert len(pool) == 7
        assert all(p["id"] and p["name"] for p in pool)
        # spectator sockets see the same public reveal
        fp_b = _recv_until(ws_b, lambda m: _is_phase(m, "free_pick"))
        assert len(_state(fp_b)["free_pick"]["pool"]) == 7

        # --- A picks 3 players to fill the roster
        for i in range(3):
            ws_a.send_json({"action": "pick", "player_id": pool[0]["id"]})
            if i < 2:
                st = _recv_until(
                    ws_a,
                    lambda m, want=6 - i: _is_phase(m, "free_pick")
                    and len(_state(m)["free_pick"]["pool"]) == want,
                    seen_a,
                )
                pool = _state(st)["free_pick"]["pool"]

        # --- B's team auto-fills, lineup window opens
        lineup_st = _recv_until(ws_a, lambda m: _is_phase(m, "lineup"), seen_a)
        fxmsg = _recv_until(ws_a, lambda m: "autofill" in _fx_kinds(m), seen_a)
        assert "lineup_open" in _fx_kinds(fxmsg)
        assert _state(lineup_st)["lineup_deadline"] > 0
        managers = _state(lineup_st)["managers"]
        assert all(
            s["player"] is not None for m in managers for s in m["spots"]
        )

        # --- swap PG <-> C during the lineup window
        me = next(m for m in managers if m["id"] == uid_a)
        pg_id = me["spots"][0]["player"]["id"]
        c_id = me["spots"][4]["player"]["id"]

        def swapped(m):
            if not _is_phase(m, "lineup"):
                return False
            mine = next(x for x in _state(m)["managers"] if x["id"] == uid_a)
            return (
                mine["spots"][0]["player"]["id"] == c_id
                and mine["spots"][4]["player"]["id"] == pg_id
            )

        ws_a.send_json({"action": "swap", "a": "PG", "b": "C"})
        _recv_until(ws_a, swapped, seen_a)

        # --- lineup timer fires: complete + the sim prompt message
        _recv_until(ws_a, lambda m: _is_phase(m, "complete"), seen_a)
        _recv_until(ws_a, lambda m: "complete" in _fx_kinds(m), seen_a)
        sim_msg = _recv_until(ws_a, lambda m: m.get("type") == "sim", seen_a)
        assert sim_msg["mode"] == "prompt"
        assert "Run the full simulation now." in sim_msg["share_prompt"]
        _recv_until(ws_b, lambda m: m.get("type") == "sim")

    # the hidden queue never leaked into any message A received
    assert '"queue"' not in json.dumps(seen_a)
    final_state = _state(_recv_last_state(seen_a))
    kinds = {entry["kind"] for entry in final_state["log"]}
    assert {"sold", "passed", "force", "pick", "autofill"} <= kinds


def _recv_last_state(seen):
    return next(m for m in reversed(seen) if m.get("type") == "state")


def test_three_manager_draft_with_broke_manager(client):
    """3 managers; Carol blows her whole budget on lot 1 (broke => inactive),
    Bob walks (autopilot), lots pass until Alice is the sole active manager:
    free pick reveals EXACTLY the live queue, Alice fills up, the other two
    auto-fill, lineup swap, completion, sim prompt to every socket."""
    cfg = Config(lot_seconds=0.4, lineup_seconds=0.6, afk_lots=99, sim="prompt")
    room, token_a, uid_a = registry.create_room(cfg, "Alice")
    b = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    c = client.post(f"/api/rooms/{room.code}/join", json={"name": "Carol"}).json()
    uid_b, uid_c = b["user_id"], c["user_id"]

    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={b['token']}") as ws_b, \
            client.websocket_connect(f"/ws/{room.code}?token={c['token']}") as ws_c:
        for ws in (ws_a, ws_b, ws_c):
            ws.receive_json()
        ws_a.send_json({"action": "start"})
        st = _recv_until(ws_a, lambda m: _is_phase(m, "auction"))
        assert _state(st)["queue_count"] == 14  # 15-player pool, 1 on the block
        _recv_until(ws_c, lambda m: _is_phase(m, "auction"))

        # --- Carol goes ALL IN: $20 of a $20 budget => broke after the sale
        ws_c.send_json({"action": "bid", "amount": 20})
        _recv_until(ws_c, lambda m: _bid_at(m, 20))
        sold_msg = _recv_until(ws_a, lambda m: "sold" in _fx_kinds(m))
        sold = _fx(sold_msg, "sold")
        assert sold["manager"] == uid_c and sold["price"] == 20

        # --- Bob leaves; broke Carol + autopilot Bob leave Alice sole active
        ws_b.send_json({"action": "leave"})
        _recv_until(ws_a, lambda m: "autopilot" in _fx_kinds(m))

        # --- remaining lots pass unbid until the free-pick transition
        fp = _recv_until(ws_a, lambda m: _is_phase(m, "free_pick"))
        assert _state(fp)["free_pick"]["picker"] == uid_a
        pool = _state(fp)["free_pick"]["pool"]
        # the reveal is EXACTLY the live hidden queue, in order
        assert [p["id"] for p in pool] == [p.id for p in room.state.queue]
        carol = next(
            m for m in _state(fp)["managers"] if m["id"] == uid_c
        )
        assert carol["budget"] == 0  # genuinely broke, not autopiloted

        # --- Alice fills all 5 slots for free
        for i in range(5):
            ws_a.send_json({"action": "pick", "player_id": pool[0]["id"]})
            if i < 4:
                st = _recv_until(
                    ws_a,
                    lambda m, want=len(pool) - 1: _is_phase(m, "free_pick")
                    and len(_state(m)["free_pick"]["pool"]) == want,
                )
                pool = _state(st)["free_pick"]["pool"]

        # --- Bob + Carol auto-fill, lineup window opens for everyone
        lineup_st = _recv_until(ws_a, lambda m: _is_phase(m, "lineup"))
        managers = _state(lineup_st)["managers"]
        assert all(
            s["player"] is not None for m in managers for s in m["spots"]
        )
        assert next(m for m in managers if m["id"] == uid_c)["budget"] == 0
        assert next(m for m in managers if m["id"] == uid_b)["autopilot"]

        # --- Alice swaps PG <-> C during the lineup window
        me = next(m for m in managers if m["id"] == uid_a)
        pg_id = me["spots"][0]["player"]["id"]
        c_id = me["spots"][4]["player"]["id"]
        ws_a.send_json({"action": "swap", "a": "PG", "b": "C"})

        def swapped(m):
            if not _is_phase(m, "lineup"):
                return False
            mine = next(x for x in _state(m)["managers"] if x["id"] == uid_a)
            return (
                mine["spots"][0]["player"]["id"] == c_id
                and mine["spots"][4]["player"]["id"] == pg_id
            )

        _recv_until(ws_a, swapped)

        # --- lineup lock => complete; the sim prompt reaches every socket
        final = _recv_until(ws_a, lambda m: _is_phase(m, "complete"))
        kinds = {e["kind"] for e in _state(final)["log"]}
        assert {"sold", "passed", "pick", "autofill"} <= kinds
        for ws in (ws_a, ws_b, ws_c):
            msg = _recv_until(ws, lambda m: m.get("type") == "sim")
            assert msg["mode"] == "prompt"
            assert "Run the full simulation now." in msg["share_prompt"]


# -------------------------------------------------------------- /simulate


def _completed_room(sim_mode):
    cfg = Config(sim=sim_mode)
    room, token, _ = registry.create_room(cfg, "Alice")
    room.state = replace(
        room.state,
        phase="complete",
        managers=(make_manager(1, cfg, filled=5), make_manager(2, cfg, filled=5)),
    )
    return room, token


def test_simulate_endpoint_gates(client):
    lobby_room, lobby_token, _ = registry.create_room(Config(), "Alice")
    resp = client.post(
        f"/api/rooms/{lobby_room.code}/simulate", json={"token": lobby_token}
    )
    assert resp.status_code == 400  # only when complete

    room, token = _completed_room("prompt")
    assert (
        client.post(f"/api/rooms/{room.code}/simulate", json={"token": "nope"})
        .status_code
        == 403
    )  # member-only

    off_room, off_token = _completed_room("off")
    assert (
        client.post(f"/api/rooms/{off_room.code}/simulate", json={"token": off_token})
        .status_code
        == 400
    )


def test_simulate_reruns_prompt_mode(client):
    room, token = _completed_room("prompt")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        assert ws.receive_json()["type"] == "state"
        resp = client.post(f"/api/rooms/{room.code}/simulate", json={"token": token})
        assert resp.status_code == 200
        msg = ws.receive_json()
        assert msg["type"] == "sim" and msg["mode"] == "prompt"
        assert "Run the full simulation now." in msg["share_prompt"]


def test_simulate_ai_falls_back_to_stats_without_key(client, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    room, token = _completed_room("ai")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        assert ws.receive_json()["type"] == "state"
        assert (
            client.post(f"/api/rooms/{room.code}/simulate", json={"token": token})
            .status_code
            == 200
        )
        msg = ws.receive_json()
        assert msg["type"] == "sim" and msg["mode"] == "ai"
        assert msg["note"] == "no LLM key — stats-only ranking"
        assert msg["champion"] in ("M1", "M2")
        assert len(msg["standings"]) == 2
