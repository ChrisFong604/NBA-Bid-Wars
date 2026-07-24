"""Load the shipped NBA player dataset (``draftbot/data/players.json``).

DESIGN §4: the dataset is built offline and committed; there is no runtime
API dependency. The pool spans every decade from the 1960s to the 2020s —
each player appears once, anchored to the decade of their prime, with
prime-years ``ppg/rpg/apg`` (the only stats recorded in every era). Star
ratings are **editorial and era-relative**, stored in the JSON; they are
validated here but never recomputed from stats.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from draftbot.models import SLOTS, Player

_DATA_PATH = Path(__file__).parent / "data" / "players.json"

#: Valid prime-decade anchors (DESIGN §1/§4): the 1960s through the 2020s.
DECADES: tuple[int, ...] = (1960, 1970, 1980, 1990, 2000, 2010, 2020)


def load_players() -> tuple[Player, ...]:
    """Read the committed dataset into immutable ``Player`` objects."""
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{_DATA_PATH}: expected a JSON array of players")
    players = tuple(_player_from(row) for row in raw)
    ids = [p.id for p in players]
    if len(set(ids)) != len(ids):
        dupes = sorted(i for i in set(ids) if ids.count(i) > 1)
        raise ValueError(f"{_DATA_PATH}: duplicate player ids: {dupes}")
    return players


def filter_by_era(
    players: Sequence[Player], era_start: int, era_end: int
) -> tuple[Player, ...]:
    """Players whose prime decade falls in ``[era_start, era_end]`` inclusive.

    Bounds are decade anchors (e.g. 1990, 2020). A reversed range
    (``era_start > era_end``) selects nothing and returns an empty tuple.
    """
    return tuple(p for p in players if era_start <= p.decade <= era_end)


def _player_from(row: object) -> Player:
    if not isinstance(row, dict):
        raise ValueError(f"{_DATA_PATH}: rows must be objects, got {type(row).__name__}")
    try:
        player = Player(
            id=str(row["id"]),
            name=str(row["name"]),
            team=str(row["team"]),
            pos=str(row["pos"]),
            ppg=float(row["ppg"]),
            rpg=float(row["rpg"]),
            apg=float(row["apg"]),
            stars=int(row["stars"]),
            decade=int(row["decade"]),
            prime=str(row["prime"]),
        )
    except KeyError as exc:
        raise ValueError(f"{_DATA_PATH}: player row missing field {exc}") from exc
    if player.pos not in SLOTS:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has invalid pos {player.pos!r}")
    if not 1 <= player.stars <= 5:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has invalid stars {player.stars}")
    if player.decade not in DECADES:
        raise ValueError(
            f"{_DATA_PATH}: {player.name!r} has invalid decade {player.decade}"
        )
    if not player.prime:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has empty prime range")
    return player
