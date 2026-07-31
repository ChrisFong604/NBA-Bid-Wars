"""Static frontend guards: the no-build webapp assets must stay parseable
and keep the wire-contract hooks (mode select, snake view, blind masking)
they were built against. String-level checks on purpose — there is no JS
test runner in this project, and these catch accidental deletions of the
dictated protocol fields."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "webapp" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_parses():
    subprocess.run(
        ["node", "--check", str(STATIC / "app.js")], check=True, capture_output=True
    )


def test_create_form_offers_the_three_modes():
    assert 'id="c-mode"' in INDEX_HTML
    for value in ("auction", "blind", "snake"):
        assert f'value="{value}"' in INDEX_HTML
    # Snake note shown when the budget input is disabled.
    assert 'id="c-budget-note"' in INDEX_HTML
    assert "$15 fixed" in INDEX_HTML


def test_create_post_sends_mode():
    assert 'mode: $("c-mode").value' in APP_JS


def test_blind_masking_hooks():
    # Null names render as the mystery placeholder everywhere...
    assert "Mystery player" in APP_JS
    # ...and the sold line celebrates the reveal after a masked lot.
    assert "It was ${fx.player.name}!" in APP_JS
    assert "lotMasked" in APP_JS


def test_snake_view_reads_dictated_wire_fields():
    # state_view: "turn" {manager, deadline} and "pool" [{...player, price}].
    assert 'st.phase === "snake" && st.turn' in APP_JS
    assert "turn.manager" in APP_JS
    assert "turn.deadline" in APP_JS
    assert "st.pool" in APP_JS
    assert "p.price" in APP_JS
    # fx: {"kind": "snake_turn", "manager": ..., "deadline": ...}.
    assert '"snake_turn"' in APP_JS
    # Picks ride the plain pick action — the engine adjudicates.
    assert 'send({ action: "pick", player_id: p.id })' in APP_JS


def test_snake_is_an_active_phase():
    # Reclaim bar + commissioner controls stay live during snake.
    assert '"snake"' in APP_JS.split("ACTIVE_PHASES = ")[1].split("]")[0]


def test_snake_styles_exist():
    for selector in (".turn-banner", ".price-badge", ".pool-card.dimmed",
                     ".mystery"):
        assert selector in STYLE_CSS
