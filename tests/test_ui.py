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
    (ui.LineupButton, (123,)),
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
    assert ui.lineup_view(1).timeout is None


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


def test_lobby_embed_shows_flat_clock_and_sim_mode():
    state = DraftState(
        config=Config(lot_seconds=45, sim="stats"), commissioner_id=1
    )
    field = next(f for f in ui.lobby_embed(state).fields if f.name == "Config")
    assert "clock **45s flat**" in field.value
    assert "stats only" in field.value
    dump = field.value.lower()
    assert "hammer" not in dump and "opening window" not in dump


def test_lot_embed_final_seconds_warning():
    lot = Lot(
        seq=1, player=JORDAN, last_call=False,
        current_bid=5, leader_id=2, deadline=1_000.0,
    )
    embed = ui.lot_embed(lot, pool_left=10, final_seconds=True)
    status = next(f.value for f in embed.fields if f.name == "Status")
    assert "FINAL SECONDS" in status and "<t:1000:R>" in status
    assert embed.color.value == ui.ORANGE


def test_lot_embed_flat_clock_no_hammer_wording():
    lot = Lot(
        seq=1, player=JORDAN, last_call=False,
        current_bid=5, leader_id=2, deadline=1_000.0,
    )
    embed = ui.lot_embed(lot, pool_left=10)
    status = next(f.value for f in embed.fields if f.name == "Status")
    assert status == "Sells <t:1000:R>"
    dump = str(embed.to_dict()).lower()
    assert "hammer" not in dump and "opening window" not in dump
    assert "no sniping" in embed.footer.text


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


def test_config_default_sim_is_prompt():
    assert Config().sim == "prompt"


# ------------------------------------------------------------------- lineup


def _full_manager() -> Manager:
    """Five filled slots; Jordan sits out of position at PG."""
    others = tuple(
        Spot(
            slot=s,
            player=Player(
                id=f"q{s}", name=f"{s} Guy", team="TST", pos=s,
                ppg=10.0, rpg=4.0, apg=3.0, stars=3, decade=2000, prime="x",
            ),
            price=1,
        )
        for s in ("SG", "SF", "PF", "C")
    )
    return Manager(
        user_id=1, name="Chris", budget=5,
        spots=(Spot(slot="PG", player=JORDAN, price=7),) + others,
    )


def test_lineup_open_embed_has_both_timestamp_forms():
    # Mobile clients don't tick <t:..:R>, so :T must appear alongside it.
    embed = ui.lineup_open_embed(1000.0)
    assert embed.title == "🔀 Lineups open — all rosters are full!"
    assert "<t:1000:R>" in embed.description
    assert "<t:1000:T>" in embed.description


def test_lineup_panel_embed_shows_all_five_slots_and_lock_clock():
    lines = ui.lineup_panel_embed(_full_manager(), 1000.0).description.split("\n")
    assert lines[0] == "**PG** — Michael Jordan (natural SG, '90s)"
    slot_lines = [line for line in lines if line.startswith("**")]
    assert [line.split("**")[1] for line in slot_lines] == ["PG", "SG", "SF", "PF", "C"]
    # Mobile clients don't tick <t:..:R> — the absolute :T form must be there.
    assert "<t:1000:R>" in lines[-1] and "<t:1000:T>" in lines[-1]


def test_lineup_panel_view_selects():
    view = ui.LineupPanelView(123, 1, _full_manager())
    assert view.timeout == 90  # ephemeral panel — must NOT be persistent
    assert [o.value for o in view.move_select.options] == list(
        ("PG", "SG", "SF", "PF", "C")
    )
    assert view.move_select.options[0].label == "Jordan (natural SG)"
    assert [o.value for o in view.into_select.options] == list(
        ("PG", "SG", "SF", "PF", "C")
    )


def test_prompt_messages_short_text_is_one_fenced_message():
    messages = ui.prompt_messages("paste me\ninto your LLM")
    assert messages == ["```\npaste me\ninto your LLM\n```"]
    assert len(messages[0]) < 2000


def test_prompt_messages_long_text_splits_on_line_boundaries():
    lines = [f"line {i:03d} " + "x" * 90 for i in range(50)]  # ~5,000 chars
    text = "\n".join(lines)
    messages = ui.prompt_messages(text)
    assert len(messages) > 1
    bodies = []
    for i, message in enumerate(messages, 1):
        assert len(message) < 2000
        assert message.count("```") == 2  # balanced fences in every chunk
        marker, fenced = message.split("\n", 1)
        assert marker == f"(part {i}/{len(messages)})"
        assert fenced.startswith("```\n") and fenced.endswith("\n```")
        bodies.append(fenced[4:-4])
    # Re-joining the fenced bodies rebuilds the text exactly — every split
    # landed on a line boundary and nothing was lost.
    assert "\n".join(bodies) == text


def test_bot_module_imports_without_any_token():
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DISCORD_TOKEN", "LLM_API_KEY", "LLM_BASE_URL", "SIM_MODEL", "TEST_GUILD_ID")
    }
    proc = subprocess.run(
        [sys.executable, "-c", "import draftbot.bot, draftbot.__main__"],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    assert proc.returncode == 0, proc.stderr
