"""Unit tests for the pure Home view-model derivation."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from home_view import build_home_view  # noqa: E402


class HomeViewTests(unittest.TestCase):
    def test_unavailable_is_not_zero(self):
        view = build_home_view({"available": False, "reason": "connecting"})
        self.assertFalse(view["available"])
        self.assertIsNone(view["solar"]["w"])
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

    def test_numeric_zero_is_live_idle(self):
        view = build_home_view({"available": True, "pp": 0, "gp": 0,
                                "bp": 0, "lp": 0, "soc": 0})
        self.assertTrue(view["available"])
        self.assertEqual(view["solar"]["kw"], 0.0)
        self.assertEqual(view["grid"]["kw"], 0.0)
        self.assertEqual(view["battery"]["kw"], 0.0)
        self.assertEqual(view["home"]["kw"], 0.0)
        self.assertEqual(view["status"]["level"], "live")


if __name__ == "__main__":
    unittest.main()
