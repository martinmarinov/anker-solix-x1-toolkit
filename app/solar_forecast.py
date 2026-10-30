"""Portable solar forecast scaled from one PV observation.

This is the model attached to the solar-ETA plan: a symmetric half-sine
between the sunrise and sunset hours supplied by the caller (default
07:00-18:00), scaled from one observed PV power.  The observation may be an
instantaneous reading or a ten-minute mean; the caller passes it at ``t0``
and the model treats it as the value there.

    S      = start_hour, E = end_hour, W = E - S
    s(t)   = sin(pi * (t - S) / W)                    inside S..E
    P_peak = r * min(P_obs / s(t0), M * P_obs)        r = 1.0 expected,
                                                      r = 0.5 low, M = 2.5
    P(t)   = P_peak * s(t)

``S`` and ``E`` normally come from :mod:`daylight`; a missing or invalid pair
falls back to the canonical 07:00-18:00 window.  Once a curve exists, every
energy helper reads the window stored on that curve.

The battery estimator builds two curves from one observation: ``r = 1.0``
anchors the expected curve at the observation, and ``r = 0.5`` keeps the
original conservative curve as the low case.

The module is pure: no Anker, no clock, no I/O.  Callers pass decimal local
hours.  Hour 10.5 means 10:30.
"""
from __future__ import annotations

import math

from daylight import DEFAULT_SUNRISE_HOUR, DEFAULT_SUNSET_HOUR

SOLAR_START_HOUR = DEFAULT_SUNRISE_HOUR
SOLAR_END_HOUR = DEFAULT_SUNSET_HOUR
SOLAR_WINDOW_HOURS = SOLAR_END_HOUR - SOLAR_START_HOUR
SOLAR_EXPECTED_RISK_FACTOR = 1.0
SOLAR_RISK_FACTOR = 0.5
SOLAR_MAX_PEAK_MULTIPLIER = 2.5
SOLAR_MIN_OBS_W = 50.0
_EPS = 1e-9


class SolarCurve:
    """One scaled half-sine over its own sunrise/sunset window."""

    __slots__ = ("peak_w", "observed_w", "observed_hour", "risk_factor",
                 "start_hour", "end_hour")

    def __init__(self, peak_w: float, observed_w: float, observed_hour: float,
                 risk_factor: float,
                 start_hour: float = SOLAR_START_HOUR,
                 end_hour: float = SOLAR_END_HOUR):
        self.peak_w = float(peak_w)
        self.observed_w = float(observed_w)
        self.observed_hour = float(observed_hour)
        self.risk_factor = float(risk_factor)
        self.start_hour = float(start_hour)
        self.end_hour = float(end_hour)

    @property
    def window_hours(self) -> float:
        return self.end_hour - self.start_hour

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"SolarCurve(peak_w={self.peak_w:.3f}, "
                f"observed_w={self.observed_w:.1f}, "
                f"observed_hour={self.observed_hour:.3f}, "
                f"window={self.start_hour:.2f}-{self.end_hour:.2f})")


def _finite(value, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def resolve_solar_window(start_hour=None, end_hour=None) -> tuple[float, float]:
    """Return a valid window, falling back to the canonical defaults.

    Both bounds must be supplied and valid together; a missing or malformed
    pair falls back to 07:00-18:00.  This is the seam used by direct
    ``estimate_battery`` callers and tests before a curve exists.
    """
    if start_hour is None or end_hour is None:
        return SOLAR_START_HOUR, SOLAR_END_HOUR
    try:
        start = float(start_hour)
        end = float(end_hour)
    except (TypeError, ValueError):
        return SOLAR_START_HOUR, SOLAR_END_HOUR
    if (math.isfinite(start) and math.isfinite(end)
            and 0.0 <= start < end <= 24.0):
        return start, end
    return SOLAR_START_HOUR, SOLAR_END_HOUR


def shape(hour: float, start_hour: float = SOLAR_START_HOUR,
          end_hour: float = SOLAR_END_HOUR) -> float:
    """Unit-height half-sine at a decimal local hour (0 outside the window)."""
    hour = _finite(hour, "hour")
    start = _finite(start_hour, "start_hour")
    end = _finite(end_hour, "end_hour")
    if not 0.0 <= start < end <= 24.0:
        raise ValueError("start_hour must be before end_hour within one day")
    if hour <= start or hour >= end:
        return 0.0
    return max(0.0, math.sin(math.pi * (hour - start) / (end - start)))


def create_solar_curve(power_w: float, hour: float,
                       risk_factor: float = SOLAR_RISK_FACTOR,
                       max_peak_multiplier: float = SOLAR_MAX_PEAK_MULTIPLIER,
                       start_hour: float = SOLAR_START_HOUR,
                       end_hour: float = SOLAR_END_HOUR) -> SolarCurve:
    """Scale one PV observation into a curve.

    ``power_w`` is either an instantaneous reading or a window mean; the
    estimator passes the ten-minute mean when it has one.  The observation
    must be strictly inside the supplied daylight window; at the boundaries
    the shape is zero and a peak cannot be derived.  The cap is applied
    before the risk factor, exactly as in the document.
    """
    power = _finite(power_w, "power_w")
    hour = _finite(hour, "hour")
    risk = _finite(risk_factor, "risk_factor")
    multiplier = _finite(max_peak_multiplier, "max_peak_multiplier")
    start = _finite(start_hour, "start_hour")
    end = _finite(end_hour, "end_hour")
    if not 0.0 <= start < end <= 24.0:
        raise ValueError("start_hour must be before end_hour within one day")
    if power <= 0:
        raise ValueError("power_w must be positive")
    if risk <= 0:
        raise ValueError("risk_factor must be positive")
    if multiplier <= 0:
        raise ValueError("max_peak_multiplier must be positive")
    if not start < hour < end:
        raise ValueError(
            f"hour must be inside the {format_hour(start)}-{format_hour(end)} window")

    unit = shape(hour, start, end)
    if unit <= _EPS:
        raise ValueError("hour is too close to a window boundary")

    raw_peak = power / unit
    peak = risk * min(raw_peak, multiplier * power)
    return SolarCurve(peak, power, hour, risk, start, end)


def solar_power_w(curve: SolarCurve, hour: float) -> float:
    """Forecast PV power in W at a decimal local hour."""
    return curve.peak_w * shape(hour, curve.start_hour, curve.end_hour)


def energy_kwh(curve: SolarCurve, start_hour: float, end_hour: float) -> float:
    """Exact integral of the forecast between two decimal local hours."""
    start = _finite(start_hour, "start_hour")
    end = _finite(end_hour, "end_hour")
    lo = max(curve.start_hour, min(start, end))
    hi = min(curve.end_hour, max(start, end))
    if hi <= lo:
        return 0.0
    span = curve.window_hours
    scale = curve.peak_w * span / (1000.0 * math.pi)
    a = math.pi * (lo - curve.start_hour) / span
    b = math.pi * (hi - curve.start_hour) / span
    return max(0.0, scale * (math.cos(a) - math.cos(b)))


def total_daily_solar_kwh(curve: SolarCurve) -> float:
    return energy_kwh(curve, curve.start_hour, curve.end_hour)


def remaining_solar_kwh(curve: SolarCurve, hour: float) -> float:
    return energy_kwh(curve, hour, curve.end_hour)


def _bisect_hour(curve: SolarCurve, start: float, target_kwh: float) -> float:
    """Find the hour at which the remaining energy reaches target_kwh."""
    lo, hi = start, curve.end_hour
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
