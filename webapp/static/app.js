// NBA Bid War — vanilla ES module frontend (no deps, no build step).
//
// Rebuilt to the design handoff (design_handoff_nba_bid_war): same wire
// behavior as before, new presentation. Talks to webapp/server.py: REST for
// create/join/simulate, one WebSocket for everything else. The server
// broadcasts a full redacted state_view after every commit; render()
// projects it into the static skeleton in index.html. fx / error / sim
// messages drive the feed, private toasts and the sim panel. The engine is
// the only authority — every control here is cosmetic and the server
// re-validates everything.
//
// SAFETY: every user-supplied string (names) flows through el()/textContent
// — never innerHTML.

const FEED_CAP = 60;
const ERR_TOAST_MS = 2600;
const COPY_FLIP_MS = 1600;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_CAP_MS = 10000;
const TICK_MS = 100;
const URGENT_SECONDS = 10;
const ADDTIME_SECONDS = 15;
const DRAG_THRESHOLD_PX = 8;
const HOLD_SOLD_MS = 1700;  // sold / forced stamp hold (handoff timing)
const HOLD_PASS_MS = 1400;  // passed stamp hold
const HOLD_REVEAL_MS = 4600; // showdown reveal card hold
const SHOWDOWN_SECONDS = 15;
const FREE_PICK_SECONDS = 60;
const ACTIVE_PHASES = ["auction", "snake", "free_pick", "lineup"];
// Blind mode serializes hidden players' names as null — render this instead.
const MYSTERY = "Mystery player";
const MODE_LABELS = { auction: "Auction", blind: "Blind auction", snake: "Snake draft" };
const TINTS = ["#f5b73c", "#5ee6a8", "#8fb4ff", "#ff9d7a", "#d9a6ff",
               "#7fe0d8", "#ffd96b", "#ff8fae", "#a8e06a", "#c3c8ff"];

const S = {
  session: null,    // {room, token, user_id, name}
  state: null,      // last state_view
  prev: null,       // previous state_view (feed-line synthesis)
  sim: null,        // last {"type":"sim"} payload
  ws: null,
  backoffMs: RECONNECT_BASE_MS,
  closed: false,    // deliberate close — suppress reconnect
  closedScreen: null, // {title, sub} — room closed / draft cancelled screen
  t0: Date.now(),   // feed timestamps count from room entry
  tapSlot: null,    // tap-A-tap-B first selection (lineup + board swap)
  drag: null,       // live pointer-drag bookkeeping (lineup swap)
  feedSeeded: false,   // log backfill done for the current socket
  lineupDirty: false,  // state arrived mid-drag — repaint the lineup on drop
  clockSamples: [],    // recent client_now - server_now (median = clock offset)
  lotMasked: false,    // last-rendered lot hid its name (blind) — sold = reveal
  lastTicket: "",      // last "Lot X/Y" label, reused on the stamp card
  stamp: null,      // {title, sub, color, player, until} — sold/passed overlay
  reveal: null,     // showdown reveal card payload + hold deadline
  snipeSeq: 0,      // lot seq whose deadline got extended (soft-close plate)
  pausedAt: null,   // server-now when paused flipped true (freeze countdowns)
  slotSigs: {},     // manager id -> slot signature (CPU shuffle feed lines)
  create: { mode: "auction", depth: "legends", sim: "prompt" },
  errTimer: 0,
  pendingReclaimLine: false, // feed line deferred past the reconnect reseed
  chat: [],         // room chat ring (server replays last 50 on connect)
  chatUnread: 0,    // messages landed while the Feed tab was showing
  chatOpen: false,  // which rail tab is active
};

const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------- DOM helper
// All user data flows through append(String) => text nodes. Never innerHTML.

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") node.style.cssText = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// -------------------------------------------------------------- utilities

const storeKey = (code) => `nbadraft:${code}`;

function loadSession(code) {
  try {
    const raw = localStorage.getItem(storeKey(code));
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  try {
    localStorage.setItem(storeKey(session.room), JSON.stringify(session));
    localStorage.setItem("nbadraft:name", session.name);
  } catch { /* storage full/blocked — session just won't survive reload */ }
}

function dropSession(code) {
  try { localStorage.removeItem(storeKey(code)); } catch { /* fine */ }
}

function hashCode() {
  return location.hash.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

function me() {
  const st = S.state;
  return st ? st.managers.find((m) => m.id === st.you) ?? null : null;
}

function mgr(id) {
  return S.state?.managers.find((m) => m.id === id) ?? null;
}

function mgrName(id) {
  return mgr(id)?.name ?? `manager ${id}`;
}

// "You" in board-style surfaces, real name in the feed.
function youName(id) {
  return id === S.state?.you ? "You" : mgrName(id);
}

function mgrIndex(id) {
  const i = S.state?.managers.findIndex((m) => m.id === id) ?? -1;
  return i < 0 ? 0 : i;
}

function tintFor(id) { return TINTS[mgrIndex(id) % TINTS.length]; }

function initialFor(name) {
  return (name || "?").replace(/^CPU /, "").charAt(0).toUpperCase();
}

function avatar(id, size) {
  const node = el("span", { class: "avatar" }, initialFor(mgrName(id)));
  node.style.background = tintFor(id);
  node.style.width = `${size}px`;
  node.style.height = `${size}px`;
  node.style.fontSize = `${Math.round(size * 0.44)}px`;
  return node;
}

function openSlots(m) {
  return m ? m.spots.filter((s) => !s.player).length : 0;
}

function shortName(name) {
  const parts = name.trim().split(/\s+/);
  return parts[parts.length - 1];
}

// Player display name — blind mode nulls out hidden names on the wire.
function pName(p) { return p?.name ?? MYSTERY; }

function fmtStat(v) { return Number(v).toFixed(1); }

function statSlash(p) {
  return `${fmtStat(p.ppg)} / ${fmtStat(p.rpg)} / ${fmtStat(p.apg)}`;
}

function eraLine(p) {
  const dec = `'${String(p.decade).slice(2)}s`;
  return p.prime ? `prime ${p.prime} · ${dec}` : dec;
}

function eraLabel(cfg) {
  return cfg.era_start === cfg.era_end
    ? `${cfg.era_start}s`
    : `${cfg.era_start}s→${cfg.era_end}s`;
}

function roomLink(code) {
  return `${location.origin}/#${code}`;
}

async function copyText(text, btn, flipLabel) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.append(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* truly no clipboard */ }
    ta.remove();
  }
  if (btn) {
    const original = btn.textContent;
    btn.textContent = flipLabel;
    setTimeout(() => { btn.textContent = original; }, COPY_FLIP_MS);
  }
}

// Server clocks drift from client clocks; every "state" message carries the
// server's `now`. The median of recent samples is subtracted from Date.now()
// when rendering countdowns, so deadlines tick against the server's clock.
const CLOCK_SAMPLES_MAX = 9;

function noteServerNow(serverNow) {
  if (typeof serverNow !== "number" || !Number.isFinite(serverNow)) return;
  const sample = Date.now() / 1000 - serverNow;
  S.clockSamples = [...S.clockSamples, sample].slice(-CLOCK_SAMPLES_MAX);
}

function clockOffset() {
  const n = S.clockSamples.length;
  if (!n) return 0;
  const sorted = [...S.clockSamples].sort((a, b) => a - b);
  const mid = Math.floor(n / 2);
  return n % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function serverNow() {
  return Date.now() / 1000 - clockOffset();
}

// ----------------------------------------------------------- error toasts
// Rejected actions surface as PRIVATE inline toasts near the bid bar (the
// handoff's blocked-action rule) — never as global banners.

function showError(message) {
  if (!$("game").hidden) {
    const t = $("err-toast");
    t.textContent = message;
    const bidbarUp = !$("bidbar").hidden;
    t.classList.toggle("floating", !bidbarUp);
    (bidbarUp ? $("bidbar") : $("stage-col")).append(t);
    t.hidden = false;
    clearTimeout(S.errTimer);
    S.errTimer = setTimeout(() => { t.hidden = true; }, ERR_TOAST_MS);
    return;
  }
  const target = !$("lobby").hidden ? $("lobby-toast") : $("home-toast-join");
  formToast(target, message);
}

function formToast(container, message) {
  if (!container) return;
  container.replaceChildren(el("div", { class: "form-toast" }, message));
  setTimeout(() => { container.replaceChildren(); }, ERR_TOAST_MS + 1000);
}

// ------------------------------------------------------------------- REST

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) {
    throw new Error(data?.detail || `Request failed (${res.status})`);
  }
  return data;
}

// -------------------------------------------------------------- WebSocket

function connect() {
  if (!S.session) return;
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${location.host}/ws/${S.session.room}` +
    `?token=${encodeURIComponent(S.session.token)}`;
  const ws = new WebSocket(url);
  S.ws = ws;
  ws.onopen = () => {
    S.backoffMs = RECONNECT_BASE_MS;
    S.feedSeeded = false; // backfill the feed from the next state's log
    $("conn").hidden = true;
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleMessage(msg);
  };
  ws.onclose = () => {
    if (S.closed || S.ws !== ws) return;
    $("conn").hidden = false;
    const delay = S.backoffMs;
    S.backoffMs = Math.min(S.backoffMs * 2, RECONNECT_CAP_MS);
    setTimeout(() => {
      if (!S.closed && S.ws === ws) connect();
    }, delay);
  };
}

function reconnect() {
  const old = S.ws;
  S.ws = null; // detach first — old.onclose must not schedule a retry
  try { old?.close(); } catch { /* already closed */ }
  connect();
}

function send(payload) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify(payload));
  } else {
    showError("Not connected — hang on…");
  }
}

function handleMessage(msg) {
  if (msg.type === "state") {
    const prev = S.state;
    S.state = msg.state;
    noteServerNow(msg.now);
    if (msg.state.paused) {
      if (S.pausedAt == null) S.pausedAt = msg.now;
    } else {
      S.pausedAt = null;
    }
    if (!S.feedSeeded) { // once per socket, before any live fx lands
      seedFeed(msg.state);
      S.feedSeeded = true;
      S.prev = msg.state;
      if (S.pendingReclaimLine) { // survive the reseed after reconnect
        S.pendingReclaimLine = false;
        // The join can 200 yet be refused by the engine (e.g. mid-free-pick)
        // — only celebrate if the fresh state shows the wake actually took.
        const my = msg.state.managers.find((m) => m.id === msg.state.you);
        if (my && !my.autopilot) {
          feedPush("You reclaimed your team — back in the bidding", "reclaim");
        }
      }
    } else {
      synthesizeFeed(prev, msg.state);
      S.prev = msg.state;
    }
    render();
  } else if (msg.type === "fx") {
    handleFx(msg.fx || []);
  } else if (msg.type === "error") {
    if (msg.message === "Room not found." ||
        msg.message === "This room has expired — start a new draft.") {
      roomGone();
      return;
    }
    showError(msg.message);
  } else if (msg.type === "sim") {
    S.sim = msg;
    render();
  } else if (msg.type === "chat") {
    S.chat.push(msg);
    if (S.chat.length > 50) S.chat.shift();
    if (!S.chatOpen && msg.from !== S.state?.you) {
      S.chatUnread += 1;
    }
    renderChat();
  } else if (msg.type === "chat_history") {
    S.chat = (msg.messages || []).slice(-50);
    renderChat();
  }
}

// ---------------------------------------------------------------- chat

function renderChat() {
  const box = $("chat-msgs");
  box.replaceChildren(...S.chat.map((m) =>
    el("div", { class: "chat-row" },
      el("span", {
        class: "chat-dot",
        style: `background:${tintFor(m.from)}`,
      }),
      el("div", { class: "chat-body" },
        el("span", { class: "chat-name", style: `color:${tintFor(m.from)}` },
          m.name),
        el("span", { class: "chat-text" }, m.text)))));
  box.scrollTop = box.scrollHeight; // newest pinned into view
  const badge = $("chat-unread");
  badge.hidden = S.chatUnread === 0;
  badge.textContent = S.chatUnread > 9 ? "9+" : String(S.chatUnread);
}

function setChatTab(open) {
  S.chatOpen = open;
  if (open) S.chatUnread = 0;
  $("tab-feed").classList.toggle("selected", !open);
  $("tab-chat").classList.toggle("selected", open);
  $("feed").hidden = open;
  $("chat-panel").hidden = !open;
  $("rail-live").textContent = open ? "managers only" : "live";
  renderChat();
  if (open) $("chat-input").focus();
}

function roomGone() {
  const code = S.session?.room;
  if (code) dropSession(code);
  showClosed(
    "Room closed",
    `Room ${code ?? ""} sat idle for an hour and expired. Rosters are gone. ` +
    "Open a new one.");
}

// ------------------------------------------------------- session handling

function startSession(res, name) {
  const session = { room: res.room, token: res.token, user_id: res.user_id, name };
  saveSession(session);
  enterRoom(session);
}

function enterRoom(session) {
  S.session = session;
  S.closed = false;
  S.closedScreen = null;
  S.state = null;
  S.prev = null;
  S.sim = null;
  S.t0 = Date.now();
  S.tapSlot = null;
  S.feedSeeded = false;
  S.lineupDirty = false;
  S.lotMasked = false;
  S.stamp = null;
  S.reveal = null;
  S.snipeSeq = 0;
  S.pausedAt = null;
  S.slotSigs = {};
  S.pendingReclaimLine = false;
  S.backoffMs = RECONNECT_BASE_MS;
  S.chat = [];
  S.chatUnread = 0;
  $("feed").replaceChildren();
  renderChat();
  if (hashCode() !== session.room) location.hash = session.room;
  connect();
}

// Reclaiming goes through POST /join with the saved token — the server
// reattaches the same identity and dispatches the Join that wakes an
// autopilot team. Opening the WS directly never wakes anyone.
async function reclaimAndEnter(saved) {
  try {
    const res = await api(`/api/rooms/${saved.room}/join`,
      { name: saved.name, token: saved.token });
    startSession(res, saved.name);
  } catch (err) {
    if (err.message === "Room not found.") dropSession(saved.room);
    formToast($("home-toast-join"), err.message);
    showSection("home");
  }
}

async function reclaimTeam(e) {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await api(`/api/rooms/${S.session.room}/join`,
      { name: S.session.name, token: S.session.token });
    S.pendingReclaimLine = true; // pushed after the reconnect's feed reseed
    reconnect(); // fresh socket + fresh state after the wake-up Join
  } catch (err) {
    if (err.message === "Room not found.") { roomGone(); return; }
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
}

function exitToHome() {
  S.closed = true;
  try { S.ws?.close(); } catch { /* already closed */ }
  S.ws = null;
  S.session = null;
  S.state = null;
  S.sim = null;
  S.closedScreen = null;
  history.replaceState(null, "", location.pathname);
  showSection("home");
}

function showClosed(title, sub) {
  S.closedScreen = { title, sub };
  S.closed = true;
  try { S.ws?.close(); } catch { /* already closed */ }
  S.ws = null;
  $("closed-title").textContent = title;
  $("closed-sub").textContent = sub;
  showSection("closed");
}

// ------------------------------------------------------------------- feed

function feedTs() {
  const s = Math.floor((Date.now() - S.t0) / 1000);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:` +
         `${String(s % 60).padStart(2, "0")}`;
}

function feedPush(text, kind = "info", ts = feedTs()) {
  const feed = $("feed");
  feed.prepend(el("div", { class: `feed-row k-${kind}` },
    el("span", { class: "ts" }, ts),
    el("span", { class: "txt" }, text)));
  while (feed.children.length > FEED_CAP) feed.lastChild.remove();
}

// state.log entries carry the player NAME (string; null when blind-masked),
// not a player object — templates mirror the live fx lines.
function logLine(entry) {
  const name = entry.player ?? MYSTERY;
  switch (entry.kind) {
    case "sold":
      return [`SOLD · ${name} → ${mgrName(entry.manager)} for $${entry.price}`, "sold"];
    case "passed":
      return [`Nobody wanted ${name} — recycled to the back`, "pass"];
    case "force":
      return [`No takers twice — ${name} force-assigned to ` +
              `${mgrName(entry.manager)} at $${entry.price ?? 1}`, "forced"];
    case "pick": // snake picks log a price; free picks are on the house
      return entry.price
        ? [`${mgrName(entry.manager)} drafts ${name} for $${entry.price}`, "sold"]
        : [`${mgrName(entry.manager)} free-picks ${name}`, "sold"];
    case "autofill":
      return [`${name} auto-filled → ${mgrName(entry.manager)}`, "info"];
    default:
      return null;
  }
}

function seedFeed(st) {
  $("feed").replaceChildren(); // rebuild from the log — no duplicates
  for (const entry of st.log || []) { // chronological; prepend ends newest-first
    const line = logLine(entry);
    if (line) feedPush(line[0], line[1], "");
  }
}

// Feed lines the wire carries only as state deltas (bids, phase changes,
// last call, soft close): derived by diffing consecutive broadcasts. Only
// data already on the wire is used — nothing is invented.
function synthesizeFeed(prev, st) {
  if (!prev) return;
  // Draft start
  if (prev.phase === "lobby" && (st.phase === "auction" || st.phase === "snake")) {
    feedPush(`Gavel's up — ${st.managers.length * 5} players on the block`, "start");
  }
  // Free-pick endgame reveal
  if (prev.phase !== "free_pick" && st.phase === "free_pick" && st.free_pick) {
    feedPush(
      `Only ${mgrName(st.free_pick.picker)} still has money — the whole ` +
      "remaining pool is revealed. Free picks, no charge.", "endgame");
  }
  // Lot lifecycle (auction)
  const lot = st.lot, plot = prev.lot;
  if (lot && (!plot || plot.seq !== lot.seq)) {
    if (lot.last_call) {
      feedPush(`🔔 LAST CALL — ${pName(lot.player)} is back on the block`, "lastcall");
    }
  } else if (lot && plot && plot.seq === lot.seq && !lot.lottery) {
    if (lot.current_bid > plot.current_bid && lot.leader != null) {
      const bidder = mgr(lot.leader);
      const allIn = bidder != null && lot.current_bid === bidder.budget;
      feedPush(
        `${mgrName(lot.leader)} bids $${lot.current_bid}` +
        `${allIn ? " — everything they've got!" : ""} on ${pName(lot.player)}`,
        "info");
      if (lot.deadline > plot.deadline + 0.5) {
        S.snipeSeq = lot.seq; // soft close: show the red plate for this lot
        feedPush("Anti-snipe: +5s on the clock", "info");
      }
    } else if (!prev.paused && !st.paused &&
               lot.deadline > plot.deadline + 2 &&
               lot.current_bid === plot.current_bid) {
      feedPush(
        `Commissioner added ${Math.round(lot.deadline - plot.deadline)}s`, "info");
    }
  }
  // +15s during snake/free-pick moves pick_deadline with no fx — synthesize,
  // guarding against ordinary re-arms (a new turn or a completed pick).
  if (["snake", "free_pick"].includes(st.phase) && prev.phase === st.phase &&
      !prev.paused && !st.paused &&
      st.queue_count === prev.queue_count &&
      st.turn?.manager === prev.turn?.manager &&
      st.pick_deadline > prev.pick_deadline + 2) {
    feedPush(
      `Commissioner added ${Math.round(st.pick_deadline - prev.pick_deadline)}s`,
      "info");
  }
  // CPU (and rival) lineup shuffles are visible during the lineup window.
  if (st.phase === "lineup") {
    for (const m of st.managers) {
      const sig = m.spots.map((s) => `${s.slot}:${s.player?.name ?? ""}`).join("|");
      const old = S.slotSigs[m.id];
      S.slotSigs[m.id] = sig;
      if (prev.phase === "lineup" && old && old !== sig && m.id !== st.you) {
        feedPush(`${m.name} shuffles their lineup`, "info");
      }
    }
  } else {
    S.slotSigs = {};
  }
}

// ------------------------------------------------------------ fx handling

function fxLines(fx) {
  switch (fx.kind) {
    case "sold": {
      const lines = [[`SOLD · ${pName(fx.player)} → ${mgrName(fx.manager)} ` +
                      `for $${fx.price}`, "sold"]];
      // Blind reveal: the lot card said "Mystery player" — the sold fx
      // carries the real name, so the feed gets the dramatic reveal line.
      if (S.lotMasked && fx.player?.name != null) {
        lines.push([`👀 It was ${fx.player.name}!`, "reveal"]);
      }
      return lines;
    }
    case "passed":
      return [[`Nobody wanted ${pName(fx.player)} — recycled to the back`, "pass"]];
    case "force": {
      // Snake forced bargains carry no price on the wire and aren't the
      // auction's $1 LAST CALL rule — keep the copy honest per mode.
      const lines = [[S.state?.config.mode === "snake"
        ? `Forced bargain — ${pName(fx.player)} goes to ${mgrName(fx.manager)}`
        : `No takers twice — ${pName(fx.player)} force-assigned ` +
          `to ${mgrName(fx.manager)} at $1`, "forced"]];
      if (S.lotMasked && fx.player?.name != null) {
        lines.push([`👀 It was ${fx.player.name}!`, "reveal"]);
      }
      return lines;
    }
    case "picked": {
      const lines = [[`${mgrName(fx.manager)} free-picks ${pName(fx.player)}`, "sold"]];
      if (S.state?.config.mode === "blind" && fx.player?.name != null) {
        lines.push([`👀 It was ${fx.player.name}!`, "reveal"]);
      }
      return lines;
    }
    case "autofill":
      return [
        ["Everyone's broke — remaining slots auto-filled free", "endgame"],
        ...fx.assignments.map((a) =>
          [`${pName(a.player)} auto-filled → ${mgrName(a.manager)}`, "info"]),
      ];
    case "snake_turn":
      return [[fx.manager === S.state?.you
        ? "🐍 Your pick — you're on the clock"
        : `🐍 ${mgrName(fx.manager)} is on the clock`, "info"]];
    case "lottery_open": {
      const [leader, matcher] = fx.participants;
      const player = pName(S.state?.lot?.player);
      return [[`🎰 ALL-IN SHOWDOWN · ${mgrName(matcher ?? leader)} matches ` +
               `$${fx.amount} on ${player} — ${mgrName(leader)} gets dragged in`,
               "showdown"]];
    }
    case "lottery_joined":
      return [[`${mgrName(fx.manager)} piles into the showdown with the ` +
               "exact same stack", "showdown"]];
    case "lottery_guessed":
      return [[`${mgrName(fx.manager)} locked in a number`, "showdown"]];
    case "lottery_cancelled": {
      const amount = S.state?.lot?.current_bid;
      return [[`${mgrName(fx.manager)} calls the whole thing off` +
               `${amount ? ` — bids $${amount}` : ""}`, "showdown"]];
    }
    case "lineup_open":
      return [[`Rosters full — ${S.state?.config.lineup_seconds ?? ""}s to set ` +
               "your lineup", "info"]];
    case "complete":
      return [["Books are closed. Final boards are up.", "done"]];
    case "paused":
      return [["Commissioner paused the room", "pause"]];
    case "resumed":
      return [["Resumed — clocks are live", "pause"]];
    case "cancelled":
      return [["The commissioner pulled the plug — draft cancelled", "kick"]];
    case "autopilot":
      return [[fx.manager === S.state?.you
        ? "Your team is on autopilot — reclaim it to bid"
        : `${mgrName(fx.manager)} — team flips to autopilot`, "autopilot"]];
    default:
      return [];
  }
}

function handleFx(fxList) {
  const soldInBatch = fxList.find((fx) => fx.kind === "sold");
  for (const fx of fxList) {
    if (fx.kind === "lottery_reveal") {
      // The reveal card owns the marquee moment; the sold stamp is skipped.
      S.reveal = {
        mystery: fx.mystery,
        guesses: fx.guesses,
        winner: fx.winner,
        player: soldInBatch?.player ?? null,
        price: soldInBatch?.price ?? null,
        until: Date.now() + HOLD_REVEAL_MS,
      };
      setTimeout(() => { S.reveal = null; render(); }, HOLD_REVEAL_MS + 30);
      feedPush(`Mystery number ${fx.mystery} · ${mgrName(fx.winner)} wins ` +
               `the lottery${soldInBatch ? ` at $${soldInBatch.price}` : ""}`,
               "showdown");
      render();
      continue;
    }
    if (fx.kind === "sold" || fx.kind === "force") {
      // The stamp overlays the auction ticket card — snake has no lot card
      // to stamp (its forces would flash a stale auction view for 1.7s).
      if (!S.reveal && S.state?.config.mode !== "snake") armStamp(fx.kind === "force" ? "FORCED" : "SOLD!",
        `${pName(fx.player)} → ${mgrName(fx.manager)} · $${fx.price ?? 1}`,
        fx.kind === "force" ? "#ff9d7a" : "#f5b73c", fx.player, HOLD_SOLD_MS);
    } else if (fx.kind === "passed") {
      armStamp("PASSED", `${pName(fx.player)} goes back in the queue`,
        "#8f8b84", fx.player, HOLD_PASS_MS);
    }
    for (const [text, kind] of fxLines(fx)) feedPush(text, kind);
  }
  render();
}

// Sold / forced / passed hold: purely visual — state keeps flowing under it,
// only the lot-card area shows the outgoing player + stamp for the hold.
function armStamp(title, sub, color, player, holdMs) {
  S.stamp = { title, sub, color, player, until: Date.now() + holdMs };
  setTimeout(() => { S.stamp = null; render(); }, holdMs + 30);
}

// ------------------------------------------------------------ render loop

function showSection(name) {
  for (const id of ["home", "lobby", "game", "closed"]) {
    $(id).hidden = id !== name;
  }
}

function render() {
  const st = S.state;
  if (S.closedScreen) { showSection("closed"); return; }
  if (!S.session || !st) { showSection("home"); return; }
  if (st.phase === "cancelled") {
    showClosed("Draft cancelled",
      "The commissioner pulled the plug. Nothing was saved — open a new " +
      "room and run it back.");
    return;
  }
  if (st.phase === "lobby") {
    showSection("lobby");
    renderLobby(st);
  } else {
    showSection("game");
    renderTopbar(st);
    renderBoard(st);
    renderStage(st);
  }
  tick(); // countdowns update immediately, not on the next 100ms beat
}

// ------------------------------------------------------------------ lobby

function renderLobby(st) {
  const cfg = st.config;
  const isCommish = st.you === st.commissioner;
  $("lobby-eyebrow").textContent = cfg.mode === "snake"
    ? "Snake draft · $15 fixed"
    : `${MODE_LABELS[cfg.mode]} · $${cfg.budget} budget`;
  $("lobby-count").textContent = `${st.managers.length} / 10 managers`;

  $("lobby-rows").replaceChildren(...st.managers.map((m) => {
    const isMe = m.id === st.you;
    const badge = m.id === st.commissioner
      ? el("span", { class: "badge commish" }, "👑 Commissioner")
      : m.cpu ? el("span", { class: "badge" }, "🤖 CPU")
      : isMe ? el("span", { class: "badge" }, "You")
      : el("span", { class: "badge" }, "Human");
    const sub = m.cpu ? "drafts on a 2s think"
      : isMe ? "that's you" : "joined by link";
    // Lobby removal only exists for CPUs on the wire — a human "kick" here
    // would silently flip them to autopilot before the draft even starts.
    const kick = isCommish && m.cpu
      ? el("button", {
          class: "kick-btn",
          title: "Remove CPU",
          onclick: () => send({ action: "remove_cpu", cpu_id: m.id }),
        }, "✖")
      : null;
    return el("div", { class: `mgr-row${isMe ? " me" : ""}` },
      avatar(m.id, 34),
      el("div", { class: "info" },
        el("div", { class: "name-line" },
          el("span", { class: "name" }, m.name), badge),
        el("div", { class: "sub" }, sub)),
      kick);
  }));

  $("lobby-footer").hidden = st.managers.length >= 10;
  $("btn-addcpu").hidden = !isCommish;

  const start = $("lobby-start");
  start.hidden = !isCommish;
  $("lobby-wait").hidden = isCommish;
  if (isCommish) {
    const need = st.managers.length < 2;
    start.disabled = need;
    start.textContent = need
      ? "Need 2 managers to start"
      : `Start the draft · ${st.managers.length * 5} players`;
  }
  $("btn-lobby-cancel").hidden = !isCommish;

  $("lobby-code").textContent = S.session.room;
  $("lobby-url").textContent = roomLink(S.session.room);
  const rows = [
    ["Mode", MODE_LABELS[cfg.mode]],
    ["Budget", cfg.mode === "snake" ? "$15 fixed" : `$${cfg.budget}`],
    ["Clock", `${cfg.lot_seconds}s`],
    ["Eras", eraLabel(cfg)],
    ["Pool", cfg.pool_depth === "legends" ? "Legends"
      : cfg.pool_depth === "household" ? "Household" : "Deep"],
    ["Lineup window", cfg.lineup_seconds === 0 ? "skipped" : `${cfg.lineup_seconds}s`],
    ["Sim", cfg.sim === "prompt" ? "LLM prompt" : cfg.sim === "stats"
      ? "Stats only" : cfg.sim === "ai" ? "AI + stats" : "Off"],
  ];
  $("lobby-settings").replaceChildren(...rows.map(([k, v]) => el(
    "div", { class: "setting-row" },
    el("span", { class: "k" }, k), el("span", { class: "v" }, v))));
}

// ------------------------------------------------------------- game chrome

function renderTopbar(st) {
  $("gh-code").textContent = S.session.room;
  const mode = $("gh-mode");
  mode.textContent = MODE_LABELS[st.config.mode] ?? st.config.mode;
  mode.classList.toggle("blind", st.config.mode === "blind");
  $("gh-queue").textContent =
    ["auction", "snake"].includes(st.phase)
      ? `${st.queue_count} players left in the queue` : "";
  const isCommish = st.you === st.commissioner;
  const active = ACTIVE_PHASES.includes(st.phase);
  $("commish-cluster").hidden = !isCommish || !active;
  const pauseBtn = $("btn-pause");
  pauseBtn.textContent = st.paused ? "Resume" : "Pause";
  pauseBtn.classList.toggle("paused", st.paused);
  // The engine categorically rejects pause/addtime once lineups are locking.
  pauseBtn.hidden = st.phase === "lineup";
  $("btn-addtime").hidden = !["auction", "snake", "free_pick"].includes(st.phase);
  $("spectate-chip").hidden = me() != null;

  const seated = me() != null;
  $("chat-input").disabled = !seated;
  $("chat-send").disabled = !seated;
  $("chat-input").placeholder = seated ? "Talk trash…" : "Join the room to chat";
  renderMobileStrip(st);

  const my = me();
  $("reclaim-banner").hidden = !my || !my.autopilot || !active;

  $("paused-overlay").hidden = !st.paused;
  $("pause-sub").textContent = "Every clock is frozen. " +
    (isCommish ? "Paused by you (commissioner)." : "Paused by the commissioner.");
}

function renderBoard(st) {
  const isCommish = st.you === st.commissioner;
  const canKick = isCommish && ACTIVE_PHASES.includes(st.phase);
  const leaderId = st.phase === "auction" ? st.lot?.leader : null;
  $("board").replaceChildren(...st.managers.map((m) => {
    const isMe = m.id === st.you;
    const badge = m.autopilot
      ? el("span", { class: "mini-badge autopilot" }, "Autopilot")
      : m.id === st.commissioner ? el("span", { class: "mini-badge" }, "👑")
      : m.cpu ? el("span", { class: "mini-badge" }, "🤖")
      : null;
    const kick = canKick && !isMe
      ? el("button", {
          class: "board-kick", title: `Kick ${m.name}`,
          onclick: () => send({ action: "kick", target: m.id }),
        }, "✖")
      : null;
    const card = el("div",
      { class: `board-card${isMe ? " me" : ""}` +
               `${leaderId != null && m.id === leaderId ? " leading" : ""}` },
      el("div", { class: "board-head" },
        avatar(m.id, 22),
        el("span", { class: "name" }, isMe ? "You" : m.name),
        badge,
        el("span", { class: "spacer" }),
        el("span", { class: `budget${m.budget === 0 ? " broke" : ""}` },
          `$${m.budget}`),
        kick),
      el("div", { class: "slot-grid" }, m.spots.map((s) => boardChip(st, m, s))));
    return card;
  }));
}

// Board slot chips: your own are tap-A-tap-B swappable any time before the
// draft ends (the engine gates the phases).
function boardChip(st, m, s) {
  const isMe = m.id === st.you;
  const swappable = isMe && ACTIVE_PHASES.includes(st.phase);
  const chip = el("div",
    {
      class: `slot-chip${s.player ? " filled" : ""}${isMe ? " mine" : ""}` +
             `${swappable && S.tapSlot === s.slot ? " selected" : ""}`,
      onclick: swappable ? () => tapSwap(s.slot) : null,
    },
    el("div", { class: "pos" }, s.slot),
    el("div", { class: "short" }, s.player ? shortName(s.player.name) : "—"),
    el("div", { class: "price" },
      s.player ? (s.price ? `$${s.price}` : "free") : ""));
  return chip;
}

function tapSwap(slot) {
  if (S.tapSlot == null) {
    S.tapSlot = slot;
  } else if (S.tapSlot === slot) {
    S.tapSlot = null;
  } else {
    const a = S.tapSlot;
    S.tapSlot = null;
    send({ action: "swap", a, b: slot });
  }
  render();
}

// ------------------------------------------------------------ stage router

function stageViews(name) {
  for (const id of ["auction-view", "showdown-view", "freepick-view",
                    "snake-view", "lineup-view", "done-view"]) {
    $(id).hidden = id !== name;
  }
}

function renderStage(st) {
  if (S.drag && st.phase !== "lineup") abortDrag(); // phase moved on mid-drag
  const inLottery = st.phase === "auction" && st.lot?.lottery != null;
  const now = Date.now();

  // Reveal card and sold/passed stamps hold the stage briefly — purely
  // visual; the fresh state is already committed and everything else
  // (board, feed, budgets) renders from it.
  if (S.reveal && now < S.reveal.until) {
    stageViews("showdown-view");
    renderReveal(st);
    $("bidbar").hidden = true;
    return;
  }
  if (S.stamp && now < S.stamp.until) {
    stageViews("auction-view");
    renderStampCard();
    if (st.phase === "auction" && st.lot) {
      renderLotChrome(st); // header/clock/bid live-update underneath
      renderBidBar(st);
      $("bidbar").hidden = false;
    } else {
      $("bidbar").hidden = true;
    }
    return;
  }

  if (st.phase === "auction" && st.lot && inLottery) {
    stageViews("showdown-view");
    renderShowdown(st);
    $("bidbar").hidden = true;
  } else if (st.phase === "auction" && st.lot) {
    stageViews("auction-view");
    renderLot(st);
    renderBidBar(st);
    $("bidbar").hidden = false;
  } else if (st.phase === "free_pick" && st.free_pick) {
    stageViews("freepick-view");
    renderFreePick(st);
    renderBidBar(st); // blocked plate: "{Name} is choosing"
    $("bidbar").hidden = false;
  } else if (st.phase === "snake" && st.turn) {
    stageViews("snake-view");
    renderSnake(st);
    $("bidbar").hidden = true;
  } else if (st.phase === "lineup") {
    $("bidbar").hidden = true;
    if (S.drag) {
      // A broadcast landed mid-drag (CPUs self-arrange constantly) — keep
      // the gesture alive and repaint from fresh state on drop.
      S.lineupDirty = true;
    } else {
      S.lineupDirty = false;
      stageViews("lineup-view");
      renderLineup(st);
    }
  } else if (st.phase === "complete") {
    stageViews("done-view");
    renderDone(st);
    $("bidbar").hidden = true;
  } else {
    stageViews("");
    $("bidbar").hidden = true;
  }
}

// ---------------------------------------------------------------- auction

function ticketCard(p, ticketLabel) {
  const masked = p.name == null;
  return el("div", { class: "ticket" },
    el("div", { class: "ticket-stripe" }),
    el("div", { class: "ticket-subhead" },
      el("span", {}, "On the block"),
      el("span", {}, ticketLabel)),
    el("div", { class: "ticket-body" },
      el("div", { class: "portrait" },
        el("span", {}, masked ? "identity\nwithheld" : "player portrait\ndrop art here")),
      el("div", { class: "ticket-text" },
        el("div", { class: "pos-line" },
          el("span", { class: "pos-chip" }, p.pos),
          el("span", { class: "team-label" }, p.team)),
        el("div", { class: `lot-name${masked ? " mystery" : ""}` }, pName(p)),
        el("div", { class: "era-line" }, eraLine(p)),
        el("div", { class: "stat-wells" },
          [["ppg", p.ppg], ["rpg", p.rpg], ["apg", p.apg]].map(([k, v]) => el(
            "div", { class: "stat-well" },
            el("div", { class: "v" }, fmtStat(v)),
            el("div", { class: "k" }, k)))))));
}

function renderLotChrome(st) {
  const lot = st.lot;
  const total = st.managers.length * 5;
  const num = Math.max(1, total - st.queue_count);
  $("lot-label").textContent = `On the block · ${num} of ${total}`;
  S.lastTicket = `Lot ${num}/${total}`;
  $("lot-lastcall").hidden = !lot.last_call;
  $("lot-right").textContent = lot.current_bid > 0
    ? `leader ${mgrName(lot.leader)}`
    : "no bids yet — opens at $1";
  $("curbid-amount").textContent = `$${lot.current_bid}`;
  const leaderEl = $("curbid-leader");
  if (lot.current_bid > 0) {
    const yours = lot.leader === st.you;
    leaderEl.textContent = yours ? "You are leading" : `${mgrName(lot.leader)} is leading`;
    leaderEl.classList.toggle("you", yours);
  } else {
    leaderEl.textContent = "Opening bid is anyone's";
    leaderEl.classList.remove("you");
  }
}

function renderLot(st) {
  const lot = st.lot;
  const p = lot.player;
  S.lotMasked = p.name == null; // blind mode — the sold fx is the reveal
  renderLotChrome(st);
  $("lot-card-wrap").replaceChildren(ticketCard(p, S.lastTicket));
}

function renderStampCard() {
  const { title, sub, color, player } = S.stamp;
  const wrap = $("lot-card-wrap");
  const card = ticketCard(player ?? { pos: "", team: "", ppg: 0, rpg: 0, apg: 0, decade: 0 },
    S.lastTicket);
  const stamp = el("div", { class: "stamp-overlay" },
    el("div", { class: "stamp" },
      el("div", { class: "title", style: `color:${color}` }, title),
      el("div", { class: "sub" }, sub)));
  wrap.replaceChildren(card, stamp);
}

// ---------------------------------------------------------------- bid bar
// Blocked states + exact-stack plate follow the handoff table verbatim.

function renderBidBar(st) {
  const my = me();
  const lot = st.phase === "auction" ? st.lot : null;
  const cur = lot ? lot.current_bid : 0;
  const open = openSlots(my);
  const leading = my != null && lot != null && lot.leader === my.id;

  $("bank-amount").textContent = `$${my?.budget ?? 0}`;
  $("bank-amount").classList.toggle("broke", (my?.budget ?? 0) === 0);
  $("bank-slots").textContent = `${open} slot${open === 1 ? "" : "s"} open`;

  let blocked = null; // [title, sub, color]
  if (my == null) {
    blocked = ["Spectating",
      "You're watching read-only. Anyone with the link can.", "#f3f0ea"];
  } else if (st.phase === "free_pick" && st.free_pick &&
             st.free_pick.picker !== st.you) {
    blocked = [`${mgrName(st.free_pick.picker)} is choosing`,
      "They out-hoarded everyone. The pool is theirs, free of charge.", "#5ee6a8"];
  } else if (my.autopilot) {
    blocked = ["On autopilot", "Reclaim your team to bid.", "#f5b73c"];
  } else if (open === 0) {
    blocked = ["Roster full",
      "All five slots are yours. Rearrange them any time before the draft ends.",
      "#5ee6a8"];
  } else if (my.budget === 0) {
    blocked = ["Out of money",
      "Minimum bid is $1. You're a spectator until the endgame — and if " +
      "you're the last one solvent, you pick free.", "#ff6a5c"];
  } else if (lot && my.budget < cur) {
    blocked = ["Priced out",
      `The bid is $${cur} and you have $${my.budget}. Sit this one out.`, "#ff6a5c"];
  }
  // An exact-stack tie is a legal bid: matching opens the showdown lottery.
  const exact = lot != null && my != null && !my.autopilot && open > 0 &&
    cur > 0 && my.budget === cur && !leading;

  $("bidbar").classList.toggle("exact", exact);
  $("bid-exact").hidden = !exact;
  $("bid-blocked").hidden = exact || !blocked;
  $("bid-active").hidden = exact || Boolean(blocked) ||
    st.phase !== "auction" || lot == null;

  if (exact) {
    $("btn-force").textContent = `🎰 Force a showdown · $${my.budget}`;
  } else if (blocked) {
    $("blk-title").textContent = blocked[0];
    $("blk-title").style.color = blocked[2];
    $("blk-sub").textContent = blocked[1];
    $("blk-dot").style.background = blocked[2];
  } else if (lot) {
    $("bid-amount").placeholder = cur > 0 ? `> ${cur}` : "any $";
    $("btn-allin").textContent = `All in · $${my.budget}`;
  }
}

// --------------------------------------------------------------- showdown

function renderShowdown(st) {
  const lot = st.lot;
  const lo = lot.lottery;
  const my = me();
  const price = lot.current_bid;
  const masked = lot.player.name == null;
  $("sd-reveal").hidden = true;
  $("sd-live").hidden = false;
  $("sd-seats").hidden = false;

  const rich = lo.participants
    .map((id) => mgr(id))
    .filter((m) => m != null && m.budget > price);
  const richPart = rich.length
    ? ` ${rich.map((m) => youName(m.id)).join(" and ")} got dragged in with ` +
      "more money, and can bid out at any moment."
    : ` Every stack here is exactly $${price}.`;
  $("sd-sub").textContent =
    `${masked ? MYSTERY : lot.player.name} · price locked at $${price} — ` +
    "closest guess to the mystery number buys him." + richPart;

  $("sd-seats").replaceChildren(...lo.participants.map((id) => {
    const m = mgr(id);
    if (!m) return null;
    const isMe = id === st.you;
    const lockedIn = lo.entered.includes(id);
    const lockLabel = lockedIn
      ? (isMe && lo.your_guess != null ? `Locked ✓ ${lo.your_guess}` : "Locked ✓")
      : "Choosing…";
    return el("div", { class: `sd-seat${isMe ? " me" : ""}` },
      el("div", { class: "head" },
        avatar(id, 26), el("span", { class: "name" }, youName(id))),
      el("div", { class: "cap" }, "whole stack"),
      el("div", { class: "stack" }, `$${m.budget}`),
      el("div", { class: `sd-lockplate${lockedIn ? " locked" : ""}` }, lockLabel));
  }));

  const mine = my != null && lo.participants.includes(my.id);
  $("sd-panel").hidden = !mine;
  $("sd-watch").hidden = mine;
  if (mine) {
    const lockedIn = lo.your_guess != null;
    const richLeader = my.budget > price;
    $("sd-title").textContent = lockedIn
      ? "Locked ✓ — still editable"
      : richLeader
        ? "Lock a number — or raise your bid to call the whole thing off"
        : "Lock your number";
    $("sd-hint").textContent = lockedIn
      ? "You can change it right up to the draw. Doing nothing gets you a " +
        "random number — nobody forfeits to a fumbled UI."
      : "Any integer 1–100. If you don't pick, one gets picked for you.";
    const lockBtn = $("sd-lock");
    lockBtn.textContent = lockedIn ? "Update lock" : "Lock it";
    lockBtn.classList.toggle("locked", lockedIn);
    const breakRow = $("sd-break-row");
    breakRow.hidden = !richLeader;
    if (richLeader) $("sd-break").textContent = `Bid $${price + 1}`;
  } else {
    const spectator = my == null;
    $("sd-watch-label").textContent = spectator
      ? "Spectating the showdown"
      : "You're not in this one — watch it burn";
    // Richer bystanders can still break the lottery; exact stacks pile in.
    const outbid = $("sd-outbid");
    if (my != null && !my.autopilot && openSlots(my) > 0 && my.budget > price) {
      outbid.hidden = false;
      outbid.textContent = `Bid $${price + 1}`;
      outbid.dataset.amount = String(price + 1);
    } else if (my != null && !my.autopilot && openSlots(my) > 0 &&
               my.budget === price) {
      outbid.hidden = false;
      outbid.textContent = `🎰 Match $${price} — pile in`;
      outbid.dataset.amount = String(price);
    } else {
      outbid.hidden = true;
    }
  }
}

function renderReveal(st) {
  const rv = S.reveal;
  $("sd-live").hidden = true;
  $("sd-seats").hidden = true;
  $("sd-panel").hidden = true;
  $("sd-watch").hidden = true;
  $("sd-sub").textContent = "";
  const box = $("sd-reveal");
  box.hidden = false;
  const winnerLine = rv.winner === st.you
    ? "You win the lottery" : `${mgrName(rv.winner)} wins the lottery`;
  const sub = rv.player?.name && rv.price != null
    ? `${rv.player.name} goes to ${mgrName(rv.winner)} for $${rv.price} — ` +
      "their entire bankroll."
    : "";
  box.replaceChildren(
    el("div", { class: "rv-eyebrow" }, "The mystery number was"),
    el("div", { class: "rv-number" }, String(rv.mystery)),
    el("div", { class: "rv-rows" }, rv.guesses.map((g) => {
      const won = g.manager === rv.winner;
      return el("div", { class: `rv-row${won ? " win" : ""}` },
        el("span", { class: "name" },
          youName(g.manager) + (won ? "  ← winner" : "")),
        el("span", { class: "guess" }, String(g.guess)),
        el("span", { class: "dist" }, `off by ${Math.abs(g.guess - rv.mystery)}`));
    })),
    el("div", { class: "rv-result" },
      el("div", { class: "title" }, winnerLine),
      sub ? el("div", { class: "sub" }, sub) : null));
}

// -------------------------------------------------------------- free pick

function renderFreePick(st) {
  const fp = st.free_pick;
  const isPicker = st.you === fp.picker;
  $("fp-title").textContent = isPicker
    ? "The pool is yours" : `${mgrName(fp.picker)} is picking free`;
  $("fp-sub").textContent = isPicker
    ? "Everyone else is broke or full. Pick anyone, no charge, until your " +
      `five are set.${st.config.mode === "blind" ? " Still masked — good luck." : ""}`
    : "Last manager standing with money. You're watching read-only.";
  const grid = $("fp-grid");
  grid.classList.toggle("dimmed", !isPicker);
  grid.replaceChildren(...fp.pool.map((p) => poolCard(p, {
    clickable: isPicker,
    // Cards send p.id verbatim — in blind mode ids are server aliases.
    onPick: () => send({ action: "pick", player_id: p.id }),
  })));
}

function poolCard(p, { clickable, onPick, price = null, infeasible = false }) {
  const masked = p.name == null;
  return el("button",
    {
      class: `pool-card${infeasible ? " dimmed" : ""}`,
      disabled: !clickable,
      onclick: onPick,
    },
    el("div", { class: "pc-head" },
      el("span", { class: "pc-pos" }, p.pos),
      el("span", { class: "pc-team" }, p.team),
      price != null ? el("span", { class: "price-badge" }, `$${price}`) : null),
    el("div", { class: `pc-name${masked ? " mystery" : ""}` },
      masked ? "Mystery" : p.name),
    el("div", { class: "pc-era" }, eraLine(p)),
    el("div", { class: "pc-stats" }, statSlash(p)));
}

// ------------------------------------------------------------------ snake
// Fully playable (decision: deviates from the handoff's "next round" tile):
// gold turn banner + the free-pick grid restyle with $1–$5 price badges and
// feasibility dimming on your turn. Dimmed cards stay clickable — the
// engine is the authority and rejects with the real reason.

function renderSnake(st) {
  const turn = st.turn;
  const my = me();
  const myTurn = my != null && turn.manager === my.id;
  $("sn-title").textContent = myTurn
    ? `🐍 Your pick — $${my.budget} left`
    : `🐍 ${mgrName(turn.manager)} is on the clock`;
  $("sn-sub").textContent = myTurn
    ? "Tap a player to draft them — keep $1 for every other empty slot."
    : "The pool is open — every price is the player's tier.";
  const empties = openSlots(my);
  // Cosmetic mirror of the engine's dollar-per-empty-slot reserve.
  const feasible = (price) =>
    my != null && price <= my.budget && my.budget - price >= empties - 1;
  const grid = $("sn-grid");
  grid.classList.toggle("dimmed", !myTurn);
  grid.replaceChildren(...(st.pool || []).map((p) => poolCard(p, {
    clickable: myTurn,
    price: p.price,
    infeasible: myTurn && !feasible(p.price),
    onPick: () => send({ action: "pick", player_id: p.id }),
  })));
}

// ----------------------------------------------------------------- lineup
// Both interactions per the handoff: pointer-drag (touch-capable) AND
// tap-one-tap-two swapping; a broadcast landing mid-drag is deferred via
// the dirty/flush pattern so the gesture survives CPU shuffles.

function renderLineup(st) {
  const my = me();
  $("lu-sub").textContent = my
    ? "Drag a card onto another, or tap one then tap two. Any player fits any slot."
    : "Managers are setting their lineups — hang tight.";
  $("lu-grid").replaceChildren(
    ...(my ? my.spots.map((s) => lineupCard(s)) : []));
}

function lineupCard(s) {
  const p = s.player;
  const card = el("div",
    { class: `lineup-card${p ? "" : " empty"}`, dataset: { slot: s.slot } },
    el("div", { class: "pos" }, s.slot),
    el("div", { class: "name" }, p ? p.name : "—"),
    el("div", { class: "meta" },
      p ? `${p.pos} · ${p.team} · ${p.decade}s` : ""),
    el("div", { class: "price" },
      p ? (s.price ? `paid $${s.price}` : "free") : ""));
  if (S.tapSlot === s.slot) card.classList.add("selected");
  card.addEventListener("pointerdown", (e) => onLineupDown(e, s.slot, card));
  card.addEventListener("pointermove", onLineupMove);
  card.addEventListener("pointerup", onLineupUp);
  card.addEventListener("pointercancel", onLineupCancel);
  return card;
}

function abortDrag() {
  const d = S.drag;
  if (!d) return;
  S.drag = null;
  d.card.classList.remove("drag-src");
  d.ghost?.remove();
  clearDropHints();
}

function flushLineupRender() {
  if (!S.lineupDirty) return;
  S.lineupDirty = false;
  render(); // paint the broadcasts skipped while the gesture was live
}

function onLineupDown(e, slot, card) {
  if (e.button !== undefined && e.button !== 0) return;
  card.setPointerCapture(e.pointerId);
  S.drag = { slot, card, id: e.pointerId, x: e.clientX, y: e.clientY, ghost: null };
}

function dropTargetAt(x, y) {
  return document.elementFromPoint(x, y)?.closest(".lineup-card:not(.ghost)");
}

function clearDropHints() {
  for (const c of document.querySelectorAll(".lineup-card.drop-hint")) {
    c.classList.remove("drop-hint");
  }
}

function onLineupMove(e) {
  const d = S.drag;
  if (!d || e.pointerId !== d.id) return;
  if (!d.ghost) {
    if (Math.hypot(e.clientX - d.x, e.clientY - d.y) < DRAG_THRESHOLD_PX) return;
    d.ghost = d.card.cloneNode(true);
    d.ghost.classList.add("ghost");
    d.ghost.style.width = `${d.card.offsetWidth}px`;
    document.body.append(d.ghost);
    d.card.classList.add("drag-src");
  }
  d.ghost.style.left = `${e.clientX}px`;
  d.ghost.style.top = `${e.clientY}px`;
  clearDropHints();
  const target = dropTargetAt(e.clientX, e.clientY);
  if (target && target !== d.card) target.classList.add("drop-hint");
}

function onLineupUp(e) {
  const d = S.drag;
  if (!d || e.pointerId !== d.id) return;
  S.drag = null;
  d.card.classList.remove("drag-src");
  clearDropHints();
  if (d.ghost) { // drag path
    d.ghost.remove();
    const target = dropTargetAt(e.clientX, e.clientY);
    if (target && target.dataset.slot && target.dataset.slot !== d.slot) {
      send({ action: "swap", a: d.slot, b: target.dataset.slot });
    }
    flushLineupRender();
    return;
  }
  // tap-A-tap-B path — same action, same engine authority
  if (S.tapSlot == null) {
    S.tapSlot = d.slot;
    d.card.classList.add("selected");
  } else if (S.tapSlot === d.slot) {
    S.tapSlot = null;
    d.card.classList.remove("selected");
  } else {
    const a = S.tapSlot;
    S.tapSlot = null;
    send({ action: "swap", a, b: d.slot });
  }
  flushLineupRender();
}

function onLineupCancel(e) {
  if (!S.drag || e.pointerId !== S.drag.id) return;
  abortDrag();
  flushLineupRender();
}

// ------------------------------------------------------------- completion

function renderDone(st) {
  $("done-sub").textContent =
    `${st.managers.length} teams, ${st.managers.length * 5} legends, ` +
    (st.config.mode === "blind" ? "every mask off." : "every dollar spent.");
  $("final-boards").replaceChildren(...st.managers.map((m) => {
    // "spent" is the sum of prices paid — free picks and auto-fills are $0,
    // and snake plays with the fixed $15 regardless of config.budget.
    const spent = m.spots.reduce((sum, s) => sum + (s.price || 0), 0);
    return el("div", { class: `final-card${m.id === st.you ? " me" : ""}` },
      el("div", { class: "final-head" },
        avatar(m.id, 26),
        el("span", { class: "name" }, youName(m.id)),
        el("span", { class: "spent" }, `$${spent} spent · $${m.budget} left`)),
      m.spots.map((s) => el("div", { class: "final-row" },
        el("span", { class: "pos" }, s.slot),
        el("span", { class: `name${s.player ? "" : " empty"}` },
          s.player ? s.player.name : "—"),
        el("span", { class: "price" },
          s.player ? (s.price ? `$${s.price}` : "free") : ""))));
  }));
  renderSimArea(st);
}

function renderSimArea(st) {
  const area = $("sim-area");
  if (st.config.sim === "off") { area.replaceChildren(); return; }
  const isCommish = st.you === st.commissioner;
  const rerun = isCommish
    ? el("button", { class: "rerun-btn", onclick: runSim }, "Rerun the sim")
    : null;
  const sim = S.sim;
  if (!sim) {
    area.replaceChildren(el("div", { class: "sim-panel" },
      el("div", { class: "sim-head" },
        el("div", {},
          el("div", { class: "sim-title" }, "Tournament sim"),
          el("div", { class: "sim-desc" }, "Waiting for sim results…")),
        rerun)));
    return;
  }
  if (sim.error) {
    area.replaceChildren(el("div", { class: "sim-panel" },
      el("div", { class: "sim-head" },
        el("div", { class: "sim-title" }, "Tournament sim"), rerun),
      el("p", { class: "sim-error" }, sim.error)));
    return;
  }
  if (sim.mode === "prompt") {
    const pre = el("pre", { class: "prompt-pre" }, sim.share_prompt || "");
    area.replaceChildren(el("div", { class: "sim-panel" },
      el("div", { class: "sim-head" },
        el("div", {},
          el("div", { class: "sim-title" }, "Run the tournament yourself"),
          el("div", { class: "sim-desc" },
            "Paste this into ChatGPT, Claude, Gemini — whatever you argue with.")),
        el("div", { style: "display:flex;gap:8px;align-items:center" },
          rerun,
          el("button", {
            class: "copy-btn",
            onclick: (e) => copyText(pre.textContent, e.currentTarget, "Copied ✓"),
          }, "Copy prompt"))),
      pre));
    return;
  }
  // stats / ai standings
  const title = sim.mode === "ai" ? "Blended standings 🏆" : "Standings 🏆";
  const rows = (sim.standings || []).map(([name, score], i) => el(
    "div", { class: `standing-row${i === 0 ? " first" : ""}` },
    el("span", { class: "rank" }, i === 0 ? "🏆" : String(i + 1)),
    el("span", { class: "name" }, name),
    el("span", { class: "pts" }, String(score))));
  area.replaceChildren(el("div", { class: "sim-panel" },
    el("div", { class: "sim-head" },
      el("div", { class: "sim-title" }, title), rerun),
    sim.note ? el("p", { class: "sim-note" }, sim.note) : null,
    el("div", { class: "standings" }, rows),
    sim.summary ? el("p", { class: "sim-story" }, sim.summary) : null));
}

async function runSim() {
  try {
    await api(`/api/rooms/${S.session.room}/simulate`,
      { token: S.session.token });
  } catch (err) {
    showError(err.message);
  }
}

// ------------------------------------------------------------- countdowns
// Server-time corrected: every clock re-derives from (deadline - serverNow)
// on each beat — a throttled tab never shows a stale time. Under 10s the
// numeral gains one decimal and the urgent state colors kick in.

function fmtClock(rem) {
  return rem < URGENT_SECONDS ? rem.toFixed(1) : String(Math.ceil(rem));
}

function remaining(deadline) {
  const now = S.pausedAt != null ? S.pausedAt : serverNow();
  return Math.max(0, deadline - now);
}

function setClockText(node, deadline) {
  const rem = remaining(deadline);
  node.textContent = fmtClock(rem);
  node.classList.toggle("urgent", rem < URGENT_SECONDS);
  return rem;
}

// The mobile strip condenses clock + bid + queue into one sticky line so
// the big desktop panels can be hidden on phones (they ate half the screen).
function activeDeadline(st) {
  if (st.phase === "auction" && st.lot) return st.lot.deadline;
  if (st.phase === "free_pick" && st.free_pick) return st.free_pick.deadline;
  if (st.phase === "snake" && st.turn) return st.turn.deadline;
  if (st.phase === "lineup" && st.lineup_deadline) return st.lineup_deadline;
  return null;
}

function renderMobileStrip(st) {
  let main = "", sub = "";
  if (st.phase === "auction" && st.lot) {
    if (st.lot.lottery) {
      main = "🎰 Showdown";
      sub = `$${st.lot.current_bid} locked`;
    } else if (st.lot.current_bid > 0) {
      main = `$${st.lot.current_bid}`;
      sub = st.lot.leader === st.you ? "you lead 👑" : mgrName(st.lot.leader);
    } else {
      main = "No bids";
      sub = "opens at $1";
    }
  } else if (st.phase === "free_pick" && st.free_pick) {
    main = "Free picks";
    sub = st.free_pick.picker === st.you ? "the pool is yours"
      : mgrName(st.free_pick.picker);
  } else if (st.phase === "snake" && st.turn) {
    main = "🐍 On the clock";
    sub = st.turn.manager === st.you ? "your pick" : mgrName(st.turn.manager);
  } else if (st.phase === "lineup") {
    main = "Set your lineup";
    sub = "locks soon";
  }
  $("m-main").textContent = main;
  $("m-sub").textContent = sub;
  $("m-queue").textContent =
    ["auction"].includes(st.phase) ? `${st.queue_count} left` : "";
}

function tick() {
  const st = S.state;
  if (!st || $("game").hidden) return;
  const dl = activeDeadline(st);
  if (dl != null) {
    const rem = remaining(dl);
    $("m-clock").textContent = fmtClock(rem);
    $("m-strip").classList.toggle("urgent", rem < URGENT_SECONDS);
  } else {
    $("m-clock").textContent = "—";
    $("m-strip").classList.remove("urgent");
  }
  if (st.phase === "auction" && st.lot && !$("auction-view").hidden) {
    const total = Math.max(1, st.config.lot_seconds);
    const rem = remaining(st.lot.deadline);
    $("clock-num").textContent = fmtClock(rem);
    const frac = Math.min(1, rem / total);
    $("clock-ringfg").setAttribute("stroke-dashoffset",
      String(276.5 * (1 - frac)));
    const urgent = rem < URGENT_SECONDS;
    $("clock-panel").classList.toggle("urgent", urgent);
    $("clock-snipe").hidden = !(urgent || S.snipeSeq === st.lot.seq);
  }
  if (!$("showdown-view").hidden && st.lot?.lottery && !S.reveal) {
    const rem = remaining(st.lot.deadline);
    $("sd-clock").textContent = String(Math.max(0, Math.ceil(rem)));
  }
  if (st.phase === "free_pick" && st.free_pick) {
    setClockText($("fp-clock"), st.free_pick.deadline);
  }
  if (st.phase === "snake" && st.turn) {
    setClockText($("sn-clock"), st.turn.deadline);
  }
  if (st.phase === "lineup" && st.lineup_deadline) {
    setClockText($("lu-clock"), st.lineup_deadline);
  }
}

// ----------------------------------------------------------- home wiring

function selectGroup(buttons, onSelect) {
  for (const btn of buttons) {
    btn.addEventListener("click", () => {
      for (const b of buttons) b.classList.toggle("selected", b === btn);
      onSelect(btn);
    });
  }
}

function createSummary() {
  const c = S.create;
  const budget = c.mode === "snake" ? 15 : Math.floor(Number($("c-budget").value) || 20);
  const clock = Math.floor(Number($("c-clock").value) || 30);
  const cpus = Math.floor(Number($("c-cpus").value) || 0);
  const from = $("c-era-from").value, to = $("c-era-to").value;
  $("create-summary").textContent =
    `${MODE_LABELS[c.mode]} · $${budget} · ${clock}s · ${from}s→${to}s · ${cpus} CPU`;
}

function wireHome() {
  const tiles = [...document.querySelectorAll(".mode-tile")];
  selectGroup(tiles, (btn) => {
    S.create.mode = btn.dataset.mode;
    // Snake plays the fixed $15 budget — the control becomes a LOCK notice.
    const snake = S.create.mode === "snake";
    $("c-budget-controls").hidden = snake;
    $("c-budget-note").hidden = !snake;
    $("c-budget-val").textContent = snake ? "$15" : `$${$("c-budget").value}`;
    createSummary();
  });
  selectGroup([...document.querySelectorAll(".pill-btn[data-depth]")],
    (btn) => { S.create.depth = btn.dataset.depth; });
  selectGroup([...document.querySelectorAll(".pill-btn[data-sim]")],
    (btn) => { S.create.sim = btn.dataset.sim; });

  const syncPair = (rangeId, numId, valId, fmt) => {
    const range = $(rangeId), num = $(numId);
    const update = (v) => {
      range.value = v;
      if (num) num.value = v;
      $(valId).textContent = fmt(v);
      createSummary();
    };
    range.addEventListener("input", () => update(range.value));
    num?.addEventListener("input", () => update(num.value));
  };
  syncPair("c-budget-range", "c-budget", "c-budget-val", (v) => `$${v}`);
  syncPair("c-clock-range", "c-clock", "c-clock-val", (v) => `${v}s`);
  $("c-lineup").addEventListener("input", () => {
    const v = Number($("c-lineup").value);
    $("c-lineup-val").textContent = v === 0 ? "skip" : `${v}s`;
  });
  $("c-cpus").addEventListener("input", () => {
    $("c-cpus-val").textContent = $("c-cpus").value;
    createSummary();
  });
  $("c-era-from").addEventListener("change", createSummary);
  $("c-era-to").addEventListener("change", createSummary);
  createSummary();

  $("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("c-name").value.trim();
    try {
      const res = await api("/api/rooms", {
        name,
        mode: S.create.mode,
        budget: Math.floor(Number($("c-budget").value)),
        clock: Math.floor(Number($("c-clock").value)),
        lineup: Math.floor(Number($("c-lineup").value)),
        cpus: Math.floor(Number($("c-cpus").value)),
        era_from: Number($("c-era-from").value),
        era_to: Number($("c-era-to").value),
        pool_depth: S.create.depth,
        sim: S.create.sim,
      });
      startSession(res, name);
    } catch (err) {
      formToast($("home-toast-create"), err.message);
    }
  });

  $("join-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("j-name").value.trim();
    const code = $("j-code").value.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    if (!code) { formToast($("home-toast-join"), "Enter a room code."); return; }
    try {
      const saved = loadSession(code);
      const res = await api(`/api/rooms/${code}/join`,
        saved?.token ? { name, token: saved.token } : { name });
      startSession(res, name);
    } catch (err) {
      formToast($("home-toast-join"), err.message);
    }
  });
}

// ----------------------------------------------------------- game wiring

function wireGame() {
  for (const btn of document.querySelectorAll(".bid-quick")) {
    btn.addEventListener("click", () => {
      const lot = S.state?.lot;
      if (!lot) return;
      send({ action: "bid", increment: Number(btn.dataset.inc), lot_seq: lot.seq });
    });
  }
  $("bid-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const lot = S.state?.lot;
    const amount = Math.floor(Number($("bid-amount").value));
    if (!lot || !Number.isFinite(amount) || amount < 1) return;
    send({ action: "bid", amount, lot_seq: lot.seq });
    $("bid-amount").value = "";
  });
  $("btn-allin").addEventListener("click", () => {
    const lot = S.state?.lot;
    const my = me();
    if (!lot || !my) return;
    send({ action: "bid", amount: my.budget, lot_seq: lot.seq });
  });
  // Exact-stack: the "force a showdown" bid IS the all-in match.
  $("btn-force").addEventListener("click", () => {
    const lot = S.state?.lot;
    const my = me();
    if (!lot || !my) return;
    send({ action: "bid", amount: my.budget, lot_seq: lot.seq });
  });
  $("sd-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const lot = S.state?.lot;
    const number = Math.floor(Number($("sd-guess").value));
    if (!lot || !Number.isFinite(number) || number < 1 || number > 100) return;
    send({ action: "guess", number, lot_seq: lot.seq });
  });
  $("sd-break").addEventListener("click", () => {
    const lot = S.state?.lot;
    if (!lot) return;
    send({ action: "bid", amount: lot.current_bid + 1, lot_seq: lot.seq });
  });
  $("sd-outbid").addEventListener("click", (e) => {
    const lot = S.state?.lot;
    const amount = Number(e.currentTarget.dataset.amount);
    if (!lot || !Number.isFinite(amount)) return;
    send({ action: "bid", amount, lot_seq: lot.seq });
  });
  $("lobby-start").addEventListener("click", () => send({ action: "start" }));
  $("btn-addcpu").addEventListener("click",
    () => send({ action: "add_cpu", count: 1 }));
  $("tab-feed").addEventListener("click", () => setChatTab(false));
  $("tab-chat").addEventListener("click", () => setChatTab(true));
  $("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("chat-input").value.trim();
    if (!text) return;
    send({ action: "chat", text });
    $("chat-input").value = "";
  });
  $("btn-pause").addEventListener("click", () =>
    send({ action: S.state?.paused ? "resume" : "pause" }));
  $("btn-addtime").addEventListener("click",
    () => send({ action: "addtime", seconds: ADDTIME_SECONDS }));
  $("btn-cancel").addEventListener("click", () => {
    if (confirm("Cancel the draft for everyone?")) send({ action: "cancel" });
  });
  $("btn-lobby-cancel").addEventListener("click", () => {
    if (confirm("Cancel the room for everyone?")) send({ action: "cancel" });
  });
  $("btn-reclaim").addEventListener("click", reclaimTeam);
  $("btn-leave").addEventListener("click", () => {
    send({ action: "leave" }); // lobby only — engine rule
    const code = S.session?.room;
    if (code) dropSession(code);
    exitToHome();
  });
  $("btn-copylink").addEventListener("click", (e) =>
    copyText(roomLink(S.session.room), e.currentTarget, "Link copied ✓"));
  $("btn-closed-home").addEventListener("click", exitToHome);
  $("btn-done-home").addEventListener("click", exitToHome);
  // Brand lockup = home. Mid-draft it's one click from abandoning the room,
  // so seated managers get a confirm; the saved token still reclaims later.
  const brandHome = () => {
    const live = S.state != null && ACTIVE_PHASES.includes(S.state.phase) &&
      me() != null;
    if (live && !confirm(
      "Head back home? The draft keeps going without you — reopen the room " +
      "link to take back over."
    )) return;
    exitToHome();
  };
  $("brand-game").addEventListener("click", brandHome);
  $("brand-lobby").addEventListener("click", brandHome);
}

// ------------------------------------------------------------------- boot

function boot() {
  wireHome();
  wireGame();
  const lastName = localStorage.getItem("nbadraft:name") || "";
  if (lastName) {
    $("c-name").value = lastName;
    $("j-name").value = lastName;
  }
  const code = hashCode();
  if (code) {
    $("j-code").value = code;
    const saved = loadSession(code);
    if (saved) {
      reclaimAndEnter(saved); // /join reclaim wakes autopilot; WS alone won't
      return void setInterval(tick, TICK_MS);
    }
  }
  showSection("home");
  setInterval(tick, TICK_MS);
}

boot();
