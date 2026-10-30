/* Wall-display Home view: formatting, state attributes, clock and flow layout.
 *
 * Classic IIFE, no build step.  app.js owns auth/network and calls:
 *   HomeView.init()
 *   HomeView.setTimezone(tz) / setDevice(label)
 *   HomeView.render(view) / renderUnavailable(message)
 *   HomeView.startClock() / stopClock()
 *   HomeView.applySky("HH:MM")   (manual/debug; the clock calls it on the minute)
 *   HomeView.setAlert(message)
 */
(function () {
  "use strict";

  var root, house, flowLayer, sceneDay, sceneNight;
  var els = {};
  var clockTimer = null;
  var timezone = "";
  var lastTime = "";
  var lastDate = "";
  var layoutQueued = false;
  var currentView = null;
  var initialized = false;
  /* Shared by CSS and JS so the fitted battery and its type override cannot
     drift apart. The ratio form is the one Chromium 95 matches. */
  var PORTRAIT_QUERY = "(max-aspect-ratio: 95/100)";
  var BATTERY_MAX_W = 0.44;
  var BATTERY_MIN_W = 0.20;

  function $(id) { return document.getElementById(id); }

  function init() {
    root = $("homeView");
    if (!root || initialized) return;
    initialized = true;
    house = $("houseImg");
    flowLayer = $("flowLayer");
    sceneDay = $("sceneDay");
    sceneNight = $("sceneNight");
    els = {
      solar: $("vSolar"), grid: $("vGrid"), batt: $("vBatt"), home: $("vHome"),
      gridState: $("gridState"), gridBars: $("gridBars"),
      solarIcon: $("solarIcon"), solarBars: $("solarBars"), homeBars: $("homeBars"),
      batteryHero: $("batteryHero"), batteryShell: $("batteryShell"),
      batterySoc: $("batterySoc"),
      batterySocUnit: $("batterySocUnit"), batterySummary: $("batterySummary"),
      batteryNote: $("batteryNote"),
      clockTime: $("clockTime"), clockDate: $("clockDate"),
      statusDot: $("statusDot"), statusText: $("statusText"),
      liveStatus: $("liveStatus"), alert: $("homeAlert"),
      daylightWatermark: $("daylightWatermark")
    };
    if (window.ResizeObserver) {
      var ro = new ResizeObserver(scheduleLayout);
      ro.observe(root);
      if (house) ro.observe(house);
    }
    window.addEventListener("resize", scheduleLayout);
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", scheduleLayout);
    }
    if (house) house.addEventListener("load", scheduleLayout);
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(scheduleLayout).catch(function () {});
    }
    scheduleLayout();
    applySky(timeFormat().format(new Date()));
  }

  function setTimezone(tz) {
    if (tz) {
      try {
        new Intl.DateTimeFormat("en-GB", { timeZone: tz }).format(new Date());
        timezone = tz;
      } catch (e) {
        timezone = "";
      }
    } else {
      timezone = "";
    }
    Daylight.clear();
    resetDaylightWatermark();
    lastTime = "";
    lastDate = "";
    updateClock();
  }

  function setDevice(label) {
    var el = $("drawerDevice");
    if (el) el.textContent = label ? " · " + label : "";
  }

  function setAlert(message) {
    if (!els.alert) return;
    if (message) {
      els.alert.textContent = message;
      els.alert.classList.remove("hide");
    } else {
      els.alert.textContent = "";
      els.alert.classList.add("hide");
    }
  }

  function fmtKw(obj) {
    if (!obj || obj.kw === null || obj.kw === undefined || !isFinite(obj.kw)) {
      return "\u2014";
    }
    return Number(obj.kw).toFixed(2);
  }

  function fmtKwh(value) {
    if (value === null || value === undefined || !isFinite(Number(value))) {
      return "\u2014";
    }
    return Number(value).toFixed(2) + " kWh";
  }

  function isNum(value) {
    return value !== null && value !== undefined && isFinite(Number(value));
  }

  function fmtDuration(hours) {
    var total = Math.max(0, Math.round(Number(hours) * 60));
    if (total < 60) return Math.max(1, total) + "m";
    var h = Math.floor(total / 60);
    var m = total % 60;
    return m ? h + "h " + m + "m" : h + "h";
  }

  /* The server owns the calendar rule: eta.label is "15:09",
     "tomorrow 16:30" or "Sat 04 Jul 02:38". The browser only joins it to
     the averaged duration, so there is no second day-aware formatter here. */
  function batteryReadout(est, batt, sunsetClock) {
    var eta = est && est.eta;
    if (eta && eta.kind) {
      if (eta.kind === "at_full") return "Full";
      if (eta.kind === "at_empty") return "Empty";
      if (eta.kind === "to_soc") {
        if (!isNum(eta.soc)) return "";
        return Math.round(Number(eta.soc)) + "% by " + (eta.at || sunsetClock || "");
      }
      if (eta.kind === "to_empty" || eta.kind === "to_full") {
        if (!isNum(eta.hours)) return "";
        var head = "~" + fmtDuration(eta.hours) + " · " +
                   (eta.kind === "to_empty" ? "empty" : "full");
        var when = eta.label || eta.clock || "";
        return when ? head + " " + when : head;
      }
      return "";
    }
    // Old server or the first frame: use the legacy aliases without inventing
    // a local clock. Server clock/label fields remain authoritative.
    var legacyWhen = (est && est.clock) || "";
    if (batt && batt.state === "discharging" && isNum(batt.hours_to_empty)) {
      return "~" + fmtDuration(batt.hours_to_empty) + " · empty" +
             (legacyWhen ? " " + legacyWhen : "");
    }
    if (batt && batt.state === "charging" && isNum(batt.hours_to_full)) {
      return "~" + fmtDuration(batt.hours_to_full) + " · full" +
             (legacyWhen ? " " + legacyWhen : "");
    }
    return "";
  }

  /* The caption is deliberately terse. The tooltip carries the averaged
     inputs and the conservative low case so the headline can stay readable. */
  function batteryDetail(est) {
    var solar = est && est.solar;
    if (!solar) return "";
    var parts = [];
    if (isNum(solar.observed_kw) && isNum(solar.load_kw)) {
      var inputs = "10-min averages: PV " +
        Number(solar.observed_kw).toFixed(2) + " kW, load " +
        Number(solar.load_kw).toFixed(2) + " kW";
      if (isNum(solar.batt_avg_kw)) {
        inputs += ", battery " + Number(solar.batt_avg_kw).toFixed(2) + " kW";
      }
      parts.push(inputs + ".");
    }
    if (solar.charge_until && isNum(solar.soc_peak)) {
      parts.push("Charges until ~" + solar.charge_until + " (peak " +
        Math.round(Number(solar.soc_peak)) + "%).");
    }
    var low = solar.low;
    if (low) {
      if (low.kind === "to_soc" && isNum(low.soc)) {
        parts.push("Low case " + Math.round(Number(low.soc)) + "%" +
          (low.at ? " by " + low.at : "") + ".");
      } else if ((low.kind === "to_full" || low.kind === "to_empty") &&
                 isNum(low.hours)) {
        parts.push("Low case ~" + fmtDuration(low.hours) + " to " +
          (low.kind === "to_full" ? "full" : "empty") + ".");
      }
    }
    return parts.join(" ");
  }

  /* Battery time-to-empty/full is computed on the server from a ten-minute,
     time-weighted average. The browser only formats it. */

  /* SOC accent: green above 80%, amber at 30%, red at 5%. Each band has a
     bright fill for the gauge and a dark ink for the shell outline; the
     readout itself stays on the page ink so it is legible over both halves
     of the shell. Smoothstep blends the two spans with the charge. */
  var BATT_GREEN = { fill: [52, 211, 153], ink: [20, 83, 45] };
  var BATT_AMBER = { fill: [251, 191, 36], ink: [120, 53, 15] };
  var BATT_RED = { fill: [248, 113, 113], ink: [127, 29, 29] };
  var BATT_GRAY = { fill: [148, 163, 184], ink: [71, 85, 105] };

  function mixColor(a, b, t) {
    return [
      Math.round(a[0] + (b[0] - a[0]) * t),
      Math.round(a[1] + (b[1] - a[1]) * t),
      Math.round(a[2] + (b[2] - a[2]) * t)
    ];
  }

  function smoothstep(t) {
    t = Math.max(0, Math.min(1, t));
    return t * t * (3 - 2 * t);
  }

  function mixTone(a, b, t) {
    return {
      fill: mixColor(a.fill, b.fill, t),
      ink: mixColor(a.ink, b.ink, t)
    };
  }

  function batteryTone(soc) {
    var value = Number(soc);
    if (!isFinite(value)) return null;
    value = Math.max(0, Math.min(100, value));
    if (value >= 80) return BATT_GREEN;
    if (value >= 30) {
      return mixTone(BATT_GREEN, BATT_AMBER, smoothstep((80 - value) / 50));
    }
    if (value >= 5) {
      return mixTone(BATT_AMBER, BATT_RED, smoothstep((30 - value) / 25));
    }
    return BATT_RED;
  }

  function applyBatteryColor(socOk, soc) {
    if (!els.batteryHero) return;
    var tone = socOk ? batteryTone(soc) : null;
    tone = tone || BATT_GRAY;
    els.batteryHero.style.setProperty("--battery-rgb", tone.fill.join(", "));
    els.batteryHero.style.setProperty("--battery-ink-rgb", tone.ink.join(", "));
  }

  /* The bar band also drives the card accent: value, unit, icon, top rule
     and tinted shadow all read the same green/amber/red/blue token. */
  function setCardLevel(el, level) {
    var tile = el && el.closest ? el.closest(".tile") : null;
    if (tile) tile.dataset.level = level || "off";
  }

  function renderHomeBars(view, available) {
    var el = els.homeBars;
    if (!el) return;
    var home = view.home || {};
    var w = home.w;
    if ((w === null || w === undefined) && home.kw !== null &&
        home.kw !== undefined && isFinite(Number(home.kw))) {
      w = Number(home.kw) * 1000;
    }

    var level = 0;
    var band = "off";
    var text = available ? "No home reading" : "Waiting for telemetry";
    if (available && w !== null && w !== undefined && isFinite(Number(w))) {
      var watts = Math.max(0, Number(w));
      if (watts < 2000) { level = 1; band = "green"; }
      else if (watts < 3000) { level = 2; band = "green"; }
      else if (watts < 4000) { level = 3; band = "amber"; }
      else if (watts < 5000) { level = 4; band = "amber"; }
      else { level = 5; band = "red"; }
      text = level + " of 5 · " + fmtKw(home) + " kW";
    }

    el.dataset.level = band;
    setCardLevel(el, band);
    el.setAttribute("aria-valuenow", String(level));
    el.setAttribute("aria-valuetext", text);
    var bars = el.children;
    for (var i = 0; i < bars.length; i++) {
      bars[i].classList.toggle("is-on", i < level);
    }
  }

  /* Solar bars: >0 W always lights bar one; 2.8 kW and above lights all
     five. The four steps between are linear (0.7 kW apart). */
  function renderSolarBars(view, available) {
    var el = els.solarBars;
    if (!el) return;
    var solar = view.solar || {};
    var w = solar.w;
    if ((w === null || w === undefined) && solar.kw !== null &&
        solar.kw !== undefined && isFinite(Number(solar.kw))) {
      w = Number(solar.kw) * 1000;
    }

    var level = 0;
    var text = available ? "No solar generation" : "Waiting for telemetry";
    if (available && w !== null && w !== undefined && isFinite(Number(w))) {
      var watts = Math.max(0, Number(w));
      if (watts > 0) {
        level = Math.min(5, Math.max(1, 1 + Math.floor((watts / 2800) * 4)));
        text = level + " of 5 · " + fmtKw(solar) + " kW";
      }
    }

    var band = level ? "green" : "off";
    el.dataset.level = band;
    setCardLevel(el, band);
    el.setAttribute("aria-valuenow", String(level));
    el.setAttribute("aria-valuetext", text);
    var bars = el.children;
    for (var i = 0; i < bars.length; i++) {
      bars[i].classList.toggle("is-on", i < level);
    }
  }

  /* Grid meter. Export is one blue scale from 50 W to 2.8 kW. Import is
     thresholded: 1/2 green, 3/4 amber, 5 red. Anything below 50 W is ignored
     so the inverter's ramp-up/down does not flash the meter. */
  function renderGridBars(view, available) {
    var el = els.gridBars;
    if (!el) return;
    var grid = view.grid || {};
    var w = grid.w;
    if ((w === null || w === undefined) && grid.kw !== null &&
        grid.kw !== undefined && isFinite(Number(grid.kw))) {
      w = Number(grid.kw) * 1000;
    }

    var level = 0;
    var band = "off";
    var text = available ? "No significant grid flow" : "Waiting for telemetry";
    var mode = grid.state === "exporting" ? "export" :
               grid.state === "importing" ? "import" : "";
    if (available && mode && w !== null && w !== undefined &&
        isFinite(Number(w)) && Number(w) >= 50) {
      var watts = Math.max(0, Number(w));
      if (mode === "export") {
        var step = (2800 - 50) / 4;
        level = Math.min(5, Math.max(1, 1 + Math.floor((watts - 50) / step)));
        band = "export";
        text = "Exporting · " + level + " of 5 · " + fmtKw(grid) + " kW";
      } else {
        if (watts < 1000) { level = 1; band = "green"; }
        else if (watts < 2000) { level = 2; band = "green"; }
        else if (watts < 5000) { level = 3; band = "amber"; }
        else if (watts < 10000) { level = 4; band = "amber"; }
        else { level = 5; band = "red"; }
        text = "Importing · " + level + " of 5 · " + fmtKw(grid) + " kW";
      }
    }

    el.dataset.level = band;
    setCardLevel(el, band);
    el.setAttribute("aria-valuenow", String(level));
    el.setAttribute("aria-valuetext", text);
    var bars = el.children;
    for (var i = 0; i < bars.length; i++) {
      bars[i].classList.toggle("is-on", i < level);
    }
  }

  /* The backdrop follows the same once-a-minute clock tick the drawer uses.
     Daylight owns the one-hour dawn/dusk easing around the server's sunrise
     and sunset; outside those windows it is fully day or fully night. Two
     style writes per minute move the two pixel-aligned layers, so the browser
     can keep the transition on the compositor. */
  function applySky(clock, at) {
    if (!sceneDay || !sceneNight) return;
    var minutes = clock
      ? Daylight.clockMinutes(clock)
      : Daylight.minutesNow(at || new Date(), timezone);
    if (minutes === null) return;
    var night = Daylight.nightAmount(minutes, Daylight.sunTimes());
    if (night === null) return;
    sceneDay.style.opacity = String(1 - night);
    sceneNight.style.opacity = String(night);
    if (root) {
      root.dataset.sky = Daylight.skyState(night);
    }
  }

  function applySolarIcon() {
    if (!els.solarIcon) return;
    var solar = currentView && currentView.solar;
    var generating = !!(solar && (solar.state === "producing" ||
      (solar.w !== null && solar.w !== undefined && Number(solar.w) > 0)));
    var minutes = Daylight.minutesNow(new Date(), timezone);
    var icon = Daylight.solarIcon(generating, minutes, Daylight.sunTimes());
    els.solarIcon.dataset.icon = icon;
  }

  function setStatus(level, label) {
    if (els.statusText) els.statusText.textContent = label || "";
    if (els.statusDot) {
      els.statusDot.className = "status-dot is-" + (level || "waiting");
    }
    if (els.liveStatus) {
      els.liveStatus.dataset.level = level || "waiting";
    }
  }

  var WATERMARK_PLACEHOLDER = "sunrise —\nsunset —";

  function clockText(value) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(value || "");
    if (!m) return null;
    var hour = Number(m[1]);
    var minute = Number(m[2]);
    if (!isFinite(hour) || !isFinite(minute)) return null;
    if (hour > 24 || minute > 59 || (hour === 24 && minute !== 0)) return null;
    return (hour < 10 ? "0" : "") + hour + ":" +
           (minute < 10 ? "0" : "") + minute;
  }

  function resetDaylightWatermark() {
    var el = els.daylightWatermark;
    if (!el) return;
    el.textContent = WATERMARK_PLACEHOLDER;
    el.dataset.source = "unknown";
    el.title = "Waiting for the server's daylight window.";
  }

  /* A quiet debug block, not a second source of truth.  Only the server's
     view.daylight reaches this function; invalid or missing values leave the
     last known window in place. */
  function renderDaylightWatermark(daylight) {
    var el = els.daylightWatermark;
    if (!el || !daylight || typeof daylight !== "object") return;
    var sunrise = clockText(daylight.sunrise);
    var sunset = clockText(daylight.sunset);
    if (sunrise === null || sunset === null) return;
    var source = daylight.source;
    var label, title;
    if (source === "ip") {
      label = "est. ip";
      title = "Estimated from the server's public IP; sunrise and sunset " +
              "can be minutes to hours off.";
    } else if (source === "config") {
      label = "est. cfg";
      title = "Estimated from the configured SOLAR_* coordinates.";
    } else if (source === "explicit") {
      label = "est.";
      title = "Estimated from coordinates supplied by the server.";
    } else {
      label = "fallback";
      title = "Location unavailable; showing the canonical 07:00-18:00 " +
              "window.";
    }
    el.textContent = "sunrise " + sunrise + "\nsunset " + sunset + "\n" +
                     label;
    el.dataset.source = (source === "ip" || source === "config" ||
                         source === "explicit") ? source : "default";
    el.title = title;
  }

  function render(view) {
    if (!view || !root) return;
    currentView = view;
    if (view.daylight && Daylight.configure(view.daylight)) {
      applySky(timeFormat().format(new Date()));
    }
    renderDaylightWatermark(view.daylight);
    var available = !!view.available;
    var stale = !!view.stale;
    var status = view.status || {};
    var waiting = status.level === "waiting";

    root.dataset.available = String(available);
    root.dataset.stale = String(stale);
    root.dataset.status = status.level || (available ? "live" : "waiting");
    root.dataset.solar = (view.solar && view.solar.state) || "unavailable";
    root.dataset.grid = (view.grid && view.grid.state) || "unavailable";
    root.dataset.battery = (view.battery && view.battery.state) || "unavailable";
    root.dataset.home = (view.home && view.home.state) || "unavailable";

    if (els.solar) els.solar.textContent = fmtKw(view.solar);
    if (els.grid) els.grid.textContent = fmtKw(view.grid);
    if (els.batt) els.batt.textContent = fmtKw(view.battery);
    if (els.home) els.home.textContent = fmtKw(view.home);

    if (els.gridState) {
      els.gridState.textContent = available
        ? ((view.grid && view.grid.label) || "Idle")
        : "";
    }
    renderHomeBars(view, available);
    renderSolarBars(view, available);
    renderGridBars(view, available);
    applySolarIcon();

    var soc = view.soc;
    var socOk = available && soc !== null && soc !== undefined &&
                isFinite(Number(soc));
    var batt = view.battery || {};
    var est = batt.estimate && typeof batt.estimate === "object"
      ? batt.estimate : null;
    root.dataset.estimate = (est && est.mode) || "unavailable";

    var readout = batteryReadout(est, batt, Daylight.sunsetClock());

    var capNote = "";
    if (est && est.solar && est.solar.capped &&
        isNum(est.solar.charge_limit_kw)) {
      capNote = "Charge capped at " +
        Number(est.solar.charge_limit_kw).toFixed(2) + " kW by the inverter.";
    }

    /* A charging battery can still end below its current SOC when the
       afternoon house load outgrows PV. Say so as quietly as possible. */
    var loadNote = "";
    if (available && socOk && isNum(batt.available_kwh) &&
        batt.state === "charging" && est && est.phase === "charging" &&
        est.eta && est.eta.kind === "to_soc" && isNum(est.eta.soc) &&
        Number(est.eta.soc) < Number(soc)) {
      loadNote = "under assumed load";
    }

    if (els.batteryHero) {
      els.batteryHero.style.setProperty("--soc", socOk ? String(Number(soc)) : "0");
    }
    applyBatteryColor(socOk, soc);
    if (els.batterySoc) els.batterySoc.textContent = socOk ? String(Math.round(Number(soc))) : "—";
    if (els.batterySocUnit) els.batterySocUnit.hidden = !socOk;
    if (els.batterySummary) {
      if (!available) {
        els.batterySummary.textContent = "—";
      } else if (batt.available_kwh === null || batt.available_kwh === undefined ||
                 !isFinite(Number(batt.available_kwh))) {
        els.batterySummary.textContent = "—";
      } else {
        var lines = [fmtKwh(batt.available_kwh)];
        if (readout) lines.push(readout);
        els.batterySummary.textContent = lines.join("\n");
      }
      var title = [capNote, batteryDetail(est)].filter(Boolean).join(" ");
      if (title) els.batterySummary.title = title;
      else els.batterySummary.removeAttribute("title");
    }
    if (els.batteryNote) {
      els.batteryNote.textContent = loadNote;
      els.batteryNote.hidden = !loadNote;
    }

    setStatus(status.level || "waiting", status.label || (waiting ? "Waiting for telemetry" : ""));
    layoutFlows();
  }

  function renderUnavailable(message) {
    var label = message && /offline|unavailable|reach|error|timed? ?out/i.test(message)
      ? "Telemetry offline" : "Waiting for telemetry";
    render({
      available: false, stale: false, age_s: null, soc: null,
      solar: { w: null, kw: null, state: "unavailable" },
      grid: { w: null, kw: null, state: "unavailable", label: "Unavailable" },
      battery: { w: null, kw: null, state: "unavailable", soc: null, label: "Unavailable" },
      home: { w: null, kw: null, state: "unavailable" },
      flows: {
        solar: { w: 0, active: false }, grid_import: { w: 0, active: false },
        grid_export: { w: 0, active: false }, battery_charge: { w: 0, active: false },
        battery_discharge: { w: 0, active: false }
      },
      status: { level: label === "Telemetry offline" ? "offline" : "waiting", label: label }
    });
  }

  /* ------------------------------------------------------------- clock */

  function timeFormat() {
    var opts = { hour: "2-digit", minute: "2-digit", hour12: false, hourCycle: "h23" };
    if (timezone) opts.timeZone = timezone;
    try { return new Intl.DateTimeFormat("en-GB", opts); }
    catch (e) { delete opts.timeZone; return new Intl.DateTimeFormat("en-GB", opts); }
  }

  function dateFormat() {
    var opts = { weekday: "short", day: "2-digit", month: "short", year: "numeric" };
    if (timezone) opts.timeZone = timezone;
    try { return new Intl.DateTimeFormat("en-GB", opts); }
    catch (e) { delete opts.timeZone; return new Intl.DateTimeFormat("en-GB", opts); }
  }

  function updateClock() {
    applySolarIcon();
    var now = new Date();
    var t = timeFormat().format(now);
    if (t !== lastTime) {
      lastTime = t;
      if (els.clockTime) els.clockTime.textContent = t;
      applySky(t);
    }
    if (!els.clockTime) return;
    var d = dateFormat().format(now).replace(/,/g, "");
    if (d !== lastDate) { els.clockDate.textContent = d; lastDate = d; }
  }

  function startClock() {
    if (clockTimer) return;
    updateClock();
    clockTimer = setInterval(function () {
      if (!document.hidden) updateClock();
    }, 1000);
  }

  function stopClock() {
    if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  }

  /* -------------------------------------------------------- flow layout */

  function scheduleLayout() {
    if (layoutQueued) return;
    layoutQueued = true;
    requestAnimationFrame(function () {
      layoutQueued = false;
      layoutFlows();
    });
  }

  function rectOf(el) {
    if (!el) return null;
    var stage = root.getBoundingClientRect();
    var r = el.getBoundingClientRect();
    return {
      left: r.left - stage.left, top: r.top - stage.top,
      right: r.right - stage.left, bottom: r.bottom - stage.top,
      width: r.width, height: r.height
    };
  }

  /* Fit the battery into the measured band between the top card row and the
     Home card. The shell keeps its 0.53 aspect; width is capped at 44% of the
     stage. The caption/status block is measured after each width change, so a
     two- or three-line estimate shrinks the shell instead of overlapping the
     Home card. */
  function fitBattery(stage, u, topEdge, bottomEdge) {
    if (!els.batteryHero || !els.batteryShell) return;
    var portrait = !!(window.matchMedia &&
      window.matchMedia(PORTRAIT_QUERY).matches);
    if (!portrait) {
      /* Let the landscape/tall media queries own the position and width. */
      els.batteryHero.style.removeProperty("--battery-w");
      els.batteryHero.style.removeProperty("--battery-top");
      return;
    }
    if (!stage.width || !isFinite(stage.width) || bottomEdge <= topEdge) return;

    var cap = els.batteryHero.querySelector(".battery-cap");
    var minW = stage.width * BATTERY_MIN_W;
    var maxW = stage.width * BATTERY_MAX_W;
    var width = maxW;
    var aspect = 0.53;
    var captionH = 0;
    var capH = 0;
    var shell = null;
    var hero = null;
    var usable = Math.max(0, bottomEdge - topEdge - u * 9);

    /* Only ever shrink: three passes absorb the caption reflow caused by the
       width change and settle before the position is applied. */
    for (var pass = 0; pass < 3; pass++) {
      els.batteryHero.style.setProperty("--battery-w", width + "px");
      shell = rectOf(els.batteryShell);
      hero = rectOf(els.batteryHero);
      if (!shell || !hero || shell.height <= 0) return;
      aspect = shell.width / shell.height;
      if (!isFinite(aspect) || aspect <= 0) aspect = 0.53;
      captionH = Math.max(0, hero.height - shell.height);
      capH = cap ? Math.max(0, shell.top - rectOf(cap).top) : 0;
      var fitW = Math.max(minW,
        Math.min(maxW, (usable - captionH - capH) * aspect));
      if (fitW >= width - 0.5) break;
      width = fitW;
    }

    width = Math.round(width * 100) / 100;
    els.batteryHero.style.setProperty("--battery-w", width + "px");
    shell = rectOf(els.batteryShell);
    hero = rectOf(els.batteryHero);
    if (!shell || !hero || shell.height <= 0) return;
    captionH = Math.max(0, hero.height - shell.height);
    capH = cap ? Math.max(0, shell.top - rectOf(cap).top) : 0;
    var clusterH = capH + shell.height + captionH;
    var spare = Math.max(0, usable - clusterH);
    var top = topEdge + u * 4 + spare / 2 + capH;
    /* Tall type is the one thing the width cap cannot absorb. Keep the cluster
       above the Home card; the 20% width floor is the safety stop. */
    var maxTop = bottomEdge - u * 5 - captionH - shell.height;
    if (top > maxTop) top = Math.max(topEdge + capH + u, maxTop);
    els.batteryHero.style.setProperty(
      "--battery-top", Math.round(top * 100) / 100 + "px"
    );
  }

  function pathH(x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    if (Math.abs(dx) < 2 || Math.abs(dy) < 2) {
      return "M" + x1 + " " + y1 + " L" + x2 + " " + y2;
    }
    var sx = dx > 0 ? 1 : -1, sy = dy > 0 ? 1 : -1;
    var r = Math.min(30, Math.abs(dx) / 2, Math.abs(dy) / 2);
    return "M" + x1 + " " + y1 +
      " L" + (x2 - sx * r) + " " + y1 +
      " Q" + x2 + " " + y1 + " " + x2 + " " + (y1 + sy * r) +
      " L" + x2 + " " + y2;
  }

  function pathV(x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    if (Math.abs(dx) < 2 || Math.abs(dy) < 2) {
      return "M" + x1 + " " + y1 + " L" + x2 + " " + y2;
    }
    var sx = dx > 0 ? 1 : -1, sy = dy > 0 ? 1 : -1;
    var r = Math.min(30, Math.abs(dx) / 2, Math.abs(dy) / 2);
    return "M" + x1 + " " + y1 +
      " L" + x1 + " " + (y2 - sy * r) +
      " Q" + x1 + " " + y2 + " " + (x1 + sx * r) + " " + y2 +
      " L" + x2 + " " + y2;
  }

  function setFlow(name, d, active) {
    if (!flowLayer) return;
    var g = flowLayer.querySelector('[data-flow="' + name + '"]');
    if (!g) return;
    g.classList.toggle("active", !!active);
    var paths = g.querySelectorAll("path");
    for (var i = 0; i < paths.length; i++) paths[i].setAttribute("d", d);
  }

  function layoutFlows() {
    if (!root || !flowLayer || !els.batteryShell) return;
    var stage = root.getBoundingClientRect();
    var w = Math.round(stage.width), h = Math.round(stage.height);
    if (!w || !h) return;

    var u = Math.max(4.6, Math.min(14, Math.min(w / 167.2, h / 94.1)));
    root.style.setProperty("--u", u + "px");

    flowLayer.setAttribute("viewBox", "0 0 " + w + " " + h);
    flowLayer.setAttribute("preserveAspectRatio", "none");

    var solarTile = rectOf(root.querySelector('[data-card="solar"]'));
    var gridTile = rectOf(root.querySelector('[data-card="grid"]'));
    var homeTile = rectOf(root.querySelector('[data-card="home"]'));
    if (!solarTile || !gridTile || !homeTile) return;

    fitBattery(stage, u, Math.max(solarTile.bottom, gridTile.bottom),
      homeTile.top);

    var battery = rectOf(els.batteryShell);
    var hero = rectOf(els.batteryHero);
    if (!battery || !hero) return;

    var gap = Math.max(4, u * 1.4);
    var batteryTop = { x: battery.left + battery.width * 0.5, y: battery.top - gap };
    var batteryRight = { x: battery.right + 2, y: battery.top + battery.height * 0.54 };
    var batteryLeft = { x: battery.left - 2, y: battery.top + battery.height * 0.95 };
    var solarSrc = { x: solarTile.right, y: solarTile.top + solarTile.height / 2 };
    /* Keep the Grid takeoff outside a wider fitted shell, but inside its own
       card. Without this the wire disappears behind the battery. */
    var gridAnchorX = Math.max(
      gridTile.left + gridTile.width * 0.32,
      battery.right + u * 1.2
    );
    gridAnchorX = Math.min(gridAnchorX, gridTile.right - u * 1.5);
    gridAnchorX = Math.max(gridAnchorX, gridTile.left + u * 1.5);
    var gridSrc = { x: gridAnchorX, y: gridTile.bottom };
    var homeTarget = { x: homeTile.right, y: homeTile.top + homeTile.height / 2 };
    var portrait = !!(window.matchMedia &&
      window.matchMedia(PORTRAIT_QUERY).matches);

    var view = currentView || {};
    var flows = view.flows || {};
    var available = !!view.available;
    var solarActive = !!(flows.solar && flows.solar.active);
    var gridImport = !!(flows.grid_import && flows.grid_import.active);
    var gridExport = !!(flows.grid_export && flows.grid_export.active);

    setFlow("solar", pathH(solarSrc.x, solarSrc.y, batteryTop.x, batteryTop.y), solarActive);

    if (gridExport && !gridImport) {
      setFlow("grid", pathV(batteryRight.x, batteryRight.y, gridSrc.x, gridSrc.y), true);
    } else {
      setFlow("grid", pathV(gridSrc.x, gridSrc.y, batteryRight.x, batteryRight.y), gridImport);
    }

    var homeActive = !!available && view.home && view.home.state === "consuming";
    if (portrait) {
      /* In portrait Home is centred under the battery. Run the wire down the
         battery's left shoulder, then across below the battery caption and
         into the top edge of the card. A straight drop would cross the SOC
         summary text. */
      var summary = $("batterySummary");
      var capLeft = summary ? rectOf(summary).left : battery.left;
      var runX = Math.max(battery.left + 2,
        Math.min(battery.left + battery.width * 0.12, capLeft - u));
      var entryX = homeTile.left + homeTile.width / 2;
      var bendY = Math.max(hero.bottom + u * 1.2, homeTile.top - u * 4);
      var corner = u * 1.6;
      setFlow("home", "M" + runX + " " + battery.bottom +
        " L" + runX + " " + (bendY - corner) +
        " Q" + runX + " " + bendY + " " + (runX + corner) + " " + bendY +
        " L" + (entryX - corner) + " " + bendY +
        " Q" + entryX + " " + bendY + " " + entryX + " " + (bendY + corner) +
        " L" + entryX + " " + homeTile.top, homeActive);
    } else {
      setFlow("home", pathV(batteryLeft.x, batteryLeft.y,
        homeTarget.x, homeTarget.y), homeActive);
    }
  }

  window.HomeView = {
    init: init,
    setTimezone: setTimezone,
    setDevice: setDevice,
    setAlert: setAlert,
    render: render,
    renderUnavailable: renderUnavailable,
    startClock: startClock,
    stopClock: stopClock,
    applySky: applySky,
    layoutFlows: layoutFlows
  };
})();
