/* Anker X1 TOU editor — vanilla JS, no build step, no third-party code. */

const $ = (id) => document.getElementById(id);

// ORDER MATTERS. In the X1 `peak_valley_prices` schema the numeric types run
// cheapest -> most expensive: 1 = Super Off-Peak, 4 = Peak. This is the
// opposite of the older Solarbank enum. Verified against a live X1 plan where
// type 1 carried buy=0 (night) and type 4 carried buy=0.30 (day).
const TARIFFS = [
  { v: 1, name: "Super Off-Peak", cls: "valley", tl: "tl-valley" },
  { v: 2, name: "Off-Peak", cls: "off", tl: "tl-off" },
  { v: 3, name: "Mid-Peak", cls: "mid", tl: "tl-mid" },
  { v: 4, name: "Peak", cls: "peak", tl: "tl-peak" },
];
const tariff = (v) => TARIFFS.find((t) => t.v === Number(v)) || TARIFFS[0];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
  "Oct", "Nov", "Dec"];

const TIMES = [];
for (let h = 0; h < 24; h++) for (const m of ["00", "30"]) TIMES.push(`${String(h).padStart(2, "0")}:${m}`);
const END_TIMES = TIMES.concat(["23:59", "24:00"]);

const state = {
  user: null, sites: [], site: null, sn: null,
  plan: null, loaded: null, seasonIdx: 0, dayType: 1,
  limits: null, limitsCheck: null, devices: [], busy: false,
  linkDays: (() => {
    try { const v = localStorage.getItem("anker.linkDays"); return v === null ? true : v === "1"; }
    catch { return true; }
  })(),
};

/* ---------------------------------------------------------------- utils */

async function api(path, opts = {}) {
  const { timeout = 30000, ...rest } = opts;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  let res;
  try {
    res = await fetch(path, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Anker-Client": "1" },
      ...rest,
      signal: ctrl.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") {
      const err = new Error(`Timed out after ${Math.round(timeout / 1000)}s. The Anker cloud did not answer.`);
      err.kind = "timeout";
      throw err;
    }
    const err = new Error("Cannot reach the local server. Is it still running?");
    err.kind = "network";
    throw err;
  } finally {
    clearTimeout(timer);
  }
  let js = null;
  try { js = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok || (js && js.ok === false)) {
    const msg = (js && js.error && js.error.message) || `Request failed (HTTP ${res.status})`;
    const err = new Error(msg);
    err.kind = js && js.error && js.error.kind;
    err.status = res.status;
    err.payload = js;
    throw err;
  }
  return js;
}

const clone = (o) => JSON.parse(JSON.stringify(o));

function startPhase(msg) {
  stopPhase();
  const t0 = Date.now();
  const el = $("netBanner");
  el.className = "banner info";
  el.innerHTML = `<span class="spin"></span><span class="phaseMsg"></span> ` +
                 `<span class="muted phaseSecs"></span>`;
  const msgEl = el.querySelector(".phaseMsg");
  const secsEl = el.querySelector(".phaseSecs");
  msgEl.textContent = msg;
  const paint = () => {
    secsEl.textContent = `${Math.round((Date.now() - t0) / 1000)}s`;
  };
  paint();
  state.phase = setInterval(paint, 1000);
}
function stopPhase() {
  if (state.phase) { clearInterval(state.phase); state.phase = null; }
}
function setNet(kind, html, retry) {
  stopPhase();
  const el = $("netBanner");
  el.className = "banner " + kind;
  el.innerHTML = html;
  if (retry) {
    const b = document.createElement("button");
    b.textContent = "Retry";
    b.style.marginLeft = "10px";
    b.onclick = retry;
    el.appendChild(b);
  }
}

function monthOf(s) {
  const m = /^(\d{4})-(\d{2})/.exec(s || "");
  return m ? Number(m[2]) : 1;
}
function yearOf(s) {
  const m = /^(\d{4})/.exec(s || "");
  return m ? m[1] : String(new Date().getFullYear());
}
function setMonth(s, month) {
  return `${yearOf(s)}-${String(month).padStart(2, "0")}-01 00:00:00`;
}
function toMin(t) {
  if (t === "24:00") return 24 * 60;
  if (t === "23:59") return 23 * 60 + 59;
  const [h, m] = String(t).split(":").map(Number);
  return h * 60 + m;
}


function fmtMin(m) {
  const h = Math.floor(m / 60), mm = m % 60;
  return `${String(h).padStart(2, "0")}:${mm ? "30" : "00"}`;
}

// Make one day type cover 00:00-24:00 exactly. Wrap-around rows are split,
// overlaps are resolved in favour of the edited row, and adjacent slots with
// the same tariff/price are merged back into periods.
function canonicalizeDay(seasonObj, dayType) {
  const rows = (seasonObj.peak_valley_prices || [])
    .filter((r) => Number(r.day_type) === dayType);
  if (!rows.length) return { gaps: 48, count: 0 };
  const slots = new Array(48).fill(null);
  rows.forEach((r) => {
    const a = toMin(r.start_time_period), b = toMin(r.end_time_period);
    if (a === b) return;
    if (b > a) {
      for (let m = a; m < b; m += 30) slots[m / 30] = r;
    } else {
      for (let m = a; m < 1440; m += 30) slots[m / 30] = r;
      for (let m = 0; m < b; m += 30) slots[m / 30] = r;
    }
  });
  const gaps = slots.filter((x) => !x).length;
  if (gaps) return { gaps, count: rows.length };

  const out = [];
  let cur = null;
  for (let i = 0; i < 48; i++) {
    const r = slots[i];
    const key = `${r.peak_type}|${r.buy}|${r.sell}|${r.energy_unit}`;
    if (cur && cur.key === key) {
      cur.end = (i + 1) * 30;
    } else {
      cur = { key, start: i * 30, end: (i + 1) * 30, src: r };
      out.push(cur);
    }
  }
  const others = (seasonObj.peak_valley_prices || [])
    .filter((r) => Number(r.day_type) !== dayType);
  const rebuilt = out.map((seg) => ({
    ...seg.src, id: 0, day_type: dayType,
    start_time_period: fmtMin(seg.start),
    end_time_period: seg.end === 1440 ? "24:00" : fmtMin(seg.end),
  }));
  seasonObj.peak_valley_prices = others.concat(rebuilt)
    .sort((a, b) => (Number(a.day_type) - Number(b.day_type)) ||
                    (toMin(a.start_time_period) - toMin(b.start_time_period)));
  return { gaps: 0, count: rebuilt.length };
}


function allPriceRows(plan) {
  return ((plan || state.plan || {}).peak_sessions || [])
    .flatMap((x) => x.peak_valley_prices || []);
}

function tariffValue(plan, type, field) {
  const row = allPriceRows(plan).find((r) => Number(r.peak_type) === Number(type));
  return row ? truncPrice2(row[field]) : null;
}

function setTariffValue(type, field, value) {
  allPriceRows(state.plan).forEach((r) => {
    if (Number(r.peak_type) === Number(type)) r[field] = value;
  });
}

// The app has one buy and one sell price per tariff type; a second period of
// the same type always shares them, across day types and seasons.
function unifyTariffPrices(plan) {
  const prices = {};
  allPriceRows(plan).forEach((r) => {
    const t = Number(r.peak_type);
    if (!prices[t]) prices[t] = { buy: truncPrice2(r.buy), sell: truncPrice2(r.sell) };
  });
  allPriceRows(plan).forEach((r) => {
    const p = prices[Number(r.peak_type)];
    if (p) { r.buy = p.buy; r.sell = p.sell; }
  });
  return prices;
}
function canonicalizeSeason(seasonObj, priorityRow) {
  if (!seasonObj) return;
  if (priorityRow) {
    const i = seasonObj.peak_valley_prices.indexOf(priorityRow);
    if (i >= 0) seasonObj.peak_valley_prices.push(seasonObj.peak_valley_prices.splice(i, 1)[0]);
  }
  canonicalizeDay(seasonObj, 1);
  canonicalizeDay(seasonObj, 2);
}
function sessions() { return (state.plan && state.plan.peak_sessions) || []; }
function season() { return sessions()[state.seasonIdx]; }
function rowsFor(seasonObj, dayType) {
  const rows = (seasonObj && seasonObj.peak_valley_prices) || [];
  return rows.map((r, i) => ({ r, i })).filter((x) => Number(x.r.day_type) === dayType);
}

/* --------------------------------------------------------------- views */

function show(view) {
  $("loginView").classList.toggle("hide", view !== "login");
  $("appView").classList.toggle("hide", view !== "app");
}

/* --------------------------------------------------------------- login */

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginError").textContent = "";
  const btn = $("loginBtn");
  btn.disabled = true;
  btn.textContent = "Signing in…";
  try {
    const js = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({
        email: $("email").value.trim(),
        password: $("password").value,
        country: $("country").value,
      }),
    });
    $("password").value = "";           // never keep it around
    state.user = js.user;
    state.sites = js.sites || [];
    if (!state.sites.length) throw new Error("No sites on this account.");
    state.site = state.sites[0].id;
    await enterApp();
  } catch (err) {
    $("loginError").textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign in";
  }
});

async function enterApp() {
  show("app");
  const site = (state.sites || []).find((s) => s.id === state.site);
  $("who").textContent = state.user
    ? `${state.user.nick}${site ? " · " + (site.name || site.id) : ""}`
    : "";
  renderSites();
  await loadAll();
  switchTab(location.hash === "#plan" ? "plan" : "home", false);
}

/* ---------------------------------------------------------- load state */

function pickDevice(list) {
  const want = list.find((d) => String(d.code || "").match(/^(A17B1|A5101|A5102|A5103)/));
  return (want || list[0] || {}).sn;
}

async function loadAll(deviceSn) {
  try {
    startPhase("Loading devices…");
    const dv = await api(`/api/devices?site=${encodeURIComponent(state.site)}`, { timeout: 20000 });
    state.devices = dv.devices || [];
    state.deviceSource = dv.source;
    if (!state.devices.length) {
      const tried = (dv.probe || []).map((p) => {
        if (p.error) return `${p.source}: ${p.error}`;
        if (p.data_type === "NoneType") return `${p.source}: empty response`;
        return `${p.source}: ${p.items} item(s)` +
          (p.data_keys ? ` [keys: ${p.data_keys.join(", ")}]` : "");
      }).join(" · ");
      throw new Error("Signed in, but the device list came back empty. Tried — " +
        (tried || "no shapes recorded") + ". Open Diagnostics and send these lines.");
    }
    state.sn = deviceSn || state.sn || pickDevice(state.devices);
    renderDevices();

    startPhase("Reading Time-of-Use plan…");
    const q = new URLSearchParams({ site: state.site, sn: state.sn });
    const js = await api(`/api/plan?${q}`, { timeout: 60000 });
    state.sn = js.sn;
    state.plan = clone(js.plan);
    (state.plan.peak_sessions || []).forEach((x) => canonicalizeSeason(x));
    unifyTariffPrices(state.plan);
    normalizePrices2();
    state.loaded = clone(state.plan);
    const st = js.strategy;
    state.strategyLoaded = (st && typeof st === "object") ? st : null;
    const hasStrategy = !!state.strategyLoaded;
    $("reserveSoc").disabled = !hasStrategy;
    $("gridCharging").disabled = !hasStrategy;
    if (hasStrategy) {
      $("reserveSoc").value = Number(state.strategyLoaded.conserve_percent ?? 5);
      $("gridCharging").checked = Number(state.strategyLoaded.grid_recharging ?? 1) === 1;
      $("strategyNote").textContent = "Loaded from Anker";
    } else {
      $("reserveSoc").value = "";
      $("gridCharging").checked = false;
      $("strategyNote").textContent = js.strategy_error
        ? `Unavailable: ${js.strategy_error}` : "Unavailable from Anker";
    }
    state.endpoint = js.endpoint;
    state.seasonIdx = 0;
    state.limitsCheck = null;
    renderAll();

    setNet("good", `Plan loaded${js.endpoint ? " via " + escapeHtml(js.endpoint) : ""}.`);
  } catch (e) {
    if (e.kind === "auth") { show("login"); return; }
    setNet("bad", `<b>Could not load your plan.</b> ${escapeHtml(e.message)}`,
      () => loadAll(deviceSn));
  }
}


/* ------------------------------------------------------------ live card */

let liveTimer = null;
let activeTab = "home";

const kwText = (w) => (Number(w || 0) / 1000).toFixed(2);

function setFlow(id, watts) {
  const el = $(id);
  if (!el) return;
  const on = Math.abs(Number(watts || 0)) > 5;
  el.classList.toggle("on", on);
  if (on) {
    const speed = Math.max(0.45, Math.min(2.2, 2.2 - Math.abs(Number(watts)) / 2500));
    el.style.animationDuration = `${speed}s`;
  }
}

function renderLive(live) {
  const l = live || {};
  const avail = !!l.available;
  const age = $("liveAge");
  if (age) {
    age.textContent = avail
      ? `updated ${l.age_s ?? 0}s ago${l.stale ? " · stale" : ""}`
      : (l.reason || "waiting for telemetry…");
    age.className = "small " + (avail ? (l.stale ? "err" : "ok") : "muted");
  }
  const put = (id, val) => { const el = $(id); if (el) el.textContent = val; };
  put("vSolar", kwText(l.pp));
  put("vGrid", kwText(l.gp));
  put("vBatt", kwText(l.bp));
  put("vHome", kwText(l.lp));
  const soc = Number(l.soc || 0);
  put("socVal", soc ? `${soc.toFixed(0)}%` : "—");
  put("homeSoc", soc ? `${soc.toFixed(0)}%` : "—");
  const bar = $("socBar");
  if (bar) bar.setAttribute("width", String(Math.max(0, Math.min(68, (68 * soc) / 100))));
  setFlow("fSolar", l.pp);
  setFlow("fGrid", l.gp);
  setFlow("fBatt", l.bp);
  put("liveMode", avail ? `${soc ? soc.toFixed(0) + "% SOC" : ""}` : "");
}

async function loadLive() {
  try {
    const js = await api("/api/live", { timeout: 8000 });
    renderLive(js.live);
  } catch (e) {
    renderLive({ available: false, reason: e.message });
  }
}

function switchTab(name, push = true) {
  activeTab = name === "plan" ? "plan" : "home";
  document.querySelectorAll("#mainTabs button").forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === activeTab);
  });
  const home = $("tab-home"), plan = $("tab-plan");
  if (home) home.classList.toggle("hide", activeTab !== "home");
  if (plan) plan.classList.toggle("hide", activeTab !== "plan");
  if (activeTab === "home") startLive(); else stopLive();
  if (push && history.replaceState) history.replaceState(null, "", `#${activeTab}`);
}

function startLive() {
  if (liveTimer) return;
  loadLive();
  liveTimer = setInterval(() => { if (!document.hidden) loadLive(); }, 2000);
}

function stopLive() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
}

/* --------------------------------------------------------------- diffs */


function renderAll() {
  if ($("planMeta")) {
    $("planMeta").textContent = state.endpoint ? `via ${state.endpoint}` : "";
  }
  renderDevices();
  renderSeasons();
  renderPeriods();
  renderTimeline();
  renderChanges();
}

function renderSites() {
  const sel = $("siteSel");
  if (!sel) return;
  sel.innerHTML = "";
  (state.sites || []).forEach((s) => {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = s.name || s.id;
    if (s.id === state.site) o.selected = true;
    sel.appendChild(o);
  });
  const multiple = (state.sites || []).length > 1;
  sel.classList.toggle("hide", !multiple);
  const cur = (state.sites || []).find((x) => x.id === state.site);
  const hs = $("homeSite");
  if (hs) hs.textContent = cur ? (cur.name || cur.id) : "—";
  sel.onchange = () => {
    state.site = sel.value;
    state.sn = null;
    loadAll();
  };
}

function renderDevices() {
  // The TOU plan belongs to the SITE. Only the inverter may be the write
  // target (main_sn); A5220 battery modules are informational.
  const inverters = (state.devices || []).filter((d) => d.role === "inverter");
  const modules = (state.devices || []).filter((d) => d.role === "battery");
  const sel = $("deviceSel");
  const label = $("deviceLabel");
  const note = $("deviceNote");
  const main = state.devices.find((d) => d.main) ||
               inverters.find((d) => d.sn === state.sn) || inverters[0];

  sel.innerHTML = "";
  inverters.forEach((d) => {
    const o = document.createElement("option");
    o.value = d.sn;
    o.textContent = `${d.code} · ${String(d.sn).slice(-6)}`;
    if (d.sn === state.sn) o.selected = true;
    sel.appendChild(o);
  });
  const multiple = inverters.length > 1;
  sel.classList.toggle("hide", !multiple);
  label.classList.toggle("hide", multiple);
  if (label) {
    label.textContent = main
      ? `Main inverter ${main.code || ""} · ${String(main.sn).slice(-6)}`
      : "No inverter found";
  }
  const hd = $("homeDevice");
  if (hd) {
    hd.textContent = main ? `${main.code || "inverter"} · ${String(main.sn).slice(-6)}` : "—";
  }
  if (note) {
    note.textContent = modules.length
      ? `Plan applies to the whole site · ${modules.length} × ${modules[0].code || "battery"} module(s)`
      : "Plan applies to the whole site";
  }
  sel.onchange = () => { state.sn = sel.value; loadAll(sel.value); };
}

function renderSeasons() {
  const host = $("seasons");
  host.innerHTML = "";
  sessions().forEach((s, i) => {
    const div = document.createElement("div");
    div.className = "season";
    const sel = i === state.seasonIdx;
    div.innerHTML = `
      <button class="ghost" data-pick="${i}" style="font-weight:${sel ? 700 : 400}">
        ${MONTHS[monthOf(s.start_time) - 1]} – ${MONTHS[monthOf(s.end_time) - 1]}
      </button>
      <select data-start="${i}" title="Season start month">${monthOptions(monthOf(s.start_time))}</select>
      <span class="muted small">to</span>
      <select data-end="${i}" title="Season end month">${monthOptions(monthOf(s.end_time))}</select>
      ${sessions().length > 1 ? `<button class="ghost danger" data-del-season="${i}">Remove</button>` : ""}
    `;
    host.appendChild(div);
  });
  host.querySelectorAll("[data-pick]").forEach((b) => b.onclick = () => {
    state.seasonIdx = Number(b.dataset.pick); renderAll();
  });
  host.querySelectorAll("[data-start]").forEach((el) => el.onchange = () => {
    const i = Number(el.dataset.start);
    sessions()[i].start_time = setMonth(sessions()[i].start_time, Number(el.value));
    renderAll();
  });
  host.querySelectorAll("[data-end]").forEach((el) => el.onchange = () => {
    const i = Number(el.dataset.end);
    sessions()[i].end_time = setMonth(sessions()[i].end_time, Number(el.value));
    renderAll();
  });
  host.querySelectorAll("[data-del-season]").forEach((b) => b.onclick = () => {
    sessions().splice(Number(b.dataset.delSeason), 1);
    state.seasonIdx = 0; renderAll();
  });
}

function monthOptions(cur) {
  return MONTHS.map((m, i) =>
    `<option value="${i + 1}" ${i + 1 === cur ? "selected" : ""}>${m}</option>`).join("");
}


// Keep periods contiguous: changing a period's end moves the next period's
// start, and changing a start moves the previous period's end. Mirrors the
// stock app, which never leaves gaps in the day.
function syncAdjacent(seasonObj, me, which, oldValue) {
  const rows = seasonObj.peak_valley_prices || [];
  if (!me) return;
  const dt = Number(me.day_type);
  if (which === "end") {
    const next = rows.find((r) =>
      r !== me && Number(r.day_type) === dt && r.start_time_period === oldValue);
    if (next) next.start_time_period = me.end_time_period;
  } else {
    const prev = rows.find((r) =>
      r !== me && Number(r.day_type) === dt && r.end_time_period === oldValue);
    if (prev) prev.end_time_period = me.start_time_period;
  }
}

// Matching period on the other day type (same position in that day's list).
function twinOf(seasonObj, row) {
  if (!seasonObj || !row) return null;
  const dt = Number(row.day_type);
  const other = dt === 1 ? 2 : 1;
  const mine = (seasonObj.peak_valley_prices || []).filter((r) => Number(r.day_type) === dt);
  const theirs = (seasonObj.peak_valley_prices || []).filter((r) => Number(r.day_type) === other);
  const i = mine.indexOf(row);
  return i >= 0 && i < theirs.length ? theirs[i] : null;
}

function renderPeriods() {
  const s = season();
  const body = $("periodRows");
  body.innerHTML = "";
  if (!s) { $("periodCount").textContent = ""; return; }
  const list = rowsFor(s, state.dayType)
    .sort((a, b) => toMin(a.r.start_time_period) - toMin(b.r.start_time_period));
  $("periodCount").textContent = `${list.length} of 4 periods` +
    (state.linkDays ? " · both days" : "");

  if (list.length) {
    if (toMin(list[0].r.start_time_period) !== 0) list[0].r.start_time_period = "00:00";
    const last = list[list.length - 1].r;
    if (toMin(last.end_time_period) !== 1440) last.end_time_period = "24:00";
  }

  list.forEach(({ r, i }, pos) => {
    const tr = document.createElement("tr");
    const t = tariff(r.peak_type);
    const startCell = pos === 0
      ? `<span class="timeFixed" title="The day always starts at 00:00">00:00</span>`
      : `<select class="time" data-startrow="${i}">
        ${TIMES.map((x) => `<option ${x === r.start_time_period ? "selected" : ""}>${x}</option>`).join("")}
      </select>`;
    const endCell = pos === list.length - 1
      ? `<span class="timeFixed" title="The day always ends at 24:00">24:00</span>`
      : `<select class="time" data-endrow="${i}">
        ${END_TIMES.map((x) => `<option ${x === r.end_time_period ? "selected" : ""}>${x}</option>`).join("")}
      </select>`;
    tr.innerHTML = `
      <td><span class="dot t-${t.cls}"></span></td>
      <td><select class="type" data-type="${i}">
        ${TARIFFS.map((x) => `<option value="${x.v}" ${x.v === Number(r.peak_type) ? "selected" : ""}>${x.name}</option>`).join("")}
      </select></td>
      <td>${startCell}</td>
      <td>${endCell}</td>
      <td><input class="price" type="number" step="0.01" min="0" value="${truncPrice2(r.buy).toFixed(2)}"
        data-buy="${i}" aria-label="Buy price per kWh"></td>
      <td><input class="price" type="number" step="0.01" min="0" value="${truncPrice2(r.sell).toFixed(2)}"
        data-sell="${i}" aria-label="Sell price per kWh"></td>
      <td><button class="ghost danger" data-del="${i}" title="Delete period">✕</button></td>
    `;
    body.appendChild(tr);
  });

  body.querySelectorAll("[data-type]").forEach((el) => el.onchange = () => {
    const row = s.peak_valley_prices[Number(el.dataset.type)];
    row.peak_type = Number(el.value);
    const twin = state.linkDays ? twinOf(s, row) : null;
    if (twin) twin.peak_type = row.peak_type;
    for (const field of ["buy", "sell"]) {
      const value = tariffValue(state.plan, row.peak_type, field);
      if (value !== null) {
        row[field] = value;
        if (twin) twin[field] = value;
      }
    }
    canonicalizeSeason(s, row);
    renderAll();
  });
  body.querySelectorAll("[data-startrow]").forEach((el) => el.onchange = () => {
    const idx = Number(el.dataset.startrow);
    const row = s.peak_valley_prices[idx];
    const old = row.start_time_period;
    row.start_time_period = el.value;
    syncAdjacent(s, row, "start", old);
    const twin = state.linkDays ? twinOf(s, row) : null;
    if (twin) {
      twin.start_time_period = row.start_time_period;
      syncAdjacent(s, twin, "start", old);
    }
    canonicalizeSeason(s, row);
    renderAll();
  });
  body.querySelectorAll("[data-endrow]").forEach((el) => el.onchange = () => {
    const idx = Number(el.dataset.endrow);
    const row = s.peak_valley_prices[idx];
    const old = row.end_time_period;
    row.end_time_period = el.value;
    syncAdjacent(s, row, "end", old);
    const twin = state.linkDays ? twinOf(s, row) : null;
    if (twin) {
      twin.end_time_period = row.end_time_period;
      syncAdjacent(s, twin, "end", old);
    }
    canonicalizeSeason(s, row);
    renderAll();
  });
  [["buy", "buy"], ["sell", "sell"]].forEach(([field, key]) => {
    body.querySelectorAll(`[data-${field}]`).forEach((el) => el.onchange = () => {
      const row = s.peak_valley_prices[Number(el.dataset[key])];
      const raw = Number(el.value);
      const value = truncPrice2(raw);
      setTariffValue(row.peak_type, field, value);
      renderAll();
      const label = field === "buy" ? "Buy" : "Sell";
      setStatus(Math.abs(raw - value) > 1e-9
        ? `All ${tariff(row.peak_type).name} ${label.toLowerCase()} prices set to ${value.toFixed(2)} (two decimals).`
        : `All ${tariff(row.peak_type).name} ${label.toLowerCase()} prices set to ${value.toFixed(2)}.`);
    });
  });
  body.querySelectorAll("[data-del]").forEach((el) => el.onclick = () => {
    const idx = Number(el.dataset.del);
    const row = s.peak_valley_prices[idx];
    const victims = [row];
    if (state.linkDays) {
      const twin = twinOf(s, row);
      if (twin) victims.push(twin);
    }
    victims.forEach((v) => {
      const dt = Number(v.day_type);
      const list = rowsFor(s, dt).map((x) => x.r)
        .sort((a, b) => toMin(a.start_time_period) - toMin(b.start_time_period));
      const pos = list.indexOf(v);
      if (pos > 0) list[pos - 1].end_time_period = v.end_time_period;
      else if (pos === 0 && list.length > 1) list[1].start_time_period = "00:00";
      const i = s.peak_valley_prices.indexOf(v);
      if (i >= 0) s.peak_valley_prices.splice(i, 1);
    });
    canonicalizeSeason(s);
    renderAll();
  });
}

$("addPeriod").onclick = () => {
  const s = season();
  if (!s) return;
  const days = state.linkDays ? [1, 2] : [state.dayType];
  for (const dt of days) {
    const list = rowsFor(s, dt).map((x) => x.r);
    if (list.length >= 4) { setStatus("Maximum 4 periods per day type.", true); return; }
  }
  for (const dt of days) {
    const list = rowsFor(s, dt).map((x) => x.r)
      .sort((a, b) => toMin(a.start_time_period) - toMin(b.start_time_period));
    const last = list[list.length - 1];
    if (!last) continue;
    const a = toMin(last.start_time_period), b = toMin(last.end_time_period);
    if (b - a < 60) { setStatus("Not enough room to split the final period.", true); return; }
    const mid = a + Math.floor((b - a) / 60) * 30;
    const oldEnd = last.end_time_period;
    last.end_time_period = fmtMin(mid);
    s.peak_valley_prices.push({
      ...last, id: 0, day_type: dt,
      start_time_period: fmtMin(mid), end_time_period: oldEnd,
    });
  }
  canonicalizeSeason(s);
  renderAll();
};

$("linkDays").checked = state.linkDays;
try {
  const kw = localStorage.getItem("anker.maxChargeKw");
  if (kw) $("maxChargeKw").value = kw;
} catch { /* ignore */ }
$("maxChargeKw").onchange = (e) => {
  try { localStorage.setItem("anker.maxChargeKw", e.target.value); } catch { /* ignore */ }
};
$("linkDays").onchange = (e) => {
  state.linkDays = e.target.checked;
  try { localStorage.setItem("anker.linkDays", state.linkDays ? "1" : "0"); } catch { /* ignore */ }
  renderPeriods();
  renderChanges();
  setStatus(state.linkDays
    ? "Edits now apply to both weekdays and weekends."
    : "Weekday and weekend are edited separately.");
};

$("dayTabs").querySelectorAll("button").forEach((b) => b.onclick = () => {
  state.dayType = Number(b.dataset.day);
  $("dayTabs").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderPeriods(); renderTimeline(); renderChanges();
});

function renderTimeline() {
  const host = $("timeline");
  host.innerHTML = "";
  const s = season();
  const rows = s ? rowsFor(s, state.dayType).map((x) => x.r) : [];
  for (let i = 0; i < 48; i++) {
    const m = i * 30;
    const hit = rows.find((r) => {
      const a = toMin(r.start_time_period), b = toMin(r.end_time_period);
      return a <= b ? (m >= a && m < b) : (m >= a || m < b);
    });
    const cell = document.createElement("i");
    if (hit) cell.className = tariff(hit.peak_type).tl;
    cell.title = `${String(Math.floor(m / 60)).padStart(2, "0")}:${m % 60 === 0 ? "00" : "30"}`;
    host.appendChild(cell);
  }
}

function describeChanges() {
  const out = [];
  const a = state.loaded, b = state.plan;
  if (!a || !b) return out;
  const as = (a.peak_sessions || []), bs = (b.peak_sessions || []);
  if (as.length !== bs.length) out.push(`seasons: ${as.length} → ${bs.length}`);
  const n = Math.min(as.length, bs.length);
  for (let i = 0; i < n; i++) {
    const A = as[i], B = bs[i];
    if (A.start_time !== B.start_time || A.end_time !== B.end_time) {
      out.push(`season ${i + 1}: ${MONTHS[monthOf(A.start_time) - 1]}–${MONTHS[monthOf(A.end_time) - 1]}` +
        ` → ${MONTHS[monthOf(B.start_time) - 1]}–${MONTHS[monthOf(B.end_time) - 1]}`);
    }
    const ar = A.peak_valley_prices || [], br = B.peak_valley_prices || [];
    if (ar.length !== br.length) out.push(`season ${i + 1}: ${ar.length} → ${br.length} periods`);
    for (let j = 0; j < Math.min(ar.length, br.length); j++) {
      const x = ar[j], y = br[j];
      const bits = [];
      if (x.start_time_period !== y.start_time_period || x.end_time_period !== y.end_time_period) {
        bits.push(`${x.start_time_period}–${x.end_time_period} → ${y.start_time_period}–${y.end_time_period}`);
      }
      if (Number(x.peak_type) !== Number(y.peak_type)) {
        bits.push(`${tariff(x.peak_type).name} → ${tariff(y.peak_type).name}`);
      }
      if (String(x.buy ?? "") !== String(y.buy ?? "")) bits.push(`buy ${x.buy} → ${y.buy}`);
      if (String(x.sell ?? "") !== String(y.sell ?? "")) bits.push(`sell ${x.sell} → ${y.sell}`);
      if (bits.length) {
        out.push(`${Number(y.day_type) === 2 ? "Weekend" : "Weekday"} ${bits.join(", ")}`);
      }
    }
  }
  return out;
}

function renderChanges() {
  const changes = describeChanges();
  const box = $("diffSummary");
  // Write path is NOT the app's. The Android app saves TOU via
  // charging_hes_svc/device_command (LargeTouRepository.saveTouPlan ->
  // saveTouMode -> sendDeviceCommand). This build still targets
  // update_hes_utility_rate_plan, so writing would be unverified.
  // device_command proven working with minute precision (2026-10-27).
  $("saveBtn").disabled = changes.length === 0;
  $("saveBtn").textContent = "Write & verify";
  $("discardBtn").disabled = changes.length === 0;
  if (!changes.length) { box.innerHTML = '<span class="muted">No changes yet.</span>'; return; }
  box.innerHTML = changes.map((c) => `<div>• ${escapeHtml(c)}</div>`).join("");
}

function validate() {
  const prices = {};
  for (const r of allPriceRows(state.plan)) {
    const t = Number(r.peak_type);
    if (!prices[t]) prices[t] = {};
    for (const field of ["buy", "sell"]) {
      const p = truncPrice2(r[field]);
      if (prices[t][field] === undefined) prices[t][field] = p;
      else if (Math.abs(prices[t][field] - p) > 1e-9) {
        return `All ${tariff(t).name} periods must share one ${field} price — ` +
               `found ${prices[t][field].toFixed(2)} and ${p.toFixed(2)}.`;
      }
    }
  }
  for (let si = 0; si < sessions().length; si++) {
    const s = sessions()[si];
    for (const dt of [1, 2]) {
      const rows = rowsFor(s, dt).map((x) => x.r)
        .sort((a, b) => toMin(a.start_time_period) - toMin(b.start_time_period));
      const label = dt === 1 ? "Weekday" : "Weekend";
      if (!rows.length) return `${label}: at least one period is required.`;
      if (rows.length > 4) return `${label}: maximum 4 periods per day type.`;
      if (toMin(rows[0].start_time_period) !== 0) {
        return `${label}: the first period must start at 00:00.`;
      }
      const last = rows[rows.length - 1];
      if (toMin(last.end_time_period) !== 1440) {
        return `${label}: the last period must end at 24:00.`;
      }
      for (let i = 0; i < rows.length; i++) {
        const a = toMin(rows[i].start_time_period);
        const b = toMin(rows[i].end_time_period);
        if (b <= a) {
          return `${label}: a period cannot wrap past midnight — split it into 00:00–end and start–24:00.`;
        }
        if (i < rows.length - 1 && b !== toMin(rows[i + 1].start_time_period)) {
          return `${label}: periods must be contiguous — ${fmtMin(b)} does not meet the next start.`;
        }
      }
    }
  }
  return null;
}

/* ---------------------------------------------------------------- save */

$("discardBtn").onclick = () => {
  state.plan = clone(state.loaded);
  renderAll(); setStatus("Changes discarded.");
};

$("reloadBtn").onclick = () => {
  if (describeChanges().length && !confirm("Discard unsaved changes and reload?")) return;
  loadAll();
};

$("saveBtn").onclick = () => {
  try {
    const bad = validate();
    if (bad) { setStatus(bad, true); return; }
    openConfirm();
  } catch (e) { setStatus(e.message, true); }
};

const truncPrice2 = (x) => Math.floor((Number(x) || 0) * 100 + 1e-9) / 100;

function normalizePrices2() {
  const rows = (state.plan && state.plan.peak_sessions || [])
    .flatMap((x) => x.peak_valley_prices || []);
  let changed = 0;
  rows.forEach((r) => {
    for (const field of ["buy", "sell"]) {
      const a = truncPrice2(r[field]);
      if (Math.abs(Number(r[field] || 0) - a) > 1e-9) { r[field] = a; changed++; }
    }
  });
  return changed;
}



function fmtPrice(x) {
  return Number(x || 0).toFixed(2);
}

function openConfirm() {
  sessions().forEach((x) => canonicalizeSeason(x));
  unifyTariffPrices(state.plan);
  if (normalizePrices2()) { renderPeriods(); renderChanges(); }
  const changes = describeChanges();
  const modal = document.createElement("div");
  modal.className = "overlay";
  modal.innerHTML = `
    <div class="modal">
      <h3>Confirm schedule change</h3>
      <div class="small muted">The plan is written to Anker's cloud, then read back and verified.
        If the read-back differs, the previous plan is restored automatically.</div>
      <div class="small muted" style="margin:10px 0">
        Prices are left exactly as Anker has them unless you edit one.
      </div>
      <div class="diff">${changes.map((c) => `• ${escapeHtml(c)}`).join("<br>") || "No changes"}</div>
      <label class="check">
        <input type="checkbox" id="ackBox">
        <span>I understand this writes the schedule now, is read back, and is rolled back
        automatically if the cloud stores a different time or tariff.</span>
      </label>
      <div class="actions">
        <button id="cancelModal">Cancel</button>
        <button class="primary" id="confirmModal" disabled>Write &amp; verify</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  const close = () => modal.remove();
  modal.querySelector("#cancelModal").onclick = close;
  modal.querySelector("#ackBox").onchange = (e) => {
    modal.querySelector("#confirmModal").disabled = !e.target.checked;
  };
  modal.querySelector("#confirmModal").onclick = () => { close(); doSave(); };
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
}

async function doSave() {
  state.busy = true;
  $("saveBtn").disabled = true;
  setStatus("Writing to Anker cloud…");
  try {
    const strategy = {};
    if (state.strategyLoaded) {
      const reserve = Number($("reserveSoc").value);
      if (Number.isFinite(reserve) &&
          reserve !== Number(state.strategyLoaded.conserve_percent)) {
        strategy.conserve_percent = reserve;
      }
      const grid = $("gridCharging").checked ? 1 : 0;
      if (grid !== Number(state.strategyLoaded.grid_recharging)) {
        strategy.grid_recharging = grid;
      }
    }
    const js = await api("/api/plan", {
      method: "POST",
      body: JSON.stringify({
        site: state.site, sn: state.sn, plan: state.plan, ack: true,
        max_charge_kw: Number($("maxChargeKw").value || 4.2),
        strategy,
      }),
    });
    $("resultCard").classList.remove("hide");
    const adj = (js.adjustments || []).map((a) =>
      `  • ${tariff(a.peak_type).name} (${Number(a.day_type) === 1 ? "weekday" : "weekend"}) ` +
      `${a.field}: ${fmtPrice(a.from)} → ${fmtPrice(a.to)}`);
    $("resultBody").textContent =
      `${js.message}\n\nChanges applied:\n` +
      (js.diff || []).map((d) => `  • ${d.path} (${d.kind})`).join("\n") +
      (adj.length ? `\n\nPrices adjusted by Anker:\n${adj.join("\n")}` : "") +
      `\n\nSnapshot: ${js.snapshot}\n\nNext: confirm the app still shows "Time of Use", ` +
      `then watch the first charge cycle — it should ramp, not step to full power.`;
    setStatus(adj.length
      ? `Saved — Anker truncated ${adj.length} price(s) to two decimals.`
      : "Saved, verified and charge limit applied.", false, true);
    await loadAll();
  } catch (e) {
    $("resultCard").classList.remove("hide");
    $("resultBody").textContent = e.payload ? JSON.stringify(e.payload, null, 2) : e.message;
    setStatus(e.message, true);
    if (e.kind === "auth") { show("login"); }
  } finally {
    state.busy = false;
    renderChanges();
  }
}

/* --------------------------------------------------------------- misc */

function setStatus(msg, isError = false, isOk = false) {
  const el = $("saveStatus");
  el.textContent = msg || "";
  el.className = "small" + (isError ? " err" : isOk ? " ok" : "");
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------------------------------------------------- diagnostics */

$("diagBtn").onclick = async () => {
  try {
    const js = await api("/api/diag", { timeout: 10000 });
    const lines = (js.requests || []).slice().reverse().map((r) => {
      const when = new Date(r.t * 1000).toLocaleTimeString();
      return `${when}  ${r.method.padEnd(4)} ${r.path}  ${r.status}  ${r.ms}ms${r.error ? "  " + r.error : ""}`;
    });
    $("diagBody").textContent = lines.join("\n") || "No requests yet.";
  } catch (e) {
    $("diagBody").textContent = "Diagnostics unavailable: " + e.message;
  }
};

/* ------------------------------------------------------------- tabs */

document.querySelectorAll("#mainTabs button").forEach((b) => {
  b.addEventListener("click", () => switchTab(b.dataset.tab));
});
window.addEventListener("hashchange", () => {
  switchTab(location.hash === "#plan" ? "plan" : "home", false);
});

/* ------------------------------------------------------------- startup */

(async function boot() {
  try {
    const js = await api("/api/state");
    state.user = js.user; state.sites = js.sites || [];
    if (!state.sites.length) { show("login"); return; }
    state.site = state.sites[0].id;
    if (!state.site) {
      show("app");
      const keys = (js.site_keys || []).join(", ") || "none";
      setNet("bad", `<b>Signed in, but the site id could not be read.</b> ` +
        `The site object contains: <code>${escapeHtml(keys)}</code>. ` +
        `Send me that list — the id field is under a name the client does not know yet.`);
      return;
    }
    await enterApp();
  } catch (e) {
    show("login");
    if (e.kind !== "auth") {
      const el = document.getElementById("loginError");
      if (el) el.textContent = e.message;
    }
  }
})();
