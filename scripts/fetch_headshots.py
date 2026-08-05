"""Fetch Basketball-Reference headshots for every player in players.json.

Basketball-Reference hosts headshots for players of every era at a stable
URL keyed by bbref slug — no HTML scraping needed. The slug for each of our
players comes from ``Player Career Info.csv`` in the same CC0 mirror the
dataset build already uses, disambiguated by prime-vs-career year overlap
(handles duplicate names like the two Luke Jacksons).

Images land in ``webapp/static/headshots/<player.id>.jpg`` so the frontend
references them by the ids it already has. Re-runnable: existing files are
skipped. 404 means bbref has no photo for that player (some 1960s role
players) — those are reported at the end; the UI should fall back to
initials for them.

Note: headshots are licensed press imagery. Fine for local play; clear the
rights before serving them from a public deployment.

Run: ``uv run python scripts/fetch_headshots.py``
"""
from __future__ import annotations

import csv
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from build_dataset import fetch

# Version segment is cache-busting; this one has been stable for years.
HEADSHOT_URL = (
    "https://www.basketball-reference.com/req/202106291/images/headshots/{slug}.jpg"
)
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
PLAYERS_PATH = Path(__file__).parent.parent / "draftbot" / "data" / "players.json"
OUT_DIR = Path(__file__).parent.parent / "webapp" / "static" / "headshots"
# Sports Reference throttles bots above 20 req/min; 3.5s stays safely under.
DELAY_S = 3.5


def slug_map() -> dict[str, str]:
    """player.id -> bbref slug for all 350 players; raises if any is unresolved."""
    players = json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))
    by_name: dict[str, list[dict]] = {}
    with fetch("Player Career Info.csv").open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            by_name.setdefault(row["player"], []).append(row)

    out: dict[str, str] = {}
    for p in players:
        # prime "1964–1967" is display years; CSV from/to are season-ending
        # years, so the window is (start+1)..end.
        start, end = (int(x) for x in p["prime"].replace("–", "-").split("-"))
        fits = [
            c
            for c in by_name.get(p["name"], [])
            if int(c["from"]) <= start + 1 and int(c["to"]) >= end
        ]
        if len(fits) != 1:
            raise ValueError(f"{p['name']} ({p['prime']}): {len(fits)} slug matches")
        out[p["id"]] = fits[0]["player_id"]
    return out


def main() -> None:
    slugs = slug_map()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    misses: list[str] = []
    done = 0
    # ponytail: sequential + fixed sleep; it's a one-shot 20-minute backfill.
    for pid, slug in sorted(slugs.items()):
        dest = OUT_DIR / f"{pid}.jpg"
        if dest.exists():
            continue
        req = urllib.request.Request(HEADSHOT_URL.format(slug=slug), headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if not resp.headers.get("Content-Type", "").startswith("image/"):
                    raise ValueError(f"{pid}: non-image response, aborting")
                data = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            misses.append(pid)
        else:
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(dest)
            done += 1
            if done % 25 == 0:
                print(f"{done} downloaded...", flush=True)
        time.sleep(DELAY_S)

    have = len(list(OUT_DIR.glob("*.jpg")))
    print(f"downloaded {done} new, {have}/{len(slugs)} total on disk")
    if misses:
        print(f"no photo on bbref for {len(misses)}: {', '.join(misses)}")


if __name__ == "__main__":
    main()
