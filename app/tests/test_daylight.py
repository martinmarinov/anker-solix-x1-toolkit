"""Tests for the canonical daylight window and its browser adapter."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daylight  # noqa: E402
from daylight import (  # noqa: E402
    DEFAULT_SUNRISE_HOUR,
    DEFAULT_SUNSET_HOUR,
    SunTimes,
    astral_available,
    cache_info,
    clear_cache,
    sun_times,
    valid_sun_times,
)
from home_view import build_home_view  # noqa: E402

APP = Path(__file__).resolve().parents[1]
DAYLIGHT_JS = APP / "static" / "daylight.js"

LONDON = (51.5074, -0.1278)
SYDNEY = (-33.8688, 151.2093)
SVALBARD = (78.2232, 15.6267)


def minutes(hour: float) -> float:
    return hour * 60.0


def assert_within_five_minutes(case, actual: float, expected: str):
    expected_hour, expected_minute = (int(part) for part in expected.split(":"))
    case.assertLessEqual(
        abs(minutes(actual) - (expected_hour * 60 + expected_minute)), 5.0,
        f"{actual:.4f} h is not within 5 minutes of {expected}")


class DaylightTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def test_default_window_is_the_canonical_day(self):
        times = sun_times(None)
        self.assertEqual(times, SunTimes(7.0, 18.0))
        self.assertEqual(times.source, "default")
        self.assertEqual(times.window_hours, 11.0)
        self.assertEqual(DEFAULT_SUNRISE_HOUR, 7.0)
        self.assertEqual(DEFAULT_SUNSET_HOUR, 18.0)

    def test_date_timezone_and_coordinates_are_cache_keys(self):
        clear_cache()
        key = (date(2026, 7, 1), "Europe/London", *LONDON)
        first = sun_times(*key)
        second = sun_times(*key)
        self.assertEqual(first, second)
        info = cache_info()
        self.assertEqual(info.misses, 1)
        self.assertEqual(info.hits, 1)

    def test_source_is_part_of_the_cache_key(self):
        first = sun_times(date(2026, 7, 1), "Europe/London", *LONDON,
                          source="ip_geojs")
        second = sun_times(date(2026, 7, 1), "Europe/London", *LONDON,
                           source="configured")
        self.assertEqual((first.source, second.source), ("ip", "config"))
        self.assertEqual(cache_info().misses, 2)

    def test_invalid_coordinates_fall_back_to_defaults(self):
        for latitude, longitude in ((999.0, -999.0), (float("inf"), 0.0),
                                    (None, None), ("bad", 0.0)):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertEqual(
                    sun_times(date(2026, 12, 21), "Europe/London",
                              latitude, longitude),
                    SunTimes(7.0, 18.0))

    def test_invalid_date_or_timezone_falls_back_to_defaults(self):
        for day, tz in (("2026-07-01", "Europe/London"),
                        (date(2026, 7, 1), ""),
                        (date(2026, 7, 1), "Bad/Zone"),
                        (None, "Europe/London")):
            with self.subTest(day=day, tz=tz):
                self.assertEqual(sun_times(day, tz, *LONDON),
                                 SunTimes(7.0, 18.0))

    def test_window_validation(self):
        self.assertTrue(valid_sun_times(SunTimes(7.0, 18.0)))
        self.assertTrue(valid_sun_times(SunTimes(0.0, 24.0)))
        self.assertFalse(valid_sun_times(SunTimes(18.0, 7.0)))
        self.assertFalse(valid_sun_times(SunTimes(7.0, 7.0)))
        self.assertFalse(valid_sun_times(SunTimes(-1.0, 18.0)))
        self.assertFalse(valid_sun_times(SunTimes(7.0, 25.0)))
        self.assertFalse(valid_sun_times(SunTimes(float("nan"), 18.0)))

    def test_view_daylight_carries_the_real_window_and_source(self):
        view = build_home_view({
            "available": True, "ts": datetime(
                2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp(),
            "soc": 50, "pp": 0, "gp": 0, "bp": 0, "lp": 0,
        }, tz="Europe/London", latitude=LONDON[0], longitude=LONDON[1],
            location_source="ip_geojs")
        self.assertEqual(view["daylight"]["source"], "ip")
        self.assertTrue(view["daylight"]["approximate"])
        self.assertEqual(len(view["daylight"]["sunrise"]), 5)
        self.assertEqual(len(view["daylight"]["sunset"]), 5)
        self.assertNotEqual(view["daylight"]["sunrise"], "07:00")

    def test_view_daylight_default_has_no_approximate_flag(self):
        view = build_home_view(None)
        self.assertEqual(view["daylight"],
                         {"sunrise": "07:00", "sunset": "18:00",
                          "source": "default", "approximate": False})


@unittest.skipUnless(astral_available(), "Astral is not installed")
class AstralProviderTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def test_london_summer_and_winter_include_dst(self):
        summer = sun_times(date(2026, 7, 1), "Europe/London", *LONDON)
        winter = sun_times(date(2026, 12, 21), "Europe/London", *LONDON)
        self.assertEqual(summer.source, "explicit")
        self.assert_within(summer.sunrise, "04:48")
        self.assert_within(summer.sunset, "21:20")
        self.assert_within(winter.sunrise, "08:04")
        self.assert_within(winter.sunset, "15:53")
        self.assertGreater(summer.window_hours, winter.window_hours)
        self.assertTrue(12.0 < summer.window_hours < 17.0)
        self.assertTrue(7.0 < winter.window_hours < 9.0)

    def test_second_hemisphere(self):
        winter = sun_times(date(2026, 6, 21), "Australia/Sydney", *SYDNEY)
        self.assertEqual(winter.source, "explicit")
        self.assert_within(winter.sunrise, "07:00")
        self.assert_within(winter.sunset, "16:54")

    def test_polar_day_and_night_fall_back(self):
        for day in (date(2026, 6, 21), date(2026, 12, 21)):
            with self.subTest(day=day):
                self.assertEqual(
                    sun_times(day, "Arctic/Longyearbyen", *SVALBARD),
                    SunTimes(7.0, 18.0))

    def test_astral_value_error_falls_back(self):
        with patch.object(daylight, "_astral_sunrise",
                          side_effect=ValueError("polar")):
            times = sun_times(date(2026, 6, 21), "Europe/London", *LONDON)
        self.assertEqual(times, SunTimes(7.0, 18.0))

    def test_source_mapping_and_fallback(self):
        day = date(2026, 7, 1)
        cases = {
            "ip_geojs": "ip",
            "ip_cache": "ip",
            "ip": "ip",
            "configured": "config",
            "config": "config",
            "explicit": "explicit",
            "something-else": "explicit",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(
                    sun_times(day, "Europe/London", *LONDON,
                              source=raw).source, expected)
        self.assertEqual(
            sun_times(None, "Europe/London", *LONDON,
                      source="ip_geojs").source, "default")

    def assert_within(self, actual: float, expected: str):
        assert_within_five_minutes(self, actual, expected)


@unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
class DaylightJavaScriptTests(unittest.TestCase):
    def run_daylight(self) -> dict:
        script = r"""
const fs = require('fs');
const vm = require('vm');
global.window = global;
vm.runInThisContext(fs.readFileSync(%s, 'utf8'));
const D = window.Daylight;
D.clear();
const out = {
  defaults: D.sunTimes(),
  defaultNight: [360, 390, 420, 1080, 1110, 1140].map(
    (m) => D.nightAmount(m)
  ),
  icons: [D.solarIcon(false, 120), D.solarIcon(false, 420),
          D.solarIcon(false, 1080), D.solarIcon(true, 120)],
  sky: [D.skyState(1), D.skyState(0.5), D.skyState(0)],
  sunsetClock: D.sunsetClock(),
  isDaylight: [D.isDaylight(419), D.isDaylight(420),
               D.isDaylight(1080), D.isDaylight(1081)],
  clockMinutes: [D.clockMinutes('18:30'), D.clockMinutes('24:00'),
                 D.clockMinutes('bad')],
  configured: D.configure({sunrise: '06:00', sunset: '19:00'}),
  reconfigured: D.configure({sunrise: '06:00', sunset: '19:00'}),
  invalid: D.configure({sunrise: '19:00', sunset: '06:00'}),
  custom: D.sunTimes(),
  customNight: [300, 330, 360, 1140, 1170, 1200].map(
    (m) => D.nightAmount(m)
  ),
  customSunsetClock: D.sunsetClock(),
  fullDayConfigured: D.configure({sunrise: '00:00', sunset: '24:00'}),
  fullDayNight: [D.nightAmount(0), D.nightAmount(720),
                 D.nightAmount(1439)],
  fullDay: D.isDaylight(0)
};
D.clear();
out.cleared = D.sunTimes();
console.log(JSON.stringify(out));
""" % json.dumps(str(DAYLIGHT_JS))
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, timeout=30)
        if proc.returncode != 0:
            self.fail(proc.stderr or proc.stdout)
        return json.loads(proc.stdout)

    def test_js_defaults_match_python(self):
        data = self.run_daylight()
        self.assertEqual(data["defaults"]["sunrise"],
                         int(DEFAULT_SUNRISE_HOUR * 60))
        self.assertEqual(data["defaults"]["sunset"],
                         int(DEFAULT_SUNSET_HOUR * 60))
        self.assertEqual(data["defaults"]["transition"], 60)
        self.assertEqual(data["sunsetClock"], "18:00")
        self.assertEqual(data["clockMinutes"], [1110, 1440, None])

    def test_js_default_day_boundaries(self):
        data = self.run_daylight()
        for actual, expected in zip(data["defaultNight"],
                                    [1, 0.5, 0, 0, 0.5, 1]):
            self.assertAlmostEqual(actual, expected, places=9)
        self.assertEqual(data["icons"], ["moon", "cloud", "cloud", "sun"])
        self.assertEqual(data["sky"], ["night", "dusk", "day"])
        self.assertEqual(data["isDaylight"], [False, True, True, False])

    def test_js_custom_window_and_validation(self):
        data = self.run_daylight()
        self.assertTrue(data["configured"])
        self.assertFalse(data["reconfigured"])
        self.assertFalse(data["invalid"])
        self.assertEqual(data["custom"],
                         {"sunrise": 360, "sunset": 1140, "transition": 60})
        for actual, expected in zip(data["customNight"],
                                    [1, 0.5, 0, 0, 0.5, 1]):
            self.assertAlmostEqual(actual, expected, places=9)
        self.assertEqual(data["customSunsetClock"], "19:00")
        self.assertTrue(data["fullDayConfigured"])
        self.assertEqual(data["fullDayNight"], [0, 0, 0])
        self.assertTrue(data["fullDay"])
        self.assertEqual(data["cleared"],
                         {"sunrise": 420, "sunset": 1080, "transition": 60})


if __name__ == "__main__":
    unittest.main()
