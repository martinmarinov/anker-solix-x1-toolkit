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
      gridState: $("gridState"),
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

    var soc = view.soc;
    var socOk = available && soc !== null && soc !== undefined &&
                isFinite(Number(soc));
    var kw = fmtKw(view.battery);
    if (els.batteryHero) {
      els.batteryHero.style.setProperty("--soc", socOk ? String(Number(soc)) : "0");
    }
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
