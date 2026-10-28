"""Normalise a raw MQTT telemetry snapshot for the wall-display Home view.

The inverter reports non-negative edge flows (`p2lp`, `g2lp`, ...) plus
aggregate powers.  The front end should not have to infer direction from
signed watts, so this module is the single place that decides whether the
house is importing/exporting, charging/discharging, and idle.  It is pure and
has no Anker dependency, which keeps it easy to unit-test.

Field meanings (see REPORT.md section 12):

    p2lp / p2bp / p2gp   PV -> load / battery / grid
    g2lp / g2bp          grid -> load / battery
    b2lp / b2gp          battery -> load / grid

The edge fields are non-negative.  Fallbacks use the aggregate sign
convention observed on the device: a positive `bp` is discharging (the
device reports 1940 W while `b2lp` is 1940 W); a positive `gp` is treated as
import.  Edge flows always win when present.
"""
from __future__ import annotations

ACTIVE_W = 5.0
POWER_KEYS = ("pp", "gp", "bp", "lp")
FLOW_KEYS = ("p2lp", "p2bp", "p2gp", "g2lp", "g2bp", "b2lp", "b2gp")


def _number(value):
    """Return a finite float, or None for missing/invalid values."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


def _positive(value) -> float:
    value = _number(value)
    return max(0.0, value) if value is not None else 0.0


def _watts(value) -> int:
    return int(round(max(0.0, value)))


def _power(value) -> dict:
    if value is None:
        return {"w": None, "kw": None}
    w = _watts(value)
    return {"w": w, "kw": round(w / 1000.0, 2)}


def _flow(value) -> dict:
    value = _positive(value)
    return {"w": _watts(value), "active": value > ACTIVE_W}


def _soc(value):
    value = _number(value)
    if value is None:
        return None
    return int(round(max(0.0, min(100.0, value))))


def build_home_view(snapshot: dict | None) -> dict:
    """Build the small, stable view model consumed by ``home-view.js``."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    available = bool(snap.get("available"))
    stale = bool(snap.get("stale"))
    try:
        age_s = int(snap.get("age_s"))
    except (TypeError, ValueError):
        age_s = None

    # An "available" snapshot with no telemetry fields is not live data. A
    # frame can be an EMS envelope with an empty/unknown data object; treating
    # that as available turns every metric into 0.00. A real idle frame still
    # has numeric zeroes, so test for None/missing, not truthiness.
    if available and not any(snap.get(k) is not None
                             for k in POWER_KEYS + FLOW_KEYS):
        snap = dict(snap)
        snap["available"] = False
        snap["reason"] = "no telemetry fields"
        available = False

    if not available:
        reason = str(snap.get("reason") or "")
        connecting = reason in ("starting", "connecting", "")
        waking = reason == "waking"
        no_fields = reason == "no telemetry fields"
        return {
            "available": False,
            "stale": False,
            "age_s": age_s,
            "soc": None,
            "solar": {"w": None, "kw": None, "state": "unavailable"},
            "grid": {"w": None, "kw": None, "state": "unavailable", "label": "Unavailable"},
            "battery": {"w": None, "kw": None, "state": "unavailable",
                        "soc": None, "label": "Unavailable"},
            "home": {"w": None, "kw": None, "state": "unavailable"},
            "flows": {
                "solar": _flow(0),
                "grid_import": _flow(0),
                "grid_export": _flow(0),
                "battery_charge": _flow(0),
                "battery_discharge": _flow(0),
            },
            "status": {
                "level": "waiting" if (connecting or waking) else "offline",
                "label": ("Waiting for telemetry" if connecting else
                          "Requesting live data" if waking else
                          "No telemetry data" if no_fields else "Telemetry offline"),
            },
        }

    pv = _positive(snap.get("pp"))
    pv_edge = (_positive(snap.get("p2lp")) + _positive(snap.get("p2bp"))
               + _positive(snap.get("p2gp")))
    solar_w = max(pv, pv_edge)

    grid_in = _positive(snap.get("g2lp")) + _positive(snap.get("g2bp"))
    grid_out = _positive(snap.get("p2gp")) + _positive(snap.get("b2gp"))
    gp = _number(snap.get("gp"))
    grid_edges = ("g2lp", "g2bp", "p2gp", "b2gp")
    if not any(snap.get(k) is not None for k in grid_edges) and gp:
        # Older/partial frames may omit edge flows; fall back to the sign of
        # the aggregate.  A positive gp is an import on this firmware.
        if gp > 0:
            grid_in = gp
        else:
            grid_out = -gp

    batt_charge = _positive(snap.get("p2bp")) + _positive(snap.get("g2bp"))
    batt_discharge = _positive(snap.get("b2lp")) + _positive(snap.get("b2gp"))
    bp = _number(snap.get("bp"))
    batt_edges = ("p2bp", "g2bp", "b2lp", "b2gp")
    if not any(snap.get(k) is not None for k in batt_edges) and bp:
        # Positive bp is discharge, matching the captured 1940 W frame.
        if bp > 0:
            batt_discharge = bp
        else:
            batt_charge = -bp

    home_raw = _positive(snap.get("lp"))
    home_edge = (_positive(snap.get("p2lp")) + _positive(snap.get("g2lp"))
                 + _positive(snap.get("b2lp")))
    # Prefer the aggregate load; fall back to the sum of edges on partial frames.
    home_w = home_raw if home_raw > ACTIVE_W else home_edge

    grid_net = grid_in - grid_out
    if grid_net > 0:
        grid_state, grid_label = "importing", "Importing"
    elif grid_net < 0:
        grid_state, grid_label = "exporting", "Exporting"
    else:
        grid_state, grid_label = "idle", "Idle"
    # The card shows the aggregate meter value; edge flows decide direction.
    grid_w = abs(gp) if gp is not None and abs(gp) > ACTIVE_W else abs(grid_net)

    batt_net = batt_charge - batt_discharge
    if batt_net > 0:
        batt_state, batt_label = "charging", "Charging"
    elif batt_net < 0:
        batt_state, batt_label = "discharging", "Discharging"
    else:
        batt_state, batt_label = "idle", "Idle"
    batt_w = abs(bp) if bp is not None and abs(bp) > ACTIVE_W else abs(batt_net)

    soc = _soc(snap.get("soc"))
    if soc is not None:
        batt_label = f"{soc}% charged"

    if stale:
        status = {"level": "stale", "label": f"Stale · {age_s}s" if age_s is not None
                  else "Stale"}
    else:
        status = {"level": "live", "label": "Live"}

    solar_state = "producing" if solar_w > ACTIVE_W else "idle"
    return {
        "available": True,
        "stale": stale,
        "age_s": age_s,
        "soc": soc,
        "solar": {**_power(solar_w), "state": solar_state},
        "grid": {**_power(grid_w), "state": grid_state, "label": grid_label},
        "battery": {**_power(batt_w), "state": batt_state, "soc": soc,
                    "label": batt_label},
        "home": {**_power(home_w),
                 "state": "consuming" if home_w > ACTIVE_W else "idle"},
        "flows": {
            "solar": _flow(solar_w),
            "grid_import": _flow(grid_in),
            "grid_export": _flow(grid_out),
            "battery_charge": _flow(batt_charge),
            "battery_discharge": _flow(batt_discharge),
        },
        "status": status,
    }
