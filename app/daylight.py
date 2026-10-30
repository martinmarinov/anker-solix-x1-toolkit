"""Canonical sunrise/sunset for one site-local day.

This module is the single server-side source for the day boundary used by
the solar-card icon, the one-hour dawn/dusk backdrop crossfade, the solar
estimate window and the battery caption's fallback sunset clock.  The front
end receives the window in ``view.daylight`` and only renders it.

The provider calculates the events locally with Astral from approximate site
coordinates supplied by :mod:`solar_location`.  It keeps the app's existing
contract: decimal site-local hours in, ``HH:MM`` strings out.  No network call
happens here and no ISO datetime is exposed, so the browser, the half-sine
solar model and the battery ETA keep their current inputs.

Fallback policy: a missing/invalid date, timezone or coordinate pair, a polar
day/night, a missing Astral install, or any window that fails
``0 <= sunrise < sunset <= 24`` returns the canonical 07:00-18:00 day with
``source="default"``.  The dashboard therefore always has a usable window.

Polar policy: the UI and the half-sine model need a same-day start < end
window, so a polar day or night is displayed and estimated like the canonical
day until an explicit policy is agreed.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

try:  # Astral is optional so a missing package degrades instead of crashing.
    from astral import Observer
    from astral.sun import sunrise as _astral_sunrise
    from astral.sun import sunset as _astral_sunset
    _ASTRAL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the fallback tests
    Observer = None
    _astral_sunrise = None
    _astral_sunset = None
    _ASTRAL_AVAILABLE = False

LOG = logging.getLogger(__name__)

DEFAULT_SUNRISE_HOUR = 7.0
DEFAULT_SUNSET_HOUR = 18.0
PUBLIC_SOURCES = frozenset({"ip", "config", "explicit", "default"})
_MISSING_ASTRAL_LOGGED = False


@dataclass(frozen=True)
class SunTimes:
    """Decimal local hours for sunrise and sunset."""

    sunrise: float
    sunset: float
    source: str = "default"

    @property
    def window_hours(self) -> float:
        return self.sunset - self.sunrise


def astral_available() -> bool:
    """True when the local astronomy package imported successfully."""
    return _ASTRAL_AVAILABLE


def valid_sun_times(values) -> bool:
    """True for a finite window with ``0 <= sunrise < sunset <= 24``."""
    try:
        sunrise = float(values.sunrise)
        sunset = float(values.sunset)
    except (AttributeError, TypeError, ValueError):
        return False
    return (math.isfinite(sunrise) and math.isfinite(sunset)
            and 0.0 <= sunrise < sunset <= 24.0)


def _default_sun_times() -> SunTimes:
    return SunTimes(DEFAULT_SUNRISE_HOUR, DEFAULT_SUNSET_HOUR)


def _public_source(source) -> str:
    value = str(source or "explicit").strip().lower()
    if value in ("ip", "ip_geojs", "ip_cache"):
        return "ip"
    if value in ("config", "configured"):
        return "config"
    if value == "default":
        return "default"
    if value == "explicit":
        return "explicit"
    return "explicit"


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _coordinates(latitude, longitude):
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
        return None
    if not (math.isfinite(lon) and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def _valid_timezone(tz) -> bool:
    if not isinstance(tz, str) or not tz.strip():
        return False
    try:
        ZoneInfo(tz)
    except Exception:
        return False
    return True


def _decimal_hour(value: datetime, tzinfo: ZoneInfo) -> float:
    local = (value.astimezone(tzinfo) if value.tzinfo
             else value.replace(tzinfo=tzinfo))
    seconds = (local.hour * 3600 + local.minute * 60 + local.second
               + local.microsecond / 1_000_000.0)
    return round(seconds / 3600.0, 6)


def _warn_missing_astral() -> None:
    global _MISSING_ASTRAL_LOGGED
    if not _MISSING_ASTRAL_LOGGED:
        _MISSING_ASTRAL_LOGGED = True
        LOG.warning("Astral is not installed; using the 07:00-18:00 daylight "
                    "fallback")


@lru_cache(maxsize=512)
def _sun_times_cached(local_date: date, tz: str, latitude: float,
                      longitude: float, source: str) -> SunTimes:
    if not _ASTRAL_AVAILABLE:
        _warn_missing_astral()
        return _default_sun_times()

    try:
        tzinfo = ZoneInfo(tz)
        observer = Observer(latitude=latitude, longitude=longitude)
        rise = _astral_sunrise(observer, date=local_date, tzinfo=tzinfo)
        set_ = _astral_sunset(observer, date=local_date, tzinfo=tzinfo)
        candidate = SunTimes(_decimal_hour(rise, tzinfo),
                             _decimal_hour(set_, tzinfo),
                             source=source)
    except Exception:
        # Astral raises ValueError for polar day/night.  Treat any calculation
        # failure as "no estimate" so a bad date/coordinate cannot 500 a poll.
        return _default_sun_times()
    return candidate if valid_sun_times(candidate) else _default_sun_times()


def sun_times(local_date: date | None, tz: str = "",
              latitude: float | None = None,
              longitude: float | None = None, *,
              source: str = "explicit") -> SunTimes:
    """Return the sun window for a site-local date and timezone.

    The cache key includes the date, timezone, coordinates and public source
    because ``/api/live`` polls every 5-60 s.  Invalid inputs bypass the cache
    and return the canonical fallback immediately.
    """
    day = _as_date(local_date)
    coords = _coordinates(latitude, longitude)
    if day is None or coords is None or not _valid_timezone(tz):
        return _default_sun_times()
    latitude, longitude = coords
    return _sun_times_cached(day, tz, latitude, longitude,
                             _public_source(source))


def clear_cache() -> None:
    """Clear the per-day calculation cache (tests and location changes)."""
    _sun_times_cached.cache_clear()


def cache_info():
    """Expose the calculation cache statistics for tests/diagnostics."""
    return _sun_times_cached.cache_info()
