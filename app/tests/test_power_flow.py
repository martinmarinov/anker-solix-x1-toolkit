"""Unit tests for the pure raw-snapshot power parsing."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from power_flow import (  # noqa: E402
    battery_charge_w, battery_discharge_w, battery_display_w, battery_net_w,
    grid_display_w, grid_export_w, grid_import_w, grid_net_w, home_w,
    has_power_fields, solar_w,
)


class PowerFlowTests(unittest.TestCase):
    def test_edges_win_over_aggregates(self):
        snap = {
            "pp": 4180, "p2lp": 900, "p2bp": 900, "p2gp": 0,
            "gp": 1200, "g2lp": 880, "g2bp": 900,
            "bp": -900, "b2lp": 0, "b2gp": 0, "lp": 1780,
        }
        self.assertEqual(solar_w(snap), 4180)
        self.assertEqual(grid_import_w(snap), 1780)
        self.assertEqual(grid_export_w(snap), 0)
        self.assertEqual(grid_net_w(snap), 1780)
        self.assertEqual(battery_charge_w(snap), 1800)
        self.assertEqual(battery_discharge_w(snap), 0)
        self.assertEqual(battery_net_w(snap), 1800)
        self.assertEqual(home_w(snap), 1780)
        self.assertEqual(grid_display_w(snap), 1200)
        self.assertEqual(battery_display_w(snap), 900)

    def test_signed_fallbacks(self):
        snap = {"pp": 1000, "gp": -500, "bp": 300, "lp": 800}
        self.assertEqual(grid_export_w(snap), 500)
        self.assertEqual(grid_import_w(snap), 0)
        self.assertEqual(battery_discharge_w(snap), 300)
        self.assertEqual(battery_net_w(snap), -300)
        self.assertEqual(home_w(snap), 800)

    def test_home_edges_fill_a_missing_load(self):
        snap = {"g2lp": 700, "b2lp": 300, "lp": 0}
        self.assertEqual(home_w(snap), 1000)

    def test_edge_frames_win_even_when_aggregate_disagrees(self):
        snap = {"gp": 9999, "g2lp": 10, "g2bp": 0,
                "b2lp": 20, "b2gp": 0}
        self.assertEqual(grid_import_w(snap), 10)
        self.assertEqual(battery_net_w(snap), -20)

    def test_invalid_values_are_zero(self):
        snap = {"pp": "bad", "gp": None, "bp": object(), "lp": "-5"}
        self.assertEqual(solar_w(snap), 0)
        self.assertEqual(grid_net_w(snap), 0)
        self.assertEqual(battery_net_w(snap), 0)
        self.assertEqual(home_w(snap), 0)

    def test_has_power_fields(self):
        self.assertFalse(has_power_fields(None))
        self.assertFalse(has_power_fields({"available": True}))
        self.assertTrue(has_power_fields({"pp": 0}))
        self.assertTrue(has_power_fields({"p2lp": 0}))


if __name__ == "__main__":
    unittest.main()
