"""Tests for draftbot.sim — no network; the anthropic client is faked."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anthropic
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


class _FakeMessages:
    def __init__(self, outcome, calls: list[dict]):
        self._outcome = outcome
        self._calls = calls

    async def parse(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _FakeClient:
    def __init__(self, outcome, calls: list[dict]):
        self.messages = _FakeMessages(outcome, calls)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_client(monkeypatch, outcome) -> list[dict]:
    """Replace anthropic.AsyncAnthropic with a fake; returns recorded calls."""
    calls: list[dict] = []
    monkeypatch.setattr(
        sim.anthropic, "AsyncAnthropic", lambda: _FakeClient(outcome, calls)
    )
    return calls


def _ok_response(tournament) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", parsed_output=tournament)


# ------------------------------------------------------------ happy path


def test_success_returns_parsed_tournament(monkeypatch):
    teams = _teams(4)
    canned = _tournament([t["manager"] for t in teams])
    _patch_client(monkeypatch, _ok_response(canned))
    result = asyncio.run(sim.run_tournament(teams))
    assert result is canned


def test_request_uses_model_and_max_tokens(monkeypatch):
    teams = _teams(3)
    calls = _patch_client(monkeypatch, _ok_response(_tournament([t["manager"] for t in teams])))
    asyncio.run(sim.run_tournament(teams))
    (call,) = calls
    assert call["model"] == "claude-opus-5"
    assert call["max_tokens"] == 16000
    assert call["output_format"] is Tournament


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
    calls = _patch_client(monkeypatch, _ok_response(None))
    with pytest.raises(SimError, match="at least two"):
        asyncio.run(sim.run_tournament(_teams(1)))
    assert calls == []  # rejected before any API call


# ------------------------------------------------------- failure handling


def test_refusal_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(
        monkeypatch, SimpleNamespace(stop_reason="refusal", parsed_output=None)
    )
    with pytest.raises(SimError, match="declined"):
        asyncio.run(sim.run_tournament(teams))


def test_none_parsed_output_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(
        monkeypatch, SimpleNamespace(stop_reason="end_turn", parsed_output=None)
    )
    with pytest.raises(SimError, match="no usable result"):
        asyncio.run(sim.run_tournament(teams))


def test_api_error_wrapped_in_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, anthropic.AnthropicError("boom"))
    with pytest.raises(SimError, match="API error"):
        asyncio.run(sim.run_tournament(teams))
