"""Web app backend tests: rooms API, redaction, timers, draft modes
(blind masking, snake turns), and full WebSocket-driven drafts. Timers
ACTUALLY fire: rooms are built directly through the RoomRegistry with
sub-second clocks, and the asyncio timer tasks (running on the
TestClient's portal event loop) drive lot expiry, free-pick, snake-turn,
and lineup transitions for real."""
from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from draftbot.models import (
    Config,
    DraftState,
    ForceAssignedFx,
    LogEntry,
    Lot,
    Lottery,
    LotteryCancelledFx,
    LotteryGuessedFx,
    LotteryJoinedFx,
    LotteryOpenedFx,
    LotteryRevealFx,
    PassedFx,
    PickedFx,
    SnakeTurnFx,
    SoldFx,
    snake_price,
)
from helpers import auction_state, make_manager, make_players
from webapp import views
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
        {"name": "A", "pool_depth": "shallow"},
        {"name": "A", "mode": "dynasty"},
    ]
    for body in bad_bodies:
        assert client.post("/api/rooms", json=body).status_code == 400, body


def test_create_room_pool_depth_roundtrips_into_config(client):
    resp = client.post("/api/rooms", json={"name": "Alice", "pool_depth": "legends"})
    assert resp.status_code == 200
    room = registry.get(resp.json()["room"])
    assert room.state.config.pool_depth == "legends"
    assert views.state_view(room.state, None)["config"]["pool_depth"] == "legends"
    # omitted -> the "legends" default
    resp = client.post("/api/rooms", json={"name": "Bob"})
    assert registry.get(resp.json()["room"]).state.config.pool_depth == "legends"


def test_create_room_mode_roundtrips_into_config(client):
    for mode in ("auction", "blind", "snake"):
        resp = client.post("/api/rooms", json={"name": "Alice", "mode": mode})
        assert resp.status_code == 200, mode
        room = registry.get(resp.json()["room"])
        assert room.state.config.mode == mode
        assert views.state_view(room.state, None)["config"]["mode"] == mode
    # omitted -> the "auction" default
    resp = client.post("/api/rooms", json={"name": "Bob"})
    assert registry.get(resp.json()["room"]).state.config.mode == "auction"


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


def test_sweep_spares_paused_live_draft():
    reg = RoomRegistry()
    room, _, _ = reg.create_room(Config(), "Alice")
    room.state = replace(room.state, phase="auction", paused=True)
    # Paused rooms stop refreshing last_active — that must not read as idle.
    room.last_active = time.time() - 3700
    reg.create_room(Config(), "Bob")  # create sweeps
    assert reg.get(room.code) is room


def test_sweep_spares_paused_live_snake_draft():
    reg = RoomRegistry()
    room, _, _ = reg.create_room(Config(mode="snake"), "Alice")
    room.state = replace(room.state, phase="snake", paused=True)
    room.last_active = time.time() - 3700
    reg.create_room(Config(), "Bob")  # create sweeps
    assert reg.get(room.code) is room


def test_sweep_spares_live_draft_with_open_sockets(client):
    room, token, _ = registry.create_room(Config(), "Alice")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        assert ws.receive_json()["type"] == "state"
        room.state = replace(room.state, phase="auction")
        room.last_active = time.time() - 3700
        client.post("/api/rooms", json={"name": "Bob"})  # create sweeps
        assert registry.get(room.code) is room


def test_sweep_closes_sockets_of_evicted_room(client):
    # A dead lobby with a socket still attached IS evicted — but the
    # socket must be told and closed, never left playing a ghost room.
    room, token, _ = registry.create_room(Config(), "Alice")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        assert ws.receive_json()["type"] == "state"
        room.last_active = time.time() - 3700
        client.post("/api/rooms", json={"name": "Bob"})  # create sweeps
        assert registry.get(room.code) is None
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "expired" in msg["message"]
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


# ------------------------------------------------------- websocket basics


def test_state_messages_carry_server_now(client):
    room, token, _ = registry.create_room(Config(), "Alice")
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "state"
        assert hello["now"] == pytest.approx(time.time(), abs=5.0)
        # Broadcast states carry it too — a join triggers a commit.
        client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"})
        msg = _recv_until(ws, lambda m: m.get("type") == "state")
        assert msg["now"] == pytest.approx(time.time(), abs=5.0)


def test_chat_roundtrip_gates_and_history(client, monkeypatch):
    import webapp.rooms as rooms_module

    monkeypatch.setattr(rooms_module, "CHAT_GAP_SECONDS", 0.0)
    room, token, _ = registry.create_room(Config(), "Alice")
    bob = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as alice_ws:
        assert alice_ws.receive_json()["type"] == "state"
        with client.websocket_connect(
            f"/ws/{room.code}?token={bob['token']}"
        ) as bob_ws:
            assert bob_ws.receive_json()["type"] == "state"
            alice_ws.send_json({"action": "chat", "text": "  your team stinks  "})
            for ws in (alice_ws, bob_ws):  # broadcast to every socket
                msg = _recv_until(ws, lambda m: m.get("type") == "chat")
                assert (msg["from"], msg["name"]) == (1, "Alice")
                assert msg["text"] == "your team stinks"  # stripped
            # Oversize text is truncated server-side, never rejected.
            alice_ws.send_json({"action": "chat", "text": "x" * 500})
            msg = _recv_until(bob_ws, lambda m: m.get("type") == "chat")
            assert len(msg["text"]) == 280
            # Rate limit: restore the real gap and fire twice fast.
            monkeypatch.setattr(rooms_module, "CHAT_GAP_SECONDS", 60.0)
            alice_ws.send_json({"action": "chat", "text": "again"})
            msg = _recv_until(alice_ws, lambda m: m.get("type") == "error")
            assert msg["message"] == "Easy — one message a second."
        # Reconnecting sockets get the ring buffer replayed.
        with client.websocket_connect(
            f"/ws/{room.code}?token={bob['token']}"
        ) as back_ws:
            assert back_ws.receive_json()["type"] == "state"
            history = back_ws.receive_json()
            assert history["type"] == "chat_history"
            assert [m["text"] for m in history["messages"]] == [
                "your team stinks", "x" * 280,
            ]
    # Tokenless spectators can read but never speak.
    with client.websocket_connect(f"/ws/{room.code}") as spec_ws:
        assert spec_ws.receive_json()["type"] == "state"
        assert spec_ws.receive_json()["type"] == "chat_history"
        spec_ws.send_json({"action": "chat", "text": "sneaky"})
        msg = _recv_until(spec_ws, lambda m: m.get("type") == "error")
        assert msg["message"] == "Join the room to talk trash."


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
        # bool is an int subclass — JSON true must never become a $1 bid
        # (it used to commit current_bid=True into state).
        ws.send_json({"action": "bid", "amount": True})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "bid", "increment": False})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "guess", "number": "seven"})  # malformed
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "guess", "number": 7})  # engine: no showdown
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
        # Star ratings are data-only — never serialized to any client.
        assert '"stars"' not in json.dumps(st)
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


# ------------------------------------------------- all-in showdown (lottery)


def _lottery_lot():
    return Lot(
        seq=1,
        player=make_players()[0],
        last_call=False,
        current_bid=3,
        leader_id=2,
        deadline=2000.0,
        lottery=Lottery(participants=(2, 3), guesses=((2, 87),)),
    )


def test_lottery_state_view_redacts_other_guesses():
    cfg = Config(budget=3)
    state = auction_state(
        cfg,
        (make_manager(1, cfg), make_manager(2, cfg), make_manager(3, cfg)),
        queue=(),
        lot=_lottery_lot(),
    )
    owner = views.state_view(state, 2)["lot"]["lottery"]
    assert owner == {"participants": [2, 3], "entered": [2], "your_guess": 87}
    # rival participant, non-participant manager, tokenless spectator: the
    # sentinel value 87 must be unreachable anywhere in their payloads.
    for viewer in (3, 1, None):
        view = views.state_view(state, viewer)
        assert view["lot"]["lottery"]["your_guess"] is None
        assert view["lot"]["lottery"]["entered"] == [2]
        assert "87" not in json.dumps(view)


def test_lottery_fx_translations():
    lot = _lottery_lot()
    assert views.fx_view(LotteryOpenedFx(lot)) == {
        "kind": "lottery_open",
        "participants": [2, 3],
        "amount": 3,
        "deadline": 2000.0,
    }
    assert views.fx_view(LotteryJoinedFx(lot, 4)) == {
        "kind": "lottery_joined",
        "manager": 4,
        "participants": [2, 3],
    }
    # who locked in is public — the number never rides a pre-reveal fx
    assert views.fx_view(LotteryGuessedFx(3)) == {
        "kind": "lottery_guessed",
        "manager": 3,
    }
    assert views.fx_view(LotteryCancelledFx(5)) == {
        "kind": "lottery_cancelled",
        "manager": 5,
    }
    assert views.fx_view(LotteryRevealFx(41, ((2, 40), (3, 90)), 2)) == {
        "kind": "lottery_reveal",
        "mystery": 41,
        "guesses": [{"manager": 2, "guess": 40}, {"manager": 3, "guess": 90}],
        "winner": 2,
    }


SECRET_KEYS = ("your_guess", "guess", "guesses", "mystery")


def _secret_pairs(node):
    """Every (key, value) under a guess-carrying key anywhere in a payload."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SECRET_KEYS:
                found.append((key, value))
            found.extend(_secret_pairs(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_secret_pairs(value))
    return found


def test_all_in_showdown_over_websockets(client):
    """Bob and Carol collide at their entire $3 budgets: the showdown opens,
    both lock a secret number, the real asyncio timer resolves it — reveal fx
    then the sale to the lottery winner in one batch. No socket ever sees
    another player's number (or the mystery) before its reveal frame."""
    cfg = Config(budget=3, lot_seconds=5, lottery_seconds=0.8, afk_lots=99,
                 snipe_window=0.05, snipe_extend=0.1, sim="off")
    room, token_a, uid_a = registry.create_room(cfg, "Alice")
    b = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    c = client.post(f"/api/rooms/{room.code}/join", json={"name": "Carol"}).json()
    uid_b, uid_c = b["user_id"], c["user_id"]

    def _lottery(m):
        return (_state(m).get("lot") or {}).get("lottery")

    seen_a: list[dict] = []
    seen_b: list[dict] = []
    seen_c: list[dict] = []
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={b['token']}") as ws_b, \
            client.websocket_connect(f"/ws/{room.code}?token={c['token']}") as ws_c:
        for ws, seen in ((ws_a, seen_a), (ws_b, seen_b), (ws_c, seen_c)):
            seen.append(ws.receive_json())
        ws_a.send_json({"action": "start"})
        for ws, seen in ((ws_a, seen_a), (ws_b, seen_b), (ws_c, seen_c)):
            _recv_until(ws, lambda m: _is_phase(m, "auction"), seen)

        # --- Bob goes all-in at his whole $3; Carol matches it exactly
        ws_b.send_json({"action": "bid", "amount": 3})
        _recv_until(ws_c, lambda m: _bid_at(m, 3), seen_c)
        ws_c.send_json({"action": "bid", "amount": 3})
        open_st = _recv_until(
            ws_a, lambda m: m.get("type") == "state" and _lottery(m), seen_a
        )
        lo = _lottery(open_st)
        assert lo["participants"] == [uid_b, uid_c]
        assert lo["entered"] == [] and lo["your_guess"] is None
        open_fx = _fx(
            _recv_until(ws_a, lambda m: "lottery_open" in _fx_kinds(m), seen_a),
            "lottery_open",
        )
        assert open_fx["participants"] == [uid_b, uid_c]
        assert open_fx["amount"] == 3
        assert open_fx["deadline"] == _state(open_st)["lot"]["deadline"]

        # --- a non-participant's guess is rejected privately
        ws_a.send_json({"action": "guess", "number": 50})
        err = _recv_until(ws_a, lambda m: m.get("type") == "error", seen_a)
        assert err["message"] == "You're not in this showdown."

        # --- both participants lock in; each acks only their own number
        ws_b.send_json({"action": "guess", "number": 13})
        _recv_until(
            ws_b,
            lambda m: m.get("type") == "state"
            and (_lottery(m) or {}).get("your_guess") == 13,
            seen_b,
        )
        guessed = _recv_until(
            ws_a, lambda m: "lottery_guessed" in _fx_kinds(m), seen_a
        )
        assert _fx(guessed, "lottery_guessed")["manager"] == uid_b
        ws_c.send_json({"action": "guess", "number": 87})
        _recv_until(
            ws_c,
            lambda m: m.get("type") == "state"
            and (_lottery(m) or {}).get("your_guess") == 87,
            seen_c,
        )
        both = _recv_until(
            ws_a,
            lambda m: m.get("type") == "state"
            and sorted((_lottery(m) or {}).get("entered", [])) == [uid_b, uid_c],
            seen_a,
        )
        assert _lottery(both)["your_guess"] is None  # Alice sees who, never what

        # --- the showdown timer fires: reveal then the sale, one fx batch
        reveal_msg = _recv_until(
            ws_a, lambda m: "lottery_reveal" in _fx_kinds(m), seen_a
        )
        _recv_until(ws_b, lambda m: "lottery_reveal" in _fx_kinds(m), seen_b)
        _recv_until(ws_c, lambda m: "lottery_reveal" in _fx_kinds(m), seen_c)
        kinds = _fx_kinds(reveal_msg)
        assert kinds.index("lottery_reveal") < kinds.index("sold")
        reveal = _fx(reveal_msg, "lottery_reveal")
        assert 1 <= reveal["mystery"] <= 100
        assert reveal["guesses"] == [
            {"manager": uid_b, "guess": 13},
            {"manager": uid_c, "guess": 87},
        ]
        dist_b = abs(13 - reveal["mystery"])
        dist_c = abs(87 - reveal["mystery"])
        if dist_b != dist_c:
            assert reveal["winner"] == (uid_b if dist_b < dist_c else uid_c)
        else:
            assert reveal["winner"] in (uid_b, uid_c)
        sold = _fx(reveal_msg, "sold")
        assert sold["manager"] == reveal["winner"] and sold["price"] == 3

        # --- the winner really bought the player at their whole budget
        post = _state(_recv_last_state(seen_a))
        winner = next(m for m in post["managers"] if m["id"] == reveal["winner"])
        assert winner["budget"] == 0
        assert any(s["player"] and s["price"] == 3 for s in winner["spots"])
        assert post["lot"]["lottery"] is None  # next lot dealt clean

    # --- pre-reveal privacy scan over EVERY frame each socket received:
    # the only guess-carrying key allowed is your own `your_guess`.
    for seen, allowed in (
        (seen_a, (None,)),
        (seen_b, (None, 13)),
        (seen_c, (None, 87)),
    ):
        reveal_at = next(
            i for i, m in enumerate(seen) if "lottery_reveal" in _fx_kinds(m)
        )
        for frame in seen[:reveal_at]:
            for key, value in _secret_pairs(frame):
                assert key == "your_guess" and value in allowed, (key, value)


# ---------------------------------------------------- the full scripted draft


def test_full_draft_over_websockets(client):
    """Create → join → start → bid war (sold) → passes → force-assigns →
    free pick (pool revealed) → lineup swap → completion + sim prompt.
    Every phase transition is driven by real asyncio timers firing."""
    cfg = Config(lot_seconds=0.4, lineup_seconds=0.6, afk_lots=99, sim="prompt",
                 snipe_window=0.05, snipe_extend=0.1)
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
    cfg = Config(lot_seconds=0.4, lineup_seconds=0.6, afk_lots=99, sim="prompt",
                 snipe_window=0.05, snipe_extend=0.1)
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


# ----------------------------------------------------------- cpu opponents


def test_create_room_with_cpus(client):
    resp = client.post("/api/rooms", json={"name": "Alice", "cpus": 2})
    assert resp.status_code == 200
    room = registry.get(resp.json()["room"])
    assert len(room.state.managers) == 3
    cpus = [m for m in room.state.managers if m.cpu]
    assert [m.user_id for m in cpus] == [-1, -2]
    assert all(m.budget == room.state.config.budget for m in cpus)

    summary = client.get(f"/api/rooms/{room.code}")
    assert summary.json()["managers"] == 3

    for bad in (-1, 9, "two", True):
        assert (
            client.post("/api/rooms", json={"name": "A", "cpus": bad}).status_code
            == 400
        ), bad


def test_add_and_remove_cpu_actions(client):
    room, token_a, _ = registry.create_room(Config(), "Alice")
    b = client.post(f"/api/rooms/{room.code}/join", json={"name": "Bob"}).json()
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={b['token']}") as ws_b:
        ws_a.receive_json()
        ws_b.receive_json()

        ws_b.send_json({"action": "add_cpu"})  # not the commissioner
        err = ws_b.receive_json()
        assert err["type"] == "error"
        assert err["message"] == "Only the commissioner can add CPUs."

        ws_a.send_json({"action": "add_cpu"})
        st = _recv_until(
            ws_a,
            lambda m: m.get("type") == "state"
            and len(_state(m)["managers"]) == 3,
        )
        cpu_view = next(m for m in _state(st)["managers"] if m["cpu"])
        assert cpu_view["id"] == -1
        assert cpu_view["name"] == "CPU 1"
        assert cpu_view["autopilot"] is False

        ws_a.send_json({"action": "remove_cpu", "cpu_id": -1})
        _recv_until(
            ws_a,
            lambda m: m.get("type") == "state"
            and len(_state(m)["managers"]) == 2,
        )
        assert room.state.manager(-1) is None

        ws_a.send_json({"action": "remove_cpu", "cpu_id": 2})  # Bob is human
        err = ws_a.receive_json()
        assert err["type"] == "error"
        assert err["message"] == "That's not a CPU manager."


def test_cpu_opponent_bids_and_draft_completes(client):
    """One human + one CPU, end to end. The room is built directly through
    the registry with a ~1.5s clock and a WIDE snipe window (the CPU brain
    times its bid inside the window, so the window must dwarf the 0.5s
    driver cadence). The commissioner adds the CPU over WS and starts; the
    per-room driver polls draftbot.cpu.decide and feeds its Bid/Pick events
    through normal dispatch. We poll WS frames (bounded by _recv_until's
    frame cap; engine timers keep frames flowing regardless of the CPU)
    until a state shows the CPU leading a lot or a sale lands on it. The
    human never acts: the AFK sweep flips her to autopilot, the CPU
    free-picks to a full roster, autofill covers hers, and the draft
    completes with every roster full."""
    cfg = Config(lot_seconds=1.5, snipe_window=1.2, snipe_extend=0.3,
                 afk_lots=2, free_pick_seconds=10, lineup_seconds=0,
                 sim="prompt")
    room, token, _ = registry.create_room(cfg, "Alice")
    seen: list[dict] = []
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        seen.append(ws.receive_json())
        ws.send_json({"action": "add_cpu"})
        st = _recv_until(
            ws,
            lambda m: m.get("type") == "state"
            and len(_state(m)["managers"]) == 2,
            seen,
        )
        cpu_mgr = next(m for m in _state(st)["managers"] if m["id"] != 1)
        assert cpu_mgr["cpu"] is True and cpu_mgr["id"] == -1

        ws.send_json({"action": "start"})
        _recv_until(ws, lambda m: _is_phase(m, "auction"), seen)

        def cpu_engaged(m):
            if m.get("type") == "state":
                if (_state(m).get("lot") or {}).get("leader") == -1:
                    return True
            return any(
                f["kind"] == "sold" and f["manager"] == -1
                for f in m.get("fx", [])
            )

        _recv_until(ws, cpu_engaged, seen)  # the CPU actually bid

        _recv_until(ws, lambda m: _is_phase(m, "complete"), seen)
        sim_msg = _recv_until(ws, lambda m: m.get("type") == "sim", seen)
        assert sim_msg["mode"] == "prompt"

    final = _state(_recv_last_state(seen))
    assert all(s["player"] for m in final["managers"] for s in m["spots"])
    # the CPU genuinely bought/picked (not just force-assign charity)
    assert any(
        e["manager"] == -1 and e["kind"] in ("sold", "pick")
        for e in final["log"]
    )
    # cpu flag is public wire data; the hidden queue still never leaks
    assert [m["cpu"] for m in final["managers"]] == [False, True]
    assert '"queue"' not in json.dumps(seen)


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
    )  # unknown token

    room.tokens["member-token"] = 2  # manager 2: member, not commissioner
    resp = client.post(
        f"/api/rooms/{room.code}/simulate", json={"token": "member-token"}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Only the commissioner can simulate."

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


# ------------------------------------------------------- draft modes: views


def test_snake_state_view_pool_turn_and_prices():
    cfg = Config(mode="snake")
    queue = tuple(
        replace(p, stars=s) for p, s in zip(make_players()[:4], (5, 3, 1, 4))
    )
    state = DraftState(
        config=cfg,
        commissioner_id=1,
        phase="snake",
        managers=(
            make_manager(1, cfg, budget=15),
            make_manager(2, cfg, budget=15),
        ),
        queue=queue,
        pick_deadline=1500.0,
    )
    view = views.state_view(state, 1)
    assert view["config"]["mode"] == "snake"
    # the open pool: full player cards plus the tier sticker price
    assert [p["price"] for p in view["pool"]] == [5, 3, 1, 4]
    assert all(p["name"] and p["id"] for p in view["pool"])
    assert view["turn"] == {"manager": 1, "deadline": 1500.0}
    assert view["lot"] is None and view["free_pick"] is None
    # price is the tier's one public face — "stars" itself never serializes
    assert '"stars"' not in json.dumps(view)
    # snaking order: with 2 picks made round 1 starts back at manager 2
    snaked = replace(
        state,
        managers=(
            make_manager(1, cfg, budget=10, filled=1),
            make_manager(2, cfg, budget=10, filled=1),
        ),
    )
    assert views.state_view(snaked, 1)["turn"]["manager"] == 2


def test_pool_and_turn_null_outside_snake_phase():
    view = views.state_view(DraftState(config=Config(), commissioner_id=1), 1)
    assert view["pool"] is None and view["turn"] is None
    assert view["config"]["mode"] == "auction"


def test_snake_turn_fx_translation():
    assert views.fx_view(SnakeTurnFx(-2, 1234.5)) == {
        "kind": "snake_turn",
        "manager": -2,
        "deadline": 1234.5,
    }


def test_blind_views_mask_exactly_the_dictated_spots():
    cfg = Config(mode="blind")
    p = make_players()[0]
    state = auction_state(
        cfg,
        (make_manager(1, cfg, filled=1), make_manager(2, cfg)),
        queue=make_players()[1:3],
        lot=Lot(seq=1, player=p, last_call=False, deadline=2000.0),
        log=(LogEntry("passed", p, None, 0), LogEntry("sold", p, 1, 3)),
    )
    view = views.state_view(state, 1)
    # (a) the live lot: stat card intact, identity FULLY null — dataset ids
    # are name slugs, so a real id would un-mask the card.
    assert view["lot"]["player"]["name"] is None
    assert view["lot"]["player"]["id"] is None
    assert view["lot"]["player"]["ppg"] == p.ppg
    # rostered spots always name their players for real
    mine = next(m for m in view["managers"] if m["id"] == 1)
    assert mine["spots"][0]["player"]["name"] == "Own 1-0"
    # (c) only the "passed" log entry is masked
    assert [e["player"] for e in view["log"]] == [None, p.name]
    # (b) the free-pick pool is a masked menu: ids and stats, no names
    fp_state = DraftState(
        config=cfg,
        commissioner_id=1,
        phase="free_pick",
        managers=(make_manager(1, cfg),),
        queue=make_players()[:3],
        pick_deadline=2000.0,
    )
    fp_pool = views.state_view(fp_state, 1, blind_salt="s3cret")["free_pick"]["pool"]
    real_ids = {x.id for x in make_players()[:3]}
    assert all(x["name"] is None and x["id"] for x in fp_pool)
    # ids are salted aliases: clickable, distinct, and never the name slug
    assert all(x["id"] not in real_ids for x in fp_pool)
    assert len({x["id"] for x in fp_pool}) == 3
    assert fp_pool[0]["id"] == views.blind_alias("s3cret", make_players()[0].id)
    # (d) fx: "passed" masked in blind only; rostering fx always real
    passed_view = views.fx_view(PassedFx(p), "blind")["player"]
    assert passed_view["name"] is None and passed_view["id"] is None
    assert views.fx_view(PassedFx(p))["player"]["name"] == p.name
    assert views.fx_view(SoldFx(p, 1, 3), "blind")["player"]["name"] == p.name
    assert (
        views.fx_view(ForceAssignedFx(p, 1), "blind")["player"]["name"]
        == p.name
    )
    assert views.fx_view(PickedFx(p, 1), "blind")["player"]["name"] == p.name


# ------------------------------------------------------ snake mode over WS


def _cheapest_feasible(s, uid):
    """The engine's feasibility rule read off the wire: affordable AND
    leaves $1 for every other still-empty slot."""
    mgr = next(m for m in s["managers"] if m["id"] == uid)
    empties = sum(1 for sp in mgr["spots"] if sp["player"] is None)
    feasible = [
        p
        for p in s["pool"]
        if p["price"] <= mgr["budget"]
        and mgr["budget"] - p["price"] >= empties - 1
    ]
    assert feasible  # the engine only stops the clock on feasible turns
    return min(feasible, key=lambda p: (p["price"], p["id"]))


def test_snake_draft_over_websockets(client):
    """A snake room created through the API and played to completion over
    real sockets: open priced pool, snaking turn order, private wrong-turn
    rejections, addtime on the snake clock, sticker-price charges, and the
    prompt-mode sim + /simulate at the end."""
    body = client.post(
        "/api/rooms",
        json={"name": "Alice", "mode": "snake", "lineup": 0, "sim": "prompt"},
    ).json()
    room = registry.get(body["room"])
    token_a, uid_a = body["token"], body["user_id"]
    joined = client.post(
        f"/api/rooms/{body['room']}/join", json={"name": "Bob"}
    ).json()
    token_b, uid_b = joined["token"], joined["user_id"]

    seen_a: list[dict] = []
    with client.websocket_connect(f"/ws/{body['room']}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{body['room']}?token={token_b}") as ws_b:
        seen_a.append(ws_a.receive_json())
        ws_b.receive_json()
        ws_a.send_json({"action": "start"})
        st = _recv_until(ws_a, lambda m: _is_phase(m, "snake"), seen_a)
        st_b = _recv_until(ws_b, lambda m: _is_phase(m, "snake"))
        s = _state(st)
        assert s["config"]["mode"] == "snake"
        assert all(m["budget"] == 15 for m in s["managers"])  # SNAKE_BUDGET
        assert s["lot"] is None and s["free_pick"] is None
        assert s["queue_count"] == 10 == len(s["pool"])
        # the pool is open to every socket, priced at the REAL star tiers
        assert {p["id"]: p["price"] for p in s["pool"]} == {
            p.id: snake_price(p) for p in room.state.queue
        }
        assert all(p["name"] and 1 <= p["price"] <= 5 for p in s["pool"])
        assert _state(st_b)["pool"] == s["pool"]
        # first turn: managers order, commissioner first — announced by fx
        assert s["turn"]["manager"] == uid_a
        turn_fx = _fx(
            _recv_until(ws_a, lambda m: "snake_turn" in _fx_kinds(m), seen_a),
            "snake_turn",
        )
        assert turn_fx["manager"] == uid_a
        assert turn_fx["deadline"] == s["turn"]["deadline"]

        # wrong-turn pick: rejected privately (Alice's stream is scanned
        # for error frames at the end of the test)
        ws_b.send_json({"action": "pick", "player_id": s["pool"][0]["id"]})
        err = _recv_until(ws_b, lambda m: m.get("type") == "error")
        assert err["message"] == "It's not your pick."

        # addtime extends the live snake clock
        before = s["turn"]["deadline"]
        ws_a.send_json({"action": "addtime", "seconds": 60})
        st = _recv_until(
            ws_a,
            lambda m: _is_phase(m, "snake")
            and _state(m)["turn"]["deadline"] > before + 30,
            seen_a,
        )
        assert _state(st)["turn"]["deadline"] == pytest.approx(before + 60)

        # Alice picks the cheapest feasible player at its sticker price...
        choice = _cheapest_feasible(_state(st), uid_a)
        ws_a.send_json({"action": "pick", "player_id": choice["id"]})
        st = _recv_until(
            ws_a,
            lambda m: m.get("type") == "state"
            and len(_state(m).get("pool") or ()) == 9,
            seen_a,
        )
        s = _state(st)
        mine = next(m for m in s["managers"] if m["id"] == uid_a)
        assert mine["budget"] == 15 - choice["price"]
        assert any(
            sp["player"]
            and sp["player"]["id"] == choice["id"]
            and sp["price"] == choice["price"]
            for sp in mine["spots"]
        )
        # ...and the turn snakes to Bob
        assert s["turn"]["manager"] == uid_b

        # drive it home: whoever is on the clock picks; infeasible turns
        # (engine-forced bargains) resolve inside the same commit
        for _ in range(12):
            if s["phase"] != "snake":
                break
            uid = s["turn"]["manager"]
            choice = _cheapest_feasible(s, uid)
            ws = ws_a if uid == uid_a else ws_b
            ws.send_json({"action": "pick", "player_id": choice["id"]})
            st = _recv_until(
                ws_a,
                lambda m, n=len(s["pool"]): m.get("type") == "state"
                and (
                    _state(m)["phase"] != "snake"
                    or len(_state(m)["pool"]) < n
                ),
                seen_a,
            )
            s = _state(st)
        assert s["phase"] == "complete"  # lineup window 0 → straight there
        assert all(sp["player"] for m in s["managers"] for sp in m["spots"])
        for m in s["managers"]:
            assert m["budget"] == 15 - sum(sp["price"] for sp in m["spots"])
        assert all(e["kind"] in ("pick", "force") for e in s["log"])
        assert all(e["player"] for e in s["log"])  # snake never masks names

        # completion flows: the sim prompt arrives, /simulate re-runs it
        sim_msg = _recv_until(ws_a, lambda m: m.get("type") == "sim", seen_a)
        assert sim_msg["mode"] == "prompt"
        resp = client.post(
            f"/api/rooms/{body['room']}/simulate", json={"token": token_a}
        )
        assert resp.status_code == 200
        _recv_until(ws_a, lambda m: m.get("type") == "sim", seen_a)

    dump = json.dumps(seen_a)
    assert '"stars"' not in dump and '"queue"' not in dump
    # Bob's wrong-turn rejection stayed private — Alice saw zero errors
    assert all(m.get("type") != "error" for m in seen_a)


def test_snake_with_cpu_forced_flows_complete(client):
    """Alice + a CPU in snake mode. The CPU brain sits idle during snake,
    so every CPU turn is resolved by the engine's turn-timer autopick —
    the forced flow — while Alice picks on her own turns. The draft must
    complete with full rosters either way."""
    cfg = Config(
        mode="snake", lot_seconds=0.8, lineup_seconds=0, sim="off", afk_lots=99
    )
    room, token, _ = registry.create_room(cfg, "Alice")
    seen: list[dict] = []
    with client.websocket_connect(f"/ws/{room.code}?token={token}") as ws:
        seen.append(ws.receive_json())
        ws.send_json({"action": "add_cpu"})
        _recv_until(
            ws,
            lambda m: m.get("type") == "state"
            and len(_state(m)["managers"]) == 2,
            seen,
        )
        ws.send_json({"action": "start"})
        st = _recv_until(ws, lambda m: _is_phase(m, "snake"), seen)
        s = _state(st)
        for _ in range(30):
            if s["phase"] != "snake":
                break
            if s["turn"]["manager"] == 1:
                choice = _cheapest_feasible(s, 1)
                ws.send_json({"action": "pick", "player_id": choice["id"]})
            # CPU on the clock: its turn autopicks when the snake timer
            # fires — either way, wait for the next resolving commit
            st = _recv_until(
                ws,
                lambda m, n=len(s["pool"]): m.get("type") == "state"
                and (
                    _state(m)["phase"] != "snake"
                    or len(_state(m)["pool"]) < n
                ),
                seen,
            )
            s = _state(st)
        assert s["phase"] == "complete"

    # the CPU's turns were announced and resolved without a human
    assert any(
        f.get("kind") == "snake_turn" and f.get("manager") == -1
        for m in seen
        for f in m.get("fx", [])
    )
    final = _state(_recv_last_state(seen))
    assert all(sp["player"] for m in final["managers"] for sp in m["spots"])
    assert all(e["kind"] in ("pick", "force") for e in final["log"])
    assert any(e["manager"] == -1 for e in final["log"])
    assert '"stars"' not in json.dumps(seen)


# ------------------------------------------------------ blind mode over WS


def test_blind_draft_masks_names_until_rostered(client):
    """Blind mode over real sockets: the live lot rides the wire nameless,
    the sale is the reveal, passes stay masked forever (fx + log), the
    free-pick pool is a masked menu — while rostered names, autofill, the
    sim prompt, and /simulate stay fully real."""
    cfg = Config(mode="blind", lot_seconds=0.4, lineup_seconds=0, afk_lots=99,
                 sim="prompt", snipe_window=0.05, snipe_extend=0.1)
    room, token_a, uid_a = registry.create_room(cfg, "Alice")
    joined = client.post(
        f"/api/rooms/{room.code}/join", json={"name": "Bob"}
    ).json()
    token_b, uid_b = joined["token"], joined["user_id"]

    seen_a: list[dict] = []
    with client.websocket_connect(f"/ws/{room.code}?token={token_a}") as ws_a, \
            client.websocket_connect(f"/ws/{room.code}?token={token_b}") as ws_b:
        seen_a.append(ws_a.receive_json())
        ws_b.receive_json()
        ws_a.send_json({"action": "start"})
        st = _recv_until(ws_a, lambda m: _is_phase(m, "auction"), seen_a)
        _recv_until(ws_b, lambda m: _is_phase(m, "auction"))
        s = _state(st)
        assert s["config"]["mode"] == "blind"
        # (a) the live lot is a mystery card: stats yes, name null
        assert s["lot"]["player"]["name"] is None
        assert s["lot"]["player"]["pos"] in ("PG", "SG", "SF", "PF", "C")
        real_name = room.state.lot.player.name  # server-side truth
        assert real_name

        # --- Alice buys lot 1 at $3: the sale IS the reveal
        ws_a.send_json({"action": "bid", "amount": 3})
        _recv_until(ws_a, lambda m: _bid_at(m, 3), seen_a)
        sold_msg = _recv_until(ws_a, lambda m: "sold" in _fx_kinds(m), seen_a)
        sold = _fx(sold_msg, "sold")
        assert sold["manager"] == uid_a
        assert sold["player"]["name"] == real_name
        post = _state(_recv_last_state(seen_a))
        mine = next(m for m in post["managers"] if m["id"] == uid_a)
        assert any(
            sp["player"] and sp["player"]["name"] == real_name
            for sp in mine["spots"]
        )  # rostered on the board under the real name
        assert any(
            e["kind"] == "sold" and e["player"] == real_name
            for e in post["log"]
        )

        # --- lots pass unbid: masked in the fx and in the log, forever
        passed_msg = _recv_until(
            ws_a, lambda m: "passed" in _fx_kinds(m), seen_a
        )
        assert _fx(passed_msg, "passed")["player"]["name"] is None
        post_pass = _state(_recv_last_state(seen_a))
        assert any(
            e["kind"] == "passed" and e["player"] is None
            for e in post_pass["log"]
        )

        # --- first LAST CALL force-assigns to Bob; force fx names for real
        force_msg = _recv_until(ws_a, lambda m: "force" in _fx_kinds(m), seen_a)
        assert _fx(force_msg, "force")["manager"] == uid_b
        assert _fx(force_msg, "force")["player"]["name"]

        # --- Bob leaves; the next force goes to Alice, then FREE PICK
        _recv_until(ws_b, lambda m: "force" in _fx_kinds(m))
        ws_b.send_json({"action": "leave"})
        _recv_until(ws_a, lambda m: "autopilot" in _fx_kinds(m), seen_a)
        force2 = _recv_until(ws_a, lambda m: "force" in _fx_kinds(m), seen_a)
        assert _fx(force2, "force")["manager"] == uid_a
        fp = next(m for m in reversed(seen_a) if _is_phase(m, "free_pick"))
        pool = _state(fp)["free_pick"]["pool"]
        assert len(pool) == 7
        # (b) the revealed pool is a masked menu: ids and stats, no names
        assert all(p["name"] is None and p["id"] for p in pool)

        # --- picking blind from the pool reveals the player on the roster
        for _ in range(2):
            ws_a.send_json({"action": "pick", "player_id": pool[0]["id"]})
            picked_msg = _recv_until(
                ws_a, lambda m: "picked" in _fx_kinds(m), seen_a
            )
            assert _fx(picked_msg, "picked")["player"]["name"]
            fp2 = next(m for m in reversed(seen_a) if _is_phase(m, "free_pick"))
            pool = _state(fp2)["free_pick"]["pool"]
            assert all(p["name"] is None for p in pool)
        # the 3rd pick fills Alice; Bob autofills and the draft completes
        # (lineup window 0) — one commit, one fx batch
        ws_a.send_json({"action": "pick", "player_id": pool[0]["id"]})
        last_msg = _recv_until(
            ws_a, lambda m: "autofill" in _fx_kinds(m), seen_a
        )
        assert _fx(last_msg, "picked")["player"]["name"]
        assert all(
            a["player"]["name"]
            for a in _fx(last_msg, "autofill")["assignments"]
        )
        assert "complete" in _fx_kinds(last_msg)

        # --- completion flows: sim prompt speaks real names; /simulate works
        sim_msg = _recv_until(ws_a, lambda m: m.get("type") == "sim", seen_a)
        assert sim_msg["mode"] == "prompt"
        assert real_name in sim_msg["share_prompt"]
        resp = client.post(
            f"/api/rooms/{room.code}/simulate", json={"token": token_a}
        )
        assert resp.status_code == 200
        _recv_until(ws_a, lambda m: m.get("type") == "sim", seen_a)

    # every frame Alice ever received: live lots nameless, free-pick pools
    # nameless, "passed" nameless everywhere — everything rostered named
    dump = json.dumps(seen_a)
    assert '"stars"' not in dump and '"queue"' not in dump
    for frame in seen_a:
        if frame.get("type") == "state":
            s = _state(frame)
            if s.get("lot"):
                assert s["lot"]["player"]["name"] is None
            if s.get("free_pick"):
                assert all(
                    p["name"] is None for p in s["free_pick"]["pool"]
                )
            for e in s["log"]:
                assert (e["player"] is None) == (e["kind"] == "passed")
        for f in frame.get("fx", []):
            if f["kind"] == "passed":
                assert f["player"]["name"] is None
