"""Unit tests for pure charge-limit extraction and fallback."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anker_client import AnkerClient  # noqa: E402
from limits import (  # noqa: E402
    FALLBACK_MAX_CHARGE_KW, fallback_limit, limit_from_attrs, limit_from_ems,
    resolve_charge_limit,
)


class LimitExtractionTests(unittest.TestCase):
    def test_flat_device_attribute_watts(self):
        info = limit_from_attrs({"ac_power_limit": 4200})
        self.assertEqual(info["kw"], 4.2)
        self.assertEqual(info["source"], "device_attrs.ac_power_limit")

    def test_nested_attributes(self):
        info = limit_from_attrs({"device_sn": "X", "attributes": {
            "ac_power_limit": 3600}})
        self.assertEqual(info["kw"], 3.6)
        self.assertEqual(info["source"], "device_attrs.ac_power_limit")

    def test_kilowatt_values_still_work(self):
        self.assertEqual(limit_from_attrs({"ac_power_limit": 4.2})["kw"], 4.2)
        self.assertEqual(limit_from_attrs({"maxChargingPower": "4.2"})["kw"], 4.2)

    def test_ems_field(self):
        info = limit_from_ems({"pp": 1200, "mcp": 8000})
        self.assertEqual(info["kw"], 8.0)
        self.assertEqual(info["source"], "realtime.ems_second.mcp")

    def test_minimum_valid_candidate_wins(self):
        info = limit_from_attrs({
            "ac_power_limit": 4200,
            "legal_power_limit": {"limit": 3600, "unit": "W"},
        })
        self.assertEqual(info["kw"], 3.6)

    def test_missing_and_invalid_values(self):
        self.assertIsNone(limit_from_attrs({})["kw"])
        self.assertEqual(limit_from_attrs({})["source"], "unavailable")
        self.assertIsNone(limit_from_attrs({"mcp": 0})["kw"])
        self.assertIsNone(limit_from_attrs({"mcp": -1})["kw"])
        self.assertIsNone(limit_from_attrs({"mcp": "bad"})["kw"])

    def test_load_power_is_not_a_charge_limit(self):
        self.assertIsNone(limit_from_ems({"gmp": 8000, "mpp": 4000})["kw"])

    def test_fallback_and_resolution_order(self):
        self.assertEqual(FALLBACK_MAX_CHARGE_KW, 6.0)
        self.assertEqual(fallback_limit()["source"], "default_6kw")
        live = {"kw": 1.0, "source": "realtime.ems_second.mcp"}
        attrs = {"kw": 4.2, "source": "device_attrs.ac_power_limit"}
        self.assertEqual(resolve_charge_limit(live, attrs)["kw"], 1.0)
        self.assertEqual(resolve_charge_limit(None, attrs)["kw"], 4.2)
        self.assertEqual(resolve_charge_limit(None, None)["source"],
                         "default_6kw")


class ClientLimitTests(unittest.TestCase):
    def test_device_attrs_are_preferred(self):
        client = AnkerClient.resume("token", "user", "GB")
        calls = []

        def fake_post(path, body, **kwargs):
            calls.append((path, body))
            return {"data": {"attributes": {"ac_power_limit": 4200}}}

        client._post = fake_post
        info = client.read_charge_limit("site", "sn")
        self.assertEqual(info["kw"], 4.2)
        self.assertEqual(info["source"], "device_attrs.ac_power_limit")
        self.assertEqual(calls[0][1]["device_sn"], "sn")

    def test_power_limit_is_the_fallback(self):
        client = AnkerClient.resume("token", "user", "GB")
        client._post = lambda *args, **kwargs: {"data": None}
        client.power_limits = lambda *args, **kwargs: {
            "ac_input_power": 3600, "ac_input_power_unit": "W"}
        info = client.read_charge_limit("site", "sn")
        self.assertEqual(info["kw"], 3.6)
        self.assertEqual(info["source"], "power_limits.ac_input_power")

    def test_no_dynamic_value_returns_none(self):
        client = AnkerClient.resume("token", "user", "GB")
        client._post = lambda *args, **kwargs: {"data": None}
        client.power_limits = lambda *args, **kwargs: None
        info = client.read_charge_limit("site", "sn")
        self.assertIsNone(info["kw"])
        self.assertEqual(info["source"], "unavailable")
        self.assertTrue(info["error"])


if __name__ == "__main__":
    unittest.main()
