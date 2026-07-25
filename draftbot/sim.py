"""Post-draft LLM tournament simulation (DESIGN.md section 5).

One request to an OpenAI-compatible chat endpoint — OpenRouter by default,
any router via ``LLM_BASE_URL``/``LLM_API_KEY``, model picked by ``SIM_MODEL``
— with every roster; the prompt embeds the ``Tournament`` JSON schema and the
reply is validated against it. Format is decided here in Python — round-robin
for small fields, a stars-seeded knockout bracket above that — and the model
is instructed to narrate results consistent with it.
"""
from __future__ import annotations

import itertools
import json
import os

import openai
import pydantic
from pydantic import BaseModel

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
    """Simulate a tournament between the drafted teams via an LLM.

    ``teams`` shape: ``[{"manager": str, "players": [{"slot", "name", "pos",
    "ppg", "rpg", "apg", "stars"}, ...]}, ...]``. Raises ``SimError`` on bad
    input, API failure, or a malformed/inconsistent result.
    """
    names = _team_names(teams)
    prompt = _build_prompt(teams)
    model = os.environ.get("SIM_MODEL", "anthropic/claude-sonnet-4.5")
    try:
        client = openai.AsyncOpenAI(
            api_key=os.environ.get("LLM_API_KEY"),
            base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        )
        async with client:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
    except openai.OpenAIError as exc:
        raise SimError(f"The tournament sim hit an API error: {exc}") from exc

    content = response.choices[0].message.content
    if not content:
        raise SimError("The sim returned no usable result — try again.")
    tournament = _parse_tournament(content)
    _validate(tournament, names)
    return tournament


# ------------------------------------------------------------------ helpers


def _parse_tournament(content: str) -> Tournament:
    """Validate the model's reply, tolerating fences and surrounding prose."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return Tournament.model_validate_json(text)
    except pydantic.ValidationError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise SimError("The sim returned a malformed result — try again.")
        try:
            return Tournament.model_validate_json(text[start : end + 1])
        except pydantic.ValidationError as exc:
            raise SimError(
                "The sim returned a malformed result — try again."
            ) from exc


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
        "- End with a short, punchy 1-2 sentence summary of the tournament.\n"
        "\n"
        "OUTPUT\n"
        "Respond with ONLY a single JSON object matching this JSON schema.\n"
        "No markdown fences, no prose before or after the JSON object.\n"
        f"{json.dumps(Tournament.model_json_schema())}"
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
