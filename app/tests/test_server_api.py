"""HTTP contract tests for the live cadence endpoints."""
from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import unittest
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import home_view  # noqa: E402
import server  # noqa: E402
from daylight import SunTimes  # noqa: E402
from solar_location import Location  # noqa: E402

SID = "api-test-session"
SITE = {"id": "site1", "name": "Home", "timezone": "Europe/London"}
SN = "TESTSN"


class FakeFeed:
    def __init__(self):
        self.interval_s = 5
        self.calls = []

    def get(self):
        now = int(time.time())
        return {
            "available": True, "ts": now - 4, "age_s": 4, "stale": False,
            "waking": False, "pp": 1200, "gp": 0, "bp": 0, "lp": 1200,
            "soc": 55, "interval_s": self.interval_s, "count": 60,
            "stale_after_s": 30, "batt_avg_kw": 0.0,
            "batt_avg_net_kw": 0.0, "load_avg_kw": 1.2,
            "solar_avg_kw": 1.2,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }

    def stats(self):
        return {"interval_s": self.interval_s, "count": 60, "frames": 3,
                "window_frames": 3, "last_gap_s": 5.0, "waking": False,
                "pending_interval_s": None, "last_trigger_error": None,
                "battery_samples": 3, "last_frame_keys": ["pp", "bp"],
                "charge_limit_source": "default_6kw", "charge_limit_kw": 6.0}

    def set_interval(self, value):
        self.calls.append(value)
        return {"interval_s": self.interval_s, "pending_interval_s": value,
                "applied": False, "count": 60}


class ServerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.CONFIG["persist"] = False
        cls.feed = FakeFeed()
        server.SESSIONS[SID] = {
            "client": object(), "profile": {"nick": "Test", "user_id": "u"},
            "sites": [SITE], "expires": time.time() + 3600,
            "lock": threading.Lock(), "mqtt_interval_s": 5,
            "live": {f"{SITE['id']}:{SN}": cls.feed},
        }
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.SESSIONS.pop(SID, None)

    def setUp(self):
        self.feed.interval_s = 5
        self.feed.calls.clear()
        server.SESSIONS[SID] = {
            "client": object(), "profile": {"nick": "Test", "user_id": "u"},
            "sites": [SITE], "expires": time.time() + 3600,
            "lock": threading.Lock(), "mqtt_interval_s": 5,
            "live": {f"{SITE['id']}:{SN}": self.feed},
        }

    def request(self, method, path, body=None, cookie=f"{server.COOKIE}={SID}"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers={"Cookie": cookie, "X-Anker-Client": "1",
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = resp.read().decode()
        headers = resp.getheaders()
        conn.close()
        return resp.status, payload, headers

    def test_live_contract(self):
        status, payload, _ = self.request(
            "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        self.assertEqual(status, 200)
        js = json.loads(payload)
        self.assertEqual(js["mqtt_interval_s"], 5)
        self.assertEqual(js["mqtt_bounds"]["max"], 60)
        self.assertEqual(js["stale_after_s"], 30)
        self.assertEqual(js["live_stats"]["interval_s"], 5)
        self.assertEqual(js["live"]["solar_avg_kw"], 1.2)
        self.assertTrue(js["view"]["available"])
        self.assertEqual(js["view"]["daylight"],
                         {"sunrise": "07:00", "sunset": "18:00",
                          "source": "default", "approximate": False})
        self.assertEqual(js["view"]["battery"]["estimate"]["mode"], "idle")
        self.assertEqual(js["view"]["battery"]["charge_limit_kw"], 6.0)

    def test_live_exposes_solar_estimate_in_the_site_timezone(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 4000, "gp": 0, "bp": -700, "lp": 500,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": 0.7, "batt_avg_kw": 0.7, "load_avg_kw": 0.5,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        try:
            status, payload, _ = self.request(
                "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        estimate = json.loads(payload)["view"]["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertAlmostEqual(estimate["hours"], 4.653, places=2)
        self.assertEqual(estimate["clock"], "15:09")
        self.assertEqual(estimate["eta"]["kind"], "to_full")
        self.assertEqual(estimate["eta"]["label"], "15:09")
        self.assertFalse(estimate["solar"]["capped"])
        self.assertEqual(estimate["solar"]["observed_source"], "instant")

    def test_live_uses_the_averaged_pv_for_the_solar_estimate(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 1000, "gp": 0, "bp": -700, "lp": 500,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": 0.7, "batt_avg_kw": 0.7, "load_avg_kw": 0.5,
            "solar_avg_kw": 4.0,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        try:
            status, payload, _ = self.request(
                "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        view = json.loads(payload)["view"]
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertAlmostEqual(estimate["hours"], 4.653, places=2)
        self.assertEqual(estimate["clock"], "15:09")
        self.assertEqual(estimate["solar"]["observed_kw"], 4.0)
        self.assertEqual(estimate["solar"]["observed_source"], "avg")
        self.assertEqual(view["solar"]["w"], 1000)
        self.assertEqual(view["solar"]["avg_kw"], 4.0)

    def test_live_exposes_a_slow_eta_with_a_day_hint(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 0, "gp": 0, "bp": 213.75, "lp": 0,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": -0.21375, "load_avg_kw": 0.0,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        try:
            status, payload, _ = self.request(
                "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        view = json.loads(payload)["view"]
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "discharging")
        self.assertAlmostEqual(estimate["hours"], 30.0, places=2)
        self.assertEqual(estimate["clock"], "16:30")
        self.assertEqual(estimate["eta"]["kind"], "to_empty")
        self.assertEqual(estimate["eta"]["label"], "tomorrow 16:30")
        self.assertEqual(view["battery"]["hours_to_empty"], 30.0)

    def test_live_ignores_daylight_query_parameters(self):
        status, payload, _ = self.request(
            "GET", f"/api/live?site={SITE['id']}&sn={SN}"
                   "&sunrise=06:00&sunset=19:00")
        self.assertEqual(status, 200)
        view = json.loads(payload)["view"]
        self.assertEqual(view["daylight"],
                         {"sunrise": "07:00", "sunset": "18:00",
                          "source": "default", "approximate": False})

    def test_patched_sun_times_flow_through_the_response(self):
        ts = datetime(2026, 7, 1, 5, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 1200, "gp": 0, "bp": -700, "lp": 500,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": 0.7, "batt_avg_kw": 0.7, "load_avg_kw": 0.5,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        try:
            with patch("home_view.sun_times",
                       return_value=SunTimes(6.0, 19.0, source="explicit")):
                status, payload, _ = self.request(
                    "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        view = json.loads(payload)["view"]
        self.assertEqual(view["daylight"],
                         {"sunrise": "06:00", "sunset": "19:00",
                          "source": "explicit", "approximate": False})
        estimate = view["battery"]["estimate"]
        self.assertEqual(estimate["mode"], "solar")
        self.assertEqual(estimate["solar"]["as_of"], "06:30")
        self.assertEqual(estimate["solar_end"], "19:00")
        self.assertEqual(estimate["eta"]["at"], "19:00")

    def test_live_uses_the_resolved_location_window(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 1200, "gp": 0, "bp": -700, "lp": 500,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": 0.7, "batt_avg_kw": 0.7, "load_avg_kw": 0.5,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        location = Location(51.5074, -0.1278, "Europe/London",
                            source="ip_geojs")
        try:
            with patch("server.solar_location.get_location",
                       return_value=location), \
                    patch("home_view.sun_times",
                          return_value=SunTimes(4.8, 21.35, source="ip")) as sun:
                status, payload, _ = self.request(
                    "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        js = json.loads(payload)
        self.assertEqual(js["timezone"], "Europe/London")
        self.assertEqual(js["view"]["daylight"],
                         {"sunrise": "04:48", "sunset": "21:21",
                          "source": "ip", "approximate": True})
        sun.assert_called_once_with(
            date(2026, 7, 1), "Europe/London", 51.5074, -0.1278,
            source="ip_geojs")

    def test_live_falls_back_when_the_location_resolver_raises(self):
        with patch("server.solar_location.get_location",
                   side_effect=RuntimeError("boom")):
            status, payload, _ = self.request(
                "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        self.assertEqual(status, 200)
        view = json.loads(payload)["view"]
        self.assertEqual(view["daylight"],
                         {"sunrise": "07:00", "sunset": "18:00",
                          "source": "default", "approximate": False})

    def test_location_timezone_replaces_a_missing_site_timezone(self):
        ts = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc).timestamp()
        original = self.feed.get
        self.feed.get = lambda: {
            "available": True, "ts": int(ts), "age_s": 4, "stale": False,
            "waking": False, "pp": 1200, "gp": 0, "bp": -700, "lp": 500,
            "soc": 50, "interval_s": 5, "count": 60, "stale_after_s": 30,
            "nominal_capacity_kwh": 15.0, "reserve_soc": 5.0,
            "batt_avg_net_kw": 0.7, "batt_avg_kw": 0.7, "load_avg_kw": 0.5,
            "charge_limit_kw": 6.0, "charge_limit_source": "default_6kw",
        }
        location = Location(40.7128, -74.0060, "America/New_York",
                            source="ip_cache")
        try:
            with patch("server.Handler._site_timezone", return_value=""), \
                    patch("server.solar_location.get_location",
                          return_value=location), \
                    patch("home_view.sun_times",
                          return_value=SunTimes(5.5, 20.5, source="ip")):
                status, payload, _ = self.request(
                    "GET", f"/api/live?site={SITE['id']}&sn={SN}")
        finally:
            self.feed.get = original
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["timezone"], "America/New_York")

    def test_interval_post_reports_pending_and_sets_cookie(self):
        self.feed.calls.clear()
        status, payload, headers = self.request(
            "POST", "/api/live/interval", {"interval_s": 60})
        self.assertEqual(status, 200)
        js = json.loads(payload)
        self.assertEqual(js["mqtt_interval_s"], 60)
        self.assertEqual(js["pending_interval_s"], 60)
        self.assertFalse(js["applied"])
        self.assertEqual(self.feed.calls, [60])
        cookies = [v for k, v in headers if k.lower() == "set-cookie"]
        self.assertTrue(any(v.startswith(f"{server.PREF_COOKIE}=60;") for v in cookies))
        self.assertFalse(any(v.startswith(f"{server.COOKIE}=") for v in cookies))

    def test_interval_post_rejects_non_number(self):
        status, payload, _ = self.request(
            "POST", "/api/live/interval", {"interval_s": "tomorrow"})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(payload)["error"]["kind"], "validation")

    def test_logout_does_not_clear_preference(self):
        status, _, headers = self.request("POST", "/api/logout")
        self.assertEqual(status, 200)
        cookies = [v for k, v in headers if k.lower() == "set-cookie"]
        self.assertTrue(any(v.startswith(f"{server.COOKIE}=") for v in cookies))
        self.assertFalse(any(v.startswith(f"{server.PREF_COOKIE}=") for v in cookies))


if __name__ == "__main__":
    unittest.main()
