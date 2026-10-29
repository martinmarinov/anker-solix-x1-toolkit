/* Anker X1 — wall-display dashboard + plan editor. Vanilla JS, no build step. */

const $ = (id) => document.getElementById(id);

// ORDER MATTERS. In the X1 `peak_valley_prices` schema the numeric types run
// cheapest -> most expensive: 1 = Super Off-Peak, 4 = Peak.
const TARIFFS = [
  { v: 1, name: "Super Off-Peak", cls: "valley", tl: "tl-valley" },
  { v: 2, name: "Off-Peak", cls: "off", tl: "tl-off" },
  { v: 3, name: "Mid-Peak", cls: "mid", tl: "tl-mid" },
  { v: 4, name: "Peak", cls: "peak", tl: "tl-peak" },
];
const tariff = (v) => TARIFFS.find((t) => t.v === Number(v)) || TARIFFS[0];

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
  "Oct", "Nov", "Dec"];

// The D156/X1 device quantises boundaries to whole hours even when the cloud
// stores :30. The editor therefore works in 24 one-hour slots.
const TIMES = [];
for (let h = 0; h < 24; h++) TIMES.push(`${String(h).padStart(2, "0")}:00`);
const END_TIMES = TIMES.slice(1).concat(["24:00"]);

const state = {
  user: null, sites: [], site: null, sn: null,
  plan: null, loaded: null, seasonIdx: 0, dayType: 2,
  limits: null, limitsCheck: null, devices: [], busy: false,
  devicesLoaded: false, planLoaded: false, planLoading: false,
  liveTimer: null, drawerOpen: false, siteTimezone: "",
  mqttInterval: 5, mqttSaved: 5, mqttActive: 5, mqttPending: null,
  mqttBounds: { min: 5, max: 60, step: 5, default: 5 },
  refreshEditing: false, refreshSaving: false, refreshNote: "",
  refreshNoteKind: "", refreshTimer: null, liveSeq: 0, liveFailures: 0,
  liveInFlight: false,
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
  if (!el) return;
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
  if (!el) return;
  el.className = "banner " + kind;
  el.innerHTML = html;
  if (retry) {
    const b = document.createElement("button");
    b.textContent = "Retry";
    b.type = "button";
    b.style.marginLeft = "10px";
    b.onclick = retry;
    el.appendChild(b);
  }
}

function siteMeta() {
  return state.sites.find((s) => s.id === state.site) || {};
}
function applySiteTimezone() {
  const tz = siteMeta().timezone || "";
  state.siteTimezone = tz;
  HomeView.setTimezone(tz);
}

// HES enum, verified by the anker-solix-api live probe and the app model:
// day_type 2 = Weekday, day_type 1 = Weekend.
const dayName = (dt) => Number(dt) === 2 ? "Weekday" : "Weekend";

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
  return `${String(h).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

// Make one day type cover 00:00-24:00 exactly. Wrap-around rows are split,
// overlaps are resolved in favour of the edited row, and adjacent slots with
// the same tariff/price are merged back into periods.
function canonicalizeDay(seasonObj, dayType) {
  const rows = (seasonObj.peak_valley_prices || [])
    .filter((r) => Number(r.day_type) === dayType);
  if (!rows.length) return { gaps: 24, count: 0 };
  const slots = new Array(24).fill(null);
  rows.forEach((r) => {
    // Floor to the hour: the device treated 23:30 as 23:00 and 05:30 as 05:00.
    const a = Math.floor(toMin(r.start_time_period) / 60);
    const b = Math.floor(toMin(r.end_time_period) / 60);
    if (a === b) return;
    if (b > a) {
      for (let h = a; h < b; h++) slots[h] = r;
    } else {
      for (let h = a; h < 24; h++) slots[h] = r;
      for (let h = 0; h < b; h++) slots[h] = r;
    }
  });
  const gaps = slots.filter((x) => !x).length;
  if (gaps) return { gaps, count: rows.length };

  const out = [];
  let cur = null;
  for (let i = 0; i < 24; i++) {
    const r = slots[i];
    const key = `${r.peak_type}|${r.buy}|${r.sell}|${r.energy_unit}`;
    if (cur && cur.key === key) {
      cur.end = (i + 1) * 60;
    } else {
      cur = { key, start: i * 60, end: (i + 1) * 60, src: r };
      out.push(cur);
    }
  }
  const others = (seasonObj.peak_valley_prices || [])
    .filter((r) => Number(r.day_type) !== dayType);
  const rebuilt = out.map((seg) => {
    const srcStart = toMin(seg.src.start_time_period);
    const srcEnd = toMin(seg.src.end_time_period);
    const unchanged = srcStart === seg.start &&
      (srcEnd === seg.end || (srcEnd === 0 && seg.end === 1440));
    return {
      ...seg.src, id: unchanged ? (Number(seg.src.id) || 0) : 0, day_type: dayType,
      start_time_period: fmtMin(seg.start),
      end_time_period: seg.end === 1440 ? "24:00" : fmtMin(seg.end),
    };
  });
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
  const app = view === "app";
  $("loginView").classList.toggle("hide", view !== "login");
  $("appView").classList.toggle("hide", !app);
  if (app) {
    HomeView.init();
    HomeView.startClock();
  } else {
    stopLive();
    HomeView.stopClock();
    closeDrawer(false, false);
  }
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
    $("password").value = "";
    state.user = js.user;
    state.sites = js.sites || [];
    if (!state.sites.length) throw new Error("No sites on this account.");
    state.mqttInterval = Number(js.mqtt_interval_s) || 5;
    state.mqttSaved = state.mqttInterval;
    state.mqttActive = state.mqttInterval;
    state.mqttPending = null;
    state.mqttBounds = js.mqtt_bounds || state.mqttBounds;
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
  HomeView.setAlert("");
  $("menuBtn").classList.remove("has-error");
  applySiteTimezone();
  renderSites();
  renderRefresh();
  const s = siteMeta();
  $("who").textContent = state.user
    ? `${state.user.nick || ""}${s && s.name ? " · " + s.name : ""}`
    : "";
  $("netBanner").className = "banner info";
  $("netBanner").textContent = "Connecting to your inverter…";

  const ok = await loadDevices();
  if (!ok) return;
  startLive();

  if (location.hash === "#plan") {
    openDrawer(true);
  } else {
    // The dashboard must paint before the plan request. Load it in the
    // background so opening the drawer later is instant.
    setTimeout(() => {
      if (!state.planLoaded && !state.planLoading && !document.hidden) loadPlan();
    }, 1400);
  }
}

/* ---------------------------------------------------------- data loading */

function pickDevice(list) {
  const want = list.find((d) => String(d.code || "").match(/^(A17B1|A5101|A5102|A5103)/));
  return (want || list[0] || {}).sn;
}

async function loadDevices(deviceSn) {
  if (!state.site) return false;
  startPhase("Loading devices…");
  try {
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
        (tried || "no shapes recorded") + ".");
    }
    state.sn = deviceSn || state.sn || pickDevice(state.devices);
    state.devicesLoaded = true;
    HomeView.setAlert("");
    renderDevices();
    setNet("good", `Live telemetry target ready${state.sn ? " · " + String(state.sn).slice(-6) : ""}.`);
    return true;
  } catch (e) {
    state.devicesLoaded = false;
    state.devices = [];
    state.sn = null;
    renderDevices();
    HomeView.setAlert("Could not load your inverter. " + e.message);
    setNet("bad", `<b>Could not load devices.</b> ${escapeHtml(e.message)}`,
      () => loadAll(deviceSn));
    if (e.kind === "auth") show("login");
    return false;
  }
}

function resetStrategyFields() {
  const reserve = $("reserveSoc");
  const grid = $("gridCharging");
  if (reserve) { reserve.value = ""; reserve.disabled = true; }
  if (grid) { grid.checked = false; grid.disabled = true; }
  const note = $("strategyNote");
  if (note) note.textContent = "Load a plan to read this.";
}

async function loadPlan() {
  if (state.planLoading) return false;
  if (!state.site) return false;
  if (!state.sn) {
    const ok = await loadDevices();
    if (!ok) return false;
  }
  state.planLoading = true;
  resetStrategyFields();
  try {
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
    state.planLoaded = true;
    renderAll();
    $("menuBtn").classList.remove("has-error");
    setNet("good", `Plan loaded${js.endpoint ? " via " + escapeHtml(js.endpoint) : ""}.`);
    return true;
  } catch (e) {
    state.planLoaded = false;
    state.endpoint = null;
    $("menuBtn").classList.add("has-error");
    setNet("bad", `<b>Could not load your plan.</b> ${escapeHtml(e.message)}`,
      () => loadPlan());
    if (e.kind === "auth") show("login");
    return false;
  } finally {
    state.planLoading = false;
  }
}

async function loadAll(deviceSn) {
  const ok = await loadDevices(deviceSn);
  if (ok) await loadPlan();
}

async function ensurePlan() {
  if (state.planLoaded || state.planLoading) return;
  if (!state.devicesLoaded) {
    const ok = await loadDevices();
    if (!ok) return;
    startLive();
  }
  await loadPlan();
}

/* ------------------------------------------------------------ live data */

function fmtInterval(value) {
  const n = Number(value);
  if (!isFinite(n)) return "—";
  if (n >= 60) return n % 60 === 0 ? `${n / 60} min` : `${n} s`;
  return `${n} s`;
}

function setRefreshNote(text, kind) {
  state.refreshNote = text || "";
  state.refreshNoteKind = kind || "";
  if (state.refreshTimer) clearTimeout(state.refreshTimer);
  if (text) {
    state.refreshTimer = setTimeout(() => {
      state.refreshNote = "";
      state.refreshNoteKind = "";
      renderRefresh();
    }, 4000);
  }
}

function renderRefresh() {
  const range = $("refreshRange");
  if (!range) return;
  const bounds = state.mqttBounds || {};
  if (bounds.min !== undefined) range.min = String(bounds.min);
  if (bounds.max !== undefined) range.max = String(bounds.max);
  if (bounds.step !== undefined) range.step = String(bounds.step);
  if (!state.refreshEditing) range.value = String(state.mqttInterval);
  range.disabled = !!state.refreshSaving;

  const selected = Number(state.mqttInterval);
  const value = $("refreshValue");
  if (value) value.textContent = fmtInterval(selected);
  range.setAttribute("aria-valuetext", `${fmtInterval(selected)} refresh`);

  const meta = $("refreshMeta");
  if (meta) {
    if (state.mqttPending) {
      meta.textContent =
        `Every ${fmtInterval(state.mqttActive)} now · ` +
        `${fmtInterval(state.mqttPending)} after the current refresh`;
    } else {
      meta.textContent = `Every ${fmtInterval(state.mqttActive || selected)}`;
    }
  }

  const status = $("refreshStatus");
  if (status) {
    status.textContent = state.refreshSaving ? "Saving…"
      : (state.refreshNote ||
         (state.mqttPending ? "Applies after the current refresh" : ""));
    const kind = state.refreshSaving ? ""
      : (state.refreshNoteKind || (state.mqttPending ? "saved" : ""));
    status.className = "refresh-status small" +
      (kind === "error" ? " is-error" : kind === "saved" ? " is-saved" : "");
  }
}

async function saveRefresh() {
  if (!state.site) return;
  const requested = Number(state.mqttInterval);
  state.refreshSaving = true;
  state.refreshNote = "";
  state.refreshNoteKind = "";
  renderRefresh();
  try {
    const js = await api("/api/live/interval", {
      method: "POST",
      body: JSON.stringify({ interval_s: requested }),
      timeout: 12000,
    });
    state.mqttSaved = js.mqtt_interval_s;
    state.mqttInterval = js.mqtt_interval_s;
    state.mqttBounds = js.mqtt_bounds || state.mqttBounds;
    state.mqttPending = js.pending_interval_s || null;
    const first = (js.feeds || [])[0];
    state.mqttActive = first ? first.interval_s : js.mqtt_interval_s;
    state.refreshSaving = false;
    state.refreshEditing = false;
    setRefreshNote(js.pending_interval_s
      ? "Applies after the current refresh" : "Saved", "saved");
    renderRefresh();
    loadLive();
  } catch (e) {
    state.refreshSaving = false;
    state.refreshEditing = false;
    state.mqttInterval = state.mqttSaved;
    setRefreshNote(e.message || "Could not save the refresh setting.", "error");
    renderRefresh();
  }
}

function startLive() {
  stopLive();
  if (!state.site) return;
  loadLive();
  state.liveTimer = setInterval(() => {
    if (!document.hidden) loadLive();
  }, 2000);
}

function stopLive() {
  if (state.liveTimer) { clearInterval(state.liveTimer); state.liveTimer = null; }
  state.liveSeq += 1; // drop any response still in flight
  state.liveFailures = 0;
  state.liveInFlight = false;
}

async function loadLive() {
  if (!state.site || state.liveInFlight) return;
  const site = state.site;
  const sn = state.sn;
  const seq = ++state.liveSeq;
  state.liveInFlight = true;
  const q = new URLSearchParams({ site });
  if (sn) q.set("sn", sn);
  try {
    const js = await api(`/api/live?${q}`, { timeout: 8000 });
    if (seq !== state.liveSeq) return; // a newer poll has already started
    if (site !== state.site || (sn && sn !== state.sn)) return; // stale response
    state.liveFailures = 0;
    if (js.timezone && js.timezone !== state.siteTimezone) {
      state.siteTimezone = js.timezone;
      HomeView.setTimezone(js.timezone);
    }
    if (js.mqtt_bounds) state.mqttBounds = js.mqtt_bounds;
    const active = js.live_stats && js.live_stats.interval_s;
    const pending = js.live_stats && js.live_stats.pending_interval_s;
    if (!state.refreshEditing && !state.refreshSaving) {
      state.mqttSaved = Number(js.mqtt_interval_s) || state.mqttSaved;
      state.mqttInterval = state.mqttSaved;
      state.mqttActive = Number(active) || state.mqttSaved;
      state.mqttPending = pending || null;
      renderRefresh();
    }
    if (js.view) HomeView.render(js.view);
  } catch (e) {
    if (seq !== state.liveSeq) return;
    if (e.kind === "auth") { stopLive(); show("login"); return; }
    if (site !== state.site) return;
    state.liveFailures += 1;
    // One dropped local poll is not an offline feed; keep the last view.
    if (state.liveFailures < 2) return;
    HomeView.renderUnavailable(e.message);
  } finally {
    if (seq === state.liveSeq) state.liveInFlight = false;
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.site && !$("appView").classList.contains("hide")) loadLive();
});

if ($("refreshRange")) {
  $("refreshRange").addEventListener("pointerdown", () => {
    state.refreshEditing = true;
  });
  $("refreshRange").addEventListener("input", (e) => {
    if (state.refreshSaving) return;
    state.refreshEditing = true;
    state.mqttInterval = Number(e.target.value);
    renderRefresh();
  });
  $("refreshRange").addEventListener("change", (e) => {
    if (state.refreshSaving) return;
    state.mqttInterval = Number(e.target.value);
    saveRefresh();
  });
}

/* --------------------------------------------------------- home rendering */

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
  sel.onchange = async () => {
    state.site = sel.value;
    state.sn = null;
    state.plan = null;
    state.loaded = null;
    state.endpoint = null;
    state.strategyLoaded = null;
    state.planLoaded = false;
    state.devicesLoaded = false;
    state.devices = [];
    state.seasonIdx = 0;
    applySiteTimezone();
    const s = siteMeta();
    $("who").textContent = state.user
      ? `${state.user.nick || ""}${s && s.name ? " · " + s.name : ""}` : "";
    stopLive();
    renderAll();
    resetStrategyFields();
    const ok = await loadDevices();
    if (!ok) return;
    startLive();
    loadPlan();
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

  if (!sel || !label || !note) return;
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
  label.textContent = main
    ? `Main inverter ${main.code || ""} · ${String(main.sn).slice(-6)}`
    : "No inverter found";
  HomeView.setDevice(main ? `${main.code || "inverter"} · ${String(main.sn).slice(-6)}` : "");
  note.textContent = modules.length
    ? `Plan applies to the whole site · ${modules.length} × ${modules[0].code || "battery"} module(s)`
    : "Plan applies to the whole site";
  sel.onchange = async () => {
    state.sn = sel.value;
    state.plan = null;
    state.loaded = null;
    state.endpoint = null;
    state.strategyLoaded = null;
    state.planLoaded = false;
    state.seasonIdx = 0;
    stopLive();
    renderAll();
    resetStrategyFields();
    startLive();
    loadPlan();
  };
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
    if (b - a < 120) { setStatus("Not enough room to split the final period — minimum is one hour per side.", true); return; }
    const mid = a + Math.max(60, Math.floor((b - a) / 120) * 60);
    const oldEnd = last.end_time_period;
    last.end_time_period = fmtMin(mid);
    // Splitting the last period in place would immediately merge back in
    // canonicalizeDay (same tariff + price). Give the new slice a tariff that
    // is unused that day so it is a real, editable period.
    const used = new Set(rowsFor(s, dt).map((x) => Number(x.r.peak_type)));
    const newType = [2, 3, 4, 1].find((t) => !used.has(t)) || 2;
    const row = {
      ...last, id: 0, day_type: dt, peak_type: newType,
      start_time_period: fmtMin(mid), end_time_period: oldEnd,
    };
    const buy = tariffValue(state.plan, newType, "buy");
    const sell = tariffValue(state.plan, newType, "sell");
    if (buy !== null) row.buy = buy;
    if (sell !== null) row.sell = sell;
    s.peak_valley_prices.push(row);
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
  for (let i = 0; i < 24; i++) {
    const m = i * 60;
    const hit = rows.find((r) => {
      const a = toMin(r.start_time_period), b = toMin(r.end_time_period);
      return a <= b ? (m >= a && m < b) : (m >= a || m < b);
    });
    const cell = document.createElement("i");
    if (hit) cell.className = tariff(hit.peak_type).tl;
    cell.title = `${String(Math.floor(m / 60)).padStart(2, "0")}:00`;
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
        out.push(`${dayName(y.day_type)} ${bits.join(", ")}`);
      }
    }
  }
  return out;
}

function renderChanges() {
  const changes = describeChanges();
  const box = $("diffSummary");
  // The Android app saves TOU via charging_hes_svc/device_command. That call
  // stores exact strings, but the D156 device applies whole hours (23:30→23:00,
  // 05:30→05:00). The editor therefore only emits hourly boundaries and
  // verifies every write by reading it back.
  $("saveBtn").disabled = changes.length === 0;
  $("saveBtn").textContent = "Review & save";
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
      const label = dayName(dt);
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
        if (a % 60 !== 0 || b % 60 !== 0) {
          return `${label}: boundaries must be whole hours (the device rounds :30 down).`;
        }
        if (b - a < 60) {
          return `${label}: each period must be at least one hour.`;
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
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
      <h3 id="confirmTitle">Confirm schedule change</h3>
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
  const previous = document.activeElement;
  const close = () => {
    modal.remove();
    if (previous && previous.focus) {
      try { previous.focus({ preventScroll: true }); } catch { /* ignore */ }
    }
  };
  const ack = modal.querySelector("#ackBox");
  modal.querySelector("#cancelModal").onclick = close;
  ack.onchange = (e) => {
    modal.querySelector("#confirmModal").disabled = !e.target.checked;
  };
  modal.querySelector("#confirmModal").onclick = () => { close(); doSave(); };
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  modal.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab") return;
    const list = Array.from(modal.querySelectorAll(
      'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])'
    ));
    if (!list.length) return;
    const first = list[0], last = list[list.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  requestAnimationFrame(() => { try { ack.focus(); } catch { /* ignore */ } });
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
      `  • ${tariff(a.peak_type).name} (${dayName(a.day_type).toLowerCase()}) ` +
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
    if (e.kind === "auth") show("login");
  }
};

/* -------------------------------------------------------------- drawer */

let lastFocus = null;

function focusables(root) {
  if (!root) return [];
  return Array.from(root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
  )).filter((el) => el.offsetParent !== null || el === document.activeElement);
}

function openDrawer(push = true) {
  if (state.drawerOpen) return;
  state.drawerOpen = true;
  lastFocus = document.activeElement;
  $("planDrawer").hidden = false;
  $("drawerBackdrop").hidden = false;
  $("menuBtn").setAttribute("aria-expanded", "true");
  document.body.classList.add("drawer-open");
  const home = $("homeView");
  if (home) { home.setAttribute("inert", ""); home.setAttribute("aria-hidden", "true"); }
  if (push) history.replaceState(null, "", "#plan");
  const drawer = $("planDrawer");
  try { drawer.focus({ preventScroll: true }); } catch { /* ignore */ }
  ensurePlan();
}

function closeDrawer(restoreFocus = true, updateHash = true) {
  if (!state.drawerOpen) return;
  state.drawerOpen = false;
  $("planDrawer").hidden = true;
  $("drawerBackdrop").hidden = true;
  $("menuBtn").setAttribute("aria-expanded", "false");
  document.body.classList.remove("drawer-open");
  const home = $("homeView");
  if (home) { home.removeAttribute("inert"); home.removeAttribute("aria-hidden"); }
  if (updateHash) history.replaceState(null, "", location.pathname + location.search);
  if (restoreFocus) {
    const target = (lastFocus && lastFocus !== document.body) ? lastFocus : $("menuBtn");
    if (target && target.focus) {
      try { target.focus({ preventScroll: true }); } catch { /* ignore */ }
    }
  }
  lastFocus = null;
}

function toggleDrawer() {
  if (state.drawerOpen) closeDrawer();
  else openDrawer();
}

$("menuBtn").addEventListener("click", toggleDrawer);
$("drawerClose").addEventListener("click", () => closeDrawer());
$("drawerBackdrop").addEventListener("click", () => closeDrawer());

$("planDrawer").addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (document.querySelector(".overlay")) return; // save confirmation wins
    e.preventDefault();
    closeDrawer();
    return;
  }
  if (e.key !== "Tab" || document.querySelector(".overlay")) return;
  const list = focusables($("planDrawer"));
  if (!list.length) return;
  const first = list[0], last = list[list.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});

window.addEventListener("hashchange", () => {
  if (location.hash === "#plan" && !state.drawerOpen) openDrawer(false);
  else if (location.hash !== "#plan" && state.drawerOpen) closeDrawer(false, false);
});

/* ------------------------------------------------------------- kiosk */

$("fullscreenBtn").addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) {
      if (!document.documentElement.requestFullscreen) throw new Error("not supported");
      await document.documentElement.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch (e) {
    setStatus(`Fullscreen unavailable: ${e.message}`, true);
  }
});

$("logoutBtn").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST", body: "{}" }); } catch { /* ignore */ }
  stopLive();
  HomeView.stopClock();
  state.user = null;
  state.sites = [];
  state.site = null;
  state.sn = null;
  state.plan = null;
  state.loaded = null;
  state.devices = [];
  state.planLoaded = false;
  state.devicesLoaded = false;
  closeDrawer(false, false);
  show("login");
});

/* ------------------------------------------------------------- startup */

(async function boot() {
  try {
    const js = await api("/api/state");
    state.user = js.user;
    state.sites = js.sites || [];
    if (!state.sites.length) { show("login"); return; }
    state.mqttInterval = Number(js.mqtt_interval_s) || 5;
    state.mqttSaved = state.mqttInterval;
    state.mqttActive = state.mqttInterval;
    state.mqttPending = null;
    state.mqttBounds = js.mqtt_bounds || state.mqttBounds;
    state.site = state.sites[0].id;
    await enterApp();
  } catch (e) {
    show("login");
    if (e.kind !== "auth") {
      const el = document.getElementById("loginError");
      if (el) el.textContent = e.message;
    }
  }
})();
