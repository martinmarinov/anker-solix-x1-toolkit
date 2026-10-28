/* Wall-display Home view: formatting, state attributes, clock and flow layout.
 *
 * Classic IIFE, no build step.  app.js owns auth/network and calls:
 *   HomeView.init()
 *   HomeView.setTimezone(tz) / setDevice(label)
 *   HomeView.render(view) / renderUnavailable(message)
 *   HomeView.startClock() / stopClock()
 *   HomeView.setAlert(message)
 */
(function () {
  "use strict";

  var root, house, flowLayer;
  var els = {};
  var clockTimer = null;
  var timezone = "";
  var lastTime = "";
  var lastDate = "";
  var layoutQueued = false;
  var currentView = null;
  var initialized = false;

  function $(id) { return document.getElementById(id); }

  function init() {
    root = $("homeView");
    if (!root || initialized) return;
    initialized = true;
    house = $("houseImg");
    flowLayer = $("flowLayer");
    els = {
      solar: $("vSolar"), grid: $("vGrid"), batt: $("vBatt"), home: $("vHome"),
      gridState: $("gridState"), gridBars: $("gridBars"),
      solarIcon: $("solarIcon"), solarBars: $("solarBars"), homeBars: $("homeBars"),
      batteryHero: $("batteryHero"), batteryShell: $("batteryShell"),
      batterySoc: $("batterySoc"),
      batterySocUnit: $("batterySocUnit"), batterySummary: $("batterySummary"),
      clockTime: $("clockTime"), clockDate: $("clockDate"),
      statusDot: $("statusDot"), statusText: $("statusText"),
      liveStatus: $("liveStatus"), alert: $("homeAlert")
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

  /* SOC accent: green above 80%, amber at 30%, red at 5%. Smoothstep
     blends the two spans so the colour moves with the state of charge. */
  var BATT_GREEN = [46, 224, 122];
  var BATT_AMBER = [251, 191, 36];
  var BATT_RED = [248, 113, 113];
  var BATT_GRAY = [143, 163, 184];

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

  function batteryColor(soc) {
    var value = Number(soc);
    if (!isFinite(value)) return null;
    value = Math.max(0, Math.min(100, value));
    if (value >= 80) return BATT_GREEN;
    if (value >= 30) {
      return mixColor(BATT_GREEN, BATT_AMBER, smoothstep((80 - value) / 50));
    }
    if (value >= 5) {
      return mixColor(BATT_AMBER, BATT_RED, smoothstep((30 - value) / 25));
    }
    return BATT_RED;
  }

  function applyBatteryColor(socOk, soc) {
    if (!els.batteryHero) return;
    var rgb = socOk ? batteryColor(soc) : null;
    rgb = rgb || BATT_GRAY;
    els.batteryHero.style.setProperty("--battery-rgb", rgb.join(", "));
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

    el.dataset.level = level ? "green" : "off";
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
    el.setAttribute("aria-valuenow", String(level));
    el.setAttribute("aria-valuetext", text);
    var bars = el.children;
    for (var i = 0; i < bars.length; i++) {
      bars[i].classList.toggle("is-on", i < level);
    }
  }

  function localHour() {
    var opts = { hour: "2-digit", hourCycle: "h23" };
    if (timezone) opts.timeZone = timezone;
    try {
      return Number(new Intl.DateTimeFormat("en-GB", opts).format(new Date()));
    } catch (e) {
      return new Date().getHours();
    }
  }

  function applySolarIcon() {
    if (!els.solarIcon) return;
    var solar = currentView && currentView.solar;
    var generating = !!(solar && (solar.state === "producing" ||
      (solar.w !== null && solar.w !== undefined && Number(solar.w) > 0)));
    var hour = localHour();
    var icon = generating ? "sun" : (hour >= 8 && hour < 20 ? "cloud" : "moon");
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

  function render(view) {
    if (!view || !root) return;
    currentView = view;
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
    var kw = fmtKw(view.battery);
    if (els.batteryHero) {
      els.batteryHero.style.setProperty("--soc", socOk ? String(Number(soc)) : "0");
    }
    applyBatteryColor(socOk, soc);
    if (els.batterySoc) els.batterySoc.textContent = socOk ? String(Math.round(Number(soc))) : "—";
    if (els.batterySocUnit) els.batterySocUnit.hidden = !socOk;
    if (els.batterySummary) {
      if (!available) {
        els.batterySummary.textContent = "—";
      } else if (socOk && kw !== "\u2014") {
        els.batterySummary.textContent = kw + " kW · " + Math.round(Number(soc)) + "%";
      } else {
        els.batterySummary.textContent = kw === "\u2014" ? "—" : kw + " kW";
      }
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
    if (!els.clockTime) return;
    var now = new Date();
    var t = timeFormat().format(now);
    var d = dateFormat().format(now).replace(/,/g, "");
    if (t !== lastTime) { els.clockTime.textContent = t; lastTime = t; }
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
    var battery = rectOf(els.batteryShell);
    if (!solarTile || !gridTile || !homeTile || !battery) return;

    var gap = Math.max(4, u * 1.4);
    var batteryTop = { x: battery.left + battery.width * 0.5, y: battery.top - gap };
    var batteryRight = { x: battery.right + 2, y: battery.top + battery.height * 0.54 };
    var batteryLeft = { x: battery.left - 2, y: battery.top + battery.height * 0.95 };
    var solarSrc = { x: solarTile.right, y: solarTile.top + solarTile.height / 2 };
    var gridSrc = { x: gridTile.left + gridTile.width * 0.32, y: gridTile.bottom };
    var homeTarget = { x: homeTile.right, y: homeTile.top + homeTile.height / 2 };

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
    setFlow("home", pathV(batteryLeft.x, batteryLeft.y, homeTarget.x, homeTarget.y), homeActive);
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
    layoutFlows: layoutFlows
  };
})();
