"""Load the shipped NBA player dataset (``draftbot/data/players.json``).

DESIGN §4: the dataset is built offline and committed; there is no runtime
API dependency. The pool spans every decade from the 1960s to the 2020s —
each player appears once, anchored to the decade of their prime, with
prime-years ``ppg/rpg/apg`` (the only stats recorded in every era). Star
ratings are **generated and era-relative** (see ``scripts/build_dataset.py``),
stored in the JSON; they are validated here but never recomputed from stats.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from draftbot.models import SLOTS, Player

_DATA_PATH = Path(__file__).parent / "data" / "players.json"

#: Valid prime-decade anchors (DESIGN §1/§4): the 1960s through the 2020s.
DECADES: tuple[int, ...] = (1960, 1970, 1980, 1990, 2000, 2010, 2020)

#: ``Config.pool_depth`` -> maximum caliber ``Player.rank`` kept in the pool.
DEPTH_RANKS: dict[str, int] = {"legends": 4, "household": 7, "deep": 10}


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
    # Pre-rank files (every row missing "rank") default everyone to 10 and
    # skip the completeness check; the shipped file always carries ranks.
    if any("rank" in row for row in raw):
        _validate_ranks(players)
    return players


def filter_by_era(
    players: Sequence[Player], era_start: int, era_end: int
) -> tuple[Player, ...]:
    """Players whose prime decade falls in ``[era_start, era_end]`` inclusive.

    Bounds are decade anchors (e.g. 1990, 2020). A reversed range
    (``era_start > era_end``) selects nothing and returns an empty tuple.
    """
    return tuple(p for p in players if era_start <= p.decade <= era_end)


def filter_by_depth(players: Sequence[Player], depth: str) -> tuple[Player, ...]:
    """Players whose caliber ``rank`` clears the ``Config.pool_depth`` cutoff.

    ``legends`` keeps ranks 1-4 (~20 per era), ``household`` 1-7 (~35),
    ``deep`` everyone. Unknown depths raise ``ValueError``.
    """
    if depth not in DEPTH_RANKS:
        expected = ", ".join(sorted(DEPTH_RANKS))
        raise ValueError(f"unknown pool depth {depth!r}; expected one of: {expected}")
    cutoff = DEPTH_RANKS[depth]
    return tuple(p for p in players if p.rank <= cutoff)


def _validate_ranks(players: Sequence[Player]) -> None:
    """Each decade x position bucket must carry each rank 1..N exactly once."""
    buckets: dict[tuple[int, str], list[int]] = {}
    for p in players:
        buckets.setdefault((p.decade, p.pos), []).append(p.rank)
    for (decade, pos), ranks in sorted(buckets.items()):
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError(
                f"{_DATA_PATH}: {decade}s {pos} ranks must be 1..{len(ranks)} "
                f"exactly once, got {sorted(ranks)}"
            )


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
            # Pre-rank files omit the key; the Player default (10) applies.
            rank=int(row.get("rank", 10)),
        )
    except KeyError as exc:
        raise ValueError(f"{_DATA_PATH}: player row missing field {exc}") from exc
    if player.pos not in SLOTS:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has invalid pos {player.pos!r}")
    if not 1 <= player.stars <= 5:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has invalid stars {player.stars}")
    if not 1 <= player.rank <= 10:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has invalid rank {player.rank}")
    if player.decade not in DECADES:
        raise ValueError(
            f"{_DATA_PATH}: {player.name!r} has invalid decade {player.decade}"
        )
    if not player.prime:
        raise ValueError(f"{_DATA_PATH}: {player.name!r} has empty prime range")
    return player
