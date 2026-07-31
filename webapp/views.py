"""Redaction layer: what each client may see (PROTOCOL.md).

CRITICAL: ``state.queue`` contents are NEVER serialized — the hidden pool is
the whole game. Two exceptions, both public by design: the free-pick phase
(Discord parity: ``FreePickFx`` reveals the pool to everyone) and snake
mode, where the queue IS the open pool. Blind mode inverts the trick: the
payload shapes are unchanged but a player's NAME serializes as null until
that player is rostered — the wire must never carry a masked name.
"""
from __future__ import annotations

import hashlib
from typing import Any

# Deliberate private import: turn order is a game rule and may not be
# reimplemented in the web layer (PROTOCOL.md) — deriving the on-turn
# manager from state keeps reconnect views correct without duplicating it.
from draftbot.engine import _snake_on_turn
from draftbot.models import (
    AutoFilledFx,
    AutopilotFx,
    CancelledFx,
    CompleteFx,
    DraftState,
    Effect,
    ForceAssignedFx,
    LineupPhaseFx,
    LogEntry,
    Lot,
    LotteryCancelledFx,
    LotteryGuessedFx,
    LotteryJoinedFx,
    LotteryOpenedFx,
    LotteryRevealFx,
    Manager,
    PassedFx,
    PausedFx,
    PickedFx,
    Player,
    ResumedFx,
    SnakeTurnFx,
    SoldFx,
    Spot,
    snake_price,
)
from draftbot.sim import TeamDict

LOG_LIMIT = 20


# -------------------------------------------------------------- serializers


def blind_alias(salt: str, player_id: str) -> str:
    """Opaque per-room wire id for a masked player. Dataset ids are name
    slugs ("bradley-beal"), so a masked card must never carry the real id —
    the salt keeps the alias unguessable and pick-resolvable server-side."""
    digest = hashlib.sha1(f"{salt}:{player_id}".encode()).hexdigest()
    return f"b{digest[:10]}"


def player_view(
    p: Player, mask_name: bool = False, alias: str | None = None
) -> dict[str, Any]:
    """Shared player serializer — used by lot, rosters, pool, and fx.
    ``mask_name`` (blind mode) hides the identity but keeps the stat card:
    name nulled, id replaced by ``alias`` (None unless the card must stay
    clickable, i.e. the blind free-pick pool)."""
    return {
        "id": alias if mask_name else p.id,
        "name": None if mask_name else p.name,
        "pos": p.pos,
        "team": p.team,
        "ppg": p.ppg,
        "rpg": p.rpg,
        "apg": p.apg,
        "decade": p.decade,
        "prime": p.prime,
    }


def _spot_view(s: Spot) -> dict[str, Any]:
    return {
        "slot": s.slot,
        "player": player_view(s.player) if s.player is not None else None,
        "price": s.price,
    }


def _manager_view(m: Manager) -> dict[str, Any]:
    return {
        "id": m.user_id,
        "name": m.name,
        "budget": m.budget,
        "autopilot": m.autopilot,
        "cpu": m.cpu,
        "spots": [_spot_view(s) for s in m.spots],
    }


def _lot_view(
    lot: Lot, viewer_id: int | None, mask_name: bool
) -> dict[str, Any]:
    # Showdown redaction: `entered` (who locked in) is public — it mirrors
    # LotteryGuessedFx — but the only guess VALUE ever serialized is the
    # viewer's own. Rivals' numbers stay server-side until LotteryRevealFx.
    lottery: dict[str, Any] | None = None
    if lot.lottery is not None:
        lottery = {
            "participants": list(lot.lottery.participants),
            "entered": [uid for uid, _ in lot.lottery.guesses],
            "your_guess": next(
                (g for uid, g in lot.lottery.guesses if uid == viewer_id), None
            ),
        }
    return {
        "seq": lot.seq,
        "player": player_view(lot.player, mask_name),
        "last_call": lot.last_call,
        "current_bid": lot.current_bid,
        "leader": lot.leader_id,
        "deadline": lot.deadline,
        "lottery": lottery,
    }


def _log_view(entry: LogEntry, mask_names: bool) -> dict[str, Any]:
    # Blind: "passed" is the one log kind whose player is still unrostered
    # (recycled into the hidden queue), so its name stays masked; every
    # other kind logs a rostered player and names them for real.
    return {
        "kind": entry.kind,
        "player": (
            None
            if mask_names and entry.kind == "passed"
            else entry.player.name
        ),
        "manager": entry.manager_id,
        "price": entry.price,
    }


# --------------------------------------------------------------- state view


def state_view(
    state: DraftState, viewer_id: int | None, blind_salt: str = ""
) -> dict[str, Any]:
    """Per-viewer wire state. Queue contents are unreachable from here except
    as the revealed pool during ``free_pick`` (reveal is public).
    ``blind_salt`` is the room's alias salt for masked-but-clickable cards."""
    cfg = state.config
    mask = cfg.mode == "blind"  # names hidden until rostered (view-only rule)
    free_pick: dict[str, Any] | None = None
    if state.phase == "free_pick":
        # The picker is the single active manager (FreePickFx carries it at
        # transition time; deriving it here keeps reconnects correct).
        actives = state.active_managers
        free_pick = {
            "picker": actives[0].user_id if actives else None,
            "pool": [
                player_view(p, mask, blind_alias(blind_salt, p.id))
                for p in state.queue
            ],
            "deadline": state.pick_deadline,
        }
    pool: list[dict[str, Any]] | None = None
    turn: dict[str, Any] | None = None
    if state.phase == "snake":
        # Snake's pool is deliberately open, sticker price included: price
        # equals the star tier, the one place that tier reaches the wire
        # ("stars" itself is still never serialized).
        pool = [
            {**player_view(p), "price": snake_price(p)} for p in state.queue
        ]
        turn = {
            "manager": _snake_on_turn(state).user_id,
            "deadline": state.pick_deadline,
        }
    return {
        "phase": state.phase,
        "you": viewer_id,
        "commissioner": state.commissioner_id,
        "config": {
            "budget": cfg.budget,
            "lot_seconds": cfg.lot_seconds,
            "era_start": cfg.era_start,
            "era_end": cfg.era_end,
            "pool_depth": cfg.pool_depth,
            "sim": cfg.sim,
            "lineup_seconds": cfg.lineup_seconds,
            "mode": cfg.mode,
        },
        "queue_count": len(state.queue),
        "managers": [_manager_view(m) for m in state.managers],
        "lot": (
            _lot_view(state.lot, viewer_id, mask)
            if state.phase == "auction" and state.lot is not None
            else None
        ),
        "free_pick": free_pick,
        "pool": pool,
        "turn": turn,
        "pick_deadline": state.pick_deadline,
        "lineup_deadline": state.lineup_deadline,
        "paused": state.paused,
        "log": [_log_view(e, mask) for e in state.log[-LOG_LIMIT:]],
    }


# ----------------------------------------------------------------- fx view


def fx_view(effect: Effect, mode: str = "auction") -> dict[str, Any] | None:
    """Translate one engine effect to a wire ``fx`` payload (PROTOCOL.md).

    Returns ``None`` for effects the state broadcast already covers (lot
    opened, bid placed, board/lobby refresh, free-pick reveal, timers) and
    for ``ErrorFx`` (routed privately by the dispatcher). ``mode`` is the
    room's ``config.mode``; blind's "passed" is the one mode-dependent
    payload — that player returns to the hidden queue unrostered, so the
    name stays masked."""
    if isinstance(effect, SoldFx):
        return {
            "kind": "sold",
            "player": player_view(effect.player),
            "manager": effect.manager_id,
            "price": effect.price,
        }
    if isinstance(effect, PassedFx):
        return {
            "kind": "passed",
            "player": player_view(effect.player, mode == "blind"),
        }
    if isinstance(effect, ForceAssignedFx):
        return {
            "kind": "force",
            "player": player_view(effect.player),
            "manager": effect.manager_id,
        }
    if isinstance(effect, PickedFx):
        return {
            "kind": "picked",
            "player": player_view(effect.player),
            "manager": effect.manager_id,
        }
    if isinstance(effect, AutoFilledFx):
        return {
            "kind": "autofill",
            "assignments": [
                {"manager": manager_id, "player": player_view(p)}
                for manager_id, p in effect.assignments
            ],
        }
    if isinstance(effect, LotteryOpenedFx):
        lo = effect.lot.lottery
        return {
            "kind": "lottery_open",
            "participants": list(lo.participants) if lo is not None else [],
            "amount": effect.lot.current_bid,
            "deadline": effect.lot.deadline,
        }
    if isinstance(effect, LotteryJoinedFx):
        lo = effect.lot.lottery
        return {
            "kind": "lottery_joined",
            "manager": effect.manager_id,
            "participants": list(lo.participants) if lo is not None else [],
        }
    if isinstance(effect, LotteryGuessedFx):
        # WHO locked in is public; the number never rides an fx pre-reveal.
        return {"kind": "lottery_guessed", "manager": effect.manager_id}
    if isinstance(effect, LotteryCancelledFx):
        return {"kind": "lottery_cancelled", "manager": effect.manager_id}
    if isinstance(effect, LotteryRevealFx):
        # The reveal is the moment guesses go public — full list on purpose.
        return {
            "kind": "lottery_reveal",
            "mystery": effect.mystery,
            "guesses": [
                {"manager": uid, "guess": g} for uid, g in effect.guesses
            ],
            "winner": effect.winner_id,
        }
    if isinstance(effect, SnakeTurnFx):
        return {
            "kind": "snake_turn",
            "manager": effect.manager_id,
            "deadline": effect.deadline,
        }
    if isinstance(effect, LineupPhaseFx):
        return {"kind": "lineup_open", "deadline": effect.deadline}
    if isinstance(effect, CompleteFx):
        return {"kind": "complete"}
    if isinstance(effect, PausedFx):
        return {"kind": "paused"}
    if isinstance(effect, ResumedFx):
        return {"kind": "resumed"}
    if isinstance(effect, CancelledFx):
        return {"kind": "cancelled"}
    if isinstance(effect, AutopilotFx):
        return {"kind": "autopilot", "manager": effect.manager_id}
    return None


# ------------------------------------------------------------------ sim I/O


def teams_for_sim(state: DraftState) -> list[TeamDict]:
    """Sim input per sim.run_stats/run_ai; manager names deduped defensively
    (same shape the Discord layer feeds the sim — serialization, not rules)."""
    seen: set[str] = set()
    teams: list[TeamDict] = []
    for m in state.managers:
        name = m.name if m.name not in seen else f"{m.name} ({m.user_id % 1000})"
        seen.add(name)
        teams.append(
            {
                "manager": name,
                "players": [
                    {
                        "slot": s.slot,
                        "name": s.player.name,
                        "pos": s.player.pos,
                        "ppg": s.player.ppg,
                        "rpg": s.player.rpg,
                        "apg": s.player.apg,
                        "stars": s.player.stars,
                        "decade": s.player.decade,
                        "prime": s.player.prime,
                    }
                    for s in m.spots
                    if s.player is not None
                ],
            }
        )
    return teams
