"""Tests for the solar-aware battery ETA mode machine and energy math."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from battery_estimate import (  # noqa: E402
    ETA_KINDS,
    PHASES,
    SOURCES,
    estimate_battery,
)

NOW = datetime(2026, 7, 1, 10, 30)


def estimate(**overrides):
    base = dict(
        soc=50, nominal_kwh=15, reserve_soc=5,
        battery_avg_net_w=700, battery_net_w=700,
        solar_w=0, grid_import_w=0, grid_export_w=0,
        home_w=500, load_avg_kw=0.5, now_hour=10.5, now_dt=NOW,
        charge_limit_kw=6.0, charge_limit_source="default_6kw",
    )
    base.update(overrides)
    return estimate_battery(**base)


class ModeTests(unittest.TestCase):
    def test_missing_battery_data_is_unavailable(self):
        self.assertEqual(estimate(nominal_kwh=None)["mode"], "unavailable")
        self.assertEqual(estimate(soc=None)["mode"], "unavailable")
        self.assertEqual(estimate(reserve_soc=None)["mode"], "unavailable")

    def test_discharge_idle_boundary(self):
        self.assertEqual(estimate(battery_avg_net_w=-30)["mode"], "idle")
        self.assertEqual(estimate(battery_avg_net_w=-49)["mode"], "idle")
        self.assertEqual(estimate(battery_avg_net_w=-50)["mode"], "discharging")
        self.assertEqual(estimate(battery_avg_net_w=-51)["mode"], "discharging")
        self.assertEqual(estimate(battery_avg_net_w=0, soc=90)["mode"], "idle")

    def test_full(self):
        result = estimate(battery_avg_net_w=0, soc=100)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["phase"], "full")
        self.assertEqual(result["source"], "none")
        self.assertEqual(result["hours"], 0.0)
        self.assertEqual(result["clock"], "10:30")
        self.assertEqual(result["eta"]["kind"], "at_full")
        self.assertEqual(result["solar_end"], None)

    def test_legacy_average_with_zero_instant_is_idle(self):
        result = estimate(battery_avg_net_w=None, battery_avg_kw=1.0,
                          battery_net_w=0)
        self.assertEqual(result["mode"], "idle")
        self.assertIsNone(result["hours"])

    def test_discharge_matches_legacy_formula(self):
        result = estimate(battery_avg_net_w=-1500, soc=50)
        self.assertAlmostEqual(result["hours"], 4.275, places=6)
        self.assertEqual(result["clock"], "14:46")
        self.assertEqual(result["phase"], "discharging")
        self.assertEqual(result["source"], "battery")
        self.assertEqual(result["eta"]["kind"], "to_empty")
        self.assertEqual(result["eta"]["label"], "14:46")

    def test_grid_priority_uses_legacy_deficit(self):
        result = estimate(grid_import_w=60, solar_w=4000,
                          charge_limit_kw=1.0)
        self.assertEqual(result["mode"], "grid")
        # deficit = 15 - 6.75 = 8.25 kWh; 0.7 kW * 0.95.
        self.assertAlmostEqual(result["hours"], 12.406015, places=5)
        self.assertIsNone(result["solar"])
        self.assertEqual(result["phase"], "charging")
        self.assertEqual(result["source"], "grid")
        self.assertEqual(result["eta"]["kind"], "to_full")
        self.assertEqual(result["eta"]["label"], "22:54")

    def test_linear_fallback_outside_window(self):
        for hour in (6.5, 7.0, 18.0, 18.5):
            result = estimate(solar_w=1200, now_hour=hour)
            self.assertEqual(result["mode"], "charging", hour)
            self.assertEqual(result["reason"], "outside_solar_window", hour)
            self.assertIsNone(result["solar"], hour)
            self.assertAlmostEqual(result["hours"], 12.406015, places=5)

    def test_linear_fallback_without_usable_solar_or_with_export(self):
        self.assertEqual(estimate(solar_w=30)["reason"], "no_usable_solar")
        self.assertEqual(estimate(solar_w=1200, grid_export_w=60)["reason"],
                         "exporting_surplus")
        self.assertEqual(estimate(solar_w=1200, now_hour=None,
                                  now_dt=None)["reason"],
                         "outside_solar_window")


class SolarTests(unittest.TestCase):
    def test_1200_w_is_not_enough_with_house_load(self):
        result = estimate(solar_w=1200, charge_limit_kw=6.0)
        self.assertEqual(result["mode"], "solar")
        self.assertEqual(result["phase"], "charging")
        self.assertEqual(result["source"], "solar")
        self.assertIsNone(result["hours"])
        self.assertEqual(result["eta"]["kind"], "to_soc")
        self.assertEqual(result["eta"]["soc"], 50.2)
        self.assertEqual(result["eta"]["at"], "18:00")
        self.assertIsNone(result["eta"]["hours"])
        self.assertEqual(result["solar_end"], "18:00")
        self.assertAlmostEqual(result["soc_final"], 50.2, places=1)
        self.assertFalse(result["solar"]["reachable"])
        self.assertAlmostEqual(result["solar"]["charge_kwh"], 0.7, places=1)
        self.assertAlmostEqual(result["solar"]["needed_kwh"], 7.89, places=1)

    def test_4000_w_uncapped(self):
        result = estimate(solar_w=4000, charge_limit_kw=None)
        self.assertEqual(result["mode"], "solar")
        self.assertAlmostEqual(result["hours"], 4.653, places=2)
        self.assertEqual(result["clock"], "15:09")
        self.assertEqual(result["eta"]["kind"], "to_full")
        self.assertEqual(result["eta"]["label"], "15:09")
        self.assertFalse(result["solar"]["capped"])
        self.assertEqual(result["solar"]["charge_limit_kw"], 6.0)
        self.assertEqual(result["solar"]["charge_limit_source"], "default_6kw")

    def test_cap_15_changes_the_completion_time(self):
        result = estimate(solar_w=4000, charge_limit_kw=1.5,
                          charge_limit_source="realtime.ems_second.mcp")
        self.assertTrue(result["solar"]["reachable"])
        self.assertAlmostEqual(result["hours"], 5.695, places=2)
        self.assertEqual(result["clock"], "16:12")
        self.assertEqual(result["eta"]["label"], "16:12")
        self.assertTrue(result["solar"]["capped"])
        self.assertEqual(result["solar"]["charge_limit_source"],
                         "realtime.ems_second.mcp")

    def test_cap_10_is_not_enough(self):
        result = estimate(solar_w=4000, charge_limit_kw=1.0,
                          charge_limit_source="device_attrs.ac_power_limit")
        self.assertFalse(result["solar"]["reachable"])
        self.assertEqual(result["solar_end"], "18:00")
        self.assertAlmostEqual(result["soc_final"], 86.5, places=1)
        self.assertTrue(result["solar"]["capped"])

    def test_cap_zero_or_negative_falls_back_to_6kw(self):
        for cap in (0, -1):
            result = estimate(solar_w=4000, charge_limit_kw=cap)
            self.assertTrue(result["solar"]["reachable"])
            self.assertEqual(result["solar"]["charge_limit_kw"], 6.0)
            self.assertEqual(result["solar"]["charge_limit_source"],
                             "default_6kw")

    def test_high_cap_does_not_bind(self):
        result = estimate(solar_w=4000, charge_limit_kw=8.0)
        self.assertFalse(result["solar"]["capped"])
        self.assertAlmostEqual(result["hours"], 4.653, places=2)

    def test_rate_is_observed_battery_rate(self):
        result = estimate(solar_w=4000, battery_avg_net_w=700)
        self.assertEqual(result["rate_kw"], 0.7)
        self.assertEqual(result["source"], "solar")
        self.assertEqual(result["phase"], "charging")

    def test_load_fallback_uses_energy_balance(self):
        result = estimate(solar_w=4000, load_avg_kw=None,
                          battery_avg_net_w=700, home_w=500)
        self.assertAlmostEqual(result["solar"]["load_kw"], 3.3, places=3)


class AveragedSolarTests(unittest.TestCase):
    def test_average_replaces_instant_for_the_curve(self):
        result = estimate(solar_w=1000, solar_avg_kw=4.0)
        self.assertEqual(result["mode"], "solar")
        self.assertAlmostEqual(result["hours"], 4.653, places=2)
        self.assertEqual(result["clock"], "15:09")
        self.assertEqual(result["solar"]["observed_kw"], 4.0)
        self.assertEqual(result["solar"]["observed_source"], "avg")

    def test_average_wins_over_an_instant_spike(self):
        result = estimate(solar_w=4000, solar_avg_kw=1.2)
        self.assertEqual(result["eta"]["kind"], "to_soc")
        self.assertAlmostEqual(result["soc_final"], 50.2, places=1)
        self.assertEqual(result["solar"]["observed_kw"], 1.2)
        self.assertEqual(result["solar"]["observed_source"], "avg")

    def test_average_drives_the_50_w_gate(self):
        result = estimate(solar_w=4000, solar_avg_kw=0.04)
        self.assertEqual(result["mode"], "charging")
        self.assertEqual(result["reason"], "no_usable_solar")
        self.assertAlmostEqual(result["hours"], 12.406015, places=5)

    def test_average_can_enable_solar_when_instant_is_zero(self):
        result = estimate(solar_w=0, solar_avg_kw=4.0)
        self.assertEqual(result["mode"], "solar")
        self.assertAlmostEqual(result["hours"], 4.653, places=2)

    def test_missing_average_falls_back_to_instant(self):
        result = estimate(solar_w=4000, solar_avg_kw=None)
        self.assertAlmostEqual(result["hours"], 4.653, places=2)
        self.assertEqual(result["solar"]["observed_source"], "instant")

    def test_numeric_string_average_is_accepted(self):
        result = estimate(solar_w=1000, solar_avg_kw="4.0")
        self.assertEqual(result["solar"]["observed_kw"], 4.0)
        self.assertEqual(result["solar"]["observed_source"], "avg")

    def test_invalid_average_falls_back_to_instant(self):
        for bad in (float("nan"), float("inf"), "bad"):
            result = estimate(solar_w=4000, solar_avg_kw=bad)
            self.assertEqual(result["solar"]["observed_source"], "instant")
            self.assertAlmostEqual(result["hours"], 4.653, places=2)

    def test_negative_average_clamps_to_zero(self):
        result = estimate(solar_w=4000, solar_avg_kw=-1.0)
        self.assertEqual(result["mode"], "charging")
        self.assertEqual(result["reason"], "no_usable_solar")

    def test_load_fallback_still_uses_instant_solar(self):
        result = estimate(solar_w=1000, solar_avg_kw=4.0, load_avg_kw=None)
        self.assertAlmostEqual(result["solar"]["load_kw"], 0.3, places=3)


class SlowEtaTests(unittest.TestCase):
    def test_thirty_hour_discharge_says_tomorrow(self):
        result = estimate(battery_avg_net_w=-213.75)
        self.assertAlmostEqual(result["hours"], 30.0, places=6)
        self.assertEqual(result["clock"], "16:30")
        self.assertEqual(result["eta"]["kind"], "to_empty")
        self.assertEqual(result["eta"]["label"], "tomorrow 16:30")

    def test_slow_discharge_uses_the_calendar_date(self):
        result = estimate(battery_avg_net_w=-100.0)
        self.assertAlmostEqual(result["hours"], 64.125, places=6)
        self.assertEqual(result["clock"], "02:38")
        self.assertEqual(result["eta"]["label"], "Sat 04 Jul 02:38")

    def test_screenshot_like_thirty_seven_hours(self):
        now = datetime(2026, 10, 30, 19, 35)
        result = estimate(soc=93.4, battery_avg_net_w=-340,
                          battery_net_w=270, now_hour=19 + 35 / 60,
                          now_dt=now)
        self.assertEqual(result["phase"], "discharging")
        self.assertEqual(result["rate_kw"], 0.34)
        self.assertAlmostEqual(result["hours"], 37.05, places=6)
        self.assertEqual(result["clock"], "08:38")
        self.assertEqual(result["eta"]["label"], "Sun 01 Nov 08:38")

    def test_at_reserve_is_empty(self):
        result = estimate(soc=5, battery_avg_net_w=-1500)
        self.assertEqual(result["mode"], "discharging")
        self.assertEqual(result["hours"], 0.0)
        self.assertEqual(result["eta"]["kind"], "at_empty")
        self.assertEqual(result["clock"], "10:30")

    def test_under_twelve_hours_stays_bare_across_midnight(self):
        result = estimate(battery_avg_net_w=-582.954545, now_hour=22.0,
                          now_dt=datetime(2026, 7, 1, 22, 0))
        self.assertAlmostEqual(result["hours"], 11.0, places=5)
        self.assertEqual(result["eta"]["label"], "09:00")

    def test_thirteen_hours_adds_tomorrow(self):
        result = estimate(battery_avg_net_w=-493.269231, now_hour=22.0,
                          now_dt=datetime(2026, 7, 1, 22, 0))
        self.assertAlmostEqual(result["hours"], 13.0, places=5)
        self.assertEqual(result["eta"]["label"], "tomorrow 11:00")

    def test_now_hour_only_keeps_the_clock_without_a_date(self):
        result = estimate(battery_avg_net_w=-213.75, now_dt=None)
        self.assertEqual(result["clock"], "16:30")
        self.assertEqual(result["eta"]["label"], "16:30")


class SchemaTests(unittest.TestCase):
    TOP_LEVEL = {"mode", "phase", "source", "rate_kw", "hours", "clock",
                 "soc_final", "solar_end", "solar", "eta", "reason"}
    ETA_KEYS = {"kind", "hours", "clock", "label", "soc", "at"}

    def cases(self):
        return [
            ("missing", dict(nominal_kwh=None)),
            ("idle", dict(battery_avg_net_w=-30)),
            ("full", dict(soc=100, battery_avg_net_w=0)),
            ("discharging", dict(battery_avg_net_w=-1500)),
            ("empty", dict(soc=5, battery_avg_net_w=-1500)),
            ("grid", dict(grid_import_w=60)),
            ("outside", dict(solar_w=1200, now_hour=6.5)),
            ("no_solar", dict(solar_w=30)),
            ("export", dict(solar_w=1200, grid_export_w=60)),
            ("solar_reachable", dict(solar_w=4000)),
            ("solar_partial", dict(solar_w=1200)),
            ("solar_average", dict(solar_w=1000, solar_avg_kw=4.0)),
        ]

    def test_every_branch_has_the_same_schema(self):
        for name, kwargs in self.cases():
            result = estimate(**kwargs)
            self.assertEqual(set(result), self.TOP_LEVEL, name)
            self.assertEqual(set(result["eta"]), self.ETA_KEYS, name)
            self.assertIn(result["eta"]["kind"], ETA_KINDS, name)
            self.assertIn(result["phase"], PHASES, name)
            self.assertIn(result["source"], SOURCES, name)
            self.assertEqual(result["eta"]["clock"], result["clock"], name)
            self.assertEqual(result["eta"]["hours"], result["hours"], name)

    def test_every_eta_kind_matches_its_time_fields(self):
        for name, kwargs in self.cases():
            eta = estimate(**kwargs)["eta"]
            kind = eta["kind"]
            if kind in ("to_empty", "to_full"):
                self.assertIsNotNone(eta["hours"], name)
                self.assertIsNotNone(eta["clock"], name)
                self.assertIsNotNone(eta["label"], name)
            elif kind == "to_soc":
                self.assertIsNone(eta["hours"], name)
                self.assertIsNotNone(eta["soc"], name)
                self.assertIsNotNone(eta["at"], name)
            elif kind in ("at_full", "at_empty"):
                self.assertEqual(eta["hours"], 0.0, name)
            else:
                self.assertIsNone(eta["hours"], name)

    def test_source_describes_the_charging_path(self):
        self.assertEqual(estimate(battery_avg_net_w=-1500)["source"],
                         "battery")
        self.assertEqual(estimate(grid_import_w=60)["source"], "grid")
        self.assertEqual(estimate(solar_w=4000)["source"], "solar")
        self.assertEqual(estimate(solar_w=1200, now_hour=6.5)["source"],
                         "solar")
        self.assertEqual(estimate(solar_w=30)["source"], "none")
        self.assertEqual(estimate(solar_w=1200, grid_export_w=60)["source"],
                         "solar")
        self.assertEqual(estimate(battery_avg_net_w=-30)["source"], "none")


if __name__ == "__main__":
    unittest.main()
