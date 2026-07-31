"""Room registry + per-room dispatch mirroring ``DraftBot.apply_event``.

Concurrency discipline copied from ``draftbot.bot``: engine.apply + state
commit + timer arm/cancel happen under the room lock (commit order); the
state broadcast and fx/error fan-out happen outside it. Timer tasks fire
``TimerExpired`` back through dispatch with the armed deadline echoed, so
the engine's stale-timer guard works exactly as on Discord.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import secrets
import time
from dataclasses import dataclass, field, replace
from typing import Any

from draftbot import cpu, engine, sim
from draftbot.models import (
    ArmTimerFx,
    CancelTimerFx,
    CompleteFx,
    Config,
    DraftState,
    Effect,
    ErrorFx,
    Event,
    Join,
    TimerExpired,
)

from . import views

log = logging.getLogger("webapp")

# Unambiguous room-code alphabet: A-Z2-9 minus I/O (look like 1/0).
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 4
ROOM_TTL_SECONDS = 3600.0
# Phases where humans are mid-game — the sweep must never strand them.
LIVE_PHASES = ("auction", "free_pick", "snake", "lineup")
CHAT_HISTORY = 50  # ring buffer replayed to (re)connecting sockets
CHAT_GAP_SECONDS = 1.0  # per-manager send rate limit


class JoinError(Exception):
    """The engine rejected a join — carries the player-facing message."""


def _log_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed", exc_info=exc)


@dataclass
class Room:
    """Mutable per-room holder. The immutable ``DraftState`` inside is the
    only game truth; everything else is web bookkeeping."""

    code: str
    state: DraftState
    rng: random.Random
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sockets: dict[Any, int | None] = field(default_factory=dict)  # ws -> uid
    tokens: dict[str, int] = field(default_factory=dict)  # token -> uid
    timer_task: asyncio.Task | None = None
    sim_task: asyncio.Task | None = None
    cpu_task: asyncio.Task | None = None
    next_user_id: int = 1
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # Per-room salt for blind-mode masked-card wire ids (views.blind_alias).
    blind_salt: str = field(default_factory=lambda: secrets.token_hex(8))
    # Room chat: social, not game state — never touches the engine.
    chat: list[dict[str, Any]] = field(default_factory=list)
    chat_last: dict[int, float] = field(default_factory=dict)  # uid -> last ts


class RoomRegistry:
    def __init__(self) -> None:
        self.rooms: dict[str, Room] = {}

    # ------------------------------------------------------------ lifecycle

    def create_room(self, config: Config, creator_name: str) -> tuple[Room, str, int]:
        """New room with the creator auto-joined as manager 1 (commissioner).

        Pure + synchronous: the room isn't shared yet, so the creator's Join
        applies without the lock (mirrors the bot's create-time auto-join)."""
        self._sweep()
        code = self._new_code()
        rng = random.Random()
        state = DraftState(config=config, commissioner_id=1)
        state, _ = engine.apply(state, Join(1, creator_name), rng)
        room = Room(code=code, state=state, rng=rng, next_user_id=2)
        token = secrets.token_urlsafe(16)
        room.tokens[token] = 1
        self.rooms[code] = room
        return room, token, 1

    def get(self, code: str) -> Room | None:
        return self.rooms.get(code.upper())

    async def join(self, room: Room, name: str) -> tuple[str, int]:
        """Join through the engine (it enforces lobby rules and capacity).
        Returns ``(token, user_id)`` or raises ``JoinError``."""
        user_id = room.next_user_id
        room.next_user_id += 1
        effects = await self.dispatch(room, Join(user_id, name))
        for fx in effects:
            if isinstance(fx, ErrorFx):
                raise JoinError(fx.message)
        token = secrets.token_urlsafe(16)
        room.tokens[token] = user_id
        return token, user_id

    def _new_code(self) -> str:
        while True:
            code = "".join(
                secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH)
            )
            if code not in self.rooms:
                return code

    def _sweep(self) -> None:
        """Evict rooms idle past the TTL (covers finished drafts and dead
        lobbies). A live draft is never evicted while paused (paused rooms
        stop refreshing ``last_active``) or while any socket is connected;
        evicted rooms close their sockets so no client plays a ghost room."""
        now = time.time()
        for code, room in list(self.rooms.items()):
            if now - room.last_active <= ROOM_TTL_SECONDS:
                continue
            if room.state.phase in LIVE_PHASES and (
                room.state.paused or room.sockets
            ):
                continue
            for task in (room.timer_task, room.sim_task, room.cpu_task):
                if task is not None:
                    task.cancel()
            del self.rooms[code]
            if room.sockets:
                self._spawn(self._close_sockets(room))

    async def _close_sockets(self, room: Room) -> None:
        for ws in list(room.sockets):
            room.sockets.pop(ws, None)
            try:
                await ws.send_json(
                    {
                        "type": "error",
                        "message": "This room has expired — start a new draft.",
                    }
                )
                await ws.close()
            except Exception:
                pass  # dead socket — eviction proceeds regardless

    # ------------------------------------------------------------- dispatch

    async def dispatch(self, room: Room, event: Event) -> list[Effect]:
        """The only path for state changes: apply + commit + timer effects
        under the lock (engine.apply is pure and synchronous — no awaits
        between read and commit); broadcast outside it."""
        async with room.lock:
            old_state = room.state
            new_state, effects = engine.apply(old_state, event, room.rng)
            room.state = new_state
            room.last_active = time.time()
            effects = self._apply_timer_effects(room, effects)
            changed = new_state is not old_state
        if changed:  # engine errors return the same state object untouched
            await self._broadcast_state(room)
        await self._send_effects(room, effects)
        if any(isinstance(fx, CompleteFx) for fx in effects):
            self.start_sim(room)
        self._ensure_cpu_driver(room)
        return effects

    def _apply_timer_effects(
        self, room: Room, effects: list[Effect]
    ) -> list[Effect]:
        """Arm/cancel timers at commit time (under ``room.lock``, in commit
        order — arming after awaited I/O would let a slow earlier transition
        clobber a newer transition's fresh timer) and strip the timer
        effects from what gets broadcast."""
        remaining: list[Effect] = []
        for fx in effects:
            if isinstance(fx, ArmTimerFx):
                self._arm_timer(room, fx.kind, fx.lot_seq, fx.deadline)
            elif isinstance(fx, CancelTimerFx):
                self._cancel_timer(room)
            else:
                remaining.append(fx)
        return remaining

    # --------------------------------------------------------------- timers

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        task.add_done_callback(_log_task_result)
        return task

    def _arm_timer(
        self, room: Room, kind: str, lot_seq: int, deadline: float
    ) -> None:
        old = room.timer_task
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        room.timer_task = self._spawn(self._timer(room, kind, lot_seq, deadline))

    def _cancel_timer(self, room: Room) -> None:
        old = room.timer_task
        if old is not None and old is not asyncio.current_task():
            old.cancel()
        room.timer_task = None

    async def _timer(
        self, room: Room, kind: str, lot_seq: int, deadline: float
    ) -> None:
        await asyncio.sleep(max(0.0, deadline - time.time()))
        event = TimerExpired(
            kind=kind, lot_seq=lot_seq, deadline=deadline, now=time.time()
        )
        await self.dispatch(room, event)

    async def add_time(self, room: Room, user_id: int, seconds: float) -> str | None:
        """/draft addtime parity: not an engine event — extend the live
        deadline in place under the lock (same shape as a Resume) and re-arm
        the timer, exactly like the bot's ``draft_addtime``. Returns an
        error message, or None on success."""
        async with room.lock:
            state = room.state
            if user_id != state.commissioner_id:
                return "Only the commissioner can add time."
            if state.paused:
                return "The draft is paused — resume first."
            if state.phase == "auction" and state.lot is not None:
                lot = replace(state.lot, deadline=state.lot.deadline + seconds)
                room.state = replace(state, lot=lot)
                self._arm_timer(room, "lot", lot.seq, lot.deadline)
            elif state.phase in ("free_pick", "snake"):
                # Both phases clock off pick_deadline; the timer kind must
                # match what the engine armed or the expiry event is stale.
                deadline = state.pick_deadline + seconds
                room.state = replace(state, pick_deadline=deadline)
                kind = "snake" if state.phase == "snake" else "pick"
                self._arm_timer(room, kind, -1, deadline)
            else:
                return "There's no live timer to extend."
            room.last_active = time.time()
        await self._broadcast_state(room)
        return None

    # ------------------------------------------------------------ cpu driver

    def _ensure_cpu_driver(self, room: Room) -> None:
        """Start the per-room CPU task when the draft is live and any manager
        is a CPU (called after every commit, so it also self-heals). Same
        shape as the timer task; it exits on complete/cancelled and is
        cancelled on eviction."""
        if room.cpu_task is not None and not room.cpu_task.done():
            return
        if room.state.phase not in LIVE_PHASES:
            return
        if not any(m.cpu for m in room.state.managers):
            return
        room.cpu_task = self._spawn(self._cpu_driver(room))

    async def _cpu_driver(self, room: Room) -> None:
        """Poll each CPU brain OUTSIDE the lock (room.state is an immutable
        snapshot) and submit its chosen events through the normal dispatch —
        the engine stays the single authority; stale decisions just bounce
        off its guards like any late human click. Runs through every live
        phase; during snake the brain currently sits idle and CPU turns
        resolve by the engine's turn-timer autopick."""
        while room.state.phase in LIVE_PHASES:
            delays = [cpu.IDLE_DELAY]
            for m in room.state.managers:
                if not m.cpu:
                    continue
                event, delay = cpu.decide(room.state, m.user_id, time.time())
                delays.append(delay)
                if event is not None:
                    await self.dispatch(room, event)
            await asyncio.sleep(max(0.5, min(delays)))

    # ------------------------------------------------------------ broadcast

    async def _broadcast_state(self, room: Room) -> None:
        """Full per-viewer state view to every socket after a commit. The
        top-level ``now`` (server epoch seconds) lets clients compute a
        clock offset for countdown rendering."""
        state = room.state
        now = time.time()
        for ws, viewer_id in list(room.sockets.items()):
            await self._send(
                room,
                ws,
                {
                    "type": "state",
                    "now": now,
                    "state": views.state_view(state, viewer_id, room.blind_salt),
                },
            )

    async def post_chat(self, room: Room, uid: int, text: str) -> str | None:
        """Validate + broadcast one chat line; returns an error string or
        None. Chat is room-level social data — the engine never sees it."""
        m = room.state.manager(uid)
        if m is None or m.cpu:
            return "Join the room to talk trash."
        now = time.time()
        if now - room.chat_last.get(uid, 0.0) < CHAT_GAP_SECONDS:
            return "Easy — one message a second."
        room.chat_last[uid] = now
        msg = {"from": uid, "name": m.name, "text": text, "at": now}
        room.chat.append(msg)
        del room.chat[:-CHAT_HISTORY]
        room.last_active = now
        await self._broadcast(room, {"type": "chat", **msg})
        return None

    async def _send_effects(self, room: Room, effects: list[Effect]) -> None:
        mode = room.state.config.mode  # fixed at creation — safe post-commit
        payloads = [
            v for fx in effects if (v := views.fx_view(fx, mode)) is not None
        ]
        if payloads:
            await self._broadcast(room, {"type": "fx", "fx": payloads})
        for fx in effects:
            if isinstance(fx, ErrorFx):  # private: only the acting user
                message = {"type": "error", "message": fx.message}
                for ws, viewer_id in list(room.sockets.items()):
                    if viewer_id == fx.user_id:
                        await self._send(room, ws, message)

    async def _broadcast(self, room: Room, message: dict[str, Any]) -> None:
        for ws in list(room.sockets):
            await self._send(room, ws, message)

    async def _send(self, room: Room, ws: Any, message: dict[str, Any]) -> None:
        try:
            await ws.send_json(message)
        except Exception:  # dead socket — drop it, never crash dispatch
            room.sockets.pop(ws, None)

    # ------------------------------------------------------------------ sim

    def start_sim(self, room: Room) -> None:
        """Kick the post-draft sim in a task so dispatch is never blocked."""
        if room.state.config.sim == "off":
            return
        room.sim_task = self._spawn(self._run_sim(room))

    async def _run_sim(self, room: Room) -> None:
        state = room.state
        mode = state.config.sim
        teams = views.teams_for_sim(state)
        if mode == "prompt":
            await self._broadcast(
                room,
                {
                    "type": "sim",
                    "mode": "prompt",
                    "share_prompt": sim.share_prompt(teams),
                },
            )
            return
        run_mode, note = mode, ""
        if mode == "ai" and not os.environ.get("LLM_API_KEY"):
            run_mode, note = "stats", "no LLM key — stats-only ranking"
        try:
            if run_mode == "ai":
                result = await sim.run_ai(teams)
            else:
                result = sim.run_stats(teams)
        except sim.SimError as exc:
            await self._broadcast(
                room, {"type": "sim", "mode": mode, "error": str(exc)}
            )
            return
        except Exception:
            log.exception("tournament sim crashed for room %s", room.code)
            await self._broadcast(
                room,
                {
                    "type": "sim",
                    "mode": mode,
                    "error": "The tournament sim crashed — try again.",
                },
            )
            return
        payload: dict[str, Any] = {
            "type": "sim",
            "mode": mode,
            "standings": [[name, score] for name, score in result.standings],
            "champion": result.champion,
            "summary": result.summary,
        }
        if note:
            payload["note"] = note
        await self._broadcast(room, payload)
