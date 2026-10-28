"""Focused tests for the MQTT EMS frame parser."""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from live import LiveFeed, build_trigger, parse_frame  # noqa: E402


def frame(trans: dict) -> bytes:
    return json.dumps({
        "payload": json.dumps({
            "trans": base64.b64encode(
                json.dumps(trans).encode("utf-8")).decode("ascii")
        })
    }).encode("utf-8")


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
        class FakeMqtt:
            def __init__(self):
                self.topics = []

            def subscribe(self, topic, qos=0):
                self.topics.append(topic)

        class FakeFeed:
            pn, sn = "A5102", "TESTSN"

            def _set(self, **kw):
                pass

        mqtt = FakeMqtt()
        LiveFeed._on_connect(FakeFeed(), mqtt, None, None, 0)
        self.assertIn(
            "dt/anker_power/A5102/TESTSN/troubleshot_info/ems_second",
            mqtt.topics,
        )

    def test_build_trigger_payload(self):
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

    def test_get_wakes_after_grace_and_cooldown(self):
        class FakeMqtt:
            def __init__(self):
                self.published = []

            def is_connected(self):
                return True

            def publish(self, topic, message, qos=0):
                self.published.append((topic, message))
                return SimpleNamespace(rc=0)

        class FakeFeed:
            _publish_trigger = LiveFeed._publish_trigger

            def __init__(self):
                self.pn, self.sn = "A5102", "TESTSN"
                self.api = SimpleNamespace(user_id="acct")
                self.client = SimpleNamespace(user_id="acct")
                self.lock = threading.Lock()
                self.snapshot = {"available": False, "reason": "connecting"}
                self._connected_at = time.time() - 20
                self._last_trigger_at = 0.0
                self._mqtt = FakeMqtt()

        feed = FakeFeed()
        snap = LiveFeed.get(feed)
        self.assertEqual(snap["reason"], "waking")
        self.assertEqual(len(feed._mqtt.published), 1)
        self.assertEqual(feed._mqtt.published[0][0],
                         "cmd/anker_power/A5102/TESTSN/req")
        # A second poll inside the cooldown must not publish again.
        LiveFeed.get(feed)
        self.assertEqual(len(feed._mqtt.published), 1)


if __name__ == "__main__":
    unittest.main()
