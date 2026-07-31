"""Redaction layer: what each client may see (PROTOCOL.md).

CRITICAL: ``state.queue`` contents are NEVER serialized — the hidden pool is
the whole game. The one exception is the free-pick phase, where the pool is
publicly revealed (Discord parity: ``FreePickFx`` reveals it to everyone).
"""
from __future__ import annotations

from typing import Any

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
    SoldFx,
    Spot,
)
from draftbot.sim import TeamDict

LOG_LIMIT = 20


# -------------------------------------------------------------- serializers


def player_view(p: Player) -> dict[str, Any]:
    """Shared player serializer — used by lot, rosters, pool, and fx."""
    return {
        "id": p.id,
        "name": p.name,
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


def _lot_view(lot: Lot, viewer_id: int | None) -> dict[str, Any]:
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
        "player": player_view(lot.player),
        "last_call": lot.last_call,
        "current_bid": lot.current_bid,
        "leader": lot.leader_id,
        "deadline": lot.deadline,
        "lottery": lottery,
    }


def _log_view(entry: LogEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind,
        "player": entry.player.name,
        "manager": entry.manager_id,
        "price": entry.price,
    }


# --------------------------------------------------------------- state view


def state_view(state: DraftState, viewer_id: int | None) -> dict[str, Any]:
    """Per-viewer wire state. Queue contents are unreachable from here except
    as the revealed pool during ``free_pick`` (reveal is public)."""
    cfg = state.config
    free_pick: dict[str, Any] | None = None
    if state.phase == "free_pick":
        # The picker is the single active manager (FreePickFx carries it at
        # transition time; deriving it here keeps reconnects correct).
        actives = state.active_managers
        free_pick = {
            "picker": actives[0].user_id if actives else None,
            "pool": [player_view(p) for p in state.queue],
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
        },
        "queue_count": len(state.queue),
        "managers": [_manager_view(m) for m in state.managers],
        "lot": (
            _lot_view(state.lot, viewer_id)
            if state.phase == "auction" and state.lot is not None
            else None
        ),
        "free_pick": free_pick,
        "pick_deadline": state.pick_deadline,
        "lineup_deadline": state.lineup_deadline,
        "paused": state.paused,
        "log": [_log_view(e) for e in state.log[-LOG_LIMIT:]],
    }


# ----------------------------------------------------------------- fx view


def fx_view(effect: Effect) -> dict[str, Any] | None:
    """Translate one engine effect to a wire ``fx`` payload (PROTOCOL.md).

    Returns ``None`` for effects the state broadcast already covers (lot
    opened, bid placed, board/lobby refresh, free-pick reveal, timers) and
    for ``ErrorFx`` (routed privately by the dispatcher)."""
    if isinstance(effect, SoldFx):
        return {
            "kind": "sold",
            "player": player_view(effect.player),
            "manager": effect.manager_id,
            "price": effect.price,
        }
    if isinstance(effect, PassedFx):
        return {"kind": "passed", "player": player_view(effect.player)}
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
