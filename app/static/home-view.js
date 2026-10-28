/* Wall-display Home view: formatting, state attributes, clock and flow layout.
 *
 * Classic IIFE, no build step.  app.js owns auth/network and calls:
 *   HomeView.init()
 *   HomeView.setTimezone(tz) / setSite(name) / setDevice(label)
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
      solarState: $("solarState"), gridState: $("gridState"),
      batteryState: $("batteryState"),
      batteryFill: $("batteryFill"),
      clockTime: $("clockTime"), clockDate: $("clockDate"),
      statusDot: $("statusDot"), statusText: $("statusText"),
      siteName: $("siteName"), alert: $("homeAlert")
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

  function setSite(name) {
    if (els.siteName) els.siteName.textContent = name || "Anker SOLIX X1";
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

    if (els.solarState) {
      els.solarState.innerHTML = available
        ? (view.solar.state === "producing" ? "Producing" : "Idle")
        : (waiting ? "Waiting for telemetry" : "Unavailable");
    }
    if (els.gridState) {
      els.gridState.textContent = available
        ? ((view.grid && view.grid.label) || "Idle")
        : (waiting ? "Waiting for telemetry" : "Unavailable");
    }
    if (els.batteryState) {
      els.batteryState.textContent = available
        ? ((view.battery && view.battery.label) || "Idle")
        : (waiting ? "Waiting for telemetry" : "Unavailable");
    }

    var soc = view.soc;
    if (els.batteryFill) {
      if (soc === null || soc === undefined || !available) {
        els.batteryFill.setAttribute("height", "0");
        els.batteryFill.setAttribute("y", "51");
      } else {
        var h = Math.max(0, Math.min(36, 36 * (Number(soc) / 100)));
        els.batteryFill.setAttribute("height", String(h));
        els.batteryFill.setAttribute("y", String(15 + (36 - h)));
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
    if (!root || !flowLayer || !house) return;
    var stage = root.getBoundingClientRect();
    var w = Math.round(stage.width), h = Math.round(stage.height);
    if (!w || !h) return;

    var u = Math.max(4.6, Math.min(14, Math.min(w / 167.2, h / 94.1)));
    root.style.setProperty("--u", u + "px");

    flowLayer.setAttribute("viewBox", "0 0 " + w + " " + h);
    flowLayer.setAttribute("preserveAspectRatio", "none");

    var solarTile = rectOf(root.querySelector('[data-card="solar"]'));
    var gridTile = rectOf(root.querySelector('[data-card="grid"]'));
    var battTile = rectOf(root.querySelector('[data-card="battery"]'));
    var homeTile = rectOf(root.querySelector('[data-card="home"]'));
    var hr = rectOf(house);
    if (!solarTile || !gridTile || !battTile || !homeTile || !hr) return;

    var roof = { x: hr.left + hr.width * 0.47, y: hr.top + hr.height * 0.39 };
    var houseRight = { x: hr.left + hr.width * 0.88, y: hr.top + hr.height * 0.56 };
    var houseLeft = { x: hr.left + hr.width * 0.13, y: hr.top + hr.height * 0.55 };
    var houseBottom = { x: hr.left + hr.width * 0.50, y: hr.top + hr.height * 0.90 };

    var solarSrc = { x: solarTile.right, y: solarTile.top + solarTile.height / 2 };
    var gridSrc = { x: gridTile.left + gridTile.width * 0.28, y: gridTile.bottom };
    var battSrc = { x: battTile.right, y: battTile.top + battTile.height / 2 };
    var homeTarget = { x: homeTile.left + homeTile.width / 2, y: homeTile.top };

    var view = currentView || {};
    var flows = view.flows || {};
    var available = !!view.available;
    var solarActive = !!(flows.solar && flows.solar.active);
    var gridImport = !!(flows.grid_import && flows.grid_import.active);
    var gridExport = !!(flows.grid_export && flows.grid_export.active);
    var battCharge = !!(flows.battery_charge && flows.battery_charge.active);
    var battDischarge = !!(flows.battery_discharge && flows.battery_discharge.active);

    setFlow("solar", pathH(solarSrc.x, solarSrc.y, roof.x, roof.y), solarActive);

    if (gridExport && !gridImport) {
      setFlow("grid", pathV(houseRight.x, houseRight.y, gridSrc.x, gridSrc.y), true);
    } else {
      setFlow("grid", pathV(gridSrc.x, gridSrc.y, houseRight.x, houseRight.y), gridImport);
    }

    if (battCharge && !battDischarge) {
      setFlow("battery", pathH(houseLeft.x, houseLeft.y, battSrc.x, battSrc.y), true);
    } else {
      setFlow("battery", pathH(battSrc.x, battSrc.y, houseLeft.x, houseLeft.y), battDischarge);
    }

    var homeActive = !!available && view.home && view.home.state === "consuming";
    setFlow("home", pathV(houseBottom.x, houseBottom.y, homeTarget.x, homeTarget.y), homeActive);
  }

  window.HomeView = {
    init: init,
    setTimezone: setTimezone,
    setSite: setSite,
    setDevice: setDevice,
    setAlert: setAlert,
    render: render,
    renderUnavailable: renderUnavailable,
    startClock: startClock,
    stopClock: stopClock,
    layoutFlows: layoutFlows
  };
})();
