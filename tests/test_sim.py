"""Tests for draftbot.sim — no network; the OpenAI-compatible client is faked."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import openai
import pytest

from draftbot import sim
from draftbot.sim import LlmRanking, SimError


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


def _one_player_team(
    name: str, stars: int, ppg: float = 0.0, rpg: float = 0.0, apg: float = 0.0
) -> dict:
    return {
        "manager": name,
        "players": [
            {
                "slot": "PG",
                "name": f"{name} Star",
                "pos": "PG",
                "ppg": ppg,
                "rpg": rpg,
                "apg": apg,
                "stars": stars,
                "decade": 1990,
                "prime": "1991–1995",
            }
        ],
    }


def _ranking(names: list[str], summary: str = "A classic.") -> LlmRanking:
    return LlmRanking(ranking=names, summary=summary)


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


def _ok_response(ranking: LlmRanking) -> SimpleNamespace:
    return _response(ranking.model_dump_json())


# ------------------------------------------------------------- stats mode


def test_stats_score_formula():
    # 4*5 + 0.35*30 + 0.5*10 + 0.7*10 = 20 + 10.5 + 5 + 7 = 42.5
    teams = [
        _one_player_team("A", stars=5, ppg=30, rpg=10, apg=10),
        _one_player_team("B", stars=1),
    ]
    result = sim.run_stats(teams)
    assert result.standings[0] == ("A", 42.5)
    assert result.champion == "A"
    assert result.summary == ""
    assert result.summary == ""


def test_stats_deterministic():
    teams = _teams(6)
    first = sim.run_stats(teams)
    second = sim.run_stats(teams)
    assert first == second
    # sorted desc by team score
    scores = [score for _, score in first.standings]
    assert scores == sorted(scores, reverse=True)


def test_stats_ties_break_by_team_name():
    teams = [
        _one_player_team("Zed", stars=3),
        _one_player_team("Abe", stars=3),
        _one_player_team("Mia", stars=3),
    ]
    result = sim.run_stats(teams)
    assert [name for name, _ in result.standings] == ["Abe", "Mia", "Zed"]
    assert result.champion == "Abe"


def test_stats_mode_makes_no_network_call(monkeypatch):
    def boom(**kwargs):
        raise AssertionError("stats mode must not build an LLM client")

    monkeypatch.setattr(sim.openai, "AsyncOpenAI", boom)
    sim.run_stats(_teams(4))


def test_stats_fewer_than_two_teams_raises():
    with pytest.raises(SimError, match="at least two"):
        sim.run_stats(_teams(1))


# ---------------------------------------------------------- ai blend math


def test_ai_blend_hand_checked_three_teams(monkeypatch):
    """Hand-checked 3-team blend.

    Stats scores: A = 4*5+0.35*30+0.5*10+0.7*10 = 42.5,
    B = 4*3+0.35*20+0.5*5+0.7*5 = 25.0, C = 4*1+0.35*10+0.5*2+0.7*2 = 9.9.
    Stats order [A, B, C] -> points A=2, B=1, C=0.
    LLM ranking [B, C, A]   -> points B=2, C=1, A=0.
    final = 0.6*stats + 0.4*llm:
      A = 0.6*2 + 0.4*0 = 1.2
      B = 0.6*1 + 0.4*2 = 1.4
      C = 0.6*0 + 0.4*1 = 0.4
    Blend order [B, A, C] — the LLM flips the champion from A to B.
    """
    teams = [
        _one_player_team("A", stars=5, ppg=30, rpg=10, apg=10),
        _one_player_team("B", stars=3, ppg=20, rpg=5, apg=5),
        _one_player_team("C", stars=1, ppg=10, rpg=2, apg=2),
    ]
    _patch_client(
        monkeypatch, _ok_response(_ranking(["B", "C", "A"], summary="B ran the table."))
    )
    result = asyncio.run(sim.run_ai(teams))
    assert [name for name, _ in result.standings] == ["B", "A", "C"]
    assert [score for _, score in result.standings] == pytest.approx([1.4, 1.2, 0.4])
    assert result.champion == "B"
    assert result.summary == "B ran the table."
    assert result.summary != ""


def test_ai_blend_ties_break_by_stats_order(monkeypatch):
    """5 teams, stats order [Y, P, X, Q, R] (points 4..0), LLM [Q, X, R, P, Y].

    final = 0.6*stats + 0.4*llm:
      Y = 0.6*4 + 0.4*0 = 2.4   X = 0.6*2 + 0.4*3 = 2.4  -> tie, Y ahead (stats)
      P = 0.6*3 + 0.4*1 = 2.2   Q = 0.6*1 + 0.4*4 = 2.2  -> tie, P ahead (stats)
      R = 0.6*0 + 0.4*2 = 0.8
    """
    teams = [
        _one_player_team("Y", stars=5),
        _one_player_team("P", stars=4),
        _one_player_team("X", stars=3),
        _one_player_team("Q", stars=2),
        _one_player_team("R", stars=1),
    ]
    _patch_client(monkeypatch, _ok_response(_ranking(["Q", "X", "R", "P", "Y"])))
    result = asyncio.run(sim.run_ai(teams))
    assert [name for name, _ in result.standings] == ["Y", "X", "P", "Q", "R"]
    assert [score for _, score in result.standings] == pytest.approx(
        [2.4, 2.4, 2.2, 2.2, 0.8]
    )
    assert result.champion == "Y"


# ------------------------------------------------------------- validation


def test_ai_ranking_with_unknown_team_raises(monkeypatch):
    teams = _teams(3)
    _patch_client(
        monkeypatch, _ok_response(_ranking(["Manager0", "Manager1", "Ghost"]))
    )
    with pytest.raises(SimError, match="permutation"):
        asyncio.run(sim.run_ai(teams))


def test_ai_ranking_with_duplicates_raises(monkeypatch):
    teams = _teams(3)
    _patch_client(
        monkeypatch, _ok_response(_ranking(["Manager0", "Manager0", "Manager1"]))
    )
    with pytest.raises(SimError, match="permutation"):
        asyncio.run(sim.run_ai(teams))


def test_ai_ranking_missing_a_team_raises(monkeypatch):
    teams = _teams(3)
    _patch_client(monkeypatch, _ok_response(_ranking(["Manager0", "Manager1"])))
    with pytest.raises(SimError, match="permutation"):
        asyncio.run(sim.run_ai(teams))


def test_ai_fewer_than_two_teams_raises(monkeypatch):
    calls = _patch_client(monkeypatch, _response(None))
    with pytest.raises(SimError, match="at least two"):
        asyncio.run(sim.run_ai(_teams(1)))
    assert calls == []  # rejected before any API call


# ------------------------------------------------------------ the request


def test_request_uses_model_and_max_tokens(monkeypatch):
    teams = _teams(3)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    (call,) = calls
    assert call["model"] == "anthropic/claude-sonnet-4.5"
    assert call["max_tokens"] == 16000


def test_sim_model_env_var_overrides_model(monkeypatch):
    teams = _teams(3)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    monkeypatch.setenv("SIM_MODEL", "meta-llama/llama-3.3-70b-instruct")
    asyncio.run(sim.run_ai(teams))
    (call,) = calls
    assert call["model"] == "meta-llama/llama-3.3-70b-instruct"


def test_prompt_embeds_output_schema(monkeypatch):
    teams = _teams(3)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "OUTPUT" in prompt
    assert '"ranking"' in prompt and '"summary"' in prompt  # schema verbatim


def test_prompt_includes_every_team_name(monkeypatch):
    teams = _teams(5)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    prompt = calls[0]["messages"][0]["content"]
    for team in teams:
        assert team["manager"] in prompt
        for player in team["players"]:
            assert player["name"] in prompt


# ------------------------------------------------------------ era framing


def test_prompt_states_primes_face_primes_across_eras(monkeypatch):
    teams = _teams(3)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "AT THEIR PRIME" in prompt
    assert "primes face primes" in prompt
    assert "1991 Jordan" in prompt and "2016 Curry" in prompt
    # era style-clash flavor is explicitly kept in the prompt
    for flavor in ("pace", "spacing", "hand-checking", "three-point volume"):
        assert flavor in prompt


def test_roster_block_includes_prime_and_decade(monkeypatch):
    teams = _teams(2)
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "prime 1991–1995 — 1990s era" in prompt


def test_roster_block_tolerates_pre_era_player_dicts(monkeypatch):
    """Players from a pre-era snapshot have no prime/decade keys — the
    roster block falls back rather than crashing the sim."""
    teams = _teams(2)
    for team in teams:
        for player in team["players"]:
            del player["decade"], player["prime"]
    names = [t["manager"] for t in teams]
    calls = _patch_client(monkeypatch, _ok_response(_ranking(names)))
    asyncio.run(sim.run_ai(teams))
    prompt = calls[0]["messages"][0]["content"]
    assert "prime n/a — 2020s era" in prompt


# ------------------------------------------------------------ output parsing


def test_fenced_json_output_parses(monkeypatch):
    teams = _teams(4)
    canned = _ranking([t["manager"] for t in teams])
    fenced = f"```json\n{canned.model_dump_json()}\n```"
    _patch_client(monkeypatch, _response(fenced))
    result = asyncio.run(sim.run_ai(teams))
    assert result.summary == canned.summary
    assert result.summary != ""


def test_json_wrapped_in_prose_parses(monkeypatch):
    teams = _teams(4)
    canned = _ranking([t["manager"] for t in teams])
    chatty = f"Here is your tournament!\n{canned.model_dump_json()}\nEnjoy."
    _patch_client(monkeypatch, _response(chatty))
    result = asyncio.run(sim.run_ai(teams))
    assert result.summary == canned.summary


# ------------------------------------------------------- failure handling


def test_empty_content_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, _response(None))
    with pytest.raises(SimError, match="no usable result"):
        asyncio.run(sim.run_ai(teams))


def test_malformed_json_raises_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, _response("sorry, no basketball today"))
    with pytest.raises(SimError, match="malformed"):
        asyncio.run(sim.run_ai(teams))


def test_api_error_wrapped_in_sim_error(monkeypatch):
    teams = _teams(4)
    _patch_client(monkeypatch, openai.OpenAIError("boom"))
    with pytest.raises(SimError, match="API error"):
        asyncio.run(sim.run_ai(teams))
