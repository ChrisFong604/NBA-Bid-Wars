"""Integrity checks for the committed cross-era player dataset (DESIGN §4)."""
from __future__ import annotations

from collections import Counter

import pytest

from draftbot.dataset import DECADES, filter_by_era, load_players
from draftbot.models import SLOTS, Player


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
