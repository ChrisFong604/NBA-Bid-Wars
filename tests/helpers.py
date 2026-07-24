"""Shared factories for the engine/store/invariant tests."""
from __future__ import annotations

import random

from draftbot import engine
from draftbot.models import (
    Config,
    DraftState,
    Join,
    Lot,
    Manager,
    Player,
    Spot,
    Start,
)

POSITIONS = ("PG", "SG", "SF", "PF", "C")


def make_players(per_pos: int = 10) -> tuple[Player, ...]:
    out: list[Player] = []
    n = 0
    for pos in POSITIONS:
        for _ in range(per_pos):
            out.append(
                Player(
                    id=f"p{n}",
                    name=f"Player {n}",
                    team="TST",
                    pos=pos,
                    ppg=10.0,
                    rpg=4.0,
                    apg=3.0,
                    stars=3,
                )
            )
            n += 1
    return tuple(out)


def make_manager(
    uid: int,
    cfg: Config,
    budget: int | None = None,
    filled: int = 0,
    autopilot: bool = False,
    last_action_lot: int = 0,
) -> Manager:
    spots = tuple(
        Spot(
            slot=s,
            player=Player(
                id=f"own-{uid}-{i}",
                name=f"Own {uid}-{i}",
                team="TST",
                pos="PG",
                ppg=1.0,
                rpg=1.0,
                apg=1.0,
                stars=1,
            ),
            price=0,
        )
        if i < filled
        else Spot(slot=s)
        for i, s in enumerate(cfg.slots)
    )
    return Manager(
        user_id=uid,
        name=f"M{uid}",
        budget=cfg.budget if budget is None else budget,
        spots=spots,
        autopilot=autopilot,
        last_action_lot=last_action_lot,
    )


def auction_state(
    cfg: Config,
    managers: tuple[Manager, ...],
    queue: tuple[Player, ...],
    lot: Lot,
    **kw,
) -> DraftState:
    return DraftState(
        config=cfg,
        commissioner_id=managers[0].user_id,
        phase="auction",
        managers=managers,
        queue=queue,
        lot=lot,
        lot_seq=lot.seq,
        **kw,
    )


def start_draft(n: int = 2, cfg: Config | None = None, seed: int = 1):
    """Lobby -> join managers with uids 1..n (commissioner is 1) -> start."""
    cfg = cfg or Config()
    state = DraftState(config=cfg, commissioner_id=1)
    for uid in range(1, n + 1):
        state, _ = engine.apply(state, Join(uid, f"M{uid}"))
    rng = random.Random(seed)
    state, fx = engine.apply(
        state, Start(user_id=1, players=make_players(), now=1000.0), rng
    )
    return state, fx, rng


def fx_of(effects, cls):
    return [e for e in effects if isinstance(e, cls)]
