"""Solar-aware battery ETA.

Mode rules (agreed with the user):

* signed ten-minute average battery power decides idle/charge/discharge;
* below 50 W discharge is idle, with no ETA;
* charging with more than 50 W grid import uses the legacy linear formula;
* charging from solar inside 07:00-18:00 uses the single-reading solar curve;
* outside that window, or with no usable solar, fall back to linear.

The solar branch subtracts a constant house load and clips the charge at the
runtime inverter limit.  The limit is never inline in the formula; it comes
from the live snapshot (realtime EMS -> device attributes -> 6 kW fallback).
"""
from __future__ import annotations

import math

from limits import FALLBACK_MAX_CHARGE_KW
from solar_forecast import (
    SOLAR_END_HOUR,
    SOLAR_MAX_PEAK_MULTIPLIER,
    SOLAR_MIN_OBS_W,
    SOLAR_RISK_FACTOR,
    SOLAR_START_HOUR,
    energy_kwh,
    format_hour,
    create_solar_curve,
)

BATTERY_IDLE_W = 50.0
GRID_MODE_W = 50.0
BATTERY_EFFICIENCY = 0.95
_SOC_EPS = 1e-6


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


def _clock_after(now_hour: float | None, hours: float | None) -> str | None:
    if now_hour is None or hours is None or not math.isfinite(hours):
        return None
    return format_hour((now_hour + hours) % 24.0)


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
    if load_kw <= 0:
        c1, c2 = SOLAR_START_HOUR, SOLAR_END_HOUR
    elif load_kw >= peak_kw:
        return 0.0
    else:
        theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
        span = SOLAR_END_HOUR - SOLAR_START_HOUR
        c1 = SOLAR_START_HOUR + span * theta / math.pi
        c2 = SOLAR_END_HOUR - span * theta / math.pi
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
        return SOLAR_END_HOUR
    if peak_kw <= load_kw:
        return None
    theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
    span = SOLAR_END_HOUR - SOLAR_START_HOUR
    return SOLAR_END_HOUR - span * theta / math.pi


def _crossing_morning(curve, load_kw: float) -> float:
    peak_kw = curve.peak_w / 1000.0
    if load_kw <= 0:
        return SOLAR_START_HOUR
    if peak_kw <= load_kw:
        return SOLAR_END_HOUR
    theta = math.asin(_clamp(load_kw / peak_kw, 0.0, 1.0))
    span = SOLAR_END_HOUR - SOLAR_START_HOUR
    return SOLAR_START_HOUR + span * theta / math.pi


def _solar_estimate(*, curve, t0: float, soc: float, nominal: float,
                    reserve: float, load_kw: float, cap_kw: float,
                    cap_source: str, efficiency: float,
                    observed_rate_kw: float) -> dict:
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
    remaining = energy_kwh(curve, t0, SOLAR_END_HOUR)
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
        return {
            "mode": "solar",
            "rate_kw": round(observed_rate_kw, 3),
            "hours": hours,
            "clock": _clock_after(t0, hours),
            "soc_final": None,
            "solar_end": None,
            "solar": solar,
        }

    e_final_dc = (efficiency * e_plus_capped(SOLAR_END_HOUR)
                  - _negative_energy(curve, t0, SOLAR_END_HOUR, load_kw)
                  / efficiency)
    soc_final = _clamp(soc + e_final_dc / nominal * 100.0, reserve, 100.0)
    solar["solar_end"] = format_hour(SOLAR_END_HOUR)
    solar["soc_final"] = round(soc_final, 1)
    return {
        "mode": "solar",
        "rate_kw": round(observed_rate_kw, 3),
        "hours": None,
        "clock": None,
        "soc_final": round(soc_final, 1),
        "solar_end": format_hour(SOLAR_END_HOUR),
        "solar": solar,
    }


def estimate_battery(*, soc, nominal_kwh, reserve_soc,
                     battery_net_w=None, battery_avg_net_w=None,
                     battery_avg_kw=None, solar_w=0, grid_import_w=0,
                     grid_export_w=0, home_w=0, load_avg_kw=None,
                     now_hour=None, charge_limit_kw=None,
                     charge_limit_source="runtime",
                     efficiency=BATTERY_EFFICIENCY) -> dict:
    """Return the estimate mode and, when calculable, its duration and clock."""
    nominal = _number(nominal_kwh)
    soc_raw = _number(soc)
    reserve = _number(reserve_soc)
    if nominal is None or nominal <= 0 or soc_raw is None or reserve is None:
        return {"mode": "unavailable", "rate_kw": None, "hours": None,
                "clock": None, "soc_final": None, "solar_end": None,
                "solar": None, "reason": "missing_battery_data"}

    soc = _clamp(soc_raw, 0.0, 100.0)
    reserve = _clamp(reserve, 0.0, 100.0)
    available_kwh = max(0.0, nominal * (soc - reserve) / 100.0)
    deficit = max(0.0, nominal - available_kwh)

    b_w = _resolve_rate(battery_avg_net_w, battery_net_w, battery_avg_kw)
    if b_w is None:
        return {"mode": "unavailable", "rate_kw": None, "hours": None,
                "clock": None, "soc_final": None, "solar_end": None,
                "solar": None, "reason": "no_battery_rate"}
    b_w = float(b_w)
    rate_kw = abs(b_w) / 1000.0
    now = _number(now_hour)

    if soc >= 100.0 - _SOC_EPS and b_w >= 0:
        return {"mode": "full", "rate_kw": 0.0, "hours": 0.0,
                "clock": format_hour(now) if now is not None else None,
                "soc_final": 100.0, "solar_end": None, "solar": None,
                "reason": "full"}

    if b_w <= -BATTERY_IDLE_W:
        hours = (available_kwh * efficiency / rate_kw) if rate_kw > 0 else None
        return {"mode": "discharging", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "discharging"}

    if b_w < 0 or abs(b_w) < 1e-9:
        return {"mode": "idle", "rate_kw": round(rate_kw, 3), "hours": None,
                "clock": None, "soc_final": None, "solar_end": None,
                "solar": None, "reason": "idle_below_50w"}

    # b_w > 0: charging.
    solar_value = max(0.0, _number(solar_w) or 0.0)
    grid_in = max(0.0, _number(grid_import_w) or 0.0)
    grid_out = max(0.0, _number(grid_export_w) or 0.0)
    home_value = max(0.0, _number(home_w) or 0.0)

    if grid_in > GRID_MODE_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return {"mode": "grid", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "grid_priority"}

    in_window = (now is not None
                 and SOLAR_START_HOUR < now < SOLAR_END_HOUR)
    if not in_window:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return {"mode": "charging", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "outside_solar_window"}

    if solar_value <= SOLAR_MIN_OBS_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return {"mode": "charging", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "no_usable_solar"}

    if grid_out > GRID_MODE_W:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return {"mode": "charging", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "exporting_surplus"}

    load_kw = _number(load_avg_kw)
    if load_kw is None:
        load_kw = (solar_value + grid_in - grid_out - max(0.0, b_w)) / 1000.0
    load_kw = max(0.0, load_kw)

    try:
        curve = create_solar_curve(
            solar_value, now,
            risk_factor=SOLAR_RISK_FACTOR,
            max_peak_multiplier=SOLAR_MAX_PEAK_MULTIPLIER)
    except ValueError:
        hours = deficit / (rate_kw * efficiency) if rate_kw > 0 else None
        return {"mode": "charging", "rate_kw": round(rate_kw, 3),
                "hours": hours,
                "clock": _clock_after(now, hours),
                "soc_final": None, "solar_end": None, "solar": None,
                "reason": "invalid_solar_observation"}

    cap = _number(charge_limit_kw)
    if cap is None or cap <= 0:
        cap = FALLBACK_MAX_CHARGE_KW
        charge_limit_source = "default_6kw"
    result = _solar_estimate(
        curve=curve, t0=now, soc=soc, nominal=nominal, reserve=reserve,
        load_kw=load_kw, cap_kw=cap, cap_source=charge_limit_source,
        efficiency=efficiency, observed_rate_kw=rate_kw)
    result["reason"] = "solar_curve"
    return result
