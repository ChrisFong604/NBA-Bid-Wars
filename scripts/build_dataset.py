"""Regenerate ``draftbot/data/players.json`` from public-domain season data.

Source: "NBA Stats (1947-present)" by Sumitro Datta (Kaggle / GitHub, CC0):
``Player Per Game.csv`` and ``All-Star Selections.csv`` fetched from the
``bball-reference-datasets`` GitHub mirror (no auth) and cached in
``scripts/.cache/``.

Methodology (fully deterministic — no randomness, all ties broken by
``player_id``):

* **NBA rows only.** ABA seasons are pace-inflated relative to same-era NBA
  play and the BAA predates the shot clock — both leagues would distort the
  era-relative rankings, so they are excluded outright.
* Multi-team seasons use the combined ``2TM``/``3TM`` row for stats; the
  real-franchise stint rows only feed the "most frequent team" pick.
* Seasons with fewer than 20 games are dropped.
* ``season_value = (pts + 0.7*trb + 0.9*ast) * min(g/65, 1)`` per season;
  a player's **prime** is the consecutive 3-season run (of his qualifying
  seasons, in order) maximizing summed value — 2-season windows only for
  2-season careers; 1-season careers are dropped.
* Decade anchor = decade of the window's midpoint season (CSV seasons are
  ending years). Pre-1960 primes are dropped; post-2020 clamp to 2020.
* **Composition (1:1 stars to glue guys):** per decade x position, the top 5
  by caliber (star tier) plus the next 5 (quality role-player tier) = 50 per
  decade, 350 total. Caliber = summed window value * (1 + 0.05 per NBA
  All-Star selection).
* **Stars are era-relative:** within each decade's 50, caliber ranks 1-4 get
  5 stars, 5-12 get 4, 13-25 get 3, 26-40 get 2, 41-50 get 1.

Run ``uv run python scripts/build_dataset.py`` to rewrite the JSON and print
a per-decade composition report. Stdlib only.
"""
from __future__ import annotations

import csv
import json
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

RAW_BASE = (
    "https://raw.githubusercontent.com/sumitrodatta/"
    "bball-reference-datasets/master/Data/"
)
CACHE_DIR = Path(__file__).parent / ".cache"
OUT_PATH = Path(__file__).parent.parent / "draftbot" / "data" / "players.json"

SLOTS = ("PG", "SG", "SF", "PF", "C")
DECADES = (1960, 1970, 1980, 1990, 2000, 2010, 2020)
MULTI_TEAM = {"2TM", "3TM", "4TM", "5TM"}
MIN_GAMES = 20
FULL_SEASON_G = 65
WINDOW = 3
TOP_PER_BUCKET = 5  # 5 stars + 5 role players per decade x position
STAR_BANDS = ((4, 5), (12, 4), (25, 3), (40, 2), (50, 1))  # rank ceiling -> stars
ALLSTAR_BOOST = 0.05
EN_DASH = "–"


def fetch(filename: str) -> Path:
    """Return the cached CSV, downloading it once if missing."""
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / filename
    if not path.exists():
        url = RAW_BASE + urllib.parse.quote(filename)
        print(f"fetching {url}")
        with urllib.request.urlopen(url) as resp:
            path.write_bytes(resp.read())
    return path


def _num(value: str) -> float:
    return 0.0 if value in ("", "NA") else float(value)


def _first_pos(raw: str) -> str | None:
    pos = raw.split("-")[0].strip()
    return pos if pos in SLOTS else None


def load_seasons() -> dict[str, dict[int, dict]]:
    """player_id -> season -> {name, g, pts, trb, ast, pos, team_games}."""
    players: dict[str, dict[int, dict]] = defaultdict(dict)
    with fetch("Player Per Game.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["lg"] != "NBA":
                continue
            season = int(row["season"])
            line = players[row["player_id"]].setdefault(
                season, {"team_games": Counter(), "combined": False}
            )
            if row["team"] in MULTI_TEAM or not line["combined"]:
                # Stats come from the combined row when one exists; a stint
                # row fills in until (and unless) the combined row arrives.
                line.update(
                    name=row["player"],
                    g=int(row["g"]),
                    pts=_num(row["pts_per_game"]),
                    trb=_num(row["trb_per_game"]),
                    ast=_num(row["ast_per_game"]),
                    pos=_first_pos(row["pos"]),
                    combined=row["team"] in MULTI_TEAM,
                )
            if row["team"] not in MULTI_TEAM:
                line["team_games"][row["team"]] += int(row["g"])
    return players


def season_value(line: dict) -> float:
    per_game = line["pts"] + 0.7 * line["trb"] + 0.9 * line["ast"]
    return per_game * min(line["g"] / FULL_SEASON_G, 1.0)


def best_window(seasons: dict[int, dict]) -> list[int] | None:
    """Consecutive qualifying-season run (len 3, or 2 for 2-season careers)
    with the highest summed value; earliest window wins ties."""
    ordered = sorted(s for s, line in seasons.items() if line["g"] >= MIN_GAMES)
    if len(ordered) < 2:
        return None
    size = min(WINDOW, len(ordered))
    best: list[int] | None = None
    best_value = float("-inf")
    for i in range(len(ordered) - size + 1):
        window = ordered[i : i + size]
        value = sum(season_value(seasons[s]) for s in window)
        if value > best_value:
            best, best_value = window, value
    return best


def build_candidate(player_id: str, seasons: dict[int, dict]) -> dict | None:
    window = best_window(seasons)
    if window is None:
        return None
    mid = window[(len(window) - 1) // 2]
    decade = min(mid // 10 * 10, 2020)
    if decade < 1960:
        return None
    lines = [seasons[s] for s in window]
    games = sum(line["g"] for line in lines)
    team_games: Counter[str] = Counter()
    pos_games: Counter[str] = Counter()
    for line in lines:
        team_games.update(line["team_games"])
        if line["pos"] is not None:
            pos_games[line["pos"]] += line["g"]
    if not pos_games or not team_games:
        return None
    pos = min(pos_games, key=lambda p: (-pos_games[p], SLOTS.index(p)))
    team = min(team_games, key=lambda t: (-team_games[t], t))
    return {
        "player_id": player_id,
        "n": seasons[max(window)]["name"],
        "team": team,
        "pos": pos,
        "ppg": round(sum(l["pts"] * l["g"] for l in lines) / games, 1),
        "rpg": round(sum(l["trb"] * l["g"] for l in lines) / games, 1),
        "apg": round(sum(l["ast"] * l["g"] for l in lines) / games, 1),
        "decade": decade,
        "prime": f"{window[0] - 1}{EN_DASH}{window[-1]}",
        "value": sum(season_value(l) for l in lines),
    }


def allstar_counts() -> Counter[str]:
    counts: Counter[str] = Counter()
    try:
        path = fetch("All-Star Selections.csv")
    except OSError as exc:  # optional file: caliber stays deterministic without it
        print(f"note: All-Star Selections.csv unavailable ({exc}); no boost applied")
        return counts
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["lg"] == "NBA":
                counts[row["player_id"]] += 1
    return counts


def slugify(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode()
    )
    words = "".join(c if c.isalnum() else " " for c in ascii_name.lower()).split()
    return "-".join(words)


def select_players() -> tuple[list[dict], list[str]]:
    stars_boost = allstar_counts()
    candidates = []
    for player_id, seasons in load_seasons().items():
        cand = build_candidate(player_id, seasons)
        if cand is not None:
            cand["caliber"] = cand["value"] * (1 + ALLSTAR_BOOST * stars_boost[player_id])
            candidates.append(cand)

    by_bucket: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for cand in candidates:
        by_bucket[(cand["decade"], cand["pos"])].append(cand)

    shortfalls: list[str] = []
    selected: list[dict] = []
    for decade in DECADES:
        for pos in SLOTS:
            pool = sorted(
                by_bucket[(decade, pos)],
                key=lambda c: (-c["caliber"], c["player_id"]),
            )
            take = pool[: 2 * TOP_PER_BUCKET]
            if len(take) < 2 * TOP_PER_BUCKET:
                shortfalls.append(
                    f"{decade}s {pos}: only {len(take)} candidates (wanted 10)"
                )
            selected.extend(take)

    # Era-relative stars: rank each decade's 50 by caliber.
    for decade in DECADES:
        cohort = sorted(
            (c for c in selected if c["decade"] == decade),
            key=lambda c: (-c["caliber"], c["player_id"]),
        )
        for rank, cand in enumerate(cohort, start=1):
            cand["stars"] = next(s for limit, s in STAR_BANDS if rank <= limit)
    return selected, shortfalls


def to_rows(selected: list[dict]) -> list[dict]:
    ordered = sorted(
        selected,
        key=lambda c: (
            c["decade"],
            SLOTS.index(c["pos"]),
            -c["caliber"],
            c["player_id"],
        ),
    )
    used: Counter[str] = Counter()
    rows = []
    for cand in ordered:
        slug = slugify(cand["n"])
        used[slug] += 1
        rows.append(
            {
                "id": slug if used[slug] == 1 else f"{slug}-{used[slug]}",
                "name": cand["n"],
                "team": cand["team"],
                "pos": cand["pos"],
                "ppg": cand["ppg"],
                "rpg": cand["rpg"],
                "apg": cand["apg"],
                "stars": cand["stars"],
                "decade": cand["decade"],
                "prime": cand["prime"],
            }
        )
    return rows


def report(selected: list[dict], shortfalls: list[str]) -> None:
    for decade in DECADES:
        cohort = [c for c in selected if c["decade"] == decade]
        pos_counts = Counter(c["pos"] for c in cohort)
        star_counts = Counter(c["stars"] for c in cohort)
        top3 = sorted(cohort, key=lambda c: (-c["caliber"], c["player_id"]))[:3]
        print(f"{decade}s: {len(cohort)} players")
        print("  pos  " + "  ".join(f"{p}:{pos_counts[p]}" for p in SLOTS))
        print(
            "  stars"
            + "".join(f"  {s}*:{star_counts[s]}" for s in (5, 4, 3, 2, 1))
        )
        print("  top3 " + ", ".join(c["n"] for c in top3))
    for line in shortfalls:
        print(f"SHORTFALL {line}")


def main() -> None:
    selected, shortfalls = select_players()
    rows = to_rows(selected)
    OUT_PATH.write_text(
        json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {len(rows)} players -> {OUT_PATH}")
    report(selected, shortfalls)


if __name__ == "__main__":
    main()
