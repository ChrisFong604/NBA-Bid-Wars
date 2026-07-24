"""Discord-free checks of the UI layer: DynamicItem custom_id templates,
persistent-view timeouts, and token-free imports of the bot module."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from draftbot import ui
from draftbot.models import Config, DraftState, Lot, Manager, Player, Spot

# (class, sample constructor args) — every registered DynamicItem.
DYNAMIC_CASES = [
    (ui.JoinButton, (123,)),
    (ui.LeaveButton, (123,)),
    (ui.QuickBidButton, (123, 7, 5)),
    (ui.CustomBidButton, (123, 7)),
    (ui.ConfirmBidButton, (123, 7, 19)),
]


def test_dynamic_cases_cover_registry():
    assert {cls for cls, _ in DYNAMIC_CASES} == set(ui.DYNAMIC_ITEMS)


@pytest.mark.parametrize("cls,args", DYNAMIC_CASES, ids=lambda v: getattr(v, "__name__", ""))
def test_dynamic_item_template_matches_own_custom_id(cls, args):
    item = cls(*args)
    pattern = cls.__discord_ui_compiled_template__  # compiled at class creation
    match = pattern.fullmatch(item.custom_id)
    assert match is not None, f"{cls.__name__} custom_id doesn't match its template"
    # from_custom_id must rebuild an equivalent item from the match groups.
    rebuilt = asyncio.run(cls.from_custom_id(None, None, match))
    assert rebuilt.custom_id == item.custom_id


def test_dynamic_templates_are_mutually_exclusive():
    """No custom_id may match two templates — dispatch would be ambiguous."""
    items = [cls(*args) for cls, args in DYNAMIC_CASES]
    for item in items:
        matchers = [
            cls.__name__
            for cls, _ in DYNAMIC_CASES
            if cls.__discord_ui_compiled_template__.fullmatch(item.custom_id)
        ]
        assert matchers == [type(item).__name__]


def test_persistent_views_never_time_out():
    # The default View(timeout=180) would silently kill buttons mid-auction.
    assert ui.lobby_view(1).timeout is None
    assert ui.bid_view(1, 2, (1, 2, 5)).timeout is None
    assert ui.confirm_view(1, 2, 10).timeout is None


def test_bid_view_layout():
    view = ui.bid_view(42, 3, (1, 2, 5))
    labels = [b.item.label for b in view.children]  # DynamicItem wraps Button
    assert labels == ["+$1", "+$2", "+$5", "Custom…"]


JORDAN = Player(
    id="jordan", name="Michael Jordan", team="CHI", pos="SG",
    ppg=31.5, rpg=6.3, apg=5.5, stars=5, decade=1990, prime="1989–1993",
)


def test_era_label():
    assert ui.era_label(1960, 2020) == "1960s–2020s"
    assert ui.era_label(2000, 2000) == "2000s"


def test_decade_tag():
    assert ui.decade_tag(1960) == "'60s"
    assert ui.decade_tag(1990) == "'90s"
    assert ui.decade_tag(2000) == "'00s"
    assert ui.decade_tag(2020) == "'20s"


def test_lobby_embed_shows_era_range():
    state = DraftState(
        config=Config(era_start=1970, era_end=1990), commissioner_id=1
    )
    field = next(f for f in ui.lobby_embed(state).fields if f.name == "Config")
    assert "Eras: **1970s–1990s**" in field.value


def test_lobby_embed_single_era_reads_as_one_decade():
    state = DraftState(
        config=Config(era_start=2000, era_end=2000), commissioner_id=1
    )
    field = next(f for f in ui.lobby_embed(state).fields if f.name == "Config")
    assert "Eras: **2000s**" in field.value


def test_lot_embed_description_has_prime_era_flavor_on_one_line():
    lot = Lot(seq=1, player=JORDAN, last_call=False, deadline=1_000.0)
    embed = ui.lot_embed(lot, pool_left=10)
    assert embed.description == (
        "CHI · prime 1989–1993 ('90s) · 31.5/6.3/5.5 · ⭐⭐⭐⭐⭐"
    )
    assert "\n" not in embed.description


def test_stat_line_omits_era_chunk_for_pre_era_player():
    old = Player(
        id="p", name="Old Snapshot Guy", team="TST", pos="C",
        ppg=10.0, rpg=5.0, apg=2.0, stars=2,  # decade/prime defaults
    )
    lot = Lot(seq=1, player=old, last_call=False, deadline=1_000.0)
    assert ui.lot_embed(lot, pool_left=3).description == "TST · 10/5/2 · ⭐⭐"


def test_pool_embeds_tag_each_player_with_decade():
    pool = tuple(
        Player(
            id=f"p{d}", name=f"P{d}", team="TST", pos="PG",
            ppg=10.0, rpg=4.0, apg=3.0, stars=3, decade=d, prime="x",
        )
        for d in (1960, 1990, 2020)
    )
    text = "\n".join(e.description for e in ui.pool_embeds(pool))
    for tag in ("'60s", "'90s", "'20s"):
        assert tag in text


def _one_manager_state() -> DraftState:
    spots = (Spot(slot="SG", player=JORDAN, price=7),) + tuple(
        Spot(slot=s) for s in ("PG", "SF", "PF", "C")
    )
    manager = Manager(user_id=1, name="Chris", budget=13, spots=spots)
    return DraftState(config=Config(), commissioner_id=1, managers=(manager,))


def test_status_roster_lines_include_decade_tag():
    state = _one_manager_state()
    embed = ui.status_embed(state, state.managers[0])
    assert "SG Jordan '90s ($7)" in embed.fields[0].value


def test_teams_for_sim_carries_decade_and_prime():
    teams = ui.teams_for_sim(_one_manager_state())
    (player,) = teams[0]["players"]
    assert player["decade"] == 1990
    assert player["prime"] == "1989–1993"


def test_bot_module_imports_without_any_token():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DISCORD_TOKEN", "ANTHROPIC_API_KEY", "TEST_GUILD_ID")
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import draftbot.bot, draftbot.__main__"],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert proc.returncode == 0, proc.stderr
