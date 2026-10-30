"""Clock and calendar labels for a battery estimate.

The estimate is calculated from averaged power in :mod:`battery_estimate`;
this module owns only the answer to "when?".  Keeping the calendar rules
here means every branch of the estimator gets the same label and the browser
does not need a second day-aware formatter.

Rules:

* the time is the *actual* target time in the site timezone, never modulo 24 h;
* under 12 hours, print only ``HH:MM`` even when the target crosses midnight;
* 12 hours or more, print ``tomorrow HH:MM`` for the next calendar day;
* two or more days out, print ``Sat 04 Jul HH:MM`` (plus the year when it
  differs from now);
* an aware ``now`` is advanced by absolute seconds, so DST transitions keep
  the correct local wall time.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from solar_forecast import format_hour

try:  # Python 3.9+; the Docker image may not ship a full tz database.
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - only on very old Python
    ZoneInfo = None

ETA_SHORT_HOURS = 12.0

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def local_datetime(epoch, tz: str = "") -> datetime | None:
    """Epoch seconds -> a datetime in the site timezone.

    An unknown or unavailable timezone falls back to the host timezone,
    matching the old ``home_view._local_hour`` behaviour.
    """
    ts = _number(epoch)
    if ts is None:
        return None
    tzinfo = None
    if tz and ZoneInfo is not None:
        try:
            tzinfo = ZoneInfo(tz)
        except Exception:
            tzinfo = None
    try:
        return datetime.fromtimestamp(ts, tzinfo)
    except (OverflowError, OSError, ValueError):
        return None


def local_hour(value: datetime | None) -> float | None:
    """Decimal local hour for a datetime (10:30 -> 10.5)."""
    if not isinstance(value, datetime):
        return None
    return (value.hour + value.minute / 60.0 + value.second / 3600.0
            + value.microsecond / 3600000000.0)


def target_datetime(now: datetime | None, hours) -> datetime | None:
    """Advance ``now`` by ``hours``, DST-safely for aware datetimes."""
    if not isinstance(now, datetime):
        return None
    value = _number(hours)
    if value is None:
        return None
    value = max(0.0, value)
    if now.tzinfo is not None:
        try:
            return datetime.fromtimestamp(now.timestamp() + value * 3600.0,
                                          now.tzinfo)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        return now + timedelta(hours=value)
    except (OverflowError, ValueError):
        return None


def clock_at(now: datetime | None, hours) -> str | None:
    """Actual target time as ``HH:MM`` in the timezone of ``now``."""
    if not isinstance(now, datetime):
        return None
    target = target_datetime(now, hours)
    if target is None:
        return None
    hour = local_hour(target)
    if hour is None:
        return None
    text = format_hour(hour)
    # format_hour keeps 24:00 for solar end times; a clock target is midnight.
    return "00:00" if text == "24:00" else text


def format_eta_label(now: datetime | None, hours) -> str | None:
    """``15:09`` | ``tomorrow 16:30`` | ``Sat 04 Jul 10:30`` | ``... 2027 05:00``."""
    if not isinstance(now, datetime):
        return None
    value = _number(hours)
    if value is None:
        return None
    target = target_datetime(now, value)
    if target is None:
        return None
    time_text = clock_at(now, value)
    if time_text is None:
        return None
    value = max(0.0, value)
    if value < ETA_SHORT_HOURS:
        return time_text
    days = (target.date() - now.date()).days
    if days <= 0:
        return time_text
    if days == 1:
        return "tomorrow " + time_text
    date_text = (f"{_WEEKDAYS[target.weekday()]} {target.day:02d} "
                 f"{_MONTHS[target.month - 1]}")
    if target.year != now.year:
        date_text += f" {target.year}"
    return date_text + " " + time_text
