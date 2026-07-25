"""Tests for draftbot.sim — no network; the OpenAI-compatible client is faked."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import openai
import pytest

from draftbot import sim
from draftbot.sim import Game, SimError, Tournament


def _teams(n: int) -> list[dict]:
    slots = ("PG", "SG", "SF", "PF", "C")
    return [
        {
            "manager": f"Manager{i}",
            "players": [
                {
                    "slot": slot,
                    "name": f"Player{i}{slot}",
                    "pos": "C",
                    "ppg": 20.5,
                    "rpg": 8.1,
                    "apg": 3.2,
                    "stars": (i % 5) + 1,
                    "decade": 1990,
                    "prime": "1991–1995",
                }
                for slot in slots
            ],
        }
        for i in range(n)
    ]


def _tournament(names: list[str], champion: str | None = None) -> Tournament:
    return Tournament(
        games=[
            Game(
                round="Final",
                home=names[0],
                away=names[1],
                home_score=112,
                away_score=104,
                recap="Player0PG went off for 40.",
            )
        ],
        champion=champion or names[0],
        mvp="Player0PG",
        summary="A classic.",
    )


class _FakeCompletions:
    def __init__(self, outcome, calls: list[dict]):
        self._outcome = outcome
        self._calls = calls

    async def create(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeClient:
    def __init__(self, outcome, calls: list[dict]):
        self.chat = SimpleNamespace(completions=_FakeCompletions(outcome, calls))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_client(monkeypatch, outcome) -> list[dict]:
    """Replace openai.AsyncOpenAI with a fake; returns recorded calls."""
    calls: list[dict] = []
    monkeypatch.setattr(
        sim.openai, "AsyncOpenAI", lambda **kwargs: _FakeClient(outcome, calls)
    )
    monkeypatch.delenv("SIM_MODEL", raising=False)
    return calls


def _response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _ok_response(tournament: Tournament) -> SimpleNamespace:
    return _response(tournament.model_dump_json())


# ------------------------------------------------------------ happy path


def test_success_returns_parsed_tournament(monkeypatch):
    teams = _teams(4)
    canned = _tournament([t["manager"] for t in teams])
    _patch_client(monkeypatch, _ok_response(canned))
    result = asyncio.run(sim.run_tournament(teams))
    assert result == canned


def test_request_uses_model_and_max_tokens(monkeypatch):
    teams = _teams(3)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    (call,) = calls
    assert call["model"] == "anthropic/claude-sonnet-4.5"
    assert call["max_tokens"] == 16000


def test_sim_model_env_var_overrides_model(monkeypatch):
    teams = _teams(3)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    monkeypatch.setenv("SIM_MODEL", "meta-llama/llama-3.3-70b-instruct")
    asyncio.run(sim.run_tournament(teams))
    (call,) = calls
    assert call["model"] == "meta-llama/llama-3.3-70b-instruct"


def test_prompt_embeds_output_schema(monkeypatch):
    teams = _teams(3)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "OUTPUT" in prompt
    assert '"champion"' in prompt  # schema is embedded verbatim


# ------------------------------------------------------- format selection


def test_round_robin_for_six_or_fewer_teams(monkeypatch):
    teams = _teams(6)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "round-robin" in prompt
    assert "single-elimination" not in prompt
    # every pairing appears exactly once (15 pairings for 6 teams)
    assert prompt.count(" vs ") == 15


def test_bracket_for_more_than_six_teams(monkeypatch):
    teams = _teams(7)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "single-elimination" in prompt
    assert "round-robin" not in prompt
    assert "Seeding:" in prompt
    assert "bye" in prompt  # 7 teams in an 8-slot bracket -> one bye


def test_bracket_seeds_by_total_stars(monkeypatch):
    teams = _teams(8)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    # Manager4 has 5-star players (25 total stars) — must be seed 1.
    assert "- seed 1: Manager4 (25 total stars)" in prompt


def test_prompt_includes_every_team_name(monkeypatch):
    teams = _teams(5)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    for team in teams:
        assert team["manager"] in prompt
        for player in team["players"]:
            assert player["name"] in prompt


# ------------------------------------------------------------ era framing


def test_prompt_states_primes_face_primes_across_eras(monkeypatch):
    teams = _teams(3)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "AT THEIR PRIME" in prompt
    assert "primes face primes" in prompt
    assert "1991 Jordan" in prompt and "2016 Curry" in prompt
    # era style-clash flavor is explicitly invited for the recaps
    for flavor in ("pace", "spacing", "hand-checking", "three-point volume"):
        assert flavor in prompt


def test_roster_block_includes_prime_and_decade(monkeypatch):
    teams = _teams(2)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "prime 1991–1995 — 1990s era" in prompt


def test_roster_block_tolerates_pre_era_player_dicts(monkeypatch):
    """Players from a pre-era snapshot have no prime/decade keys — the
    roster block falls back rather than crashing the sim."""
    teams = _teams(2)
    for team in teams:
        for player in team["players"]:
            del player["decade"], player["prime"]
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "prime n/a — 2020s era" in prompt


# ------------------------------------------------------------- validation


def test_champion_not_in_field_raises(monkeypatch):
    teams = _teams(4)
    names = [t["manager"] for t in teams]
    _patch_client(monkeypatch, _ok_response(_tournament(names, champion="Nobody")))
    with pytest.raises(SimError, match="champion"):
        asyncio.run(sim.run_tournament(teams))


def test_game_with_unknown_team_raises(monkeypatch):
    teams = _teams(4)
    names = [t["manager"] for t in teams]
    bad = _tournament(names)
    bad = bad.model_copy(
        update={"games": [bad.games[0].model_copy(update={"away": "Ghost"})]}
    )
    _patch_client(monkeypatch, _ok_response(bad))
    with pytest.raises(SimError, match="Ghost"):
        asyncio.run(sim.run_tournament(teams))


def test_fewer_than_two_teams_raises(monkeypatch):
    calls = _patch_client(monkeypatch, _response(None))
    with pytest.raises(SimError, match="at least two"):
        asyncio.run(sim.run_tournament(_teams(1)))
    assert calls == []  # rejected before any API call


# ------------------------------------------------------------ output parsing


def test_fenced_json_output_parses(monkeypatch):
    teams = _teams(4)
    canned = _tournament([t["manager"] for t in teams])
    fenced = f"```json\n{canned.model_dump_json()}\n```"
    _patch_client(monkeypatch, _response(fenced))
    result = asyncio.run(sim.run_tournament(teams))
    assert result == canned


def test_json_wrapped_in_prose_parses(monkeypatch):
    teams = _teams(4)
    canned = _tournament([t["manager"] for t in teams])
    chatty = f"Here is your tournament!\n{canned.model_dump_json()}\nEnjoy."
    _patch_client(monkeypatch, _response(chatty))
    result = asyncio.run(sim.run_tournament(teams))
    assert result == canned


# ------------------------------------------------------- failure handling


def test_empty_content_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, _response(None))
    with pytest.raises(SimError, match="no usable result"):
        asyncio.run(sim.run_tournament(teams))


def test_malformed_json_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, _response("sorry, no basketball today"))
    with pytest.raises(SimError, match="malformed"):
        asyncio.run(sim.run_tournament(teams))


def test_api_error_wrapped_in_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, openai.OpenAIError("boom"))
    with pytest.raises(SimError, match="API error"):
        asyncio.run(sim.run_tournament(teams))
