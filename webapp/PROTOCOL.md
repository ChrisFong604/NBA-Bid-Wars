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

## Redacted state view — `views.state_view(state, viewer_id)`

NEVER serialize `state.queue` contents. The hidden pool is the whole game.

```json
{
  "phase": "auction", "you": 3, "commissioner": 1,
  "config": {"budget": 20, "lot_seconds": 30, "era_start": 1960,
              "era_end": 2020, "sim": "prompt", "lineup_seconds": 60},
  "queue_count": 12,
  "managers": [{"id": 1, "name": "Chris", "budget": 7, "autopilot": false,
                 "spots": [{"slot": "PG", "player": {...} | null, "price": 3}]}],
  "lot": {"seq": 4, "player": {"name": "Michael Jordan", "pos": "SG",
           "team": "CHI", "ppg": 31.5, "rpg": 6.3, "apg": 5.5, "stars": 5,
           "decade": 1990, "prime": "1989–1993"},
          "last_call": false, "current_bid": 5, "leader": 2,
          "deadline": 1769480000.0},
  "free_pick": {"picker": 3, "pool": [{...player}], "deadline": ...} | null,
  "lineup_deadline": 0.0,
  "paused": false,
  "log": [{"kind": "sold", "player": "...", "manager": 2, "price": 5}]
}
```

`free_pick.pool` is only present during `free_pick` (the reveal is public,
same as Discord). Deadlines are epoch seconds; clients tick countdowns
locally (web renders live clocks natively — no Discord mobile caveat).

## WebSocket `/ws/{room}?token=...`

Server→client messages:
- `{"type": "state", "state": <state_view>}` — after every commit (full
  view; no diffing, states are tiny).
- `{"type": "fx", "fx": [{"kind": "sold"|"passed"|"force"|"picked"|
   "autofill"|"lineup_open"|"complete"|"paused"|"resumed"|"cancelled"|
   "autopilot", ...payload}]}` — render-worthy effects for toasts/feed,
  translated from engine effects (same vocabulary as `draftbot.ui`).
- `{"type": "error", "message": "..."}` — private, only to the acting
  socket (ErrorFx equivalent).
- `{"type": "sim", "mode": "prompt"|"stats"|"ai", ...}` — at completion:
  `prompt` carries the full `share_prompt` text (client shows a copy
  button); `stats`/`ai` carry `standings`/`champion`/`summary`.

Client→server messages (server maps to engine events verbatim):
- `{"action": "start"}` `{"action": "bid", "increment": 1}` /
  `{"amount": 7}` — engine validates everything.
- `{"action": "pick", "player_id": "..."}`
- `{"action": "swap", "a": "PG", "b": "C"}`
- `{"action": "pause"} {"action": "resume"} {"action": "addtime",
   "seconds": 30} {"action": "kick", "target": 4} {"action": "cancel"}`
- `{"action": "leave"}` (lobby only — engine rule).

Engine `Bid/Pick/...` events carry `now=time.time()` server-side. The
commissioner-gate stays IN the engine: the server passes the caller's
`user_id`; no Manage-Server fallback on the web (creator is commissioner).

## Sim modes

Same Config, same defaults (`sim="prompt"`). `prompt` → `sim.share_prompt`
text over WS + copy button. `stats`/`ai` → `sim.run_stats`/`run_ai`
server-side (`ai` needs `LLM_API_KEY` env, falls back to stats with a note —
same rule as the bot). `/api/rooms/{code}/simulate` re-runs (commissioner
or any manager, matching `/simulate`'s draft-member gate).

## Web-first UX requirements (frontend)

- Lobby: create form (budget / clock / era range / sim mode / lineup
  window), shareable room link (`/#K7QX`), join by name.
- Auction: lot card with LIVE ticking countdown (client-side), +$1/+$2/+$5
  and custom-amount bid controls, board sidebar ALWAYS visible (managers,
  budgets, rosters), scrolling event feed. Layout: sidebar + main on
  desktop, stacked with sticky lot card on mobile (responsive, one
  breakpoint).
- Free pick: revealed pool as a clickable grid for the picker; spectators
  see the grid read-only.
- Lineup window: countdown + the five slots; drag to rearrange via Pointer
  Events (works mouse + touch), plus tap-A-tap-B click-to-swap fallback.
  Both paths send `{"action": "swap"}` — the engine remains the authority.
- Completion: final rosters + sim output; `prompt` mode gets a one-click
  "Copy prompt" button (`navigator.clipboard`).
- No external assets: system font stack, emoji for icons, dark theme.

## Out of scope v1 (deliberate)

Snapshots/recovery (bot-only infra), auth, spectator links, room persistence
across server restarts, horizontal scaling. Rooms are in-memory with a
cleanup sweep (complete/cancelled rooms evicted after 1h idle).
