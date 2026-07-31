# NBA Blind-Auction Draft Bot

A Discord bot that runs a blind-auction NBA draft in a thread: players are
revealed one at a time from a hidden shuffled queue, managers bid live with
buttons, and every roster fills to five. The pool can span any run of eras
from the 1960s through the 2020s — every player is anchored to the decade of
their prime, and the commissioner picks the era range at creation. When the
draft completes, the bot posts a copy-pastable tournament prompt for your own
LLM by default (no API key, no cost) — or runs the sim itself: pure stats, or
stats blended with an LLM ranking (via OpenRouter or any OpenAI-compatible
router) where primes face primes across eras (1991 Jordan vs 2016 Curry) —
and crowns a champion. State is snapshotted atomically to disk, so the bot
survives a mid-auction restart.

**The rules in brief:** everyone starts with the same budget (default $20).
Each revealed player stays on the block for one flat clock (default 30s) —
a bid in the final 10 seconds adds 5 more (soft close — no last-second
sniping); the high bid when it expires wins. If the leader is all-in and you
hold exactly that amount, matching it triggers a 🎰 **all-in showdown**: each
tied manager secretly picks a number 1–100 and the closest to the bot's
mystery number buys the player (15s clock; a richer manager can still break
it up by bidding higher). No bid means the
player is recycled once — on their second appearance (🔔 LAST CALL) they sell
or get force-assigned at $1. The pool is exactly five players per manager, so
every player ends up on a roster — zero leftovers. The commissioner also
picks how deep the pool goes: legends only (~20 per era), household names
(~35), or the full deep pool (~50, stars + role players — the default). Go broke and you're a
spectator whose empty slots fill for free at the end; outlast everyone else
with money and you get free picks from the fully revealed pool. Any player
can occupy any lineup slot (`/swap` rearranges yours), and rosters/budgets
are always public on the board, which is reposted at the bottom of the
thread after every sale — no scrolling up. When the last roster fills,
everyone gets ~60s to arrange their lineup — tap **🔀 Arrange my lineup** or
use `/swap` — then lineups lock and the sim runs on the final arrangements.

## Setup

### 1. Create the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications)
   and click **New Application**.
2. Go to **Bot** → **Reset Token** and copy the token (this is your
   `DISCORD_TOKEN`).
3. No privileged intents are needed — leave Presence, Server Members, and
   Message Content **off**.

### 2. Invite the bot

Use this URL with your application's client ID (from **General Information**):

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=309237991424
```

The permissions integer `309237991424` covers exactly: Send Messages, Create
Public Threads, Send Messages in Threads, Embed Links, Read Message History,
and Use External Emojis. It was computed with:

```bash
uv run python -c "import discord; print(discord.Permissions(send_messages=True, create_public_threads=True, send_messages_in_threads=True, embed_links=True, read_message_history=True, use_external_emojis=True).value)"
```

### 3. Environment variables

| Variable        | Required | Purpose                                                                     |
| --------------- | -------- | --------------------------------------------------------------------------- |
| `DISCORD_TOKEN` | yes      | Bot token from the Developer Portal                                          |
| `LLM_API_KEY`   | no       | Only needed for sim mode `AI + stats` (e.g. an OpenRouter key); the prompt and stats-only modes need no key |
| `LLM_BASE_URL`  | no       | OpenAI-compatible endpoint; default `https://openrouter.ai/api/v1`           |
| `SIM_MODEL`     | no       | Model slug for the sim; default `anthropic/claude-sonnet-4.5`                |
| `TEST_GUILD_ID` | no       | Guild ID to mirror slash commands into for instant availability              |

The sim speaks the OpenAI-compatible chat API, so any router works —
OpenRouter, LiteLLM, Groq, or a local Ollama. Point `LLM_BASE_URL` at the
endpoint and set `SIM_MODEL` to whatever model slug it serves.

Global slash-command sync can take **up to an hour** to propagate; setting
`TEST_GUILD_ID` additionally syncs the commands to that one server instantly,
which is what you want while testing.

### 4. Run

```bash
uv sync
uv run python -m draftbot
```

Snapshots land in `snapshots/` next to the package (override with
`SNAPSHOT_DIR`); if the bot restarts mid-draft it re-opens the interrupted
player and carries on.

### Deploying on a free GCP e2-micro

Google's always-free tier covers one `e2-micro` VM — enough for this bot.
When creating the instance, the free tier only applies if you pick exactly:
region **us-west1, us-central1, or us-east1**, machine type **e2-micro**,
and a **standard** persistent disk (not balanced/SSD, ≤30 GB). Debian 12+.
Then:

```bash
gcloud compute ssh <instance-name>          # or SSH from the console
git clone <your-repo-url> && sudo bash nba-draft-bot/deploy/setup.sh <your-repo-url>
sudo nano /etc/draftbot.env                 # paste real tokens
sudo systemctl start draftbot
journalctl -u draftbot -f                   # watch it come up
```

The script installs uv, creates a `draftbot` system user under
`/opt/nba-draft-bot`, adds 1 GB of swap (installs can OOM 1 GB of RAM),
and installs a systemd unit that restarts the bot on crash and boot. For a
private GitHub repo, clone with a fine-grained token URL
(`https://<token>@github.com/you/nba-draft-bot.git`) or make the repo public.

## How to play

1. `/draft create` in a text channel — options: `budget` (default $20),
   `clock` (15–300s each player stays on the block — late bids extend
   it; default 30), `lineup` (0–300s to arrange lineups after the last
   roster fills; default 60, 0 skips the window), `sim` (**Prompt for your own LLM** / **Off** /
   **Stats only** / **AI + stats**; default Prompt — a copy-pastable prompt
   you run in your own LLM), `pool` (**Legends only** ~20 per era /
   **Household names** ~35 / **Deep** ~50 incl. the role-player tier;
   default Deep — how deep the player pool goes), and an era range
   (`era_from` / `era_to`, decade choices from
   **1960s** to **2020s**; default is all eras). Only players whose prime
   falls inside the range enter the pool — a narrow range or shallow pool
   with a big lobby may not be feasible, in which case `/draft start` asks
   you to widen the era range, deepen the pool, or shrink the lobby (legends
   on a single decade maxes out at 4 managers). It spawns a
   `🏀 Draft — <date>` thread with a
   lobby; the creator is the commissioner.
2. Everyone clicks **Join** in the lobby (2–10 managers). Short on humans?
   The commissioner can seat computer opponents with `/draft addcpu [count]`
   — CPUs are ordinary managers (they bid, join showdowns, go broke, and
   free-pick like anyone else), so a solo 1v1 works: you plus one CPU.
   `/draft removecpu` removes the newest one.
3. The commissioner runs `/draft start`. The board tracks budgets and
   rosters and is reposted at the bottom of the thread after every sale.
4. Bid with **+$1 / +$2 / +$5** or **Custom…** on each lot card. The high bidder
   when the flat clock expires pays and the player slots into their team.
   - **🎰 All-in showdown:** if the leader's bid is their entire budget and
     yours is exactly the same amount, bidding it joins a showdown instead
     of being rejected — tap **🎲 Pick my number** and pick 1–100 in secret
     (15s; no pick gets a random one). Closest to the bot's mystery number
     wins the player at that price; a richer manager can cancel the whole
     thing by simply bidding more on the lot card.
5. The last manager with money left picks the rest of their roster free via
   `/pick` (with autocomplete); everyone else's empty slots auto-fill.
6. When the last roster fills, everyone gets ~60s to arrange their lineup —
   tap **🔀 Arrange my lineup** (an ephemeral panel with two selects: move
   this player… into this slot) or use `/swap`; then lineups lock and the
   sim prompt drops. Set `lineup: 0` at creation to skip straight to the
   results.
7. When every roster is full, the default **Prompt** mode posts a
   copy-pastable prompt — paste it into your favorite LLM (ChatGPT, Claude,
   Gemini) and it runs the tournament for you; no API key, no cost. The
   bot-run modes post the standings and a champion directly: stats-only
   needs no key, **AI + stats** blends in an LLM ranking and falls back to
   stats with a note when `LLM_API_KEY` is unset. Re-run (or re-post) any
   time with `/simulate`.

Commissioner tools: `/draft addcpu [count]`, `/draft removecpu`,
`/draft pause`, `/draft resume`, `/draft addtime`,
`/draft kick @user [replacement]`, `/draft cancel`. Anyone: `/swap`,
`/status`, `/pick` (picker only).

## Web app

The same game, browser-first, lives in `webapp/` — it imports the identical
engine (`draftbot.engine`), so the rules can't drift. Run it with:

```bash
uv run uvicorn webapp.server:app
```

then open `http://localhost:8000`, create a room, and share the room link.
Live ticking countdowns, always-visible board, drag-to-rearrange lineups,
and a one-click copy button for the tournament prompt. Architecture and
wire protocol: `webapp/PROTOCOL.md`.

## Refreshing the dataset

`draftbot/data/players.json` is a generated static file (350 players — 50
per decade, 1960s through 2020s) — there is no runtime API dependency, so
draft night works offline. It is built by `scripts/build_dataset.py`
(stdlib-only, deterministic) from **"NBA Stats (1947-present)" by Sumitro
Datta on Kaggle (CC0 public domain)**, fetched from its GitHub mirror and
cached in `scripts/.cache/`:

```bash
uv run python scripts/build_dataset.py   # rewrites players.json + prints a report
```

Methodology: NBA-league seasons only (ABA/BAA excluded as pace/era
outliers); each player's **prime** is the consecutive 3-season window
maximizing `(pts + 0.7*trb + 0.9*ast) * min(g/65, 1)`, and the player
appears once, anchored to the decade of that window's midpoint. Cards show
only `ppg/rpg/apg` (games-weighted prime means) because those are the three
stats recorded in every era since 1960 — steals, blocks, and efficiency
metrics weren't tracked before the mid-1970s, so using them would
misrepresent older players. Composition follows a **1:1 star/role rule**:
per decade and position, the top 5 by caliber (prime value + a 5% boost per
All-Star selection) plus the next 5 — the JJ Redick / Tyson Chandler tier —
so every era offers both headliners and glue guys. Star ratings (1–5⭐) are
era-relative: each decade's 50 are ranked by caliber and banded into the
same pyramid (4×5⭐ … 10×1⭐), so every decade carries the full spread from
fringe pickup to inner-circle superstar. Each player also carries `rank` —
his 1-based caliber order within his decade × position bucket (1–10) — which
powers the pool-depth modes (legends keeps ranks 1–4, household 1–7, deep
all). The loader validates positions,
duplicate ids, and rank completeness per bucket (see `draftbot/dataset.py`).
