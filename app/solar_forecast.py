"""Portable solar forecast scaled from one PV observation.

This is the model attached to the solar-ETA plan: a symmetric half-sine
between 07:00 and 18:00, peak at 12:30, scaled from one observed PV power.
The observation may be an instantaneous reading or a ten-minute mean; the
caller passes it at ``t0`` and the model treats it as the value there.

    s(t)   = sin(pi * (t - 7) / 11)                   inside 07:00..18:00
    P_peak = r * min(P_obs / s(t0), M * P_obs)        r = 0.5, M = 2.5
    P(t)   = P_peak * s(t)

The module is pure: no Anker, no clock, no I/O.  Callers pass decimal local
hours.  Hour 10.5 means 10:30.
"""
from __future__ import annotations

import math

SOLAR_START_HOUR = 7.0
SOLAR_END_HOUR = 18.0
SOLAR_WINDOW_HOURS = SOLAR_END_HOUR - SOLAR_START_HOUR
SOLAR_RISK_FACTOR = 0.5
SOLAR_MAX_PEAK_MULTIPLIER = 2.5
SOLAR_MIN_OBS_W = 50.0
_EPS = 1e-9


class SolarCurve:
    """One scaled half-sine, defined only by its peak and observation."""

    __slots__ = ("peak_w", "observed_w", "observed_hour", "risk_factor")

    def __init__(self, peak_w: float, observed_w: float, observed_hour: float,
                 risk_factor: float):
        self.peak_w = float(peak_w)
        self.observed_w = float(observed_w)
        self.observed_hour = float(observed_hour)
        self.risk_factor = float(risk_factor)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"SolarCurve(peak_w={self.peak_w:.3f}, "
                f"observed_w={self.observed_w:.1f}, "
                f"observed_hour={self.observed_hour:.3f})")


def _finite(value, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def shape(hour: float) -> float:
    """Unit-height half-sine at a decimal local hour (0 outside the window)."""
    hour = _finite(hour, "hour")
    if hour <= SOLAR_START_HOUR or hour >= SOLAR_END_HOUR:
        return 0.0
    return max(0.0, math.sin(math.pi * (hour - SOLAR_START_HOUR)
                            / SOLAR_WINDOW_HOURS))


def create_solar_curve(power_w: float, hour: float,
                       risk_factor: float = SOLAR_RISK_FACTOR,
                       max_peak_multiplier: float = SOLAR_MAX_PEAK_MULTIPLIER
                       ) -> SolarCurve:
    """Scale one PV observation into a curve.

    ``power_w`` is either an instantaneous reading or a window mean; the
    estimator passes the ten-minute mean when it has one.  The observation
    must be strictly inside the daylight window; at the boundaries the shape
    is zero and a peak cannot be derived.  The cap is applied before the risk
    factor, exactly as in the document.
    """
    power = _finite(power_w, "power_w")
    hour = _finite(hour, "hour")
    risk = _finite(risk_factor, "risk_factor")
    multiplier = _finite(max_peak_multiplier, "max_peak_multiplier")
    if power <= 0:
        raise ValueError("power_w must be positive")
    if risk <= 0:
        raise ValueError("risk_factor must be positive")
    if multiplier <= 0:
        raise ValueError("max_peak_multiplier must be positive")
    if not SOLAR_START_HOUR < hour < SOLAR_END_HOUR:
        raise ValueError("hour must be inside the 07:00-18:00 window")

    unit = shape(hour)
    if unit <= _EPS:
        raise ValueError("hour is too close to a window boundary")

    raw_peak = power / unit
    peak = risk * min(raw_peak, multiplier * power)
    return SolarCurve(peak, power, hour, risk)


def solar_power_w(curve: SolarCurve, hour: float) -> float:
    """Forecast PV power in W at a decimal local hour."""
    return curve.peak_w * shape(hour)


def energy_kwh(curve: SolarCurve, start_hour: float, end_hour: float) -> float:
    """Exact integral of the forecast between two decimal local hours."""
    start = _finite(start_hour, "start_hour")
    end = _finite(end_hour, "end_hour")
    lo = max(SOLAR_START_HOUR, min(start, end))
    hi = min(SOLAR_END_HOUR, max(start, end))
    if hi <= lo:
        return 0.0
    scale = curve.peak_w * SOLAR_WINDOW_HOURS / (1000.0 * math.pi)
    a = math.pi * (lo - SOLAR_START_HOUR) / SOLAR_WINDOW_HOURS
    b = math.pi * (hi - SOLAR_START_HOUR) / SOLAR_WINDOW_HOURS
    return max(0.0, scale * (math.cos(a) - math.cos(b)))


def total_daily_solar_kwh(curve: SolarCurve) -> float:
    return energy_kwh(curve, SOLAR_START_HOUR, SOLAR_END_HOUR)


def remaining_solar_kwh(curve: SolarCurve, hour: float) -> float:
    return energy_kwh(curve, hour, SOLAR_END_HOUR)


def _bisect_hour(curve: SolarCurve, start: float, target_kwh: float) -> float:
    """Find the hour at which the remaining energy reaches target_kwh."""
    lo, hi = start, SOLAR_END_HOUR
    for _ in range(70):
        mid = (lo + hi) / 2.0
        if energy_kwh(curve, start, mid) < target_kwh:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def time_to_generate_kwh(curve: SolarCurve, target_kwh: float,
                         from_hour: float) -> float:
    """Hours needed to generate target_kwh of gross PV from from_hour.

    Returns ``math.inf`` when the remaining curve cannot deliver the target.
    This is the no-load helper from the document; the battery estimator adds
    house load and the charge cap separately.
    """
    target = _finite(target_kwh, "target_kwh")
    start = _finite(from_hour, "from_hour")
    if target <= 0:
        return 0.0
    remaining = remaining_solar_kwh(curve, start)
    if target >= remaining - 1e-6:
        return math.inf
    when = _bisect_hour(curve, start, target)
    return max(0.0, when - start)


def format_hour(hour: float) -> str:
    """Format a decimal hour as 24-hour HH:MM (24:00 stays 24:00)."""
    value = _finite(hour, "hour")
    total = int(round(value * 60.0))
    total = max(0, min(24 * 60, total))
    h, m = divmod(total, 60)
    return f"{h:02d}:{m:02d}"
