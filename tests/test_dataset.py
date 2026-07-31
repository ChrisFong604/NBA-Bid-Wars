"""Integrity checks for the committed cross-era player dataset (DESIGN §4)."""
from __future__ import annotations

import json
import random
from collections import Counter

import pytest

from draftbot import dataset, engine
from draftbot.dataset import DECADES, filter_by_depth, filter_by_era, load_players
from draftbot.models import SLOTS, Config, Player


@pytest.fixture(scope="module")
def players() -> tuple[Player, ...]:
    return load_players()


def test_loads_the_full_350_player_pool(players: tuple[Player, ...]) -> None:
    assert len(players) == 350


def test_every_decade_has_exactly_50_players(
    players: tuple[Player, ...],
) -> None:
    counts = Counter(p.decade for p in players)
    for decade in DECADES:
        assert counts[decade] == 50, f"{decade}s: {counts[decade]} players"


def test_every_decade_has_at_least_10_per_position(
    players: tuple[Player, ...],
) -> None:
    """5 stars + 5 quality role players per decade x position (1:1 rule)."""
    counts = Counter((p.decade, p.pos) for p in players)
    for decade in DECADES:
        for slot in SLOTS:
            n = counts[(decade, slot)]
            assert n >= 10, f"{decade}s {slot}: only {n} players"


def test_single_decade_supports_a_ten_manager_draft(
    players: tuple[Player, ...],
) -> None:
    """Era-sweep feasibility: any one-decade era range must field 10 managers
    x 5 slots — 50 players with every position coverable 10 times."""
    for decade in DECADES:
        pool = filter_by_era(players, decade, decade)
        assert len(pool) >= 50, f"{decade}s: only {len(pool)} players"
        per_pos = Counter(p.pos for p in pool)
        for slot in SLOTS:
            assert per_pos[slot] >= 10, f"{decade}s {slot}: {per_pos[slot]} < 10"


def test_ids_unique(players: tuple[Player, ...]) -> None:
    ids = [p.id for p in players]
    assert len(set(ids)) == len(ids)


def test_ids_are_ascii_slugs(players: tuple[Player, ...]) -> None:
    for p in players:
        assert p.id.isascii(), f"{p.name}: non-ascii id {p.id!r}"
        assert p.id == p.id.lower(), f"{p.name}: id not lowercase {p.id!r}"


def test_names_unique(players: tuple[Player, ...]) -> None:
    names = [p.name for p in players]
    assert len(set(names)) == len(names)


def test_every_pos_is_a_valid_slot(players: tuple[Player, ...]) -> None:
    for p in players:
        assert p.pos in SLOTS, f"{p.name}: bad pos {p.pos!r}"


def test_decades_are_valid_anchors(players: tuple[Player, ...]) -> None:
    for p in players:
        assert p.decade in DECADES, f"{p.name}: bad decade {p.decade}"


def test_primes_are_nonempty_display_ranges(players: tuple[Player, ...]) -> None:
    for p in players:
        assert p.prime, f"{p.name}: empty prime"
        assert "–" in p.prime, f"{p.name}: prime {p.prime!r} missing en dash"


def test_stars_in_range(players: tuple[Player, ...]) -> None:
    for p in players:
        assert 1 <= p.stars <= 5, f"{p.name}: stars {p.stars}"


def test_every_decade_has_the_full_star_pyramid(
    players: tuple[Player, ...],
) -> None:
    """Era-relative stars: each decade carries every tier from 1 to 5."""
    for decade in DECADES:
        tiers = {p.stars for p in players if p.decade == decade}
        assert tiers == {1, 2, 3, 4, 5}, f"{decade}s: tiers {sorted(tiers)}"


def test_every_decade_has_at_least_8_cheap_gambles(
    players: tuple[Player, ...],
) -> None:
    """Every era needs 1-2 star role players/journeymen (DESIGN §4)."""
    for decade in DECADES:
        cheap = [p for p in players if p.decade == decade and p.stars <= 2]
        assert len(cheap) >= 8, f"{decade}s: only {len(cheap)} players at 1-2 stars"


def test_stats_within_sane_bounds(players: tuple[Player, ...]) -> None:
    for p in players:
        assert 0.0 <= p.ppg <= 45.0, f"{p.name}: ppg {p.ppg}"
        assert 0.0 <= p.rpg <= 26.0, f"{p.name}: rpg {p.rpg}"
        # 14.5 admits John Stockton's real 3-year peak (14.1 apg, 1988–1991).
        assert 0.0 <= p.apg <= 14.5, f"{p.name}: apg {p.apg}"


# -------------------------------------------------------------- caliber rank


def test_ranks_complete_within_every_bucket(players: tuple[Player, ...]) -> None:
    """rank = 1-based caliber order: every decade x position carries 1..10."""
    for decade in DECADES:
        for slot in SLOTS:
            ranks = sorted(
                p.rank for p in players if p.decade == decade and p.pos == slot
            )
            assert ranks == list(range(1, 11)), f"{decade}s {slot}: {ranks}"


def _row(i: int, **overrides: object) -> dict:
    base = {
        "id": f"p{i}", "name": f"Player {i}", "team": "TST", "pos": "PG",
        "ppg": 10.0, "rpg": 5.0, "apg": 5.0, "stars": 3,
        "decade": 1990, "prime": "1991–1993",
    }
    base.update(overrides)
    return base


def _patch_dataset(tmp_path, monkeypatch, rows: list[dict]) -> None:
    path = tmp_path / "players.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dataset, "_DATA_PATH", path)


def test_pre_rank_file_loads_with_default_rank_10(tmp_path, monkeypatch) -> None:
    """Old-file tolerance: no "rank" anywhere -> default 10, no bucket check."""
    _patch_dataset(tmp_path, monkeypatch, [_row(1), _row(2)])
    loaded = load_players()
    assert [p.rank for p in loaded] == [10, 10]


def test_rank_out_of_range_rejected(tmp_path, monkeypatch) -> None:
    _patch_dataset(tmp_path, monkeypatch, [_row(1, rank=11)])
    with pytest.raises(ValueError, match="invalid rank"):
        load_players()


def test_duplicate_rank_in_bucket_rejected(tmp_path, monkeypatch) -> None:
    _patch_dataset(tmp_path, monkeypatch, [_row(1, rank=1), _row(2, rank=1)])
    with pytest.raises(ValueError, match="exactly once"):
        load_players()


# ---------------------------------------------------------- filter_by_depth


def test_depth_legends_composition(players: tuple[Player, ...]) -> None:
    pool = filter_by_depth(players, "legends")
    assert len(pool) == 140  # 7 decades x 20
    per_decade = Counter(p.decade for p in pool)
    per_bucket = Counter((p.decade, p.pos) for p in pool)
    for decade in DECADES:
        assert per_decade[decade] == 20, f"{decade}s: {per_decade[decade]}"
        for slot in SLOTS:
            assert per_bucket[(decade, slot)] == 4


def test_depth_household_composition(players: tuple[Player, ...]) -> None:
    pool = filter_by_depth(players, "household")
    assert len(pool) == 245  # 7 decades x 35
    per_decade = Counter(p.decade for p in pool)
    per_bucket = Counter((p.decade, p.pos) for p in pool)
    for decade in DECADES:
        assert per_decade[decade] == 35, f"{decade}s: {per_decade[decade]}"
        for slot in SLOTS:
            assert per_bucket[(decade, slot)] == 7


def test_depth_deep_keeps_everyone(players: tuple[Player, ...]) -> None:
    assert filter_by_depth(players, "deep") == players


def test_depth_unknown_raises(players: tuple[Player, ...]) -> None:
    with pytest.raises(ValueError, match="pool depth"):
        filter_by_depth(players, "shallow")


def test_full_range_legends_supports_ten_managers(
    players: tuple[Player, ...],
) -> None:
    pool = filter_by_depth(players, "legends")
    built = engine.build_pool(pool, 10, Config(), random.Random(1))
    assert len(built) == 50  # 10 managers x 5 slots


def test_single_decade_legends_supports_exactly_four_managers(
    players: tuple[Player, ...],
) -> None:
    """Legends mode on one decade is 4 per position = 20 players: a 4-manager
    pool (5*4=20) builds, a 5th manager tips it over."""
    for decade in DECADES:
        pool = filter_by_depth(filter_by_era(players, decade, decade), "legends")
        assert len(pool) == 20, f"{decade}s: {len(pool)}"
    pool = filter_by_depth(filter_by_era(players, 1990, 1990), "legends")
    built = engine.build_pool(pool, 4, Config(), random.Random(1))
    assert len(built) == 20
    with pytest.raises(ValueError, match="not enough players"):
        engine.build_pool(pool, 5, Config(), random.Random(1))


# ------------------------------------------------------------ filter_by_era


def test_filter_single_decade(players: tuple[Player, ...]) -> None:
    pool = filter_by_era(players, 1990, 1990)
    assert pool
    assert all(p.decade == 1990 for p in pool)


def test_filter_inclusive_bounds(players: tuple[Player, ...]) -> None:
    pool = filter_by_era(players, 1970, 1990)
    assert {p.decade for p in pool} == {1970, 1980, 1990}


def test_filter_full_range_keeps_everyone(players: tuple[Player, ...]) -> None:
    assert filter_by_era(players, 1960, 2020) == players


def test_filter_reversed_bounds_is_empty(players: tuple[Player, ...]) -> None:
    assert filter_by_era(players, 2020, 1960) == ()


def test_filter_preserves_order(players: tuple[Player, ...]) -> None:
    pool = filter_by_era(players, 2000, 2010)
    expected = tuple(p for p in players if p.decade in (2000, 2010))
    assert pool == expected
