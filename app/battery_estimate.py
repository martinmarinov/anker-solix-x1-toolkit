"""Solar-aware battery ETA.

Mode rules (agreed with the user):

* signed ten-minute average battery power decides idle/charge/discharge;
* below 50 W discharge is idle, with no ETA;
* charging with more than 50 W grid import uses the legacy linear formula;
* charging from solar inside the supplied sunrise/sunset window uses the
  solar curve scaled from ten-minute averaged PV, with an instantaneous
  fallback;
* outside that window, or with no usable solar, fall back to linear.

The solar branch subtracts a constant house load and clips the charge at the
runtime inverter limit.  The limit is never inline in the formula; it comes
from the live snapshot (realtime EMS -> device attributes -> 6 kW fallback).
"""
from __future__ import annotations

import math
from datetime import datetime

from eta import clock_at, format_eta_label, local_hour
from limits import FALLBACK_MAX_CHARGE_KW
from solar_forecast import (
    SOLAR_MAX_PEAK_MULTIPLIER,
    SOLAR_MIN_OBS_W,
    SOLAR_RISK_FACTOR,
    energy_kwh,
    format_hour,
    create_solar_curve,
    resolve_solar_window,
)

BATTERY_IDLE_W = 50.0
GRID_MODE_W = 50.0
BATTERY_EFFICIENCY = 0.95
_SOC_EPS = 1e-6

ETA_KINDS = frozenset({"none", "at_full", "at_empty", "to_full", "to_empty",
                       "to_soc"})
PHASES = frozenset({"unavailable", "idle", "full", "discharging", "charging"})
SOURCES = frozenset({"none", "battery", "solar", "grid", "mixed", "unknown"})


def _number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _legacy_clock(now_hour, hours):
    """Time-of-day fallback for callers that have no calendar date."""
    now = _number(now_hour)
    span = _number(hours)
    if now is None or span is None:
        return None
    return format_hour((now + span) % 24.0)


def _result(*, mode, phase, source, rate_kw=None, hours=None, now=None,
            now_hour=None, soc_final=None, solar_end=None, solar=None,
            reason="", eta_kind="none", eta_soc=None, eta_at=None) -> dict:
    """Build every estimate with the same schema and one clock/label rule.

    ``clock`` is the actual target time-of-day.  ``eta.label`` adds the
    calendar hint.  When a caller supplies only ``now_hour`` (no date), the
    clock falls back to the old modulo arithmetic and the label stays bare;
    the live view always supplies ``now``.
    """
    if eta_kind not in ETA_KINDS:
        raise ValueError(f"unknown eta kind: {eta_kind}")
    if phase not in PHASES:
        raise ValueError(f"unknown phase: {phase}")
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")

    span = _number(hours)
    clock = label = None
    if span is not None:
        if now is not None:
            clock = clock_at(now, span)
            label = format_eta_label(now, span)
        else:
            clock = _legacy_clock(now_hour, span)
            label = clock
    rate = _number(rate_kw)
    return {
        "mode": mode,              # legacy formula: solar | grid | charging | ...
        "phase": phase,            # averaged state: the time basis for this ETA
        "source": source,          # where the charge/discharge comes from
        "rate_kw": round(rate, 3) if rate is not None else None,
        "hours": span,
        "clock": clock,            # actual target HH:MM (not modulo 24 h)
        "soc_final": soc_final,
        "solar_end": solar_end,
        "solar": solar,
        "eta": {
            "kind": eta_kind,
            "hours": span,
            "clock": clock,
            "label": label,        # tomorrow / date prefix, server-owned
            "soc": eta_soc,
            "at": eta_at,
        },
        "reason": reason,
    }


def _resolve_rate(battery_avg_net_w, battery_net_w, battery_avg_kw):
    """Return signed battery W; average first, then legacy magnitude+sign."""
    avg = _number(battery_avg_net_w)
    if avg is not None:
        return avg
    legacy = _number(battery_avg_kw)
    instant = _number(battery_net_w)
    if legacy is not None and legacy > 0 and instant is not None:
        if instant > 0:
            return legacy * 1000.0       # signed net: positive is charging
        if instant < 0:
            return -legacy * 1000.0
        return 0.0
    return instant


def _positive_energy(curve, start: float, end: float, load_kw: float) -> float:
    """Integral of max(0, P_solar(t)/1000 - load_kw) over [start, end]."""
    if end <= start:
        return 0.0
    peak_kw = curve.peak_w / 1000.0
    if peak_kw <= 0:
        return 0.0
    span = curve.window_hours
    if load_kw <= 0:
        c1, c2 = curve.start_hour, curve.end_hour
    elif load_kw >= peak_kw:
        return 0.0
    else:
        theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
        c1 = curve.start_hour + span * theta / math.pi
        c2 = curve.end_hour - span * theta / math.pi
    lo = max(start, c1)
    hi = min(end, c2)
    if hi <= lo:
        return 0.0
    gross = energy_kwh(curve, lo, hi)
    return max(0.0, gross - load_kw * (hi - lo))


def _negative_energy(curve, start: float, end: float, load_kw: float) -> float:
    """Integral of max(0, load_kw - P_solar(t)/1000) over [start, end]."""
    if end <= start or load_kw <= 0:
        return 0.0
    gross = energy_kwh(curve, start, end)
    surplus = _positive_energy(curve, start, end, load_kw)
    return max(0.0, load_kw * (end - start) - gross + surplus)


def _crossing_afternoon(curve, load_kw: float) -> float | None:
    peak_kw = curve.peak_w / 1000.0
    if load_kw <= 0:
        return curve.end_hour
    if peak_kw <= load_kw:
        return None
    theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
    span = curve.window_hours
    return curve.end_hour - span * theta / math.pi


def _crossing_morning(curve, load_kw: float) -> float:
    peak_kw = curve.peak_w / 1000.0
    if load_kw <= 0:
        return curve.start_hour
    if peak_kw <= load_kw:
        return curve.end_hour
    theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
    span = curve.window_hours
    return curve.start_hour + span * theta / math.pi


def _solar_estimate(*, curve, t0: float, soc: float, nominal: float,
                    reserve: float, load_kw: float, cap_kw: float,
                    cap_source: str, efficiency: float,
                    observed_rate_kw: float, observed_kw: float,
                    observed_source: str, now=None) -> dict:
    """Build the solar-mode estimate and diagnostics."""
    load_kw = max(0.0, load_kw)
    cap_kw = _number(cap_kw)
    if cap_kw is None or cap_kw <= 0:
        cap_kw = FALLBACK_MAX_CHARGE_KW
        cap_source = "default_6kw"

    def e_plus(end: float, load: float) -> float:
        return _positive_energy(curve, t0, end, load)

    def e_plus_capped(end: float) -> float:
        return max(0.0, e_plus(end, load_kw) - e_plus(end, load_kw + cap_kw))

    q_dc = nominal * max(0.0, 100.0 - soc) / 100.0
    q_ac = q_dc / efficiency
    t2 = _crossing_afternoon(curve, load_kw)
    t1 = _crossing_morning(curve, load_kw)
    remaining = energy_kwh(curve, t0, curve.end_hour)
    peak_kw = curve.peak_w / 1000.0

    if t2 is None or t2 <= t0:
        e_max = 0.0
        uncapped_max = 0.0
    else:
        e_max = e_plus_capped(t2)
        uncapped_max = e_plus(t2, load_kw)

    solar = {
        "as_of": format_hour(t0),
        "remaining_kwh": round(remaining, 2),
        "charge_kwh": round(efficiency * e_max, 2),
        "needed_kwh": round(q_ac, 2),
        "load_kw": round(load_kw, 3),
        "peak_kw": round(peak_kw, 3),
        "observed_kw": round(observed_kw, 3),
        "observed_source": observed_source,
        "risk_factor": curve.risk_factor,
        "reachable": bool(e_max + 1e-6 >= q_ac),
        "capped": bool(e_max + 1e-6 < uncapped_max),
        "charge_limit_kw": round(cap_kw, 3),
        "charge_limit_source": cap_source,
    }

    if solar["reachable"]:
        lo = max(t0, t1)
        hi = t2
        if q_ac <= 0:
            t_full = t0
        else:
            for _ in range(80):
                mid = (lo + hi) / 2.0
                if e_plus_capped(mid) < q_ac:
                    lo = mid
                else:
                    hi = mid
            t_full = (lo + hi) / 2.0
        hours = max(0.0, t_full - t0)
        solar["solar_end"] = None
        return _result(
            mode="solar", phase="charging", source="solar",
            rate_kw=observed_rate_kw, hours=hours, now=now, now_hour=t0,
            solar_end=None, solar=solar, reason="solar_curve",
            eta_kind="to_full")

    e_final_dc = (efficiency * e_plus_capped(curve.end_hour)
                  - _negative_energy(curve, t0, curve.end_hour, load_kw)
                  / efficiency)
    soc_final = _clamp(soc + e_final_dc / nominal * 100.0, reserve, 100.0)
    solar_end = format_hour(curve.end_hour)
    solar["solar_end"] = solar_end
    solar["soc_final"] = round(soc_final, 1)
    return _result(
        mode="solar", phase="charging", source="solar",
        rate_kw=observed_rate_kw, hours=None, now=now, now_hour=t0,
        soc_final=round(soc_final, 1), solar_end=solar_end, solar=solar,
        reason="solar_curve", eta_kind="to_soc",
        eta_soc=round(soc_final, 1), eta_at=solar_end)


def estimate_battery(*, soc, nominal_kwh, reserve_soc,
                     battery_net_w=None, battery_avg_net_w=None,
                     battery_avg_kw=None, solar_w=0, solar_avg_kw=None,
                     grid_import_w=0, grid_export_w=0, home_w=0,
                     load_avg_kw=None, now_hour=None, now_dt=None,
                     charge_limit_kw=None,
                     charge_limit_source="runtime",
                     solar_start_hour=None, solar_end_hour=None,
                     efficiency=BATTERY_EFFICIENCY) -> dict:
    """Return the estimate mode and, when calculable, its duration and clock.

    ``solar_avg_kw`` is the ten-minute time-weighted PV mean.  When present it
    is the single observation for both the 50 W solar gate and the curve;
    otherwise the estimator falls back to instantaneous ``solar_w``.  The
    instantaneous value still feeds the load energy-balance fallback.

    ``now_dt`` supplies the calendar date for the label; ``now_hour`` remains
    the decimal local hour used by the solar-curve window.  ``solar_start_hour``
    and ``solar_end_hour`` are the site's sunrise/sunset; a missing or invalid
    pair falls back to 07:00-18:00.  This is the only averaged time estimate in
    the view model; every other battery field stays instantaneous.
    """
    now = now_dt if isinstance(now_dt, datetime) else None
    hour = _number(now_hour)
    if hour is None and now is not None:
        hour = local_hour(now)

    solar_start_hour, solar_end_hour = resolve_solar_window(
        solar_start_hour, solar_end_hour)

    nominal = _number(nominal_kwh)
    soc_raw = _number(soc)
    reserve = _number(reserve_soc)
    if nominal is None or nominal <= 0 or soc_raw is None or reserve is None:
        return _result(mode="unavailable", phase="unavailable", source="none",
                       now=now, now_hour=hour, reason="missing_battery_data")

    soc = _clamp(soc_raw, 0.0, 100.0)
    reserve = _clamp(reserve, 0.0, 100.0)
    available_kwh = max(0.0, nominal * (soc - reserve) / 100.0)
    deficit = max(0.0, nominal - available_kwh)

    b_w = _resolve_rate(battery_avg_net_w, battery_net_w, battery_avg_kw)
    if b_w is None:
        return _result(mode="unavailable", phase="unavailable", source="none",
                       now=now, now_hour=hour, reason="no_battery_rate")
    b_w = float(b_w)
    rate_kw = abs(b_w) / 1000.0

    if soc >= 100.0 - _SOC_EPS and b_w >= 0:
        return _result(mode="full", phase="full", source="none",
                       rate_kw=0.0, hours=0.0, now=now, now_hour=hour,
                       soc_final=100.0, reason="full", eta_kind="at_full")

    if b_w <= -BATTERY_IDLE_W:
        hours = (available_kwh * efficiency / rate_kw) if rate_kw > 0 else None
        kind = "at_empty" if available_kwh <= _SOC_EPS else "to_empty"
        if kind == "at_empty":
            hours = 0.0
        return _result(mode="discharging", phase="discharging",
                       source="battery", rate_kw=rate_kw, hours=hours,
                       now=now, now_hour=hour, reason="discharging",
                       eta_kind=kind if hours is not None else "none")

    if b_w < 0 or abs(b_w) < 1e-9:
        return _result(mode="idle", phase="idle", source="none",
                       rate_kw=rate_kw, hours=None, now=now, now_hour=hour,
                       reason="idle_below_50w")

    # b_w > 0: charging.  The averaged rate is the ETA input; the averaged PV
    # (instant fallback) scales the curve, while the instantaneous grid
    # readings still choose the formula.
    solar_instant = max(0.0, _number(solar_w) or 0.0)
    solar_avg = _number(solar_avg_kw)
    if solar_avg is None:
        solar_obs_w = solar_instant
        observed_source = "instant"
    else:
        solar_obs_w = max(0.0, solar_avg) * 1000.0
        observed_source = "avg"
    grid_in = max(0.0, _number(grid_import_w) or 0.0)
    grid_out = max(0.0, _number(grid_export_w) or 0.0)
    solar_source = "solar" if solar_obs_w > SOLAR_MIN_OBS_W else "none"

    if grid_in > GRID_MODE_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return _result(mode="grid", phase="charging", source="grid",
                       rate_kw=rate_kw, hours=hours, now=now, now_hour=hour,
                       reason="grid_priority",
                       eta_kind="to_full" if hours is not None else "none")

    in_window = (hour is not None
                 and solar_start_hour < hour < solar_end_hour)
    if not in_window:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return _result(mode="charging", phase="charging", source=solar_source,
                       rate_kw=rate_kw, hours=hours, now=now, now_hour=hour,
                       reason="outside_solar_window",
                       eta_kind="to_full" if hours is not None else "none")

    if solar_obs_w <= SOLAR_MIN_OBS_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return _result(mode="charging", phase="charging", source="none",
                       rate_kw=rate_kw, hours=hours, now=now, now_hour=hour,
                       reason="no_usable_solar",
                       eta_kind="to_full" if hours is not None else "none")

    if grid_out > GRID_MODE_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return _result(mode="charging", phase="charging", source="solar",
                       rate_kw=rate_kw, hours=hours, now=now, now_hour=hour,
                       reason="exporting_surplus",
                       eta_kind="to_full" if hours is not None else "none")

    load_kw = _number(load_avg_kw)
    if load_kw is None:
        load_kw = (solar_instant + grid_in - grid_out - max(0.0, b_w)) / 1000.0
    load_kw = max(0.0, load_kw)

    try:
        curve = create_solar_curve(
            solar_obs_w, hour,
            risk_factor=SOLAR_RISK_FACTOR,
            max_peak_multiplier=SOLAR_MAX_PEAK_MULTIPLIER,
            start_hour=solar_start_hour, end_hour=solar_end_hour)
    except ValueError:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return _result(mode="charging", phase="charging", source="solar",
                       rate_kw=rate_kw, hours=hours, now=now, now_hour=hour,
                       reason="invalid_solar_observation",
                       eta_kind="to_full" if hours is not None else "none")

    cap = _number(charge_limit_kw)
    if cap is None or cap <= 0:
        cap = FALLBACK_MAX_CHARGE_KW
        charge_limit_source = "default_6kw"
    return _solar_estimate(
        curve=curve, t0=hour, soc=soc, nominal=nominal, reserve=reserve,
        load_kw=load_kw, cap_kw=cap, cap_source=charge_limit_source,
        efficiency=efficiency, observed_rate_kw=rate_kw,
        observed_kw=solar_obs_w / 1000.0, observed_source=observed_source,
        now=now)
