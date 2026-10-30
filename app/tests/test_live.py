"""Focused tests for the MQTT EMS frame parser and cadence engine."""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import unittest
from collections import deque
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live import (  # noqa: E402
    LiveFeed, build_trigger, count_for_interval, normalise_interval,
    parse_frame, stale_after_s,
)


def frame(trans: dict) -> bytes:
    return json.dumps({
        "payload": json.dumps({
            "trans": base64.b64encode(
                json.dumps(trans).encode("utf-8")).decode("ascii")
        })
    }).encode("utf-8")


class FakeMqtt:
    def __init__(self, connected: bool = True):
        self.connected = connected
        self.published = []

    def is_connected(self) -> bool:
        return self.connected

    def publish(self, topic, message, qos=0):
        self.published.append((topic, message))
        return SimpleNamespace(rc=0)


def make_feed(interval: int = 5, snapshot: dict | None = None,
              connected_for: float = 20.0) -> LiveFeed:
    """A LiveFeed with the network/HTTP parts replaced by test doubles."""
    feed = LiveFeed.__new__(LiveFeed)
    feed.pn, feed.sn = "A5102", "TESTSN"
    feed.api = SimpleNamespace(user_id="acct")
    feed.client = SimpleNamespace(user_id="acct")
    feed.lock = threading.Lock()
    feed.interval_s = normalise_interval(interval)
    feed.count = count_for_interval(feed.interval_s)
    feed.snapshot = snapshot or {"available": False, "reason": "connecting"}
    feed.nominal_capacity_kwh = 15.0
    feed.reserve_soc = 5.0
    feed._connected_at = time.time() - connected_for
    feed._last_trigger_at = 0.0
    feed._last_frame_at = 0.0
    feed._last_gap_s = None
    feed._window_frames = 0
    feed._frames_total = 0
    feed._pending_interval_s = None
    feed._waking_since = None
    feed._last_trigger_error = None
    feed._battery_samples = deque()
    feed.site = None
    feed._charge_limit = None
    feed._device_limit = None
    feed._last_frame_keys = ()
    feed._limits_thread = None
    feed._last_limits_error = None
    feed._mqtt = FakeMqtt()
    feed.wakes = 0

    def count_wake():
        feed.wakes += 1

    feed._publish_trigger = count_wake
    return feed


class LiveFrameTests(unittest.TestCase):
    def test_empty_ems_data_is_not_a_frame(self):
        self.assertIsNone(parse_frame(frame({"type": "ems", "data": {}})))

    def test_non_dict_data_is_not_a_frame(self):
        self.assertIsNone(parse_frame(frame({"type": "ems", "data": None})))

    def test_zero_values_are_valid_telemetry(self):
        snap = parse_frame(frame({
            "type": "ems", "soc": 0,
            "data": {"pp": 0, "gp": 0, "bp": 0, "lp": 0, "soc": 0},
        }))
        self.assertIsNotNone(snap)
        self.assertEqual(snap["pp"], 0)
        self.assertEqual(snap["lp"], 0)

    def test_connect_subscribes_to_ems_second(self):
        feed = make_feed()

        class FakeMqttSub:
            def __init__(self):
                self.topics = []

            def subscribe(self, topic, qos=0):
                self.topics.append(topic)

        mqtt = FakeMqttSub()
        LiveFeed._on_connect(feed, mqtt, None, None, 0)
        self.assertIn(
            "dt/anker_power/A5102/TESTSN/troubleshot_info/ems_second",
            mqtt.topics,
        )


class CadenceHelperTests(unittest.TestCase):
    def test_normalise_interval(self):
        cases = {None: 5, 0: 5, 5: 5, 7: 5, 8: 10, 58: 60,
                 118: 60, 999: 60, "bad": 5, "45": 45}
        for value, expected in cases.items():
            self.assertEqual(normalise_interval(value), expected, value)

    def test_count_for_interval(self):
        self.assertEqual(count_for_interval(5), 60)
        self.assertEqual(count_for_interval(10), 2)
        self.assertEqual(count_for_interval(30), 2)
        self.assertEqual(count_for_interval(60), 2)

    def test_stale_after(self):
        self.assertEqual(stale_after_s(5), 30)
        self.assertEqual(stale_after_s(30), 50)
        self.assertEqual(stale_after_s(60), 95)


class TriggerTests(unittest.TestCase):
    def test_default_trigger_keeps_production_frame(self):
        topic, message = build_trigger("A5102", "TESTSN", "acct", now=123)
        self.assertEqual(topic, "cmd/anker_power/A5102/TESTSN/req")
        outer = json.loads(message)
        self.assertEqual(outer["head"]["cmd"], 9)
        self.assertEqual(outer["head"]["client_id"], "cloud")
        inner = json.loads(outer["payload"])
        self.assertEqual(inner["account_id"], "acct")
        trans = json.loads(base64.b64decode(inner["trans"]))
        self.assertEqual(trans["cmdId"], 4)
        self.assertEqual(trans["config"], {"count": 60, "interval": 5})

    def test_60_s_trigger_uses_two_frames(self):
        topic, message = build_trigger(
            "A5102", "TESTSN", "acct", now=123, interval_s=60)
        inner = json.loads(json.loads(message)["payload"])
        trans = json.loads(base64.b64decode(inner["trans"]))
        self.assertEqual(trans["config"], {"count": 2, "interval": 60})


class LiveFeedTests(unittest.TestCase):
    def test_get_wakes_after_grace_and_cooldown(self):
        feed = make_feed()
        snap = feed.get()
        self.assertEqual(snap["reason"], "waking")
        self.assertEqual(feed.wakes, 1)
        # A second poll inside the cooldown must not publish again.
        feed.get()
        self.assertEqual(feed.wakes, 1)

    def test_fresh_window_does_not_wake(self):
        now = int(time.time())
        feed = make_feed(interval=60, snapshot={
            "available": True, "ts": now - 5, "pp": 1000, "bp": 0,
            "soc": 50,
        })
        feed._window_frames = 1
        feed.get()
        self.assertEqual(feed.wakes, 0)

    def test_window_end_renews_before_stale(self):
        now = int(time.time())
        feed = make_feed(interval=60, snapshot={
            "available": True, "ts": now - 61, "pp": 1000, "bp": 0,
            "soc": 50,
        })
        feed._window_frames = 2
        snap = feed.get()
        self.assertEqual(feed.wakes, 1)
        self.assertTrue(snap["waking"])
        self.assertFalse(snap["stale"])
        self.assertEqual(snap["pp"], 1000)

    def test_stale_keeps_values_and_sets_waking(self):
        now = int(time.time())
        feed = make_feed(interval=60, snapshot={
            "available": True, "ts": now - 200, "pp": 1200, "bp": 0,
            "soc": 44,
        })
        snap = feed.get()
        self.assertEqual(feed.wakes, 1)
        self.assertTrue(snap["available"])
        self.assertTrue(snap["waking"])
        self.assertFalse(snap["stale"])
        self.assertEqual(snap["pp"], 1200)
        self.assertEqual(snap["age_s"], 200)

    def test_waking_timeout_allows_retry(self):
        feed = make_feed(connected_for=300)
        feed._waking_since = time.time() - 61
        feed._last_trigger_at = time.time() - 61
        feed.snapshot = {"available": False, "reason": "waking"}
        feed.get()
        self.assertEqual(feed.wakes, 1)

    def test_set_interval_pending_until_window_end(self):
        now = int(time.time())
        feed = make_feed(interval=5, snapshot={
            "available": True, "ts": now - 1, "pp": 1000, "bp": 0,
            "soc": 50,
        })
        feed._window_frames = 10
        out = feed.set_interval(60)
        self.assertFalse(out["applied"])
        self.assertEqual(out["pending_interval_s"], 60)
        self.assertEqual(feed.interval_s, 5)
        self.assertEqual(feed.wakes, 0)

        feed.snapshot["ts"] = now - 6
        feed._window_frames = 60
        snap = feed.get()
        self.assertEqual(feed.interval_s, 60)
        self.assertEqual(feed.count, 2)
        self.assertEqual(feed.wakes, 1)
        self.assertTrue(snap["waking"])
        self.assertEqual(feed.stats()["pending_interval_s"], None)

    def test_set_interval_applies_at_once_without_a_window(self):
        feed = make_feed()
        out = feed.set_interval(60)
        self.assertTrue(out["applied"])
        self.assertEqual(feed.interval_s, 60)
        self.assertEqual(feed.count, 2)
        self.assertEqual(feed.wakes, 1)

    def test_battery_average_is_time_weighted(self):
        feed = make_feed(interval=60)
        now = time.time()
        feed._battery_samples.extend([
            (now - 400, 1.0, 0.4), (now - 200, 3.0, 0.6), (now, 5.0, 0.8)])
        averages = feed._averages(now)
        self.assertEqual(averages["batt_avg_net_kw"], 3.0)
        self.assertEqual(averages["batt_avg_kw"], 3.0)
        self.assertAlmostEqual(averages["load_avg_kw"], 0.6)

    def test_battery_average_prunes_old_samples(self):
        feed = make_feed(interval=60)
        now = time.time()
        feed._battery_samples.extend([
            (now - 700, 9.0, 1.0), (now - 10, 2.0, 0.5)])
        averages = feed._averages(now)
        self.assertEqual(averages["batt_avg_net_kw"], 2.0)
        self.assertEqual(averages["load_avg_kw"], 0.5)

    def test_battery_average_keeps_signed_direction(self):
        feed = make_feed(interval=60)
        now = time.time()
        feed._battery_samples.extend([(now - 100, -1.5, 0.5), (now, -0.5, 0.5)])
        averages = feed._averages(now)
        self.assertAlmostEqual(averages["batt_avg_net_kw"], -1.0)
        self.assertEqual(averages["batt_avg_kw"], 1.0)

    def test_on_message_updates_window_and_average(self):
        feed = make_feed(interval=60)
        feed._on_message(None, None, SimpleNamespace(payload=frame({
            "type": "ems",
            "data": {"pp": 1000, "bp": -1500, "gp": 0, "lp": 500, "soc": 50},
        })))
        self.assertTrue(feed.snapshot["available"])
        self.assertEqual(feed._window_frames, 1)
        self.assertEqual(feed.stats()["battery_samples"], 1)
        snap = feed.get()
        self.assertAlmostEqual(snap["batt_avg_net_kw"], 1.5)
        self.assertAlmostEqual(snap["batt_avg_kw"], 1.5)
        self.assertAlmostEqual(snap["load_avg_kw"], 0.5)
        self.assertEqual(snap["charge_limit_kw"], 6.0)
        self.assertEqual(snap["charge_limit_source"], "default_6kw")
        self.assertFalse(snap["waking"])
        self.assertFalse(snap["stale"])

    def test_parse_frame_carries_realtime_charge_limit(self):
        snap = parse_frame(frame({
            "type": "ems",
            "data": {"pp": 1000, "bp": -500, "lp": 500, "soc": 50,
                     "mcp": 8000, "unknown_key": 1},
        }))
        self.assertEqual(snap["charge_limit_w"], 8000)
        self.assertEqual(snap["charge_limit_source"], "realtime.ems_second.mcp")
        self.assertIn("mcp", snap["_ems_keys"])

    def test_on_message_caches_realtime_cap_and_stats_keys(self):
        feed = make_feed(interval=60)
        feed._on_message(None, None, SimpleNamespace(payload=frame({
            "type": "ems",
            "data": {"pp": 1000, "bp": -1500, "gp": 0, "lp": 500, "soc": 50,
                     "ac_power_limit": 4200},
        })))
        snap = feed.get()
        self.assertEqual(snap["charge_limit_kw"], 4.2)
        self.assertEqual(snap["charge_limit_source"],
                         "realtime.ems_second.ac_power_limit")
        self.assertIn("ac_power_limit", feed.stats()["last_frame_keys"])

    def test_stale_realtime_cap_falls_back_to_device_attrs_then_6kw(self):
        feed = make_feed(interval=60)
        now = time.time()
        feed._charge_limit = {"kw": 1.0, "source": "realtime.ems_second.mcp",
                              "ts": now - 400}
        feed._device_limit = {"kw": 4.2,
                              "source": "device_attrs.ac_power_limit",
                              "ts": now - 10}
        snap = feed.get()
        self.assertEqual(snap["charge_limit_kw"], 4.2)
        self.assertEqual(snap["charge_limit_source"],
                         "device_attrs.ac_power_limit")
        feed._device_limit = None
        snap = feed.get()
        self.assertEqual(snap["charge_limit_kw"], 6.0)
        self.assertEqual(snap["charge_limit_source"], "default_6kw")

    def test_plan_save_cap_overrides_device_cache(self):
        feed = make_feed(interval=60)
        feed.set_charge_limit(3.5, "plan-save")
        snap = feed.get()
        self.assertEqual(snap["charge_limit_kw"], 3.5)
        self.assertEqual(snap["charge_limit_source"], "plan-save")


if __name__ == "__main__":
    unittest.main()
