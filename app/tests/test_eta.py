"""Tests for the single ETA clock/date formatter."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eta import (  # noqa: E402
    clock_at,
    format_eta_label,
    local_datetime,
    local_hour,
    target_datetime,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


class ClockTests(unittest.TestCase):
    def test_same_day_keeps_the_clock_only(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(clock_at(now, 4.653), "15:09")
        self.assertEqual(format_eta_label(now, 4.653), "15:09")

    def test_short_cross_midnight_prints_only_the_time(self):
        now = datetime(2026, 7, 1, 23, 0)
        self.assertEqual(clock_at(now, 8), "07:00")
        self.assertEqual(format_eta_label(now, 8), "07:00")

    def test_twelve_hour_boundary_is_not_short(self):
        now = datetime(2026, 7, 1, 23, 0)
        self.assertEqual(format_eta_label(now, 11.99), "10:59")
        self.assertEqual(format_eta_label(now, 12.0), "tomorrow 11:00")

    def test_thirteen_hours_crossing_midnight_says_tomorrow(self):
        now = datetime(2026, 7, 1, 23, 0)
        self.assertEqual(clock_at(now, 13), "12:00")
        self.assertEqual(format_eta_label(now, 13), "tomorrow 12:00")

    def test_thirty_hours_says_tomorrow(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(clock_at(now, 30), "16:30")
        self.assertEqual(format_eta_label(now, 30), "tomorrow 16:30")

    def test_two_days_out_uses_the_calendar_date(self):
        now = datetime(2026, 10, 30, 19, 35)
        self.assertEqual(clock_at(now, 37.05), "08:38")
        self.assertEqual(format_eta_label(now, 37.05),
                         "Sun 01 Nov 08:38")

    def test_twenty_five_hours_from_late_evening_is_two_days_out(self):
        now = datetime(2026, 7, 1, 23, 0)
        self.assertEqual(format_eta_label(now, 25), "Fri 03 Jul 00:00")

    def test_two_day_rounding_matches_the_working_example(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(clock_at(now, 64.125), "02:38")
        self.assertEqual(format_eta_label(now, 64.125),
                         "Sat 04 Jul 02:38")

    def test_exact_day_boundaries(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(format_eta_label(now, 24), "tomorrow 10:30")
        self.assertEqual(format_eta_label(now, 48), "Fri 03 Jul 10:30")

    def test_year_is_added_only_when_it_differs(self):
        now = datetime(2026, 12, 31, 23, 0)
        self.assertEqual(format_eta_label(now, 13), "tomorrow 12:00")
        self.assertEqual(format_eta_label(now, 25),
                         "Sat 02 Jan 2027 00:00")

    def test_rounding_matches_the_old_clock_formatter(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(clock_at(now, 4.275), "14:46")

    def test_midnight_rounds_to_zero_not_twenty_four(self):
        now = datetime(2026, 7, 1, 23, 59, 45)
        self.assertEqual(clock_at(now, 0), "00:00")

    def test_missing_and_non_finite_values_have_no_label(self):
        now = datetime(2026, 7, 1, 10, 30)
        for value in (None, float("nan"), float("inf"), float("-inf"), "x"):
            self.assertIsNone(clock_at(now, value), value)
            self.assertIsNone(format_eta_label(now, value), value)
        self.assertIsNone(clock_at(None, 4))
        self.assertIsNone(format_eta_label(None, 4))

    def test_target_rejects_non_datetimes(self):
        self.assertIsNone(target_datetime(None, 4))
        self.assertIsNone(target_datetime("2026-07-01", 4))


class TimezoneTests(unittest.TestCase):
    def test_local_datetime_uses_the_site_timezone(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        value = local_datetime(ts, "Europe/London")
        self.assertEqual(value.hour, 10)
        self.assertEqual(value.minute, 30)
        self.assertEqual(local_hour(value), 10.5)

    def test_unknown_timezone_falls_back_to_the_host(self):
        ts = 0
        self.assertEqual(local_datetime(ts, "Nowhere/Nope"),
                         datetime.fromtimestamp(ts))

    def test_invalid_epoch_returns_none(self):
        for value in (None, "bad", float("nan"), float("inf")):
            self.assertIsNone(local_datetime(value, "Europe/London"), value)

    def test_naive_datetime_is_advanced_by_wall_time(self):
        now = datetime(2026, 7, 1, 10, 30)
        self.assertEqual(target_datetime(now, 30),
                         datetime(2026, 7, 2, 16, 30))

    @unittest.skipIf(ZoneInfo is None, "zoneinfo is unavailable")
    def test_dst_spring_forward_uses_absolute_seconds(self):
        tz = ZoneInfo("Europe/London")
        now = datetime(2026, 3, 29, 0, 30, tzinfo=tz)
        self.assertEqual(clock_at(now, 3), "04:30")

    @unittest.skipIf(ZoneInfo is None, "zoneinfo is unavailable")
    def test_dst_fall_back_uses_absolute_seconds(self):
        tz = ZoneInfo("Europe/London")
        now = datetime(2026, 10, 25, 0, 30, tzinfo=tz)
        self.assertEqual(clock_at(now, 3), "02:30")


if __name__ == "__main__":
    unittest.main()
