"""Tests for the single-reading solar-curve model."""
from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solar_forecast import (  # noqa: E402
    SOLAR_END_HOUR, SOLAR_START_HOUR, create_solar_curve, energy_kwh,
    format_hour, remaining_solar_kwh, resolve_solar_window, shape,
    solar_power_w, time_to_generate_kwh, total_daily_solar_kwh,
)


class SolarForecastTests(unittest.TestCase):
    def setUp(self):
        self.curve = create_solar_curve(1200, 10.5)

    def test_document_worked_example(self):
        self.assertAlmostEqual(self.curve.peak_w, 713.22, places=1)
        self.assertAlmostEqual(total_daily_solar_kwh(self.curve), 4.99, places=2)
        self.assertAlmostEqual(remaining_solar_kwh(self.curve, 10.5), 3.85,
                               places=2)

    def test_energy_to_two_and_four_kwh(self):
        hours = time_to_generate_kwh(self.curve, 2.0, 10.5)
        self.assertAlmostEqual(hours, 2.92, places=1)
        self.assertEqual(format_hour(10.5 + hours), "13:25")
        self.assertTrue(math.isinf(time_to_generate_kwh(self.curve, 4.0, 10.5)))

    def test_battery_helper_efficiency(self):
        # 5 kWh battery, 3 kWh stored, 90 % efficiency -> 2.222 kWh AC.
        hours = time_to_generate_kwh(self.curve, 2.0 / 0.9, 10.5)
        self.assertAlmostEqual(hours, 3.25, places=1)
        self.assertEqual(format_hour(10.5 + hours), "13:45")

    def test_curve_is_zero_outside_the_window(self):
        self.assertEqual(shape(0), 0.0)
        self.assertEqual(shape(7.0), 0.0)
        self.assertEqual(shape(18.0), 0.0)
        self.assertEqual(shape(23), 0.0)
        self.assertEqual(energy_kwh(self.curve, 0, 6.99), 0.0)
        self.assertEqual(energy_kwh(self.curve, 18.01, 23), 0.0)
        for hour in (0, 6.99, 7.0, 18.0, 18.01, 23):
            self.assertEqual(solar_power_w(self.curve, hour), 0.0)

    def test_shape_peaks_at_1230(self):
        self.assertAlmostEqual(shape(12.5), 1.0, places=9)
        self.assertAlmostEqual(shape(10.0), shape(15.0), places=9)

    def test_observation_boundaries_are_rejected(self):
        for hour in (SOLAR_START_HOUR, SOLAR_END_HOUR, 6.9, 18.1):
            with self.assertRaises(ValueError):
                create_solar_curve(1000, hour)

    def test_observation_must_be_positive(self):
        with self.assertRaises(ValueError):
            create_solar_curve(0, 10.5)
        with self.assertRaises(ValueError):
            create_solar_curve(-1, 10.5)
        with self.assertRaises(ValueError):
            create_solar_curve(1000, 10.5, risk_factor=0)

    def test_risk_factor_and_peak_cap(self):
        curve = create_solar_curve(1000, 8.0)
        self.assertAlmostEqual(curve.peak_w, 0.5 * min(1000 / shape(8.0), 2500),
                               places=6)
        # The cap applies before the risk factor.
        capped = create_solar_curve(1000, 8.0, max_peak_multiplier=0.1)
        self.assertAlmostEqual(capped.peak_w, 0.5 * 100, places=6)

    def test_energy_is_monotonic_from_an_observation(self):
        previous = 0.0
        for hour in (10.5, 11, 12, 13, 14, 15, 16, 17, 18):
            value = energy_kwh(self.curve, 10.5, hour)
            self.assertGreaterEqual(value + 1e-9, previous)
            previous = value

    def test_format_hour(self):
        self.assertEqual(format_hour(10.5), "10:30")
        self.assertEqual(format_hour(13.249), "13:15")
        self.assertEqual(format_hour(24), "24:00")

    def test_custom_window_is_stored_on_the_curve(self):
        curve = create_solar_curve(1200, 10.5, start_hour=6.0, end_hour=19.0)
        self.assertEqual(curve.start_hour, 6.0)
        self.assertEqual(curve.end_hour, 19.0)
        self.assertAlmostEqual(curve.window_hours, 13.0)
        self.assertAlmostEqual(shape(12.5, 6.0, 19.0), 1.0, places=9)
        self.assertEqual(shape(6.0, 6.0, 19.0), 0.0)
        self.assertEqual(shape(19.0, 6.0, 19.0), 0.0)

    def test_custom_window_energy_uses_the_curve_bounds(self):
        curve = create_solar_curve(1200, 10.5, start_hour=6.0, end_hour=19.0)
        self.assertEqual(energy_kwh(curve, 0.0, 6.0), 0.0)
        self.assertEqual(energy_kwh(curve, 19.0, 23.0), 0.0)
        self.assertAlmostEqual(total_daily_solar_kwh(curve),
                               energy_kwh(curve, 6.0, 19.0), places=9)
        self.assertGreater(remaining_solar_kwh(curve, 18.0), 0.0)

    def test_custom_observation_boundaries_are_rejected(self):
        for hour in (6.0, 19.0, 5.9, 19.1):
            with self.assertRaises(ValueError):
                create_solar_curve(1000, hour, start_hour=6.0, end_hour=19.0)

    def test_invalid_windows_are_rejected(self):
        for start, end in ((19.0, 6.0), (6.0, 6.0), (-1.0, 19.0),
                           (6.0, 25.0), (6.0, 5.0)):
            with self.assertRaises(ValueError):
                create_solar_curve(1000, 10.5, start_hour=start, end_hour=end)

    def test_resolve_solar_window_falls_back_to_defaults(self):
        self.assertEqual(resolve_solar_window(), (7.0, 18.0))
        self.assertEqual(resolve_solar_window(None, 19.0), (7.0, 18.0))
        self.assertEqual(resolve_solar_window(6.0, None), (7.0, 18.0))
        self.assertEqual(resolve_solar_window("bad", 19.0), (7.0, 18.0))
        self.assertEqual(resolve_solar_window(19.0, 6.0), (7.0, 18.0))
        self.assertEqual(resolve_solar_window(6.0, 19.0), (6.0, 19.0))
        self.assertEqual(resolve_solar_window(0.0, 24.0), (0.0, 24.0))


if __name__ == "__main__":
    unittest.main()
