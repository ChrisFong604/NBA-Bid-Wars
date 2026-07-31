# Design brief: NBA cross-era fantasy draft web app

Design a complete website UI for a real-time multiplayer NBA fantasy draft
game. Friends join a room and draft 5-player lineups of the greatest NBA
players across eras (1960s–2020s), then settle the winner with a simulated
tournament. It is fast, social, and a little chaotic — think "game night,"
not "fantasy spreadsheet." Design every screen, component, and state listed
below; the rules are exhaustive on purpose so nothing is missed.

## Product facts

- Real-time rooms: one host ("commissioner") creates a room, gets a 4-letter
  room code and shareable link; 2–10 managers total, humans join by typing a
  display name (no accounts). Computer opponents (CPUs, shown with 🤖) can
  fill any number of seats — solo vs CPUs is a first-class way to play.
- Every manager drafts exactly 5 players into 5 lineup slots:
  PG / SG / SF / PF / C. Any player can occupy any slot (a center CAN play
  point guard); slots are your lineup for the simulation, never a
  restriction on drafting.
- The player pool is always EXACTLY 5 players per manager (5N) and always
  drains to zero — no leftovers.
- Player cards show: name (except blind mode, below), natural position,
  franchise of their prime, prime era (e.g. "prime 1989–1993 · '90s"), and
  prime-years ppg/rpg/apg. There are NO ratings, stars, grades, or scores
  shown anywhere in the entire product — knowing who's good is the skill.
- Budgets, rosters, and prices paid are public to everyone at all times.
- Three game modes share all of the above: **Auction** (classic),
  **Blind auction**, **Snake draft**.

## Room creation options (design the create form)

- Game mode: Auction (classic) / Blind auction — mystery players /
  Snake draft — $15, $1–$5 tiers.
- Budget: default $20 (range 1–1000). In snake mode this control is locked
  showing "$15 fixed — one player from every tier is exactly $15."
- Clock: seconds per player/turn, 15–300, default 30.
- Era range: two dropdowns, decades 1960s→2020s, default 2000s→2020s.
- Player pool depth: Legends only (~20/era, default) / Household names
  (~35/era) / Deep (~50/era, stars + role players).
- Lineup window: 0–300s, default 60 (0 skips it).
- Sim mode: "Prompt for your own LLM" (default) / Off / Stats only /
  AI + stats.
- CPU opponents: 0–8.

## Global UI concepts (all modes)

- **Board** — always-visible sidebar: every manager with remaining budget,
  five slots with drafted players + price paid, 👑 on the commissioner, 🤖
  on CPUs, an "autopilot" badge on abandoned teams. Desktop: game stage
  gets the wide column, board/feed a ~320px rail; mobile: stacked with the
  stage sticky on top.
- **Feed** — scrolling event log of every resolution (sold, passed, forced,
  picked, showdown events, pauses). Backfills recent history on
  reconnect/refresh.
- **Countdowns** — live ticking clocks everywhere a deadline exists
  (auction lot, showdown, snake turn, free pick, lineup lock). Server-time
  corrected; they must read at a glance and get urgent near zero.
- **Commissioner controls** — pause / resume / add time / cancel draft;
  kick any manager (✖ on their row, never on self); rerun the sim after
  completion. Kicked or left managers' teams flip to autopilot.
- **Autopilot & reclaim** — a manager idle for 10 straight lots, or who
  leaves, flips to "autopilot" (their team stops acting, fills free at the
  end). Their own screen shows a prominent "🔁 Reclaim my team" banner;
  all inputs disabled with the note "On autopilot — reclaim your team to
  bid." Reclaiming instantly restores control.
- **Paused** — a clear paused state over the stage; clocks frozen.
- **Spectators** — anyone with the link can watch read-only ("Spectating").
- **Errors** — rejected actions (late bid, not your turn, can't afford)
  return as private inline toasts, never global.
- Room lifecycle: idle finished rooms expire after ~1h; design a "room
  closed" end state.

## Mode 1 — Auction (classic)

The pool is a HIDDEN queue; one mystery-order player is revealed at a time
(show a "players remaining" count).

1. **Lot card**: the revealed player + one flat countdown (default 30s),
   armed at reveal. Nothing resets the clock EXCEPT the anti-snipe soft
   close: any bid landing in the final 10s pushes the deadline +5s.
2. **Bidding**: integer dollars, strictly above the current bid. Opening
   bid can be any amount $1 → your whole budget (jump bids and instant
   all-ins are legal, and there is no confirmation step — your money is
   yours to burn). Quick buttons **+$1 / +$2 / +$5** raise the LIVE bid at
   the moment the server processes them; a **Custom** amount is absolute
   and rejected if it no longer beats the current bid. The current leader
   cannot raise their own bid (one exception in #5). Full-roster managers
   can't bid. At $0 you're a spectator (minimum bid is $1).
3. **Pass & LAST CALL**: if a lot's clock expires with no bid, the player
   is recycled to the back of the hidden queue. On their SECOND
   appearance the card is badged **🔔 LAST CALL** — if nobody bids again,
   they're force-assigned at $1 to a random still-active team with the
   most open slots.
4. **Sold**: high bid at expiry wins; player auto-fills the winner's first
   empty slot at that price; the board reposts. Managers can rearrange
   their own slots ANY time before the draft ends (swap).
5. **🎰 All-in showdown** (design this as a marquee moment): when a
   manager's ENTIRE remaining budget exactly equals the current bid,
   bidding it doesn't get rejected — it opens a showdown lottery. The
   current leader is pulled in as a participant no matter how rich they
   are; any other manager whose whole stack exactly matches can pile in
   while it runs. A 15s countdown replaces the lot clock. Each participant
   secretly locks a number 1–100 (editable until reveal; anyone who
   doesn't pick gets a random one — nobody forfeits to a fumbled UI).
   At zero, a mystery number is drawn: closest guess buys the player at
   the tied price (distance ties broken randomly). The reveal shows the
   mystery number, every guess with its distance, and the winner —
   design a celebratory reveal card. Anyone RICHER — including the
   dragged-in leader — can break the showdown at any moment by simply
   bidding higher (normal bidding resumes with a fresh 10s window). The
   participant's panel needs three distinct states: "lock your number,"
   "locked ✓ (editable)," and for a rich leader: "lock a number — or
   raise your bid to call the whole thing off."
6. **Endgame**: a manager is "active" while they have empty slots, ≥$1,
   and aren't on autopilot. The moment exactly ONE manager is active, the
   auction stops and the ENTIRE remaining pool is revealed to everyone —
   the last solvent manager free-picks anyone they want, no charge, 60s
   per pick, until full (idling flips them to autopilot). When nobody is
   active, all remaining empty slots auto-fill randomly, free. Hoarding
   money is deliberately rewarded with total information + free choice.

## Mode 2 — Blind auction

Exactly the auction above, with one twist that should drive the whole
mood: **you bid on players without knowing who they are.** The lot card
shows the full stat line — position, franchise, era, ppg/rpg/apg — but the
name is a mystery ("❓ Mystery player"). Deducing "31.5/6.3/5.5, CHI,
'90s… that's Jordan" IS the game.

- The sale is the reveal: the moment a player is bought, force-assigned,
  or picked, their real name appears everywhere — make the reveal in the
  feed a beat of drama ("👀 It was Michael Jordan!").
- Recycled (passed) players return to the queue still masked, and stay
  masked on their LAST CALL appearance.
- The endgame free-pick grid is also masked — the last manager picks
  blind off stat lines alone.
- Rostered players on the board always show real names. Showdowns,
  budgets, and every other rule work identically to auction mode.

## Mode 3 — Snake draft

No bidding, no hidden queue — the ENTIRE pool is revealed from the start
as a browsable grid, and managers take turns picking. Strategy is
budget-tier math instead of auction timing.

- **Budget: $15, fixed.** Every player has a sticker price by tier:
  **$5** all-time greats (LeBron, Giannis) · **$4** stars that aren't
  all-time greats (Kyrie, Dame, Paul George) · **$3** very solid
  (John Wall, Bradley Beal) · **$2** and **$1** role players. Design a
  clear price badge on every pool card. One player from every tier costs
  exactly $15 — stacking two $5 superstars forces bargain-bin picks
  later. Show each manager's remaining budget prominently.
- **Turn order snakes**: lobby order forward (1→N), then reversed (N→1),
  alternating for 5 rounds — everyone gets exactly 5 picks. Design a
  turn banner: "🐍 Your pick — $7 left" with countdown when it's you,
  "{Name} is on the clock" otherwise; a visible pick order strip helps.
- **Feasibility rule**: you may pick a player only if you can afford them
  AND still keep at least $1 for every other empty slot you have left.
  On your turn, unaffordable/infeasible cards are visibly dimmed with the
  reason on hover/tap.
- **Turn clock** (default 30s): expiry auto-picks the best feasible
  player for you (no autopilot penalty). If the pool ever leaves someone
  with NOTHING affordable, they automatically receive the cheapest
  remaining player charged at most their remaining budget — the draft can
  never deadlock; show it in the feed as a forced bargain.
- CPUs pick after a ~2s think. Era range and pool depth options apply.
  Pause / add time / kick all work mid-snake.

## Shared finale (all modes)

1. **Lineup window (~60s)**: when the last roster fills, everyone gets one
   timed window to arrange their five players into the right slots —
   drag-and-drop AND tap-one-tap-two swapping (both must work on touch).
   Big lock countdown. CPUs visibly rearrange their own lineups during
   this window.
2. **Completion**: final boards with every price paid. Then, by sim mode:
   - **Prompt** (default): a ready-to-paste tournament prompt with a
     one-click "Copy prompt" button — players run it in their own ChatGPT/
     Claude/Gemini to simulate the tournament.
   - **Stats**: instant standings + champion 🏆.
   - **AI + stats**: blended standings, champion, plus a short
     tournament-story paragraph.
   - **Off**: rosters only.
   The commissioner can rerun the sim.

## Screens to deliver

1. Home / landing: create room (all options above) + join by code.
2. Lobby: manager list (👑/🤖/✖ affordances), add/remove CPUs, room link
   share, start button (commissioner only, 2+ managers).
3. Auction stage (lot card, bid bar with every state: spectating, roster
   full, leading, priced out, exact-stack "force a showdown" affordance,
   on-autopilot, paused).
4. Showdown takeover + reveal card.
5. Blind variants of lot card, free-pick grid, and feed reveals.
6. Snake stage: pool grid with price badges + turn banner + pick order.
7. Free-pick endgame (picker view + spectator view).
8. Lineup window (desktop + touch).
9. Completion + each sim-output variant.
10. Edge states: paused, reclaim banner, kicked, connection lost,
    room closed, spectator.

Mobile-first responsiveness is required throughout — this gets played on
phones at parties. Dark theme base. No external assets required by the
current implementation (system fonts + emoji), but propose freely; the
game's personality should feel like a live auction house crossed with a
group chat.
