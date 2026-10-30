"""Unit tests for the pure Home view-model derivation."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from daylight import SunTimes  # noqa: E402
from home_view import build_home_view  # noqa: E402


class HomeViewTests(unittest.TestCase):
    def test_unavailable_is_not_zero(self):
        view = build_home_view({"available": False, "reason": "connecting"})
        self.assertFalse(view["available"])
        self.assertIsNone(view["solar"]["w"])
        self.assertIsNone(view["solar"]["avg_kw"])
        self.assertIsNone(view["grid"]["kw"])
        self.assertEqual(view["status"]["level"], "waiting")
        self.assertFalse(any(f["active"] for f in view["flows"].values()))

    def test_offline_reason(self):
        view = build_home_view({"available": False, "reason": "SSLError"})
        self.assertEqual(view["status"]["level"], "offline")
        self.assertNotIn("SSLError", view["status"]["label"])

    def test_importing_and_charging_from_edges(self):
        view = build_home_view({
            "available": True, "soc": 42.6,
            "pp": 4180, "gp": 1200, "bp": -900, "lp": 1780,
            "p2lp": 900, "p2bp": 900, "p2gp": 0,
            "g2lp": 880, "g2bp": 900,
            "b2lp": 0, "b2gp": 0,
        })
        self.assertEqual(view["solar"]["state"], "producing")
        self.assertEqual(view["solar"]["w"], 4180)
        self.assertEqual(view["grid"]["state"], "importing")
        self.assertEqual(view["grid"]["w"], 1200)
        self.assertEqual(view["battery"]["state"], "charging")
        self.assertEqual(view["battery"]["w"], 900)
        self.assertEqual(view["battery"]["soc"], 43)
        self.assertEqual(view["home"]["w"], 1780)
        self.assertTrue(view["flows"]["grid_import"]["active"])
        self.assertFalse(view["flows"]["grid_export"]["active"])

    def test_exporting_and_discharging_from_edges(self):
        view = build_home_view({
            "available": True, "soc": 63,
            "pp": 4180, "gp": -4180, "bp": 2400, "lp": 1780,
            "p2lp": 780, "p2bp": 0, "p2gp": 2400,
            "g2lp": 0, "g2bp": 0,
            "b2lp": 1000, "b2gp": 0,
        })
        self.assertEqual(view["grid"]["state"], "exporting")
        self.assertEqual(view["grid"]["w"], 4180)
        self.assertEqual(view["battery"]["state"], "discharging")
        self.assertEqual(view["battery"]["w"], 2400)
        self.assertEqual(view["battery"]["label"], "63% charged")
        self.assertEqual(view["home"]["w"], 1780)

    def test_signed_fallbacks(self):
        view = build_home_view({
            "available": True, "soc": 50,
            "pp": 1000, "gp": -500, "bp": 300, "lp": 800,
        })
        self.assertEqual(view["grid"]["state"], "exporting")
        self.assertEqual(view["grid"]["w"], 500)
        self.assertEqual(view["battery"]["state"], "discharging")
        self.assertEqual(view["battery"]["w"], 300)
        self.assertTrue(view["flows"]["grid_export"]["active"])
        self.assertTrue(view["flows"]["battery_discharge"]["active"])

    def test_charging_signed_fallback(self):
        view = build_home_view({"available": True, "bp": -1200, "pp": 0,
                                "gp": 1200, "lp": 0})
        self.assertEqual(view["battery"]["state"], "charging")
        self.assertEqual(view["battery"]["w"], 1200)
        self.assertEqual(view["grid"]["state"], "importing")

    def test_edges_only_frames_fall_back_to_flow_sums(self):
        view = build_home_view({
            "available": True, "soc": 50,
            "g2lp": 700, "b2lp": 300, "lp": 1000,
            "p2lp": 0, "p2bp": 0, "p2gp": 0, "g2bp": 0, "b2gp": 0,
        })
        self.assertEqual(view["grid"]["w"], 700)
        self.assertEqual(view["grid"]["state"], "importing")
        self.assertEqual(view["battery"]["w"], 300)
        self.assertEqual(view["battery"]["state"], "discharging")
        self.assertEqual(view["home"]["w"], 1000)

    def test_stale_keeps_last_values(self):
        view = build_home_view({
            "available": True, "stale": True, "age_s": 42, "soc": 90,
            "pp": 1000, "gp": 0, "bp": 0, "lp": 1000,
        })
        self.assertTrue(view["stale"])
        self.assertEqual(view["solar"]["w"], 1000)
        self.assertEqual(view["status"]["level"], "stale")
        self.assertEqual(view["status"]["label"], "Stale · 42s")

    def test_soc_clamped_and_idle(self):
        view = build_home_view({"available": True, "soc": 140, "pp": 0,
                                "gp": 0, "bp": 0, "lp": 0})
        self.assertEqual(view["soc"], 100)
        self.assertEqual(view["grid"]["state"], "idle")
        self.assertEqual(view["battery"]["state"], "idle")
        self.assertEqual(view["solar"]["state"], "idle")

    def test_missing_and_bad_values_do_not_raise(self):
        view = build_home_view({"available": True, "pp": "bad", "gp": None,
                                "bp": object(), "lp": "1200", "soc": "nope"})
        self.assertEqual(view["home"]["w"], 1200)
        self.assertIsNone(view["soc"])
        self.assertEqual(view["status"]["level"], "live")

    def test_empty_snapshot(self):
        view = build_home_view(None)
        self.assertFalse(view["available"])
        self.assertEqual(view["status"]["level"], "waiting")

    def test_available_without_power_fields_is_placeholder(self):
        view = build_home_view({"available": True, "soc": None})
        self.assertFalse(view["available"])
        self.assertIsNone(view["solar"]["kw"])
        self.assertIsNone(view["grid"]["kw"])
        self.assertIsNone(view["battery"]["kw"])
        self.assertIsNone(view["home"]["kw"])
        self.assertEqual(view["status"]["level"], "offline")
        self.assertEqual(view["status"]["label"], "No telemetry data")

    def test_waking_is_waiting_not_offline(self):
        view = build_home_view({"available": False, "reason": "waking"})
        self.assertFalse(view["available"])
        self.assertEqual(view["status"]["level"], "waiting")
        self.assertEqual(view["status"]["label"], "Requesting live data")

    def test_fresh_wake_keeps_values_and_shows_refreshing(self):
        view = build_home_view({
            "available": True, "waking": True, "waking_notice": True,
            "stale": False, "age_s": 12,
            "soc": 60, "pp": 1800, "gp": 200, "bp": 0, "lp": 1600,
        })
        self.assertTrue(view["available"])
        self.assertFalse(view["stale"])
        self.assertEqual(view["status"]["level"], "waking")
        self.assertEqual(view["status"]["label"], "Refreshing · 12s")
        self.assertEqual(view["solar"]["w"], 1800)

    def test_wake_inside_the_notice_grace_stays_live(self):
        view = build_home_view({
            "available": True, "waking": True, "waking_notice": False,
            "stale": False, "age_s": 4,
            "soc": 60, "pp": 1800, "gp": 200, "bp": 0, "lp": 1600,
        })
        self.assertTrue(view["available"])
        self.assertEqual(view["status"]["level"], "live")
        self.assertEqual(view["status"]["label"], "Live")
        self.assertEqual(view["solar"]["w"], 1800)

    def test_waking_without_the_notice_field_falls_back(self):
        view = build_home_view({
            "available": True, "waking": True, "stale": False, "age_s": 12,
            "soc": 60, "pp": 1800, "gp": 200, "bp": 0, "lp": 1600,
        })
        self.assertEqual(view["status"]["level"], "waking")
        self.assertEqual(view["status"]["label"], "Refreshing · 12s")

    def test_stale_wins_over_a_wake_in_flight(self):
        view = build_home_view({
            "available": True, "waking": True, "waking_notice": True,
            "stale": True, "age_s": 200,
            "soc": 60, "pp": 1800, "gp": 200, "bp": 0, "lp": 1600,
        })
        self.assertTrue(view["available"])
        self.assertTrue(view["stale"])
        self.assertEqual(view["status"]["level"], "stale")
        self.assertEqual(view["status"]["label"], "Stale · 3m 20s")
        self.assertEqual(view["solar"]["w"], 1800)

    def test_stale_age_uses_minutes_after_sixty(self):
        view = build_home_view({
            "available": True, "stale": True, "age_s": 125,
            "pp": 1000, "gp": 0, "bp": 0, "lp": 1000,
        })
        self.assertEqual(view["status"]["label"], "Stale · 2m 5s")

    def test_eta_uses_server_average(self):
        view = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "lp": 0,
            "bp": 3000, "batt_avg_kw": 1.0, "nominal_capacity_kwh": 15,
            "reserve_soc": 5,
        })
        self.assertEqual(view["battery"]["hours_to_empty"], 6.41)
        self.assertIsNone(view["battery"]["hours_to_full"])
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["phase"], "discharging")
        self.assertEqual(estimate["source"], "battery")
        self.assertEqual(estimate["eta"]["kind"], "to_empty")
        self.assertNotIn("phase", view["battery"])
        self.assertNotIn("source", view["battery"])

    def test_charging_eta_uses_server_average(self):
        view = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "lp": 0,
            "bp": -3000, "batt_avg_kw": 1.0, "nominal_capacity_kwh": 15,
            "reserve_soc": 5,
        })
        self.assertEqual(view["battery"]["hours_to_full"], 8.68)
        self.assertIsNone(view["battery"]["hours_to_empty"])
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["phase"], "charging")
        self.assertEqual(estimate["source"], "none")
        self.assertEqual(estimate["eta"]["kind"], "to_full")

    def test_numeric_zero_is_live_idle(self):
        view = build_home_view({"available": True, "pp": 0, "gp": 0,
                                "bp": 0, "lp": 0, "soc": 0})
        self.assertTrue(view["available"])
        self.assertEqual(view["solar"]["kw"], 0.0)
        self.assertEqual(view["grid"]["kw"], 0.0)
        self.assertEqual(view["battery"]["kw"], 0.0)
        self.assertEqual(view["home"]["kw"], 0.0)
        self.assertEqual(view["status"]["level"], "live")

    def test_battery_available_capacity_and_discharge_time(self):
        view = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "lp": 0,
            "bp": 1500, "nominal_capacity_kwh": 15, "reserve_soc": 5,
        })
        self.assertEqual(view["battery"]["available_kwh"], 6.75)
        self.assertEqual(view["battery"]["state"], "discharging")
        self.assertEqual(view["battery"]["hours_to_empty"], 4.27)
        self.assertIsNone(view["battery"]["hours_to_full"])

    def test_battery_charge_time_uses_efficiency(self):
        view = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "lp": 0,
            "bp": -1500, "nominal_capacity_kwh": 15, "reserve_soc": 5,
        })
        self.assertEqual(view["battery"]["state"], "charging")
        self.assertEqual(view["battery"]["hours_to_full"], 5.79)
        self.assertIsNone(view["battery"]["hours_to_empty"])

    def _solar_snapshot(self, ts, **overrides):
        snap = {
            "available": True, "ts": ts, "soc": 50,
            "pp": 4000, "gp": 0, "bp": -700, "lp": 500,
            "nominal_capacity_kwh": 15, "reserve_soc": 5,
            "batt_avg_net_kw": 0.7, "load_avg_kw": 0.5,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        snap.update(overrides)
        return snap

    def test_solar_estimate_uses_site_timezone_and_curve(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(ts), tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertAlmostEqual(estimate["hours"], 1.975, places=2)
        self.assertEqual(estimate["clock"], "12:29")
        self.assertEqual(estimate["eta"]["kind"], "to_full")
        self.assertEqual(estimate["eta"]["label"], "12:29")
        self.assertEqual(view["battery"]["hours_to_full"], 1.98)
        self.assertAlmostEqual(estimate["solar"]["net_kw_at_t0"], 3.5, places=2)
        self.assertEqual(estimate["solar"]["low"]["label"], "15:09")
        self.assertIsNone(view["battery"]["hours_to_empty"])
        self.assertEqual(estimate["solar"]["as_of"], "10:30")
        self.assertFalse(estimate["solar"]["capped"])
        self.assertIsNone(view["solar"]["avg_kw"])
        self.assertEqual(estimate["solar"]["observed_source"], "instant")

    def test_solar_estimate_uses_the_averaged_pv(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(
            self._solar_snapshot(ts, pp=1000, solar_avg_kw=4.0),
            tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertAlmostEqual(estimate["hours"], 1.975, places=2)
        self.assertEqual(estimate["clock"], "12:29")
        self.assertEqual(view["solar"]["w"], 1000)
        self.assertEqual(view["solar"]["avg_kw"], 4.0)
        self.assertEqual(estimate["solar"]["observed_kw"], 4.0)
        self.assertEqual(estimate["solar"]["observed_source"], "avg")

    def test_zero_average_keeps_the_card_instant_but_blocks_solar(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(
            self._solar_snapshot(ts, pp=4000, solar_avg_kw=0.0),
            tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "charging")
        self.assertEqual(estimate["reason"], "no_usable_solar")
        self.assertEqual(view["solar"]["w"], 4000)
        self.assertEqual(view["solar"]["avg_kw"], 0.0)

    def test_solar_estimate_respects_site_timezone_window(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(ts),
                               tz="America/New_York")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "charging")
        self.assertEqual(estimate["reason"], "outside_solar_window")
        self.assertIsNone(estimate["solar"])

    def test_solar_not_reachable_exposes_soc_and_end(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(ts, pp=1200),
                               tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertIsNone(estimate["hours"])
        self.assertEqual(estimate["solar_end"], "18:00")
        self.assertAlmostEqual(estimate["soc_final"], 74.8, places=1)
        self.assertTrue(estimate["solar"]["reachable"] is False)
        self.assertEqual(estimate["eta"]["kind"], "to_soc")
        self.assertEqual(estimate["eta"]["soc"], 74.8)
        self.assertEqual(estimate["eta"]["at"], "18:00")
        self.assertAlmostEqual(estimate["solar"]["low"]["soc"], 50.2, places=1)

    def test_idle_below_50_w_has_no_eta(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(
            ts, pp=0, bp=-30, batt_avg_net_kw=-0.03,
            charge_limit_kw=6.0), tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "idle")
        self.assertIsNone(estimate["hours"])
        self.assertEqual(estimate["eta"]["kind"], "none")
        self.assertEqual(estimate["eta"]["label"], None)
        self.assertIsNone(view["battery"]["hours_to_empty"])
        self.assertIsNone(view["battery"]["hours_to_full"])

    def test_solar_cap_is_reported(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(
            ts, charge_limit_kw=1.0,
            charge_limit_source="device_attrs.ac_power_limit"),
            tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertTrue(estimate["solar"]["capped"])
        self.assertEqual(estimate["solar"]["charge_limit_kw"], 1.0)
        self.assertEqual(estimate["solar"]["charge_limit_source"],
                         "device_attrs.ac_power_limit")

    def test_estimate_clock_uses_discharge_formula(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(
            ts, pp=0, bp=1500, batt_avg_net_kw=-1.5), tz="Europe/London")
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "discharging")
        self.assertEqual(estimate["clock"], "14:46")
        self.assertEqual(estimate["eta"]["label"], "14:46")

    def test_instantaneous_card_power_uses_the_averaged_eta(self):
        view = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "lp": 0,
            "bp": -3000, "batt_avg_net_kw": -0.3,
            "nominal_capacity_kwh": 15, "reserve_soc": 5,
        })
        self.assertEqual(view["battery"]["state"], "charging")
        self.assertEqual(view["battery"]["w"], 3000)
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["phase"], "discharging")
        self.assertEqual(estimate["source"], "battery")
        self.assertAlmostEqual(estimate["rate_kw"], 0.3)
        self.assertAlmostEqual(estimate["hours"], 21.375)

    def test_slow_eta_carries_the_calendar_date(self):
        ts = datetime(2026, 10, 30, 19, 35, tzinfo=timezone.utc).timestamp()
        view = build_home_view({
            "available": True, "ts": ts, "soc": 93.4,
            "pp": 0, "gp": 0, "bp": 270, "lp": 0,
            "nominal_capacity_kwh": 15, "reserve_soc": 5,
            "batt_avg_net_kw": -0.34, "load_avg_kw": 0.0,
        }, tz="Europe/London")
        battery = view["battery"]
        self.assertEqual(battery["available_kwh"], 13.26)
        self.assertEqual(battery["w"], 270)
        estimate = battery["estimate"]
        self.assertAlmostEqual(estimate["hours"], 37.05, places=2)
        self.assertEqual(estimate["clock"], "08:38")
        self.assertEqual(estimate["eta"]["kind"], "to_empty")
        self.assertEqual(estimate["eta"]["label"], "Sun 01 Nov 08:38")
        self.assertEqual(estimate["eta"]["hours"], estimate["hours"])

    def test_at_reserve_and_at_full_aliases(self):
        empty = build_home_view({
            "available": True, "soc": 5, "pp": 0, "gp": 0, "lp": 0,
            "bp": 1500, "nominal_capacity_kwh": 15, "reserve_soc": 5,
        })
        self.assertEqual(empty["battery"]["hours_to_empty"], 0.0)
        self.assertIsNone(empty["battery"]["hours_to_full"])
        self.assertEqual(empty["battery"]["estimate"]["eta"]["kind"],
                         "at_empty")
        full = build_home_view({
            "available": True, "soc": 100, "pp": 0, "gp": 0, "lp": 0,
            "bp": 0, "nominal_capacity_kwh": 15, "reserve_soc": 5,
        })
        self.assertEqual(full["battery"]["hours_to_full"], 0.0)
        self.assertIsNone(full["battery"]["hours_to_empty"])
        self.assertEqual(full["battery"]["estimate"]["eta"]["kind"], "at_full")

    def test_daylight_default_contract_in_both_returns(self):
        expected = {"sunrise": "07:00", "sunset": "18:00",
                    "source": "default", "approximate": False}
        unavailable = build_home_view(None)
        self.assertEqual(unavailable["daylight"], expected)
        available = build_home_view({
            "available": True, "soc": 50, "pp": 0, "gp": 0, "bp": 0, "lp": 0,
        })
        self.assertEqual(available["daylight"], expected)

    @patch("home_view.sun_times",
           return_value=SunTimes(6.0, 19.0, source="explicit"))
    def test_custom_daylight_reaches_the_view_and_eta(self, mocked):
        ts = datetime(2026, 7, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(ts, pp=1200),
                               tz="Europe/London")
        mocked.assert_called_once()
        self.assertEqual(view["daylight"],
                         {"sunrise": "06:00", "sunset": "19:00",
                          "source": "explicit", "approximate": False})
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertEqual(estimate["solar"]["as_of"], "06:30")
        self.assertIsNone(estimate["solar_end"])
        self.assertEqual(estimate["eta"]["kind"], "to_full")
        self.assertEqual(estimate["eta"]["label"], "08:51")
        self.assertEqual(estimate["solar"]["low"]["at"], "19:00")

    @patch("home_view.sun_times",
           return_value=SunTimes(4.8, 21.35, source="ip"))
    def test_location_is_forwarded_and_marks_the_window_approximate(
            self, mocked):
        ts = datetime(2026, 7, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        view = build_home_view(self._solar_snapshot(ts, pp=1200),
                               tz="Europe/London", latitude=51.5074,
                               longitude=-0.1278,
                               location_source="ip_geojs")
        mocked.assert_called_once_with(
            date(2026, 7, 1), "Europe/London", 51.5074, -0.1278,
            source="ip_geojs")
        self.assertEqual(view["daylight"],
                         {"sunrise": "04:48", "sunset": "21:21",
                          "source": "ip", "approximate": True})


if __name__ == "__main__":
    unittest.main()
