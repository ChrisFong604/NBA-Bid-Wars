# NBA Blind-Auction Draft Bot

A Discord bot that runs a blind-auction NBA draft in a thread: players are
revealed one at a time from a hidden shuffled queue, managers bid live with
buttons, and every roster fills to five. The pool can span any run of eras
from the 1960s through the 2020s — every player is anchored to the decade of
their prime, and the commissioner picks the era range at creation. When the
draft completes, Claude simulates a tournament between the drafted teams —
primes face primes across eras (1991 Jordan vs 2016 Curry) — and crowns a
champion. State is snapshotted atomically to disk, so the bot survives a
mid-auction restart.

**The rules in brief:** everyone starts with the same budget (default $20).
Each revealed player gets a 20s opening window; the first bid starts a 10s
hammer timer that resets on every raise. No bid means the player is recycled
once — on their second appearance (🔔 LAST CALL) they sell or get
force-assigned at $1. Go broke and you're a spectator whose empty slots fill
for free at the end; outlast everyone else with money and you get free picks
from the fully revealed pool. Any player can occupy any lineup slot
(`/swap` rearranges yours), and rosters/budgets are always public on the
pinned board.

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
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot%20applications.commands&permissions=309237999616
```

The permissions integer `309237999616` covers exactly: Send Messages, Create
Public Threads, Send Messages in Threads, Embed Links, Manage Messages (to pin
the board), Read Message History, and Use External Emojis. It was computed
with:

```bash
uv run python -c "import discord; print(discord.Permissions(send_messages=True, create_public_threads=True, send_messages_in_threads=True, embed_links=True, manage_messages=True, read_message_history=True, use_external_emojis=True).value)"
```

### 3. Environment variables

| Variable            | Required | Purpose                                                        |
| ------------------- | -------- | -------------------------------------------------------------- |
| `DISCORD_TOKEN`     | yes      | Bot token from the Developer Portal                             |
| `ANTHROPIC_API_KEY` | no       | Enables the post-draft tournament sim (skipped without it)      |
| `TEST_GUILD_ID`     | no       | Guild ID to mirror slash commands into for instant availability |

Global slash-command sync can take **up to an hour** to propagate; setting
`TEST_GUILD_ID` additionally syncs the commands to that one server instantly,
which is what you want while testing.

### 4. Run

```bash
uv sync
uv run python -m draftbot
```

Snapshots land in `snapshots/` next to the package; if the bot restarts
mid-draft it re-opens the interrupted player and carries on.

## How to play

1. `/draft create` in a text channel — options for budget, opening window,
   hammer timer, the sim, and an era range (`era_from` / `era_to`, decade
   choices from **1960s** to **2020s**; default is all eras). Only players
   whose prime falls inside the range enter the pool — a narrow range with a
   big lobby may not be feasible, in which case `/draft start` asks you to
   widen the range or shrink the lobby. It spawns a `🏀 Draft — <date>`
   thread with a lobby; the creator is the commissioner.
2. Everyone clicks **Join** in the lobby (2–10 managers).
3. The commissioner runs `/draft start`. The pinned board tracks budgets and
   rosters.
4. Bid with **+$1 / +$2 / +$5** or **Custom…** on each lot card. Bids at or
   above half your remaining budget ask for a confirm tap. The high bidder
   when the hammer falls pays and the player slots into their team.
5. The last manager with money left picks the rest of their roster free via
   `/pick` (with autocomplete); everyone else's empty slots auto-fill.
6. When every roster is full, the tournament sim posts game recaps and a
   champion — re-run it any time with `/simulate`.

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
