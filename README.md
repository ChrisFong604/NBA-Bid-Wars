# NBA Blind-Auction Draft Bot

A Discord bot that runs a blind-auction NBA draft in a thread: players are
revealed one at a time from a hidden shuffled queue, managers bid live with
buttons, and every roster fills to five. The pool can span any run of eras
from the 1960s through the 2020s — every player is anchored to the decade of
their prime, and the commissioner picks the era range at creation. When the
draft completes, a tournament sim ranks the teams — pure stats, or stats
blended with an LLM ranking (via OpenRouter or any OpenAI-compatible router)
where primes face primes across eras (1991 Jordan vs 2016 Curry) — and crowns
a champion. State is snapshotted atomically to disk, so the bot survives a
mid-auction restart.

**The rules in brief:** everyone starts with the same budget (default $20).
Each revealed player stays on the block for one flat clock (default 60s) —
bids never extend it; the high bid when it expires wins. No bid means the
player is recycled once — on their second appearance (🔔 LAST CALL) they sell
or get force-assigned at $1. The pool is exactly five players per manager, so
every player ends up on a roster — zero leftovers. Go broke and you're a
spectator whose empty slots fill for free at the end; outlast everyone else
with money and you get free picks from the fully revealed pool. Any player
can occupy any lineup slot (`/swap` rearranges yours), and rosters/budgets
are always public on the board, which is reposted at the bottom of the
thread after every sale — no scrolling up.

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
| `LLM_API_KEY`   | no       | Only needed for sim mode `AI + stats` (e.g. an OpenRouter key); the stats-only sim needs no key |
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
   `clock` (15–300s each player stays on the block — flat, bids don't extend
   it; default 60), `sim` (**Off** / **Stats only** / **AI + stats**; default
   Off), and an era range (`era_from` / `era_to`, decade choices from
   **1960s** to **2020s**; default is all eras). Only players whose prime
   falls inside the range enter the pool — a narrow range with a big lobby
   may not be feasible, in which case `/draft start` asks you to widen the
   range or shrink the lobby. It spawns a `🏀 Draft — <date>` thread with a
   lobby; the creator is the commissioner.
2. Everyone clicks **Join** in the lobby (2–10 managers).
3. The commissioner runs `/draft start`. The board tracks budgets and
   rosters and is reposted at the bottom of the thread after every sale.
4. Bid with **+$1 / +$2 / +$5** or **Custom…** on each lot card. Bids at or
   above half your remaining budget ask for a confirm tap. The high bidder
   when the flat clock expires pays and the player slots into their team.
5. The last manager with money left picks the rest of their roster free via
   `/pick` (with autocomplete); everyone else's empty slots auto-fill.
6. When every roster is full, the tournament sim posts the standings and a
   champion — re-run it any time with `/simulate`. Stats-only mode needs no
   API key; **AI + stats** blends in an LLM ranking and falls back to stats
   with a note when `LLM_API_KEY` is unset.

Commissioner tools: `/draft pause`, `/draft resume`, `/draft addtime`,
`/draft kick @user [replacement]`, `/draft cancel`. Anyone: `/swap`,
`/status`, `/pick` (picker only).

## Refreshing the dataset

`draftbot/data/players.json` is a curated static file (~320 players spanning
every decade from the 1960s to the 2020s) — there is no runtime API
dependency, so draft night works offline. Each player appears once, anchored
to the decade of their prime, with prime-years stats and a display range
(e.g. Jordan → 1990s, `1989–1993`, CHI). Cards show only `ppg/rpg/apg`
because those are the three stats recorded in every era since 1960 — steals,
blocks, and efficiency metrics weren't tracked before the mid-1970s, so
using them would misrepresent older players. Star ratings (1–5⭐) are
era-relative: each player's prime is rated within their own era, so every
decade carries the full spread from fringe pickup to inner-circle superstar.
Edit or regenerate the file to taste; the loader validates positions and
duplicate ids (see `draftbot/dataset.py`).
