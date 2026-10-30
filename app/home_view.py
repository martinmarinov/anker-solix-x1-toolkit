"""Normalise a raw MQTT telemetry snapshot for the wall-display Home view.

The inverter reports non-negative edge flows (`p2lp`, `g2lp`, ...) plus
aggregate powers.  The front end should not have to infer direction from
signed watts, so this module is the single place that decides whether the
house is importing/exporting, charging/discharging, and idle.  It is pure and
has no Anker dependency, which keeps it easy to unit-test.

The battery ETA is delegated to :mod:`battery_estimate`, which uses the
ten-minute signed average for the state and the instantaneous solar/grid
readings for the solar-curve branch.  Everything the cards display (battery
watts/state, flows, solar/grid/home and the shimmer) stays on instantaneous
telemetry; only ``battery.estimate``, its ``hours_to_*`` aliases and the
``batt_avg_*``/``load_avg_kw`` diagnostics use the average, and those
diagnostics are never rendered as card power.
"""
from __future__ import annotations

from battery_estimate import estimate_battery
from eta import local_datetime, local_hour
from power_flow import (
    ACTIVE_W,
    battery_charge_w,
    battery_discharge_w,
    battery_display_w,
    battery_net_w,
    grid_display_w,
    grid_export_w,
    grid_import_w,
    grid_net_w,
    has_power_fields,
    home_w,
    number,
    positive,
    solar_w,
    watts,
)


def _power(value) -> dict:
    if value is None:
        return {"w": None, "kw": None}
    w = watts(value)
    return {"w": w, "kw": round(w / 1000.0, 2)}


def _flow(value) -> dict:
    value = positive(value)
    return {"w": watts(value), "active": value > ACTIVE_W}


def _soc(value):
    value = number(value)
    if value is None:
        return None
    return int(round(max(0.0, min(100.0, value))))


def _age_label(age_s):
    if age_s is None:
        return ""
    if age_s < 60:
        return f"{age_s}s"
    minutes, seconds = divmod(age_s, 60)
    return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"


def _empty_estimate() -> dict:
    return {
        "mode": "unavailable",
        "phase": "unavailable",
        "source": "none",
        "rate_kw": None,
        "hours": None,
        "clock": None,
        "soc_final": None,
        "solar_end": None,
        "solar": None,
        "eta": {"kind": "none", "hours": None, "clock": None, "label": None,
                "soc": None, "at": None},
        "reason": "unavailable",
    }


def _legacy_hours(estimate: dict) -> tuple[float | None, float | None]:
    """Old-client aliases, derived from ``eta.kind`` rather than a mode list."""
    eta = estimate.get("eta") or {}
    kind = eta.get("kind")
    hours = estimate.get("hours")
    if kind == "to_empty" and hours is not None:
        return round(hours, 2), None
    if kind == "at_empty":
        return 0.0, None
    if kind == "to_full" and hours is not None:
        return None, round(hours, 2)
    if kind == "at_full":
        return None, 0.0
    return None, None


def build_home_view(snapshot: dict | None, now=None, tz: str = "") -> dict:
    """Build the small, stable view model consumed by ``home-view.js``."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    available = bool(snap.get("available"))
    waking = bool(snap.get("waking"))
    stale = bool(snap.get("stale")) and not waking
    try:
        age_s = int(snap.get("age_s"))
    except (TypeError, ValueError):
        age_s = None

    # An "available" snapshot with no telemetry fields is not live data. A
    # frame can be an EMS envelope with an empty/unknown data object; treating
    # that as available turns every metric into 0.00. A real idle frame still
    # has numeric zeroes, so test for None/missing, not truthiness.
    if available and not has_power_fields(snap):
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
                        "soc": None, "label": "Unavailable",
                        "nominal_capacity_kwh": None, "reserve_soc": None,
                        "available_kwh": None, "hours_to_empty": None,
                        "hours_to_full": None, "batt_avg_kw": None,
                        "batt_avg_net_kw": None, "load_avg_kw": None,
                        "charge_limit_kw": None,
                        "charge_limit_source": "unavailable",
                        "estimate": _empty_estimate()},
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

    solar_value = solar_w(snap)
    grid_in = grid_import_w(snap)
    grid_out = grid_export_w(snap)
    grid_net = grid_net_w(snap)
    grid_w = grid_display_w(snap)
    if grid_net > 0:
        grid_state, grid_label = "importing", "Importing"
    elif grid_net < 0:
        grid_state, grid_label = "exporting", "Exporting"
    else:
        grid_state, grid_label = "idle", "Idle"

    batt_charge = battery_charge_w(snap)
    batt_discharge = battery_discharge_w(snap)
    batt_net = battery_net_w(snap)
    batt_w = battery_display_w(snap)
    if batt_net > 0:
        batt_state, batt_label = "charging", "Charging"
    elif batt_net < 0:
        batt_state, batt_label = "discharging", "Discharging"
    else:
        batt_state, batt_label = "idle", "Idle"

    load_w = home_w(snap)

    soc = _soc(snap.get("soc"))
    if soc is not None:
        batt_label = f"{soc}% charged"

    nominal = number(snap.get("nominal_capacity_kwh"))
    reserve = number(snap.get("reserve_soc"))
    soc_raw = number(snap.get("soc"))
    average_kw = number(snap.get("batt_avg_kw"))
    avg_net_kw = number(snap.get("batt_avg_net_kw"))
    load_avg_kw = number(snap.get("load_avg_kw"))
    charge_limit_kw = number(snap.get("charge_limit_kw"))
    charge_limit_source = str(snap.get("charge_limit_source") or "default_6kw")

    available_kwh = None
    if nominal is not None and nominal > 0 and soc_raw is not None and reserve is not None:
        usable_soc = max(0.0, min(100.0, soc_raw) - reserve)
        available_kwh = max(0.0, nominal * usable_soc / 100.0)

    local_dt = local_datetime(now if now is not None else snap.get("ts"), tz)
    local_hour_value = local_hour(local_dt)
    estimate = estimate_battery(
        soc=soc_raw,
        nominal_kwh=nominal,
        reserve_soc=reserve,
        battery_net_w=batt_net,
        battery_avg_net_w=(avg_net_kw * 1000.0 if avg_net_kw is not None else None),
        battery_avg_kw=average_kw,
        solar_w=solar_value,
        grid_import_w=grid_in,
        grid_export_w=grid_out,
        home_w=load_w,
        load_avg_kw=load_avg_kw,
        now_hour=local_hour_value,
        now_dt=local_dt,
        charge_limit_kw=charge_limit_kw,
        charge_limit_source=charge_limit_source,
    )
    hours_to_empty, hours_to_full = _legacy_hours(estimate)
    estimate = dict(estimate)
    if estimate.get("hours") is not None:
        estimate["hours"] = round(estimate["hours"], 3)
    eta = dict(estimate.get("eta") or {})
    eta["hours"] = estimate.get("hours")
    estimate["eta"] = eta

    if available_kwh is not None:
        available_kwh = round(available_kwh, 2)

    if waking:
        status = {"level": "waking",
                  "label": f"Refreshing · {_age_label(age_s)}" if age_s is not None
                  else "Refreshing"}
    elif stale:
        status = {"level": "stale",
                  "label": f"Stale · {_age_label(age_s)}" if age_s is not None
                  else "Stale"}
    else:
        status = {"level": "live", "label": "Live"}

    solar_state = "producing" if solar_value > ACTIVE_W else "idle"
    return {
        "available": True,
        "stale": stale,
        "age_s": age_s,
        "soc": soc,
        "solar": {**_power(solar_value), "state": solar_state},
        "grid": {**_power(grid_w), "state": grid_state, "label": grid_label},
        "battery": {**_power(batt_w), "state": batt_state, "soc": soc,
                    "label": batt_label,
                    "nominal_capacity_kwh": nominal,
                    "reserve_soc": reserve,
                    "available_kwh": available_kwh,
                    "hours_to_empty": hours_to_empty,
                    "hours_to_full": hours_to_full,
                    "batt_avg_kw": average_kw,
                    "batt_avg_net_kw": avg_net_kw,
                    "load_avg_kw": load_avg_kw,
                    "charge_limit_kw": charge_limit_kw,
                    "charge_limit_source": charge_limit_source,
                    "estimate": estimate},
        "home": {**_power(load_w),
                 "state": "consuming" if load_w > ACTIVE_W else "idle"},
        "flows": {
            "solar": _flow(solar_value),
            "grid_import": _flow(grid_in),
            "grid_export": _flow(grid_out),
            "battery_charge": _flow(batt_charge),
            "battery_discharge": _flow(batt_discharge),
        },
        "status": status,
    }
