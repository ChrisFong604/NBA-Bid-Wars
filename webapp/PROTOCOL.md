# Web app — architecture & wire protocol

The web app is a thin real-time shell around the EXISTING pure engine:
`draftbot.engine.apply(state, event, rng)`, `draftbot.models`,
`draftbot.dataset`, `draftbot.sim`. No game rule may be reimplemented in the
web layer — same rulesets by construction. Discord (`draftbot.bot`,
`draftbot.ui`) is never imported here.

## Stack

- `webapp/server.py` — FastAPI app: room CRUD, one WebSocket per client,
  static file serving. Run: `uv run uvicorn webapp.server:app`.
- `webapp/rooms.py` — Room registry + per-room dispatch mirroring
  `DraftBot.apply_event`: `async with room.lock: engine.apply → commit →
  process ArmTimerFx/CancelTimerFx at commit time → broadcast`. Timer tasks
  fire `TimerExpired` exactly like the bot does.
- `webapp/views.py` — REDACTION layer (critical): what each client may see.
- `webapp/static/` — `index.html`, `app.js`, `style.css`. No build step,
  no frontend framework, no CDN. Vanilla ES modules.

## Identity

No auth. `POST /api/rooms` creates a room (code like `K7QX`), the creator
gets `{room, token, user_id}`; `POST /api/rooms/{code}/join` with
`{"name": ...}` returns the same shape. `user_id` is a server-assigned int
(engine speaks ints); `token` is the bearer for the WS. Tokens live in
`localStorage`; rejoining with a token reattaches to the same manager
(mirrors the bot's Join-reclaim rules — engine decides, not the server).

`POST /api/rooms` also accepts `"cpus"` (int 0–8, default 0): the room is
pre-seeded with that many computer opponents, applied right after creation
as an `AddCpu` event through the normal dispatch (the engine gates
capacity, not the server). It also accepts `"pool_depth"`
(`"legends"` | `"household"` | `"deep"`, default `"legends"`): how deep the
player pool goes — legends only (~20 per era), household names (~35), or
the full pool (~50 incl. the role-player tier). And `"mode"`
(`"auction"` | `"blind"` | `"snake"`, default `"auction"`): the draft
mode, validated server-side and echoed in the state view's `config.mode`
(see "Draft modes" below).

## Draft modes

- **auction** (default) — the classic flow documented throughout this
  file.
- **blind** — the auction flow UNTOUCHED (same phases, events, fx); the
  twist lives entirely in `views.py`: a player's NAME serializes as
  `null` until that player is rostered — the wire never carries a masked
  name. Masked (name `null`, stat card intact): the live lot's player,
  `free_pick.pool` entries, log entries of kind `"passed"`, and the
  `"passed"` fx payload. Always real: names of ROSTERED players — manager
  `spots`, the `sold`/`force`/`picked`/`autofill` fx and log entries,
  completion boards, and sim output. A sale/force/pick is the reveal.
  Masked cards also hide the `id` (dataset ids are name slugs): the lot
  player and `"passed"` fx carry `id: null`; blind `free_pick.pool`
  entries — the one masked surface that must stay clickable — carry a
  per-room salted opaque alias instead, and `{"action": "pick"}` echoes
  that alias (ONLY aliases resolve in blind mode, so a guessed real slug
  can never snipe a masked player by name).
- **snake** — no auction at all: the engine runs phase `"snake"`, a
  snaking turn order (managers order, reversing every round) over an OPEN
  pool. Every manager gets the fixed $15 budget (`SNAKE_BUDGET`;
  `config.budget` is ignored) and the sticker price is the player's star
  tier ($5 best … $1) — the one place that tier reaches the wire
  (`"stars"` itself is still never serialized). The manager on the clock
  sends the ordinary `{"action": "pick", "player_id": ...}`; the engine
  enforces turn, price, and the keep-$1-per-other-empty-slot reserve. An
  expired turn autopicks the best feasible player (no autopilot flip);
  managers with no feasible pick are force-assigned a bargain inside the
  same commit. Each new live turn is announced by the `snake_turn` fx and
  clocked by `state.pick_deadline` (`turn.deadline`); `addtime`, `pause`/
  `resume`, and eviction protection treat snake exactly like the other
  live phases.

## CPU opponents

CPU managers are ORDINARY managers inside the engine — negative
`user_id`s, `"cpu": true` in the manager view (public wire data; there is
nothing to redact), and every rule applies to them unchanged (bidding,
soft close, all-in showdowns, going broke, free pick, force-assign). The
server runs one per-room driver task (started when the draft starts if any
manager is a CPU) that polls the pure brain `draftbot.cpu.decide(state,
cpu_id, now)` outside the room lock and submits its chosen events through
the same dispatch as human actions — the engine remains the single
authority. Their bids/picks reach clients as perfectly ordinary `state`
broadcasts and `fx` payloads.

## Redacted state view — `views.state_view(state, viewer_id)`

NEVER serialize `state.queue` contents. The hidden pool is the whole game.
Two exceptions, both public by design: the `free_pick` reveal and snake
mode's open `pool`.

```json
{
  "phase": "auction", "you": 3, "commissioner": 1,
  "config": {"budget": 20, "lot_seconds": 30, "era_start": 2000,
              "era_end": 2020, "pool_depth": "legends", "sim": "prompt",
              "lineup_seconds": 60, "mode": "auction"},
  "queue_count": 12,
  "managers": [{"id": 1, "name": "Chris", "budget": 7, "autopilot": false,
                 "cpu": false,
                 "spots": [{"slot": "PG", "player": {...} | null, "price": 3}]}],
  "lot": {"seq": 4, "player": {"name": "Michael Jordan", "pos": "SG",
           "team": "CHI", "ppg": 31.5, "rpg": 6.3, "apg": 5.5,
           "decade": 1990, "prime": "1989–1993"},
          "last_call": false, "current_bid": 5, "leader": 2,
          "deadline": 1769480000.0,
          "lottery": {"participants": [2, 3], "entered": [2],
                       "your_guess": 42}},
  "free_pick": {"picker": 3, "pool": [{...player}], "deadline": ...} | null,
  "pool": [{...player, "price": 4}] | null,
  "turn": {"manager": 2, "deadline": 1769480000.0} | null,
  "pick_deadline": 0.0,
  "lineup_deadline": 0.0,
  "paused": false,
  "log": [{"kind": "sold", "player": "...", "manager": 2, "price": 5}]
}
```

`free_pick.pool` is only present during `free_pick` (the reveal is public,
same as Discord). Deadlines are epoch seconds; clients tick countdowns
locally (web renders live clocks natively — no Discord mobile caveat).

`pool` and `turn` are non-null ONLY during phase `"snake"`: `pool` is the
whole open pool, every entry a full `player_view` plus its sticker
`"price"` (the star tier — deliberately public; `"stars"` never rides the
wire in any mode), identical for every socket including spectators.
`turn` is the manager on the clock and their deadline
(`state.pick_deadline`).

In blind mode (`config.mode == "blind"`) the shapes above are unchanged
but `player.name` is `null` for the live lot's player and every
`free_pick.pool` entry, and `log[].player` is `null` for `"passed"`
entries. Rostered players (manager `spots`, non-passed log kinds) always
carry real names.

`lot.lottery` is the live all-in showdown, else `null`: `participants`
(user ids, leader first), `entered` (who has locked a number — public,
mirroring `LotteryGuessedFx`), and `your_guess` — the VIEWER'S own number
or `null`. Other players' numbers are NEVER serialized before the
`lottery_reveal` fx; `lot.deadline` is the showdown countdown while a
lottery is open.

## WebSocket `/ws/{room}?token=...`

Server→client messages:
- `{"type": "state", "now": <epoch seconds>, "state": <state_view>}` —
  after every commit (full view; no diffing, states are tiny). The
  top-level `now` is the server clock at send time: clients compute
  `offset = now - Date.now()/1000` and render every deadline countdown
  against the server clock, immune to local clock skew.
- `{"type": "fx", "fx": [{"kind": "sold"|"passed"|"force"|"picked"|
   "autofill"|"lottery_open"|"lottery_joined"|"lottery_guessed"|
   "lottery_cancelled"|"lottery_reveal"|"snake_turn"|"lineup_open"|
   "complete"|"paused"|"resumed"|"cancelled"|"autopilot", ...payload}]}` —
  render-worthy effects for toasts/feed, translated from engine effects
  (same vocabulary as `draftbot.ui`). `snake_turn` (snake mode) carries
  `manager` + `deadline` — that manager is now on the pick clock. In
  blind mode the `"passed"` payload's `player.name` is `null` (the player
  returns to the hidden queue unrostered); `sold`/`force`/`picked`/
  `autofill` always name players for real — the sale is the reveal.
  Showdown payloads: `lottery_open` carries
  `participants`/`amount`/`deadline`; `lottery_joined` carries `manager` +
  the grown `participants`; `lottery_guessed` carries only `manager`
  (never the number); `lottery_cancelled` carries `manager` (the new
  leader — the `state` broadcast has the new price/deadline);
  `lottery_reveal` carries `mystery`, `guesses`
  (`[{"manager", "guess"}]`, public at that point) and `winner`, and is
  followed by the normal `sold` fx in the same batch.
- `{"type": "error", "message": "..."}` — private, only to the acting
  socket (ErrorFx equivalent).
- `{"type": "chat", "from": 2, "name": "Bob", "text": "...", "at": epoch}`
  — room chat, broadcast to every socket. Social data only: it never
  touches the engine and carries the sender's display name so clients
  don't depend on roster lookups.
- `{"type": "chat_history", "messages": [<chat payloads>]}` — the last 50
  chat lines, sent once right after the initial `state` on every
  (re)connect.
- `{"type": "sim", "mode": "prompt"|"stats"|"ai", ...}` — at completion:
  `prompt` carries the full `share_prompt` text (client shows a copy
  button); `stats`/`ai` carry `standings`/`champion`/`summary`.

Client→server messages (server maps to engine events verbatim):
- `{"action": "start"}` `{"action": "bid", "increment": 1}` /
  `{"amount": 7}` — engine validates everything.
- `{"action": "pick", "player_id": "..."}` — the free-pick phase AND your
  snake turn (the engine adjudicates turn, price, and reserve).
- `{"action": "guess", "number": 42}` — showdown participants' 1-100 pick
  (resubmission overwrites; the engine gates participants/deadline/range).
- `{"action": "swap", "a": "PG", "b": "C"}`
- `{"action": "pause"} {"action": "resume"} {"action": "addtime",
   "seconds": 30} {"action": "kick", "target": 4} {"action": "cancel"}`
- `{"action": "add_cpu", "count": 1}` / `{"action": "remove_cpu",
   "cpu_id": -2}` — commissioner seats/removes computer opponents
   (lobby only; the engine gates commissioner, phase, and capacity —
   `count` defaults to 1).
- `{"action": "leave"}` — in the lobby this removes the manager;
  mid-draft it flips the leaver's team to autopilot (engine rule, same as
  Discord's Leave).
- `{"action": "chat", "text": "..."}` — seated managers only (tokenless
  spectators and CPUs are rejected). Server-side: stripped, truncated to
  280 chars, rate-limited to one message per second per manager; empty
  text is silently ignored.

Engine `Bid/Pick/...` events carry `now=time.time()` server-side. The
commissioner-gate stays IN the engine: the server passes the caller's
`user_id`; no Manage-Server fallback on the web (creator is commissioner).

## Sim modes

Same Config, same defaults (`sim="prompt"`). `prompt` → `sim.share_prompt`
text over WS + copy button. `stats`/`ai` → `sim.run_stats`/`run_ai`
server-side (`ai` needs `LLM_API_KEY` env, falls back to stats with a note —
same rule as the bot). `/api/rooms/{code}/simulate` re-runs — commissioner
only, matching Discord's `/simulate` gate.

## Web-first UX requirements (frontend)

- Lobby: create form (budget / clock / era range / player pool depth /
  sim mode / lineup window / CPU opponents 0–8), shareable room link
  (`/#K7QX`), join by name. The commissioner gets an "Add CPU 🤖" button plus a per-CPU
  remove ✖; CPU managers render with 🤖 in the lobby list and board.
- Auction: lot card with LIVE ticking countdown (client-side), +$1/+$2/+$5
  and custom-amount bid controls, board sidebar ALWAYS visible (managers,
  budgets, rosters), scrolling event feed. Layout: sidebar + main on
  desktop, stacked with sticky lot card on mobile (responsive, one
  breakpoint).
- All-in showdown: when `lot.lottery` is set the lot card grows a 🎰 strip
  naming the participants with the ticking countdown (`lot.deadline`);
  participants get a 1-100 input + Lock-in button (✓ + own number after
  the state ack, editable until the reveal); richer managers keep the live
  bid controls (outbidding cancels). The `lottery_reveal` fx renders a
  result card in the feed: mystery number, every guess with its distance,
  winner highlighted.
- Free pick: revealed pool as a clickable grid for the picker; spectators
  see the grid read-only.
- Draft modes: the create form offers auction / blind / snake (the budget
  input disables with a "$15 fixed" note in snake). Snake renders a turn
  banner (from `turn` + the `snake_turn` fx) over a priced pool grid
  (price badges, unaffordable cards dimmed); blind renders null names as
  "Mystery player" cards and the `sold` toast celebrates the reveal.
- Lineup window: countdown + the five slots; drag to rearrange via Pointer
  Events (works mouse + touch), plus tap-A-tap-B click-to-swap fallback.
  Both paths send `{"action": "swap"}` — the engine remains the authority.
- Completion: final rosters + sim output; `prompt` mode gets a one-click
  "Copy prompt" button (`navigator.clipboard`).
- No external assets: system font stack, emoji for icons, dark theme.

## Out of scope v1 (deliberate)

Snapshots/recovery (bot-only infra), auth, spectator links, room persistence
across server restarts, horizontal scaling. Rooms are in-memory with a
cleanup sweep: rooms idle past 1h are evicted, except a live draft
(auction/free_pick/snake/lineup) that is paused or still has connected
sockets. Evicted rooms close any remaining sockets with an `error`
message first, so no client keeps playing a ghost room.
