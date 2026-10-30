"""Approximate site location for the sunrise/sunset estimate.

The dashboard usually runs on a machine in the house, so its outgoing public
IP is a good first guess at the site's latitude, longitude and IANA timezone.
GeoJS supplies that guess without a key or account.  The result is cached on
disk for 30 days; all daily astronomy then runs locally in :mod:`daylight`
with no network call.

Resolution order:

1. ``SOLAR_LATITUDE``/``SOLAR_LONGITUDE``/``SOLAR_TIMEZONE`` when all three
   are set (for a cloud host, VPN, CGNAT, or a more accurate fix).
2. A fresh cache entry (30-day TTL).
3. One GeoJS lookup.

A failed refresh keeps a valid stale cache and backs off for 24 hours.  With
no cache the resolver returns ``None`` and the caller uses the canonical
07:00-18:00 window.  It never invents coordinates and never raises out of
:meth:`LocationResolver.get`.

The cache holds approximate coordinates.  Logs deliberately name only the
source and failure class, never coordinates, city or IP address.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LOG = logging.getLogger(__name__)

GEOJS_URL = "https://get.geojs.io/v1/ip/geo.json"
CACHE_TTL_S = 30 * 24 * 60 * 60
FAILED_REFRESH_RETRY_S = 24 * 60 * 60
MAX_RESPONSE_BYTES = 64 * 1024
FETCH_TIMEOUT_S = 5.0

_CONFIG_KEYS = ("SOLAR_LATITUDE", "SOLAR_LONGITUDE", "SOLAR_TIMEZONE")
_INVALID_CONFIG = object()


@dataclass(frozen=True)
class Location:
    """One approximate site fix, with the source that produced it."""

    latitude: float
    longitude: float
    timezone: str
    city: str | None = None
    accuracy_km: float | None = None
    source: str = "ip_geojs"


def default_cache_path() -> Path:
    """Default persistent cache, overridable with ``SOLAR_LOCATION_CACHE``."""
    override = os.environ.get("SOLAR_LOCATION_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".anker-tou" / "solar-location.json"


def validate_location(raw, source: str) -> Location:
    """Normalise and validate a GeoJS payload, cache entry or override.

    Raises :class:`ValueError` for a missing/invalid coordinate or timezone.
    """
    if isinstance(raw, Location):
        raw = asdict(raw)
    if not isinstance(raw, dict):
        raise ValueError("location is not an object")
    try:
        latitude = float(raw["latitude"])
        longitude = float(raw["longitude"])
        timezone = str(raw["timezone"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("location is missing coordinates or timezone") from exc

    if not (math.isfinite(latitude) and -90.0 <= latitude <= 90.0):
        raise ValueError("invalid latitude")
    if not (math.isfinite(longitude) and -180.0 <= longitude <= 180.0):
        raise ValueError("invalid longitude")
    if not timezone:
        raise ValueError("empty timezone")
    try:
        ZoneInfo(timezone)
    except Exception as exc:
        raise ValueError("invalid timezone") from exc

    accuracy = raw.get("accuracy_km", raw.get("accuracy"))
    accuracy_km = None
    if accuracy not in (None, ""):
        try:
            accuracy_km = float(accuracy)
        except (TypeError, ValueError):
            accuracy_km = None
        else:
            if not (math.isfinite(accuracy_km) and accuracy_km >= 0.0):
                accuracy_km = None

    city = raw.get("city")
    city = str(city).strip() if city not in (None, "") else None
    return Location(latitude=latitude, longitude=longitude,
                    timezone=timezone, city=city or None,
                    accuracy_km=accuracy_km, source=source)


def fetch_geojs() -> dict:
    """One GeoJS request from this host's outgoing public IP."""
    request = Request(GEOJS_URL,
                      headers={"User-Agent": "anker-tou-solar/1.0"})
    with urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("GeoJS response too large")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GeoJS response is not an object")
    return payload


class LocationResolver:
    """Resolve, cache and refresh the approximate site location.

    ``fetch``, ``now`` and ``environ`` are injectable so tests run entirely
    offline.  ``get()`` is thread-safe within one process.
    """

    def __init__(self, cache_path=None, fetch=None, now=None,
                 ttl_s: float = CACHE_TTL_S,
                 retry_s: float = FAILED_REFRESH_RETRY_S,
                 environ=None):
        self.cache_path = (Path(cache_path) if cache_path is not None
                           else default_cache_path())
        self._fetch = fetch or fetch_geojs
        self._now = now or time.time
        self.ttl_s = float(ttl_s)
        self.retry_s = float(retry_s)
        self._environ = environ if environ is not None else os.environ
        self._lock = threading.Lock()
        self._warned = False
        # In-memory backoff for a failure with no cache to persist.  It keeps
        # an invalid payload from being retried on every dashboard poll while
        # still honouring the "do not cache an invalid response" rule.
        self._no_cache_retry_after = 0.0

    def get(self) -> Location | None:
        """Return the best available location, or ``None`` for the fallback."""
        configured = self._configured()
        if configured is _INVALID_CONFIG:
            return None
        if configured is not None:
            return configured

        with self._lock:
            now = float(self._now())
            cache = self._read_cache()

            if cache is not None:
                location = cache["location"]
                age = now - cache["fetched_at"]
                if (location is not None and 0.0 <= age < self.ttl_s):
                    return location
                if now < cache["retry_after"]:
                    return location
            elif now < self._no_cache_retry_after:
                return None

            try:
                fresh = validate_location(self._fetch(), "ip_geojs")
            except Exception as exc:
                if cache is None:
                    self._no_cache_retry_after = now + self.retry_s
                return self._failed(now, cache, exc)

            self._save_cache({"location": asdict(fresh),
                              "fetched_at": now, "retry_after": 0.0})
            self._warned = False
            self._no_cache_retry_after = 0.0
            return fresh

    # ------------------------------------------------------------ internals

    def _configured(self):
        values = [self._environ.get(key) for key in _CONFIG_KEYS]
        if all(value in (None, "") for value in values):
            return None
        if any(value in (None, "") for value in values):
            self._warn_once("SOLAR_* override is incomplete; using the "
                            "07:00-18:00 fallback")
            return _INVALID_CONFIG
        try:
            return validate_location(
                {"latitude": values[0], "longitude": values[1],
                 "timezone": values[2]}, "configured")
        except ValueError:
            self._warn_once("SOLAR_* override is invalid; using the "
                            "07:00-18:00 fallback")
            return _INVALID_CONFIG

    def _failed(self, now: float, cache, exc: Exception) -> Location | None:
        """Keep a stale cache when possible; otherwise record a backoff."""
        retry_after = now + self.retry_s
        stale = cache["location"] if cache is not None else None
        if stale is not None:
            self._save_cache({"location": asdict(stale),
                              "fetched_at": cache["fetched_at"],
                              "retry_after": retry_after})
            self._warn_once("GeoJS refresh failed (%s); using the cached "
                            "location" % type(exc).__name__)
            return stale

        # A successful fetch with an invalid payload must not be cached: the
        # next request retries rather than hiding a configuration/API change
        # behind a 24-hour backoff.  A transport failure may record one so a
        # persistent outage does not hammer GeoJS on every poll.
        if not isinstance(exc, ValueError):
            self._save_cache({"location": None, "fetched_at": 0.0,
                              "retry_after": retry_after})
        self._warn_once("GeoJS location discovery failed (%s); using the "
                        "07:00-18:00 fallback" % type(exc).__name__)
        return None

    def _read_cache(self):
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return None
            raw_location = raw.get("location")
            location = (validate_location(raw_location, "ip_cache")
                        if raw_location else None)
            fetched_at = float(raw.get("fetched_at") or 0.0)
            retry_after = float(raw.get("retry_after") or 0.0)
            if not math.isfinite(fetched_at) or fetched_at < 0.0:
                return None
            if not math.isfinite(retry_after) or retry_after < 0.0:
                retry_after = 0.0
            return {"location": location, "fetched_at": fetched_at,
                    "retry_after": retry_after}
        except (OSError, ValueError, TypeError):
            return None

    def _save_cache(self, data: dict) -> None:
        """Best-effort atomic 0600 write; never disrupt a valid result."""
        tmp_name = None
        try:
            parent = self.cache_path.parent
            created = not parent.exists()
            parent.mkdir(parents=True, exist_ok=True)
            if created:
                try:
                    os.chmod(parent, 0o700)
                except OSError:
                    pass
            fd, tmp_name = tempfile.mkstemp(
                dir=str(parent), prefix=".solar-location-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.cache_path)
        except OSError as exc:
            LOG.warning("Cannot save solar location cache (%s)",
                        type(exc).__name__)
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            LOG.warning(message)


# --------------------------------------------------------------- module API

_RESOLVER: LocationResolver | None = None


def warm_location() -> Location | None:
    """Resolve once at startup so normal requests do only local astronomy.

    Call before serving.  Repeated calls reuse the resolver and its cache.
    """
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = LocationResolver()
    try:
        return _RESOLVER.get()
    except Exception as exc:  # defensive: a resolver bug must not stop startup
        LOG.warning("Solar location resolver failed (%s); using the fallback",
                    type(exc).__name__)
        return None


def get_location() -> Location | None:
    """Return the warmed location, or ``None``.

    Deliberately inert until :func:`warm_location` has run.  This keeps tests
    and any accidental early request from contacting GeoJS, and it gives the
    server one explicit composition point.
    """
    if _RESOLVER is None:
        return None
    try:
        return _RESOLVER.get()
    except Exception as exc:
        LOG.warning("Solar location lookup failed (%s); using the fallback",
                    type(exc).__name__)
        return None


def reset() -> None:
    """Forget the module-level resolver (test isolation)."""
    global _RESOLVER
    _RESOLVER = None
