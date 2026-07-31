"""Discord-free checks of the UI layer: DynamicItem custom_id templates,
persistent-view timeouts, and token-free imports of the bot module."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import replace

import pytest

from draftbot import cpu, engine, ui
from draftbot.models import (
    AddCpu,
    Bid,
    Config,
    DraftState,
    Join,
    Lot,
    Lottery,
    LotteryGuess,
    LotteryRevealFx,
    Manager,
    Pick,
    Player,
    RemoveCpu,
    Spot,
    Swap,
)

# (class, sample constructor args) — every registered DynamicItem.
DYNAMIC_CASES = [
    (ui.JoinButton, (123,)),
    (ui.LeaveButton, (123,)),
    (ui.QuickBidButton, (123, 7, 5)),
    (ui.CustomBidButton, (123, 7)),
    (ui.LotteryPickButton, (123, 7)),
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
    assert ui.lottery_view(1, 2).timeout is None


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
    # No star rating anywhere on the card — data-only, it would bias bids.
    assert embed.description == "CHI · prime 1989–1993 ('90s) · 31.5/6.3/5.5"
    assert "\n" not in embed.description


def test_stat_line_omits_era_chunk_for_pre_era_player():
    old = Player(
        id="p", name="Old Snapshot Guy", team="TST", pos="C",
        ppg=10.0, rpg=5.0, apg=2.0, stars=2,  # decade/prime defaults
    )
    lot = Lot(seq=1, player=old, last_call=False, deadline=1_000.0)
    assert ui.lot_embed(lot, pool_left=3).description == "TST · 10/5/2"


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


# --------------------------------------------------------- all-in showdown


# Sentinel guess 77 appears nowhere else in the fixtures — if it shows up in
# any pre-reveal public surface, a secret leaked.
SHOWDOWN_LOT = Lot(
    seq=3, player=JORDAN, last_call=False, current_bid=5, leader_id=2,
    deadline=1_000.0, lottery=Lottery(participants=(2, 3), guesses=((2, 77),)),
)


def test_lot_embed_showdown_status_line():
    embed = ui.lot_embed(SHOWDOWN_LOT, pool_left=10)
    status = next(f.value for f in embed.fields if f.name == "Status")
    assert status == "🎰 Showdown — locks <t:1000:R>"


def test_lot_embed_showdown_beats_final_seconds_styling():
    # bot._timer skips the warning edit during a lottery; even if the flag
    # sneaks through, the showdown status must win.
    embed = ui.lot_embed(SHOWDOWN_LOT, pool_left=10, final_seconds=True)
    status = next(f.value for f in embed.fields if f.name == "Status")
    assert "FINAL SECONDS" not in status
    assert status == "🎰 Showdown — locks <t:1000:R>"


def test_showdown_open_text_names_stakes_and_player():
    assert ui.showdown_open_text(SHOWDOWN_LOT) == (
        "🎰 **ALL-IN SHOWDOWN** — <@2> and <@3> face off at $5 "
        "on **Michael Jordan**!"
    )


def test_showdown_embed_explains_rules_with_both_timestamp_forms():
    embed = ui.showdown_embed(SHOWDOWN_LOT)
    assert "1 to 100" in embed.description
    assert "mystery number" in embed.description
    assert "$5" in embed.description and "Michael Jordan" in embed.description
    # Mobile clients don't tick <t:..:R> — the absolute :T form must be there.
    assert "<t:1000:R>" in embed.description
    assert "<t:1000:T>" in embed.description


def _showdown_state() -> DraftState:
    empty = tuple(Spot(slot=s) for s in ("PG", "SG", "SF", "PF", "C"))
    return DraftState(
        config=Config(),
        commissioner_id=2,
        managers=(
            Manager(user_id=2, name="Ann", budget=5, spots=empty),
            Manager(user_id=3, name="Bob", budget=5, spots=empty),
        ),
    )


def test_lottery_reveal_embed_shows_mystery_guesses_and_winner():
    fx = LotteryRevealFx(mystery=40, guesses=((2, 33), (3, 55)), winner_id=2)
    embed = ui.lottery_reveal_embed(fx, _showdown_state())
    assert "40" in embed.title
    assert "🏆 **Ann picked 33 — off by 7**" in embed.description
    assert "Bob picked 55 — off by 15" in embed.description


def test_guess_values_never_leak_before_the_reveal():
    # Every public builder that can render while guesses exist, dumped whole.
    surfaces = [
        str(ui.lot_embed(SHOWDOWN_LOT, pool_left=10).to_dict()),
        ui.showdown_open_text(SHOWDOWN_LOT),
        str(ui.showdown_embed(SHOWDOWN_LOT).to_dict()),
        ui.lottery_joined_text(3),
        ui.lottery_guessed_text("Ann"),
        ui.lottery_cancelled_text("Rich"),
    ]
    for surface in surfaces:
        assert "77" not in surface


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


# ------------------------------------------------------------- cpu managers


def _empty_spots() -> tuple[Spot, ...]:
    return tuple(Spot(slot=s) for s in ("PG", "SG", "SF", "PF", "C"))


def _cpu_manager(n: int = 1, budget: int = 20) -> Manager:
    return Manager(
        user_id=-n, name=f"CPU {n}", budget=budget, spots=_empty_spots(), cpu=True
    )


def test_display_helpers():
    assert ui.display(_cpu_manager(1)) == "🤖 CPU 1"
    human = Manager(user_id=5, name="Chris", budget=20, spots=_empty_spots())
    assert ui.display(human) == "<@5>"
    assert ui.display_id(-2) == "🤖 CPU 2"
    assert ui.display_id(7) == "<@7>"


def test_addcpu_seats_cpu_managers_and_lobby_lists_them_plain():
    state = DraftState(config=Config(), commissioner_id=1)
    state, _ = engine.apply(state, Join(1, "Chris"))
    state, _ = engine.apply(state, AddCpu(user_id=1, count=2))
    cpus = [m for m in state.managers if m.cpu]
    assert all(m.user_id < 0 for m in cpus) and len(cpus) == 2
    assert all(m.budget == state.config.budget for m in cpus)
    assert all(m.name == f"CPU {-m.user_id}" for m in cpus)
    field = next(
        f for f in ui.lobby_embed(state).fields if f.name.startswith("Managers")
    )
    assert "🤖 CPU 1" in field.value and "🤖 CPU 2" in field.value
    assert "<@-" not in field.value
    state, _ = engine.apply(state, RemoveCpu(user_id=1, cpu_id=-2))
    assert [m.user_id for m in state.managers if m.cpu] == [-1]


def test_cpu_never_rendered_as_negative_mention():
    cpu_m = _cpu_manager(1)
    state = DraftState(config=Config(), commissioner_id=1, managers=(cpu_m,))
    cpu_lot = replace(SHOWDOWN_LOT, lottery=Lottery(participants=(-1, 3)))
    surfaces = [
        str(ui.board_embed(state).to_dict()),
        str(ui.lobby_embed(state).to_dict()),
        str(ui.sold_embed(JORDAN, cpu_m, 5).to_dict()),
        str(ui.force_embed(JORDAN, cpu_m).to_dict()),
        str(ui.autofill_embed(((-1, JORDAN),)).to_dict()),
        ui.pick_prompt(-1, 1000.0),
        ui.lottery_joined_text(-1),
        ui.showdown_open_text(cpu_lot),
    ]
    for surface in surfaces:
        assert "<@-" not in surface


def test_lot_embed_leader_field_shows_cpu_plain():
    lot = Lot(
        seq=1, player=JORDAN, last_call=False,
        current_bid=5, leader_id=-1, deadline=1_000.0,
    )
    embed = ui.lot_embed(lot, pool_left=10)
    leader = next(f.value for f in embed.fields if f.name == "Leader")
    assert leader == "🤖 CPU 1"


def _cpu_auction_state(lot: Lot, cpu_m: Manager) -> DraftState:
    return DraftState(
        config=Config(), commissioner_id=1, phase="auction",
        managers=(cpu_m,), lot=lot, lot_seq=lot.seq,
    )


def test_cpu_decide_opens_a_bid_near_the_deadline():
    state = _cpu_auction_state(
        Lot(seq=1, player=JORDAN, last_call=False, deadline=1_000.0),
        _cpu_manager(1),
    )
    event, _ = cpu.decide(state, -1, now=999.5)
    assert event == Bid(-1, 1, 999.5, amount=1)
    # Far from the deadline the CPU waits instead of acting.
    event2, delay2 = cpu.decide(state, -1, now=900.0)
    assert event2 is None and delay2 > 0


def test_cpu_decide_locks_a_showdown_guess():
    lot = Lot(
        seq=3, player=JORDAN, last_call=False, current_bid=5, leader_id=3,
        deadline=1_000.0, lottery=Lottery(participants=(3, -1)),
    )
    state = _cpu_auction_state(lot, _cpu_manager(1, budget=5))
    event, _ = cpu.decide(state, -1, now=990.0)
    assert isinstance(event, LotteryGuess) and 1 <= event.guess <= 100
    # Once a guess is locked, the CPU sits tight.
    locked = replace(lot, lottery=Lottery(participants=(3, -1), guesses=((-1, 42),)))
    event2, _ = cpu.decide(_cpu_auction_state(locked, _cpu_manager(1, budget=5)), -1, now=990.0)
    assert event2 is None


def test_cpu_decide_free_pick_takes_best_player():
    scrub = Player(
        id="scrub", name="Scrub", team="TST", pos="PG",
        ppg=5.0, rpg=2.0, apg=1.0, stars=1,
    )
    state = DraftState(
        config=Config(), commissioner_id=1, phase="free_pick",
        managers=(_cpu_manager(1),), queue=(scrub, JORDAN),
        pick_deadline=1_060.0,
    )
    event, _ = cpu.decide(state, -1, now=1_059.0)
    assert isinstance(event, Pick) and event.player_id == "jordan"
    # Right after the window opens, it thinks for a beat first.
    event2, delay2 = cpu.decide(state, -1, now=1_000.5)
    assert event2 is None and delay2 > 0


def test_cpu_decide_joins_live_showdown_when_all_in_matches():
    # Outsider CPU whose exact stack ties the live amount piles into a
    # running lottery instead of sitting it out (one-sided open, rule #19).
    filled = tuple(
        Spot(slot, _rated(f"f{slot}", slot, 2))
        for slot in ("PG", "SG", "SF", "PF")
    ) + (Spot("C"),)
    m = Manager(user_id=-1, name="CPU 1", budget=5, spots=filled, cpu=True)
    lot = Lot(
        seq=3, player=JORDAN, last_call=False, current_bid=5, leader_id=3,
        deadline=1_000.0, lottery=Lottery(participants=(3, 4)),
    )
    state = DraftState(
        config=Config(), commissioner_id=1, phase="auction",
        managers=(m,), lot=lot, lot_seq=3,
    )
    event, _ = cpu.decide(state, -1, now=990.0)
    assert event == Bid(-1, 3, 990.0, amount=5)


def _rated(pid: str, pos: str, stars: int) -> Player:
    return Player(
        id=pid, name=pid, team="TST", pos=pos,
        ppg=20.0, rpg=5.0, apg=5.0, stars=stars,
    )


def _snake_spots(*players: Player) -> tuple[Spot, ...]:
    slots = ("PG", "SG", "SF", "PF", "C")
    return tuple(
        Spot(slot, players[i] if i < len(players) else None)
        for i, slot in enumerate(slots)
    )


def _snake_state(
    managers: tuple[Manager, ...],
    queue: tuple[Player, ...],
    pick_deadline: float = 1_030.0,  # turn opened at 1_000 (30s clock)
) -> DraftState:
    return DraftState(
        config=Config(mode="snake"), commissioner_id=1, phase="snake",
        managers=managers, queue=queue, pick_deadline=pick_deadline,
    )


def test_cpu_decide_snake_skips_unaffordable_star_for_best_feasible():
    # Two-manager snake (h,c | c,h | h,c ...): human has 3 picks, the CPU 2
    # ($5+$4 spent -> $6 left, 3 empty), so pick #6 is the CPU's. Jordan's
    # $5 tier would leave $1 for 2 empty slots -> infeasible; best 4★ wins,
    # with the bigger combined stat line breaking the star tie.
    human = Manager(
        user_id=1, name="Chris", budget=3,
        spots=_snake_spots(
            _rated("h1", "PG", 5), _rated("h2", "SG", 4), _rated("h3", "SF", 3)
        ),
    )
    cpu_m = Manager(
        user_id=-1, name="CPU 1", budget=6,
        spots=_snake_spots(_rated("c1", "PG", 5), _rated("c2", "SG", 4)),
        cpu=True,
    )
    barkley = Player(
        id="barkley", name="Charles Barkley", team="PHX", pos="PF",
        ppg=25.0, rpg=12.0, apg=4.0, stars=4,
    )
    weak4 = Player(
        id="weak4", name="Weak Four", team="TST", pos="C",
        ppg=15.0, rpg=8.0, apg=2.0, stars=4,
    )
    state = _snake_state((human, cpu_m), (JORDAN, weak4, barkley))
    event, _ = cpu.decide(state, -1, now=1_005.0)
    assert event == Pick(-1, "barkley", 1_005.0)
    # Right after the turn opens, it thinks for a beat first (~2s in).
    event2, delay2 = cpu.decide(state, -1, now=1_000.5)
    assert event2 is None and delay2 == pytest.approx(1.5)


def test_cpu_decide_snake_waits_off_turn():
    human = Manager(user_id=1, name="Chris", budget=15, spots=_empty_spots())
    state = _snake_state((human, _cpu_manager(1, budget=15)), (JORDAN,))
    event, delay = cpu.decide(state, -1, now=1_005.0)
    assert event is None and delay == cpu.IDLE_DELAY


def test_cpu_decide_snake_stranded_returns_none():
    # $1 left with 2 empty slots: even a $1 pick breaks the $1-per-slot
    # reserve, so nothing is feasible. The CPU sits; the engine's forced
    # bargain resolves the turn at the timer.
    cpu_m = Manager(
        user_id=-1, name="CPU 1", budget=1,
        spots=_snake_spots(
            _rated("c1", "PG", 5), _rated("c2", "SG", 5), _rated("c3", "SF", 4)
        ),
        cpu=True,
    )
    state = _snake_state((cpu_m,), (JORDAN, _rated("scrub", "C", 1)))
    event, _ = cpu.decide(state, -1, now=1_010.0)  # well past the think beat
    assert event is None


def _lineup_state(spots: tuple[Spot, ...]) -> DraftState:
    m = Manager(user_id=-1, name="CPU 1", budget=0, spots=spots, cpu=True)
    return DraftState(
        config=Config(), commissioner_id=1, phase="lineup",
        managers=(m,), lineup_deadline=1_060.0,
    )


def test_cpu_lineup_five_star_center_claims_center():
    state = _lineup_state((
        Spot("PG", _rated("pg", "PG", 3)),
        Spot("SG", _rated("sg", "SG", 3)),
        Spot("SF", _rated("sf", "SF", 3)),
        Spot("PF", _rated("hakeem", "C", 5)),
        Spot("C", _rated("chandler", "C", 4)),
    ))
    event, _ = cpu.decide(state, -1, now=1_000.0)
    assert event == Swap(-1, "PF", "C")
    state2, _ = engine.apply(state, event)
    lineup = {s.slot: s.player.id for s in state2.manager(-1).spots}
    assert lineup["C"] == "hakeem" and lineup["PF"] == "chandler"
    # Arranged — the brain has nothing more to do.
    event2, _ = cpu.decide(state2, -1, now=1_001.0)
    assert event2 is None


def test_cpu_lineup_converges_with_two_point_guards():
    # 5★ PG takes PG; the 4★ PG is relegated to SG, bumping the 3★ SG
    # into the open SF hole. Must settle (decide -> None) within 4 swaps.
    state = _lineup_state((
        Spot("PG", _rated("kidd", "PG", 4)),
        Spot("SG", _rated("magic", "PG", 5)),
        Spot("SF", _rated("sg", "SG", 3)),
        Spot("PF", _rated("pf", "PF", 3)),
        Spot("C", _rated("c", "C", 3)),
    ))
    for swaps in range(5):
        event, _ = cpu.decide(state, -1, now=1_000.0 + swaps)
        if event is None:
            break
        assert isinstance(event, Swap)
        state, _ = engine.apply(state, event)
    assert swaps <= 4
    lineup = {s.slot: s.player.id for s in state.manager(-1).spots}
    assert lineup["PG"] == "magic" and lineup["SG"] == "kidd"


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
