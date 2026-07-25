"""Post-draft simulation (DESIGN.md section 5).

Two modes. ``stats``: no network — every player scores
``4*stars + 0.35*ppg + 0.5*rpg + 0.7*apg`` (era-relative stars primary, prime
stats secondary), a team is the sum of its players, best total wins. ``ai``:
the stats ranking PLUS one request to an OpenAI-compatible chat endpoint —
OpenRouter by default, any router via ``LLM_BASE_URL``/``LLM_API_KEY``, model
picked by ``SIM_MODEL`` — which returns its own ranking of the teams and a
short summary; final standings blend the two rankings, stats-heavy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

import openai
import pydantic
from pydantic import BaseModel

MAX_TOKENS = 16000
# ponytail: the one blend knob — LLM's share of the final score; stats gets
# the rest. 0.0 = pure stats, 1.0 = pure vibes.
LLM_WEIGHT = 0.4


class LlmRanking(BaseModel):
    ranking: list[str]  # every team name exactly once, champion first
    summary: str  # 2-4 sentences on how the tournament played out


class SimError(Exception):
    """Simulation failed — report to the channel and offer a retry."""


@dataclass(frozen=True)
class SimResult:
    standings: tuple[tuple[str, float], ...]  # (team, score), best first
    champion: str
    summary: str = ""  # LLM's tournament story (ai mode only)
    blended: bool = False  # True when standings mix stats + LLM rankings


def player_score(p: dict) -> float:
    """Era-fair player value: stars carry it, prime stats break it open."""
    return 4 * p["stars"] + 0.35 * p["ppg"] + 0.5 * p["rpg"] + 0.7 * p["apg"]


def run_stats(teams: list[dict]) -> SimResult:
    """Deterministic offline ranking — no network, no randomness.

    ``teams`` shape: ``[{"manager": str, "players": [{"slot", "name", "pos",
    "ppg", "rpg", "apg", "stars"}, ...]}, ...]``. Standings sort by team
    score descending, ties broken by team name for determinism.
    """
    _team_names(teams)
    scored = sorted(
        (
            (sum(player_score(p) for p in t["players"]), t["manager"])
            for t in teams
        ),
        key=lambda pair: (-pair[0], pair[1]),
    )
    standings = tuple((name, score) for score, name in scored)
    return SimResult(standings=standings, champion=standings[0][0])


async def run_ai(teams: list[dict]) -> SimResult:
    """Stats ranking blended with an LLM's own ranking of the same teams.

    Each ranking converts to points (p-th of N earns N-p); final score is
    ``(1-LLM_WEIGHT)*stats + LLM_WEIGHT*llm``, ties broken by stats order.
    Raises ``SimError`` on bad input, API failure, or a malformed reply.
    """
    stats = run_stats(teams)
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
    verdict = _parse_ranking(content)

    stats_order = [name for name, _ in stats.standings]
    if sorted(verdict.ranking) != sorted(stats_order):
        raise SimError(
            "The sim's ranking isn't a permutation of the team names — try again."
        )
    n = len(stats_order)
    stats_pts = {name: n - rank for rank, name in enumerate(stats_order, 1)}
    llm_pts = {name: n - rank for rank, name in enumerate(verdict.ranking, 1)}
    final = {
        # round() so equal blends compare equal despite float noise
        name: round(
            (1 - LLM_WEIGHT) * stats_pts[name] + LLM_WEIGHT * llm_pts[name], 9
        )
        for name in stats_order
    }
    # stable sort over stats_order = ties broken by stats order
    ordered = sorted(stats_order, key=lambda name: -final[name])
    return SimResult(
        standings=tuple((name, final[name]) for name in ordered),
        champion=ordered[0],
        summary=verdict.summary,
        blended=True,
    )


# ------------------------------------------------------------------ helpers


def _parse_ranking(content: str) -> LlmRanking:
    """Validate the model's reply, tolerating fences and surrounding prose."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return LlmRanking.model_validate_json(text)
    except pydantic.ValidationError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise SimError("The sim returned a malformed result — try again.")
        try:
            return LlmRanking.model_validate_json(text[start : end + 1])
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


def _build_prompt(teams: list[dict]) -> str:
    rosters = "\n\n".join(_roster_block(t) for t in teams)
    names = ", ".join(t["manager"] for t in teams)
    return (
        "You are simulating a post-draft NBA tournament between fantasy teams\n"
        "assembled in a blind-auction draft. Team names are the manager names\n"
        "given below — use them exactly, spelled exactly, in the ranking.\n"
        "\n"
        "Rosters can span decades, and every player competes AT THEIR PRIME:\n"
        "primes face primes across eras — 1991 Jordan takes the floor against\n"
        "2016 Curry. Each player's stats are prime-years numbers from their\n"
        "own era; never age a player up or down. When rosters mix eras, weigh\n"
        "the style clashes — pace, spacing, hand-checking, three-point volume\n"
        "— as prime meets prime across decades.\n"
        "\n"
        "Players are listed by lineup slot. A player's natural position may\n"
        "differ from the slot they occupy — a center running point guard is\n"
        "legal and should factor into how their team actually plays.\n"
        "\n"
        f"TEAMS\n{rosters}\n"
        "\n"
        "Play the whole tournament out, then respond with:\n"
        f"- ranking: every team ({names}) exactly once, champion first,\n"
        "  last place last.\n"
        "- summary: a brief 2-4 sentence story of how the tournament played\n"
        "  out — the champion's run, key upsets, era style clashes.\n"
        "\n"
        "OUTPUT\n"
        "Respond with ONLY a single JSON object matching this JSON schema.\n"
        "No markdown fences, no prose before or after the JSON object.\n"
        f"{json.dumps(LlmRanking.model_json_schema())}"
    )
