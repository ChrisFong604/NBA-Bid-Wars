"""Post-draft LLM tournament simulation (DESIGN.md section 5).

One request to Claude with every roster; structured output guarantees a
parseable ``Tournament``. Format is decided here in Python — round-robin for
small fields, a stars-seeded knockout bracket above that — and the model is
instructed to narrate results consistent with it.
"""
from __future__ import annotations

import itertools

import anthropic
import pydantic
from pydantic import BaseModel

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
ROUND_ROBIN_MAX_TEAMS = 6


class Game(BaseModel):
    round: str
    home: str
    away: str
    home_score: int
    away_score: int
    recap: str


class Tournament(BaseModel):
    games: list[Game]
    champion: str
    mvp: str
    summary: str


class SimError(Exception):
    """Simulation failed — report to the channel and offer a retry."""


async def run_tournament(teams: list[dict]) -> Tournament:
    """Simulate a tournament between the drafted teams via Claude.

    ``teams`` shape: ``[{"manager": str, "players": [{"slot", "name", "pos",
    "ppg", "rpg", "apg", "stars"}, ...]}, ...]``. Raises ``SimError`` on bad
    input, API failure, refusal, or an inconsistent result.
    """
    names = _team_names(teams)
    prompt = _build_prompt(teams)
    try:
        async with anthropic.AsyncAnthropic() as client:
            response = await client.messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                output_format=Tournament,
            )
    except anthropic.AnthropicError as exc:
        raise SimError(f"The tournament sim hit an API error: {exc}") from exc
    except pydantic.ValidationError as exc:
        raise SimError("The sim returned a malformed result — try again.") from exc

    if response.stop_reason == "refusal":
        raise SimError("Claude declined to simulate this tournament — try again.")
    tournament = response.parsed_output
    if tournament is None:
        raise SimError("The sim returned no usable result — try again.")
    _validate(tournament, names)
    return tournament


# ------------------------------------------------------------------ helpers


def _team_names(teams: list[dict]) -> frozenset[str]:
    """Validate the input shape and return the set of team (manager) names."""
    if not isinstance(teams, list) or len(teams) < 2:
        raise SimError("A tournament needs at least two teams.")
    names: list[str] = []
    for team in teams:
        manager = team.get("manager")
        players = team.get("players")
        if not manager or not players:
            raise SimError("Every team needs a manager name and a non-empty roster.")
        names.append(manager)
    if len(set(names)) != len(names):
        raise SimError("Team names must be unique.")
    return frozenset(names)


def _total_stars(team: dict) -> int:
    return sum(p["stars"] for p in team["players"])


def _roster_block(team: dict) -> str:
    lines = [f"Team {team['manager']} ({_total_stars(team)} total stars):"]
    for p in team["players"]:
        # Pre-era snapshots can carry players without a prime range.
        prime = p.get("prime") or "n/a"
        decade = p.get("decade", 2020)
        lines.append(
            f"  {p['slot']}: {p['name']} (natural {p['pos']}, "
            f"prime {prime} — {decade}s era, "
            f"{p['ppg']:g} ppg / {p['rpg']:g} rpg / {p['apg']:g} apg, "
            f"{p['stars']}/5 stars)"
        )
    return "\n".join(lines)


def _round_robin_section(teams: list[dict]) -> str:
    names = [t["manager"] for t in teams]
    pairings = "\n".join(f"- {a} vs {b}" for a, b in itertools.combinations(names, 2))
    return (
        "FORMAT: round-robin. Every team plays every other team exactly once.\n"
        "Required matchups (play each exactly once, in any order):\n"
        f"{pairings}\n"
        "The champion is the team with the best win-loss record; break ties on\n"
        "total point differential. Keep every game score consistent with the\n"
        "final standings and the champion you crown."
    )


def _bracket_section(teams: list[dict]) -> str:
    seeded = sorted(teams, key=lambda t: (-_total_stars(t), t["manager"]))
    seed_lines = "\n".join(
        f"- seed {i}: {t['manager']} ({_total_stars(t)} total stars)"
        for i, t in enumerate(seeded, 1)
    )
    size = 1
    while size < len(seeded):
        size *= 2
    round_one: list[str] = []
    for hi in range(1, size // 2 + 1):
        lo = size - hi + 1
        if lo <= len(seeded):
            round_one.append(
                f"- (seed {hi}) {seeded[hi - 1]['manager']} vs "
                f"(seed {lo}) {seeded[lo - 1]['manager']}"
            )
        else:
            round_one.append(
                f"- (seed {hi}) {seeded[hi - 1]['manager']} — bye, advances automatically"
            )
    return (
        "FORMAT: single-elimination bracket, seeded by total roster stars.\n"
        f"Seeding:\n{seed_lines}\n"
        "First-round matchups (byes advance without playing):\n"
        + "\n".join(round_one)
        + "\nWinners advance until one champion remains. Include every game\n"
        "actually played, and label each game's round (e.g. Quarterfinal,\n"
        "Semifinal, Final)."
    )


def _build_prompt(teams: list[dict]) -> str:
    rosters = "\n\n".join(_roster_block(t) for t in teams)
    if len(teams) <= ROUND_ROBIN_MAX_TEAMS:
        format_section = _round_robin_section(teams)
    else:
        format_section = _bracket_section(teams)
    return (
        "You are simulating a post-draft NBA tournament between fantasy teams\n"
        "assembled in a blind-auction draft. Team names are the manager names\n"
        "given below — use them exactly in the home, away, and champion fields.\n"
        "\n"
        "Rosters can span decades, and every player competes AT THEIR PRIME:\n"
        "primes face primes across eras — 1991 Jordan takes the floor against\n"
        "2016 Curry. Each player's stats are prime-years numbers from their\n"
        "own era; never age a player up or down.\n"
        "\n"
        "Players are listed by lineup slot. A player's natural position may\n"
        "differ from the slot they occupy — a center running point guard is\n"
        "legal and should flavor the recaps.\n"
        "\n"
        f"TEAMS\n{rosters}\n"
        "\n"
        f"{format_section}\n"
        "\n"
        "RULES\n"
        "- Use realistic NBA final scores (roughly 85-135 per team, no ties).\n"
        "- Write a vivid 2-3 sentence recap for every game that references the\n"
        "  actual players on these rosters by name — big performances, clutch\n"
        "  shots, out-of-position heroics or disasters.\n"
        "- When rosters mix eras, play up the style clashes in the recaps —\n"
        "  pace, spacing, hand-checking, three-point volume — as prime meets\n"
        "  prime across decades.\n"
        "- Name one tournament MVP: an individual player, not a manager.\n"
        "- End with a short, punchy 1-2 sentence summary of the tournament."
    )


def _validate(tournament: Tournament, names: frozenset[str]) -> None:
    if tournament.champion not in names:
        raise SimError(
            f"The sim crowned an unknown champion {tournament.champion!r} — try again."
        )
    for game in tournament.games:
        for side in (game.home, game.away):
            if side not in names:
                raise SimError(
                    f"The sim invented a team {side!r} in game "
                    f"{game.round!r} — try again."
                )
