"""Tests for the solar-aware battery ETA mode machine and energy math."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from battery_estimate import estimate_battery  # noqa: E402


def estimate(**overrides):
    base = dict(
        soc=50, nominal_kwh=15, reserve_soc=5,
        battery_avg_net_w=700, battery_net_w=700,
        solar_w=0, grid_import_w=0, grid_export_w=0,
        home_w=500, load_avg_kw=0.5, now_hour=10.5,
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
        self.assertEqual(result["hours"], 0.0)
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

    def test_grid_priority_uses_legacy_deficit(self):
        result = estimate(grid_import_w=60, solar_w=4000,
                          charge_limit_kw=1.0)
        self.assertEqual(result["mode"], "grid")
        # deficit = 15 - 6.75 = 8.25 kWh; 0.7 kW * 0.95.
        self.assertAlmostEqual(result["hours"], 12.406015, places=5)
        self.assertIsNone(result["solar"])

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
        self.assertEqual(estimate(solar_w=1200, now_hour=None)["reason"],
                         "outside_solar_window")


class SolarTests(unittest.TestCase):
    def test_1200_w_is_not_enough_with_house_load(self):
        result = estimate(solar_w=1200, charge_limit_kw=6.0)
        self.assertEqual(result["mode"], "solar")
        self.assertIsNone(result["hours"])
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
        self.assertFalse(result["solar"]["capped"])
        self.assertEqual(result["solar"]["charge_limit_kw"], 6.0)
        self.assertEqual(result["solar"]["charge_limit_source"], "default_6kw")

    def test_cap_15_changes_the_completion_time(self):
        result = estimate(solar_w=4000, charge_limit_kw=1.5,
                          charge_limit_source="realtime.ems_second.mcp")
        self.assertTrue(result["solar"]["reachable"])
        self.assertAlmostEqual(result["hours"], 5.695, places=2)
        self.assertEqual(result["clock"], "16:12")
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

    def test_load_fallback_uses_energy_balance(self):
        result = estimate(solar_w=4000, load_avg_kw=None,
                          battery_avg_net_w=700, home_w=500)
        self.assertAlmostEqual(result["solar"]["load_kw"], 3.3, places=3)


if __name__ == "__main__":
    unittest.main()
