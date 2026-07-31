// NBA Draft Night — vanilla ES module frontend (no deps, no build step).
//
// Talks to webapp/server.py: REST for create/join/simulate, one WebSocket for
// everything else. The server broadcasts a full redacted state_view after
// every commit; render() rebuilds the visible view from scratch on each one
// (states are tiny). fx / error / sim messages drive the feed, toasts and the
// sim panel. The engine is the only authority — every control here is
// cosmetic and the server re-validates everything.

const FEED_CAP = 50;
const TOAST_MS = 4000;
const RECONNECT_BASE_MS = 1000;
const RECONNECT_CAP_MS = 10000;
const TICK_MS = 250;
const URGENT_SECONDS = 10;
const ADDTIME_SECONDS = 30;
const DRAG_THRESHOLD_PX = 8;
const ACTIVE_PHASES = ["auction", "snake", "free_pick", "lineup"];
const PHASE_LABELS = {
  lobby: "Lobby", auction: "Auction", snake: "Snake draft",
  free_pick: "Free pick", lineup: "Lineup", complete: "Complete",
  cancelled: "Cancelled",
};
// Blind mode serializes hidden players' names as null — render this instead.
const MYSTERY = "❓ Mystery player";
const MODE_LABELS = {
  auction: "", blind: "🕶 Blind auction", snake: "🐍 Snake draft",
};

const S = {
  session: null,   // {room, token, user_id, name}
  state: null,     // last state_view
  sim: null,       // last {"type":"sim"} payload
  ws: null,
  backoffMs: RECONNECT_BASE_MS,
  closed: false,   // deliberate close — suppress reconnect
  lastLotSeq: 0,   // for the new-lot flash animation
  tapSlot: null,   // tap-A-tap-B first selection (lineup swap)
  drag: null,      // live pointer-drag bookkeeping (lineup swap)
  feedSeeded: false,  // log backfill done for the current socket
  lineupDirty: false, // state arrived mid-drag — repaint the lineup on drop
  clockSamples: [],   // recent client_now - server_now (median = clock offset)
  lotMasked: false,   // last-rendered lot hid its name (blind) — sold = reveal
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

function hashCode() {
  return location.hash.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
}

function mgrName(id) {
  const m = S.state?.managers.find((m) => m.id === id);
  return m ? m.name : `manager ${id}`;
}

function shortName(name) {
  const parts = name.trim().split(/\s+/);
  return parts[parts.length - 1];
}

function statLine(p) {
  return `${p.ppg} ppg · ${p.rpg} rpg · ${p.apg} apg`;
}

// Player display name — blind mode nulls out hidden names on the wire.
function pName(p) {
  return p?.name ?? MYSTERY;
}

function eraLabel(cfg) {
  return cfg.era_start === cfg.era_end
    ? `${cfg.era_start}s`
    : `${cfg.era_start}s–${cfg.era_end}s`;
}

function roomLink(code) {
  return `${location.origin}/#${code}`;
}

async function copyText(text, btn, label = "✅ Copied!") {
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
    btn.textContent = label;
    setTimeout(() => { btn.textContent = original; }, 1500);
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

function toast(message, kind = "info") {
  const box = $("toasts");
  const t = el("div", { class: `toast ${kind}` }, message);
  box.append(t);
  while (box.children.length > 5) box.firstChild.remove();
  setTimeout(() => {
    t.classList.add("bye");
    setTimeout(() => t.remove(), 350);
  }, TOAST_MS);
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
    toast("Not connected — hang on…", "error");
  }
}

function handleMessage(msg) {
  if (msg.type === "state") {
    S.state = msg.state;
    noteServerNow(msg.now);
    if (!S.feedSeeded) { // once per socket, before any live fx lands
      seedFeed(msg.state);
      S.feedSeeded = true;
    }
    render();
  } else if (msg.type === "fx") {
    handleFx(msg.fx || []);
  } else if (msg.type === "error") {
    if (msg.message === "Room not found.") { roomGone(); return; }
    toast(msg.message, "error");
  } else if (msg.type === "sim") {
    S.sim = msg;
    render();
  }
}

function roomGone() {
  const code = S.session?.room;
  if (code) { try { localStorage.removeItem(storeKey(code)); } catch {} }
  toast("That room is gone.", "error");
  exitToHome();
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
  S.state = null;
  S.sim = null;
  S.lastLotSeq = 0;
  S.tapSlot = null;
  S.feedSeeded = false;
  S.lineupDirty = false;
  S.lotMasked = false;
  S.backoffMs = RECONNECT_BASE_MS;
  $("feed").replaceChildren();
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
    if (err.message === "Room not found.") {
      try { localStorage.removeItem(storeKey(saved.room)); } catch {}
    }
    toast(err.message, "error");
    showSection("home");
  }
}

async function reclaimTeam(e) {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    await api(`/api/rooms/${S.session.room}/join`,
      { name: S.session.name, token: S.session.token });
    reconnect(); // fresh socket + fresh state after the wake-up Join
  } catch (err) {
    if (err.message === "Room not found.") { roomGone(); return; }
    toast(err.message, "error");
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
  history.replaceState(null, "", location.pathname);
  showSection("home");
}

// ------------------------------------------------------------ fx handling

function fxLines(fx) {
  switch (fx.kind) {
    case "sold":
      // Blind reveal: the lot card said "❓ Mystery player" — the sold fx
      // carries the real name, so the feed line celebrates it.
      return S.lotMasked && fx.player?.name != null
        ? [`🔨 Sold to ${mgrName(fx.manager)} for $${fx.price} — ` +
           `👀 It was ${fx.player.name}!`]
        : [`🔨 ${pName(fx.player)} → ${mgrName(fx.manager)} for $${fx.price}`];
    case "passed":
      return [`↩️ ${pName(fx.player)} passed — back in the pool`];
    case "force":
      return [`⚡ ${pName(fx.player)} force-assigned to ${mgrName(fx.manager)}`];
    case "picked":
      return [`🎯 ${mgrName(fx.manager)} picked ${pName(fx.player)}`];
    case "autofill":
      return fx.assignments.map(
        (a) => `🤖 ${pName(a.player)} auto-filled → ${mgrName(a.manager)}`);
    case "snake_turn":
      return [fx.manager === S.state?.you
        ? "🐍 You're on the clock — make your pick!"
        : `🐍 ${mgrName(fx.manager)} is on the clock`];
    case "lottery_open":
      return [`🎰 ALL-IN SHOWDOWN — ${fx.participants.map(mgrName).join(" vs ")}` +
              ` tied at $${fx.amount}!`];
    case "lottery_joined":
      return [`🎰 ${mgrName(fx.manager)} joins the showdown!`];
    case "lottery_guessed":
      return [`🔒 ${mgrName(fx.manager)} locked in a number`];
    case "lottery_cancelled":
      return [`💥 ${mgrName(fx.manager)} outbid the tie — showdown off!`];
    case "lineup_open":
      return ["🧩 Rosters full — arrange your lineup!"];
    case "complete":
      return ["🏁 The draft is complete!"];
    case "paused":
      return ["⏸️ Draft paused"];
    case "resumed":
      return ["▶️ Draft resumed"];
    case "cancelled":
      return ["🛑 Draft cancelled"];
    case "autopilot":
      return [`🤖 ${mgrName(fx.manager)} is on autopilot`];
    default:
      return [];
  }
}

function handleFx(fxList) {
  for (const fx of fxList) {
    if (fx.kind === "lottery_reveal") { // result card, not a one-liner
      feedPush(revealCard(fx));
      toast(`🎰 Mystery number: ${fx.mystery} — ${mgrName(fx.winner)} wins!`);
      continue;
    }
    const lines = fxLines(fx);
    for (const line of lines) feedPush(line);
    if (fx.kind === "autofill" && fx.assignments.length > 1) {
      toast(`🤖 Auto-filled ${fx.assignments.length} roster spots`);
    } else if (lines.length) {
      toast(lines[0]);
    }
  }
}

function feedPush(line) {
  const feed = $("feed");
  feed.prepend(el("div", { class: "feed-item" }, line));
  while (feed.children.length > FEED_CAP) feed.lastChild.remove();
}

// ----------------------------------------------------------- feed backfill

// state.log entries carry the player NAME (string; null when blind-masked),
// not a player object — phrasing mirrors fxLines so backfilled and live
// lines read identically.
function logLine(entry) {
  const name = entry.player ?? MYSTERY;
  switch (entry.kind) {
    case "sold":
      return `🔨 ${name} → ${mgrName(entry.manager)} for $${entry.price}`;
    case "passed":
      return `↩️ ${name} passed — back in the pool`;
    case "force":
      return `⚡ ${name} force-assigned to ${mgrName(entry.manager)}`;
    case "pick": // snake picks log a price; free picks are on the house
      return `🎯 ${mgrName(entry.manager)} picked ${name}` +
        (entry.price ? ` ($${entry.price})` : "");
    case "autofill":
      return `🤖 ${name} auto-filled → ${mgrName(entry.manager)}`;
    default:
      return null;
  }
}

function seedFeed(st) {
  $("feed").replaceChildren(); // rebuild from the log — no duplicates
  for (const entry of st.log || []) { // chronological; prepend ends newest-first
    const line = logLine(entry);
    if (line) feedPush(line);
  }
}

// ------------------------------------------------------------ render loop

function showSection(name) {
  for (const id of ["home", "lobby", "game"]) $(id).hidden = id !== name;
}

function render() {
  const st = S.state;
  if (!S.session || !st) {
    showSection("home");
    return;
  }
  if (st.phase === "lobby") {
    showSection("lobby");
    renderLobby(st);
  } else {
    showSection("game");
    renderHeader(st);
    renderBoard(st);
    renderStage(st);
    const me = st.managers.find((m) => m.id === st.you);
    $("reclaim-bar").hidden =
      !me || !me.autopilot || !ACTIVE_PHASES.includes(st.phase);
  }
  $("pause-overlay").hidden = !st.paused;
  $("btn-resume").hidden = st.you !== st.commissioner;
  tick(); // countdowns update immediately, not after the next 250ms beat
}

// ----------------------------------------------------------------- lobby

function renderLobby(st) {
  $("lobby-code").textContent = S.session.room;
  const cfg = st.config;
  const mode = MODE_LABELS[cfg.mode] || "";
  const budget = cfg.mode === "snake" ? "$15 fixed" : `$${cfg.budget} budget`;
  $("lobby-cfg").textContent =
    `${mode ? `${mode} · ` : ""}${budget} · ${cfg.lot_seconds}s clock` +
    ` · ${eraLabel(cfg)} · pool: ${cfg.pool_depth} · sim: ${cfg.sim}` +
    ` · lineup ${cfg.lineup_seconds}s`;
  const isCommish = st.you === st.commissioner;
  $("lobby-mgrs").replaceChildren(
    ...st.managers.map((m) => el(
      "li",
      { class: m.id === st.you ? "me" : "" },
      `${m.cpu ? "🤖 " : ""}${m.name}` +
      `${m.id === st.commissioner ? " 👑" : ""}` +
      `${m.id === st.you ? " (you)" : ""}`,
      m.cpu && isCommish
        ? el("button", {
            class: "ghost small-btn rm-cpu",
            title: "Remove CPU",
            onclick: () => send({ action: "remove_cpu", cpu_id: m.id }),
          }, "✖")
        : !m.cpu && isCommish && m.id !== st.you
          ? el("button", {
              class: "ghost small-btn kick-btn",
              title: `Kick ${m.name}`,
              onclick: () => send({ action: "kick", target: m.id }),
            }, "✖")
          : null,
    )),
  );
  $("btn-start").hidden = !isCommish;
  $("btn-start").disabled = st.managers.length < 2;
  $("btn-addcpu").hidden = !isCommish;
  $("btn-lobby-cancel").hidden = !isCommish;
}

// ---------------------------------------------------------- game chrome

function renderHeader(st) {
  $("gh-code").textContent = S.session.room;
  $("gh-phase").textContent = PHASE_LABELS[st.phase] ?? st.phase;
  $("gh-pool").textContent =
    ["auction", "snake", "free_pick"].includes(st.phase)
      ? `${st.queue_count} in pool` : "";
  const isCommish = st.you === st.commissioner;
  $("commish-controls").hidden = !isCommish || !ACTIVE_PHASES.includes(st.phase);
  $("btn-pause").hidden = st.paused;
  $("btn-addtime").hidden =
    !["auction", "snake", "free_pick"].includes(st.phase);
}

function renderBoard(st) {
  const canKick =
    st.you === st.commissioner && ACTIVE_PHASES.includes(st.phase);
  $("board").replaceChildren(...st.managers.map((m) => el(
    "div",
    { class: `mgr${m.id === st.you ? " me" : ""}` },
    el("div", { class: "mgr-head" },
      el("span", { class: "mgr-name" },
        `${m.cpu ? "🤖 " : ""}${m.name}` +
        `${m.id === st.commissioner ? " 👑" : ""}` +
        `${m.autopilot ? " 💤" : ""}`),
      el("span", { class: "mgr-budget" }, `$${m.budget}`),
      canKick && m.id !== st.you
        ? el("button", {
            class: "ghost kick-btn",
            title: `Kick ${m.name}`,
            onclick: () => send({ action: "kick", target: m.id }),
          }, "✖")
        : null),
    el("div", { class: "mgr-slots" }, m.spots.map((s) => el(
      "div", { class: "mgr-slot" },
      el("span", { class: "slot-tag" }, s.slot),
      el("span", { class: `ms-name${s.player ? "" : " muted"}` },
        s.player ? shortName(s.player.name) : "—"),
      el("span", { class: "ms-price muted" },
        s.player && s.price ? `$${s.price}` : ""),
    ))),
  )));
}

// ------------------------------------------------------------------ stage

function renderStage(st) {
  if (S.drag && st.phase !== "lineup") abortDrag(); // phase moved on mid-drag
  const zone = $("lot-zone");
  const stage = $("stage");
  if (st.phase === "auction" && st.lot) {
    zone.hidden = false;
    renderLot(st);
    updateShowdown(st);
    updateBidBar(st);
    stage.replaceChildren();
    return;
  }
  zone.hidden = true; // snake/free_pick/lineup: no lot card, no bid bar
  if (st.phase === "snake" && st.turn) {
    stage.replaceChildren(snakeView(st));
  } else if (st.phase === "free_pick" && st.free_pick) {
    stage.replaceChildren(freePickView(st));
  } else if (st.phase === "lineup") {
    if (S.drag) {
      // A broadcast landed mid-drag (CPUs self-arrange constantly) — keep
      // the gesture alive and repaint from fresh state on drop.
      S.lineupDirty = true;
    } else {
      S.lineupDirty = false;
      stage.replaceChildren(lineupView(st));
    }
  } else if (st.phase === "complete") {
    stage.replaceChildren(completeView(st));
  } else if (st.phase === "cancelled") {
    stage.replaceChildren(cancelledView());
  } else {
    stage.replaceChildren();
  }
}

// ---------------------------------------------------------------- auction

function renderLot(st) {
  const lot = st.lot;
  const p = lot.player;
  const fresh = lot.seq !== S.lastLotSeq;
  S.lastLotSeq = lot.seq;
  S.lotMasked = p.name == null; // blind mode — the sold fx is the reveal
  const meta = [p.pos, p.team, `${p.decade}s`];
  if (p.prime) meta.push(`prime ${p.prime}`);
  const card = el("div", { class: `lot-card${fresh ? " flash" : ""}` },
    el("div", { class: "lot-top" },
      el("span", { class: "muted small" }, `Lot #${lot.seq}`),
      lot.last_call ? el("span", { class: "last-call" }, "🚨 LAST CALL") : null,
      el("span", { class: "countdown big", dataset: { deadline: lot.deadline } })),
    el("h2", { class: `lot-name${p.name == null ? " mystery" : ""}` }, pName(p)),
    el("div", { class: "lot-meta muted" }, meta.join(" · ")),
    el("div", { class: "lot-stats" }, statLine(p)),
    el("div", { class: "lot-bid" },
      lot.current_bid > 0
        ? `💰 $${lot.current_bid} — ${mgrName(lot.leader)}` +
          `${lot.leader === st.you ? " (you)" : ""}`
        : "No bids yet"));
  $("lot-slot").replaceChildren(card);
}

function updateBidBar(st) {
  const lot = st.lot;
  const me = st.managers.find((m) => m.id === st.you);
  const full = me != null && me.spots.every((s) => s.player);
  const leading = me != null && lot.leader === me.id;
  const inShowdown =
    lot.lottery != null && me != null && lot.lottery.participants.includes(me.id);
  const onAutopilot = me != null && me.autopilot;
  // Cosmetic only — the engine re-validates every bid. Richer managers keep
  // live bid controls during a showdown (outbidding cancels it) — including
  // the dragged-in leader, whose raise is what kills their own lottery.
  const blocked = (need) =>
    me == null || onAutopilot || full || (leading && lot.lottery == null) ||
    me.budget < lot.current_bid + need;
  // An exact-stack tie is a legal Custom bid: it opens or joins the showdown.
  const allInTie =
    me != null && !onAutopilot && !full && !leading && !inShowdown &&
    lot.current_bid >= 1 && me.budget === lot.current_bid;
  for (const btn of document.querySelectorAll(".bid-quick")) {
    btn.disabled = blocked(Number(btn.dataset.inc));
  }
  $("bid-amount").disabled = blocked(1) && !allInTie;
  $("bid-custom").disabled = blocked(1) && !allInTie;
  $("bid-note").textContent =
    me == null ? "Spectating"
      : onAutopilot ? "On autopilot — reclaim your team to bid"
      : full ? "Roster full"
      : inShowdown && leading && me.budget > lot.current_bid
        ? "🎰 Lock in a number — or raise your bid to call the showdown off"
      : inShowdown ? "🎰 You're in the showdown — lock in your number!"
      : allInTie ? `🎰 Match with your last $${me.budget} to force a showdown!`
      : leading ? "You lead 👑"
      : me.budget <= lot.current_bid ? `Priced out — $${me.budget} left`
      : `$${me.budget} left`;
}

// --------------------------------------------------------------- showdown

function updateShowdown(st) {
  const box = $("showdown");
  const lo = st.lot?.lottery;
  if (!lo) {
    box.hidden = true;
    $("sd-clock").dataset.deadline = "";
    return;
  }
  box.hidden = false;
  // All names flow through textContent — never markup.
  $("sd-banner").textContent =
    `🎰 ALL-IN SHOWDOWN — ${lo.participants.map(mgrName).join(" vs ")} ` +
    `tied at $${st.lot.current_bid}. Closest guess to the mystery ` +
    "number (1–100) wins the player!";
  $("sd-clock").dataset.deadline = st.lot.deadline;
  const mine = st.you != null && lo.participants.includes(st.you);
  $("sd-form").hidden = !mine;
  const note = $("sd-note");
  if (mine) {
    note.textContent = lo.your_guess != null
      ? `✓ Locked in: ${lo.your_guess} — you can change it until the reveal.`
      : "Pick a number and lock it in — it stays secret until the reveal.";
    return;
  }
  const me = st.managers.find((m) => m.id === st.you);
  const canCancel =
    me != null && !me.autopilot && me.budget > st.lot.current_bid &&
    !me.spots.every((s) => s.player);
  note.textContent =
    `${lo.entered.length}/${lo.participants.length} locked in` +
    (canCancel ? ` — outbid $${st.lot.current_bid} to cancel the showdown.` : ".");
}

function revealCard(fx) {
  return el("div", { class: "reveal-card" },
    el("div", { class: "reveal-head" }, `🎰 Mystery number: ${fx.mystery}`),
    fx.guesses.map((g) => el("div",
      { class: `reveal-row${g.manager === fx.winner ? " win" : ""}` },
      el("span", {},
        `${g.manager === fx.winner ? "🏆 " : ""}${mgrName(g.manager)}`),
      el("span", { class: "muted" },
        `guessed ${g.guess} · off by ${Math.abs(g.guess - fx.mystery)}`))));
}

// ------------------------------------------------------------------ snake

function snakeView(st) {
  const turn = st.turn;
  const me = st.managers.find((m) => m.id === st.you);
  const myTurn = me != null && turn.manager === me.id;
  const empties = me ? me.spots.filter((s) => !s.player).length : 0;
  // Cosmetic mirror of the engine's dollar-per-empty-slot reserve: a pick
  // must leave $1 for each other empty slot. Dimmed cards stay clickable —
  // the engine is the authority and rejects with the real reason.
  const feasible = (price) =>
    me != null && price <= me.budget && me.budget - price >= empties - 1;
  return el("div", { class: "stage-block" },
    el("div", { class: `turn-banner${myTurn ? " your-turn" : ""}` },
      el("span", { class: "turn-text" },
        myTurn
          ? `🐍 Your pick — $${me.budget} left`
          : `🐍 ${mgrName(turn.manager)} is on the clock`),
      el("span", { class: "countdown big", dataset: { deadline: turn.deadline } })),
    el("p", { class: "muted small" },
      myTurn
        ? "Tap a player to draft them — keep $1 for every other empty slot."
        : "The pool is open — every price is the player's star tier."),
    el("div", { class: "pool-grid" }, (st.pool || []).map((p) => el(
      "button",
      {
        class: `pool-card${myTurn && !feasible(p.price) ? " dimmed" : ""}`,
        disabled: !myTurn,
        onclick: () => send({ action: "pick", player_id: p.id }),
      },
      el("div", { class: "pc-head" },
        el("span", { class: "pc-name" }, pName(p)),
        el("span", { class: "price-badge" }, `$${p.price}`)),
      el("div", { class: "pc-meta muted" },
        `${p.pos} · ${p.team} · ${p.decade}s`),
      el("div", { class: "pc-stats muted" }, statLine(p)),
    ))));
}

// -------------------------------------------------------------- free pick

function freePickView(st) {
  const fp = st.free_pick;
  const isPicker = st.you === fp.picker;
  return el("div", { class: "stage-block" },
    el("div", { class: "stage-head" },
      el("h2", {}, `🎯 Free pick — ${mgrName(fp.picker)} chooses`),
      el("span", { class: "countdown big", dataset: { deadline: fp.deadline } })),
    el("p", { class: "muted" },
      isPicker
        ? "You're the last board standing — take anyone, on the house."
        : "The pool is revealed. Spectating while they choose…"),
    el("div", { class: "pool-grid" }, fp.pool.map((p) => el(
      "button",
      {
        class: "pool-card",
        disabled: !isPicker,
        onclick: () => send({ action: "pick", player_id: p.id }),
      },
      el("div", { class: `pc-name${p.name == null ? " mystery" : ""}` },
        pName(p)),
      el("div", { class: "pc-meta muted" },
        `${p.pos} · ${p.team} · ${p.decade}s`),
      el("div", { class: "pc-stats muted" }, statLine(p)),
    ))));
}

// ----------------------------------------------------------------- lineup

function lineupView(st) {
  const me = st.managers.find((m) => m.id === st.you);
  const block = el("div", { class: "stage-block" },
    el("div", { class: "stage-head" },
      el("h2", {}, "🧩 Arrange your lineup"),
      el("span", { class: "countdown big", dataset: { deadline: st.lineup_deadline } })));
  if (!me) {
    block.append(el("p", { class: "muted" },
      "Managers are setting their lineups — hang tight."));
    return block;
  }
  block.append(
    el("div", { class: "lineup-grid" }, me.spots.map((s) => lineupCard(s))),
    el("p", { class: "muted small" },
      "Drag a card onto another — or tap two — to swap slots."));
  return block;
}

function lineupCard(s) {
  const card = el("div",
    { class: "lineup-card", dataset: { slot: s.slot } },
    el("div", { class: "slot-tag big-tag" }, s.slot),
    s.player
      ? [
          el("div", { class: "lc-name" }, s.player.name),
          el("div", { class: "lc-meta muted" },
            `${s.player.pos} · ${s.player.decade}s`),
          el("div", { class: "lc-stats muted" }, statLine(s.player)),
        ]
      : el("div", { class: "lc-name muted" }, "—"));
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

function completeView(st) {
  return el("div", { class: "stage-block" },
    el("h2", {}, "🏁 Final rosters"),
    el("div", { class: "roster-grid" }, st.managers.map((m) => rosterCard(m, st))),
    simPanel(st));
}

function rosterCard(m, st) {
  // Sum of prices paid, not config.budget - budget: snake plays with the
  // fixed $15 budget regardless of the configured auction budget.
  const spent = m.spots.reduce((sum, s) => sum + (s.price || 0), 0);
  return el("div", { class: `panel roster-card${m.id === st.you ? " me" : ""}` },
    el("div", { class: "mgr-head" },
      el("span", { class: "mgr-name" },
        `${m.cpu ? "🤖 " : ""}${m.name}` +
        `${m.id === st.commissioner ? " 👑" : ""}` +
        `${m.autopilot ? " 💤" : ""}`),
      el("span", { class: "muted small" }, `spent $${spent}`)),
    el("div", { class: "mgr-slots" }, m.spots.map((s) => el(
      "div", { class: "mgr-slot" },
      el("span", { class: "slot-tag" }, s.slot),
      el("span", { class: "ms-name" }, s.player ? s.player.name : "—"),
      el("span", { class: "ms-price muted" },
        s.player && s.price ? `$${s.price}` : ""),
    ))));
}

function simPanel(st) {
  if (st.config.sim === "off") return el("div");
  const panel = el("div", { class: "panel sim-panel" },
    el("h3", {}, "🎲 Tournament sim"));
  const rerunBtn = () => el("button",
    { class: "ghost", onclick: runSim }, "🔁 Run sim");
  const sim = S.sim;
  if (!sim) {
    panel.append(
      el("p", { class: "muted" }, "Waiting for sim results…"),
      rerunBtn());
  } else if (sim.error) {
    panel.append(el("p", { class: "sim-error" }, sim.error), rerunBtn());
  } else if (sim.mode === "prompt") {
    const ta = el("textarea", { class: "prompt-ta", readonly: "", rows: "12" });
    ta.value = sim.share_prompt || "";
    panel.append(
      el("p", { class: "muted small" },
        "Paste this into your favorite LLM to run the tournament."),
      ta,
      el("button", {
        class: "primary",
        onclick: (e) => copyText(ta.value, e.currentTarget),
      }, "📋 Copy prompt"));
  } else {
    if (sim.note) panel.append(el("p", { class: "muted small" }, `ℹ️ ${sim.note}`));
    panel.append(
      el("table", { class: "standings" },
        el("thead", {}, el("tr", {},
          el("th", {}, "#"), el("th", {}, "Team"), el("th", {}, "Score"))),
        el("tbody", {}, (sim.standings || []).map(([name, score], i) =>
          el("tr", { class: i === 0 ? "champ" : "" },
            el("td", {}, i + 1), el("td", {}, name), el("td", {}, score))))),
      el("p", { class: "champion" }, `🏆 Champion: ${sim.champion}`),
      el("p", { class: "sim-summary" }, sim.summary || ""),
      rerunBtn());
  }
  return panel;
}

async function runSim() {
  try {
    await api(`/api/rooms/${S.session.room}/simulate`, { token: S.session.token });
    toast("Sim started…");
  } catch (err) {
    toast(err.message, "error");
  }
}

function cancelledView() {
  return el("div", { class: "stage-block center" },
    el("h2", {}, "🛑 Draft cancelled"),
    el("p", { class: "muted" }, "The commissioner called it off."),
    el("button", { class: "primary", onclick: exitToHome }, "🏠 Back home"));
}

// ------------------------------------------------------------- countdowns

function fmtClock(rem) {
  if (rem >= 60) {
    const m = Math.floor(rem / 60);
    const s = Math.floor(rem % 60);
    return `${m}:${String(s).padStart(2, "0")}`;
  }
  if (rem < URGENT_SECONDS) return `${rem.toFixed(1)}s`;
  return `${Math.ceil(rem)}s`;
}

function tick() {
  // Deadlines are server epoch seconds — correct for local clock drift.
  const now = Date.now() / 1000 - clockOffset();
  const paused = Boolean(S.state?.paused);
  for (const node of document.querySelectorAll("[data-deadline]")) {
    const deadline = parseFloat(node.dataset.deadline);
    if (!deadline) {
      node.textContent = "";
      node.classList.remove("urgent");
      continue;
    }
    if (paused) {
      node.textContent = "⏸";
      node.classList.remove("urgent");
      continue;
    }
    const rem = Math.max(0, deadline - now);
    node.textContent = fmtClock(rem);
    node.classList.toggle("urgent", rem < URGENT_SECONDS);
  }
}

// ------------------------------------------------------------------- boot

function wireHome() {
  // Snake plays with the fixed $15 budget — the budget input goes inert.
  const syncMode = () => {
    const snake = $("c-mode").value === "snake";
    $("c-budget").disabled = snake;
    $("c-budget-note").hidden = !snake;
  };
  $("c-mode").addEventListener("change", syncMode);
  syncMode();
  $("create-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("c-name").value.trim();
    try {
      const res = await api("/api/rooms", {
        name,
        mode: $("c-mode").value,
        budget: Math.floor(Number($("c-budget").value)),
        clock: Math.floor(Number($("c-clock").value)),
        lineup: Math.floor(Number($("c-lineup").value)),
        cpus: Math.floor(Number($("c-cpus").value)),
        era_from: Number($("c-era-from").value),
        era_to: Number($("c-era-to").value),
        pool_depth: $("c-pool").value,
        sim: $("c-sim").value,
      });
      startSession(res, name);
    } catch (err) {
      toast(err.message, "error");
    }
  });
  $("join-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = $("j-name").value.trim();
    const code = $("j-code").value.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    if (!code) { toast("Enter a room code.", "error"); return; }
    try {
      const saved = loadSession(code);
      const res = await api(`/api/rooms/${code}/join`,
        saved?.token ? { name, token: saved.token } : { name });
      startSession(res, name);
    } catch (err) {
      toast(err.message, "error");
    }
  });
}

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
  $("sd-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const lot = S.state?.lot;
    const number = Math.floor(Number($("sd-guess").value));
    if (!lot || !Number.isFinite(number) || number < 1 || number > 100) return;
    send({ action: "guess", number, lot_seq: lot.seq });
  });
  $("btn-start").addEventListener("click", () => send({ action: "start" }));
  $("btn-addcpu").addEventListener("click",
    () => send({ action: "add_cpu", count: 1 }));
  $("btn-pause").addEventListener("click", () => send({ action: "pause" }));
  $("btn-resume").addEventListener("click", () => send({ action: "resume" }));
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
    if (code) { try { localStorage.removeItem(storeKey(code)); } catch {} }
    exitToHome();
  });
  const copyRoomLink = (e) =>
    copyText(roomLink(S.session.room), e.currentTarget, "✅");
  $("btn-copylink").addEventListener("click", copyRoomLink);
  $("btn-share").addEventListener("click", copyRoomLink);
}

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
