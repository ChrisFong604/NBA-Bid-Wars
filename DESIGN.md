# NBA Blind-Auction Draft Bot — Design & Build Plan

A Discord bot that runs a blind-auction NBA draft: players are revealed one at a
time from a hidden shuffled queue, managers bid live with buttons, and when every
roster is full a sim ranks the drafted teams (pure stats, optionally blended with
an LLM ranking) and crowns a champion.

**Terminology:** *managers* = the humans drafting. *players* = NBA players in the pool.

---

## 1. Game Rules (v1)

### Setup
1. `/draft create` in a channel spawns a public thread for the draft. The creator
   is the **commissioner**. Options (all have defaults): budget `$20`, lot clock
   `60s` (flat — bids never extend it), sim mode `off`/`stats`/`ai` (default
   `ai`), max managers `10`, and an **era range** in 10-year increments —
   anywhere from the 1960s through the 2020s (e.g. 2000s–2020s, or
   1960s–1990s). Default is all eras. Each player belongs to the decade of
   their prime; only players whose prime falls inside the range enter the pool.
2. Managers join via a Join button in the lobby message. 2–10 managers. Once the
   draft starts, **no one can join**.
3. On `/draft start` the bot builds the pool: **exactly 5N players** —
   stratified best-effort (up to N per natural position PG/SG/SF/PF/C, any
   shortfall filled from other positions), shuffled into a hidden queue.
   Pool == total slots, so every pool player ends the draft on a roster.
   Managers never see the queue — only players already resolved, the one on the
   block, and the remaining-pool count.

### The auction loop
4. The next player is revealed as a card: name, team, position, last-season
   `ppg/rpg/apg`, and a 1–5 ⭐ rating derived from those stats.
5. **Flat clock (default 60s):** each lot gets one countdown, armed at reveal —
   bids never extend it. Any eligible manager may open at any integer from $1
   up to their entire remaining budget — jump openings allowed, and going all-in
   ($20 on the very first player) is legal.
6. **No opening bid → PASSED.**
   - **First pass:** the player is recycled to the back of the hidden queue —
     they'll come back around, possibly when you're broke.
   - **Second pass (LAST CALL):** a player making their second appearance is
     badged **🔔 LAST CALL** on the card. If nobody opens, they are
     **force-assigned at $1 to a random active team with the most open slots**
     (active = still has money and empty slots, see #12; broke teams can't be
     charged — their slots fill for free at the end, #14).
   - Why not unlimited recycling: with free recycling, passing costs nothing —
     managers can fish through the whole "hidden" queue, early bids collapse, and
     the draft can stall for hours (worst case is provably O(N²) reveals). The
     pass-once rule keeps the "they might come back" tension while making every
     second appearance final, which also guarantees the draft ends in at most
     2 × 5N reveals. Unlimited recycle stays available as a config flag if
     the group prefers it.
7. **Bidding:** all bids race the same flat clock — nothing resets it. When it
   expires, the high bidder wins (no bid → #6).
8. **Bid validity:**
   - Integer dollars, strictly greater than the current bid.
   - **No reserve — your money is yours to burn.** Max bid = your entire
     remaining budget; nothing protects you from yourself. The price of going
     broke is #11: no more bidding, and your empty slots get filled randomly at
     the end.
   - The current leader cannot raise their own bid.
   - Managers with full rosters cannot bid.
   - Quick buttons **+$1 / +$2 / +$5** raise the *live* current bid at the moment
     the server processes the click (two near-simultaneous +$1 clicks become $6
     then $7 — both succeed). The **Custom…** modal takes an absolute amount and
     is rejected if ≤ current by the time it's submitted.
   - Server arrival order is authoritative; bids landing after the timer fires
     are void. Bids are binding — no retraction.
9. **Sold:** winner pays, the player is auto-placed into their first empty slot.
   Placement is fully flexible — **any player can occupy any slot** (a center can
   play point guard). `/swap` rearranges your own slots any time before the draft
   ends. Slots therefore never block a bid; they're your lineup for the sim.
10. Budgets and rosters are **public** at all times on the board, which is
    reposted at the bottom of the thread after every resolution.

### Going broke & the endgame
11. **Broke = spectator.** At $0 with empty slots you can't bid (minimum bid is
    $1). You watch the rest of the auction; your empty slots are filled for
    free at the end (#14).
12. A manager is **active** while they have empty slots, at least $1, and
    aren't on autopilot. The auction loop (#4–#9) runs while **two or more**
    managers are active.
13. **Free-pick phase:** the moment exactly one manager is active, the auction
    stops and the remaining pool — including every player never revealed — is
    posted for all to see. The last solvent manager picks whoever they want,
    free of charge, until their roster is full (60s per pick; idling flips
    them to autopilot and the draft proceeds to #14). Outlasting the spenders
    buys total information and free choice — that's the built-in reward for
    hoarding, so leftover budget needs no other value.
14. **Auto-fill:** once nobody is active, every broke/autopilot team's empty
    slots are filled one at a time with random players from the remaining
    pool, free. The pool is exactly total slots (5N), so auto-fill drains it
    to zero — no leftovers.
15. The draft ends when every team has 5 players. LAST CALL (#6) bounds the
    auction phase at 2 × 5N reveals, so no stall is possible.
16. **AFK / leavers:** a manager with no interaction for 10 consecutive lots is
    flagged **autopilot** — their team never bids and fills at auto-fill.
    Someone who leaves goes autopilot; rejoining reclaims the team. The
    commissioner can `/draft kick @user [replace:@user2]` — a replacement
    inherits roster and budget verbatim.

### The winner — tournament sim
17. When the draft completes (sim mode ≠ `off`), the bot ranks every roster by
    prime stats; in `ai` mode one LLM call adds a second ranking blended
    60/40 (stats/LLM) into the final standings, plus a short "how it played
    out" summary. One standings embed, one champion. `stats` mode is
    deterministic and needs no API key; `ai` without a key falls back to
    stats with a note.

### Commissioner controls
18. `/draft pause`, `/draft resume`, `/draft addtime <s>`, `/draft kick`,
    `/draft cancel` (destructive ones behind a confirm button). Commissioner is
    the creator; `Manage Server` permission is the fallback.

---

## 2. Discord UX

Findings from the UX research, discord.py-flavored:

- **One public thread per draft** (`autoArchiveDuration=1440`). The thread's
  scrollback *is* the auction log. Board message + one new message per lot;
  closed lots become green (✅ SOLD $7 — Jalen Suggs) / grey (➖ PASSED)
  tombstones.
- **Lot card:** embed with author line `Lot #14 · 23 players left in the pool`,
  title `🏀 Jalen Suggs — PG`, fields for Current Bid / Leader / Status, and a
  footer warning: *"flat clock — bids never add time · bid up to your full
  remaining budget; hit $0 and you're done bidding."* LAST CALL players get
  the 🔔 badge and a warning line.
- **Countdown with zero edit spam:** deadlines render as Discord relative
  timestamps (`discord.utils.format_dt(dt, style='R')`) — the client ticks them
  live. The embed is only edited when a bid lands (which changes price/leader
  anyway) and once at close. The server deadline is authoritative; client clocks
  can be ±2s off.
- **Buttons:** one row — `[+$1] [+$2] [+$5] [Custom…]`. `custom_id`s encode
  `bid:<draftId>:<lotSeq>:...`; `lotSeq` is the stale-click guard (clicks on a
  closed lot get an ephemeral "that auction already closed"). Buttons are never
  per-user — all legality checks are server-side with ephemeral errors like
  *"You've only got $6 left."*
- **Persistent views:** `discord.ui.View(timeout=None)` with fixed custom_ids,
  registered via `bot.add_view()` on startup — survives restarts, no dead
  buttons (the default 180s View timeout would silently kill the lot mid-
  auction).
- **The 15-minute token trap:** all persistent messages (lot, board, prompts)
  are sent with `channel.send()` and edited by message ID — never via
  interaction tokens, which expire after 15 minutes and don't exist at all for
  timer-driven closes. Interactions are only used in the moment:
  `interaction.response.edit_message()` for bid acks (atomically acks + edits,
  free of channel rate-limit buckets), `send_modal()` for Custom (must be the
  *first* response — can't defer first), ephemeral replies for errors.
- **Board:** one embed, one inline field per manager
  (`🟢 Chris — $9 left`, or `💸 BROKE` at $0, plus roster lines). After every
  resolution it's **reposted fresh at the bottom of the thread** (the old one
  is deleted — nobody scrolls up), with a 2s trailing debounce coalescing
  rapid sales into one repost.
- **Close sequencing:** terminal lot edit → board edit → ~2s beat → next lot
  send, to stay friendly with the per-channel rate bucket.
- **Free-pick phase UX:** the remaining pool is posted as a reveal embed, and
  the last solvent manager picks via `/pick <player>` with autocomplete
  (select menus cap at 25 options; autocomplete doesn't). Each pick gets a 60s
  `<t:R>` deadline; auto-fill results post as a rapid-fire sequence of short
  embeds.

---

## 3. Architecture (Python + discord.py)

Single process, minimal dependencies: `discord.py`, `openai`, and `nba_api`
(dataset script only). No database server.

```
src/
  engine.py      # pure game logic — zero discord imports, zero I/O, zero timers
  models.py      # dataclasses: DraftState, Manager, Lot, Config
  store.py       # atomic JSON snapshot load/save
  bot.py         # discord.py wiring: commands, views, embeds, timers
  sim.py         # post-draft Claude tournament sim
data/players.json
scripts/build_dataset.py
tests/
```

- **Pure engine:** everything is `apply(state, event) -> (new_state, effects)`.
  Events: `join, leave, start, bid(manager, amount, lot_seq), timer_expired(lot_seq),
  pick(manager, player_id), swap, pause, resume, kick`. Effects are descriptions (`Sold(...)`,
  `ArmTimer(ms)`, `ForceAssign(...)`), not actions — the Discord layer renders
  them. Time is just an event, so tests fast-forward by feeding events.
  Immutable state transitions (new objects each step) per house style.
- **Concurrency:** one `asyncio.Lock` per draft. Every transition — button
  click, modal submit, timer fire, commissioner command — acquires it, applies
  the event, commits, then does Discord I/O outside the critical section.
- **Timers:** one `asyncio` task per lot, armed once at reveal for the flat
  `lot_seconds` clock — bids never touch it. `deadline` (epoch) lives in state
  and is the source of truth (only pause/resume and `/draft addtime` shift
  it, re-arming the task). When a timer fires it re-checks, under the lock,
  that its `lot_seq` and deadline still match state before closing the lot —
  this closes the fired-while-addtime-was-landing race (the "close-cycle
  token" pattern from prior art).
- **Persistence:** in-memory state is authoritative; one JSON file per draft
  (`snapshots/<thread_id>.json`), written atomically (`tmp` + `os.replace`)
  **only at lot boundaries** (created/started/sold/passed/swap/complete),
  deleted on completion. A few KB, human-readable, zero native deps.
- **Crash recovery:** resume the draft, restart the current lot. Lot-boundary
  snapshots mean the interrupted player is still at the head of the queue; on
  boot the bot posts a fresh lot message ("bot restarted — re-opening at $1")
  with fresh timers. ~20 lines, no reconciliation.

### Testing
- `engine.py` unit tests: all-in bids, broke-manager lockout, self-raise
  rejection, stale-lot bids, pass-once → LAST CALL → force-assign (active
  teams only), active-count phase transitions, free-pick, auto-fill,
  autopilot, termination.
- **Simulation test:** random bidders through 1,000 full drafts asserting
  invariants after every event — budgets never overdrawn, rosters ≤ 5, pool
  never runs dry, every draft reaches COMPLETE with all rosters full within
  the reveal bound. This one test carries most of the coverage.
- Discord layer stays a thin adapter, verified by hand on a test server.
- Store: one write → "crash" → load → resume test.

---

## 4. Player Dataset (cross-era, 1960s–2020s)

- `draftbot/data/players.json` is a **curated static file spanning every
  decade from the 1960s to the 2020s** (~320+ players). Each player appears
  once, anchored to the **decade of their prime**, with **prime-years stats**
  and a display range (e.g. Jordan → 1990s, `1989–1993`, CHI). No runtime API
  dependency; works offline on draft night.
- **Era-safe stats only:** the card shows `ppg/rpg/apg` — the three stats
  recorded in every era since 1960. Steals, blocks, 3PT, and efficiency
  metrics are deliberately excluded because they weren't tracked before the
  mid-1970s; using them would misrepresent older players.
- **Era-relative stars:** `stars` (1–5 ⭐) is an editorial rating of the
  player's prime *within their own era* — a raw `ppg+rpg+apg` formula would
  crown every pace-inflated 1960s stat line. 5★ = inner-circle superstar,
  3★ = quality starter, 1★ = fringe/role player; every decade carries the full
  spread so cheap gambles exist in every era.
- Positions use the modern PG/SG/SF/PF/C mapping by playstyle (Oscar
  Robertson → PG, Havlicek → SF).
- Coverage: ≥ 30 players and ≥ 6 per position in every decade (deeper from the
  1990s on). A narrow era range with a big lobby can be infeasible — the pool
  builder errors loudly and the bot tells the host to widen the range or
  shrink the lobby. Names/teams/positions/stats are uncopyrightable facts.
- Record shape: `{id, name, team, pos, ppg, rpg, apg, stars, decade, prime}`.

---

## 5. Tournament Sim (`sim.py`)

- Three modes on `Config.sim`: `off`, `stats`, `ai`. `ai` without
  `LLM_API_KEY` downgrades to `stats` with a note — the sim never blocks on
  a missing key.
- **`stats`** — deterministic, no network: rank teams by summed player score
  `4*stars + 0.35*ppg + 0.5*rpg + 0.7*apg`, name tie-break.
- **`ai`** — the stats ranking plus one LLM call: Python `openai` SDK pointed
  at an OpenAI-compatible router (`LLM_BASE_URL`, default OpenRouter), model
  from `SIM_MODEL` (default **`anthropic/claude-sonnet-4.5`**), `LLM_API_KEY`
  from env. One request with all rosters (slot assignments, prime
  stats/years/era); the prompt states **primes face primes** — 1991 Jordan vs
  2016 Curry — and invites cross-era style clashes for flavor in the summary.
  The reply is a permutation-validated Pydantic `LlmRanking{ranking, summary}`
  (fence/prose-tolerant). Final score = `0.6*stats + 0.4*LLM` rank points;
  ties resolve in stats order.
- The bot posts one 🏆 standings embed (champion title, ranked scores, the
  LLM's summary when present). On API failure: report the error and point at
  `/simulate` to retry — never fake a result.

---

## 6. Config Defaults

```python
DEFAULTS = dict(
    budget=20, roster_size=5, slots=["PG", "SG", "SF", "PF", "C"],
    lot_seconds=60,          # flat clock — bids never extend it
    quick_bids=[1, 2, 5], max_managers=10,
    pass_rule="pass_once",   # or "recycle_forever" if the group insists
    placement="any",
    afk_lots=10, free_pick_seconds=60,
    sim="ai",                # "off" | "stats" | "ai"
)
```

Config is frozen into draft state at `/draft start` so snapshots and restarts
honor it.

---

## 7. Build Plan

| Phase | Deliverable (independently demoable) | Effort |
|---|---|---|
| **1. Pure engine** | `engine.py` + tests + 1,000-draft simulation test green; CLI script plays a full draft with scripted bids against a 30-player stub | 30% |
| **2. Discord wiring** | Thread + lobby, lot embeds with bid buttons + custom modal, `<t:R>` countdowns, sold/passed tombstones, reposted board, `/swap`, fast mode. Demo: real 3-human draft on a test server | 35% |
| **3. Persistence & recovery** | Atomic snapshots, boot-time resume, restart-current-lot. Demo: `kill -9` mid-bid, restart, draft continues | 10% |
| **4. Real dataset** | `build_dataset.py` → `players.json` (~200 players). Demo: draft with real names | 10% |
| **5. LLM sim** | `sim.py` + game recap embeds + champion card. Demo: full draft → simulated tournament | 10% |
| **6. Hardening** | Commissioner controls, AFK autopilot, config options on `/draft create`, friendly error copy, end-of-draft summary | 5% |

Order is dependency order; phase 4 can run in parallel with 2–3. After phase 2
a real draft night already works; 3–6 make it survive one.

**Deliberately skipped** (add only if someone asks): database server, web
dashboard, multi-guild sharding, player headshots (image rights are the one
murky asset — names and stats are safe), i18n.

---

## 8. Prior Art

No fork candidate exists (everything in the space is 0–2 star hobby code, and
the closest match is unlicensed). Borrowing instead:

- **Saejdot23/Auction_bot** (Apache-2.0, Python/discord.py): the
  nominate → countdown → cancel-on-bid → sold/unsold → advance-queue loop and
  JSON-with-backup persistence.
- **Srfw74/Auction-bot** (patterns only — no license): button increments +
  custom-bid modal, close-cycle stale-timer token, per-user bid cooldown.
- **FroostySnoowman/BiddingBot**: discord.py modal validation + ephemeral
  error UX.
