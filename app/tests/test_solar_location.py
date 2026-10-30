"""Offline tests for the cached GeoJS location resolver."""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import solar_location  # noqa: E402
from solar_location import (  # noqa: E402
    CACHE_TTL_S,
    FAILED_REFRESH_RETRY_S,
    Location,
    LocationResolver,
    fetch_geojs,
    validate_location,
)

logging.getLogger("solar_location").addHandler(logging.NullHandler())

GEO = {"latitude": "51.5074", "longitude": "-0.1278",
       "timezone": "Europe/London", "city": "London", "accuracy": "20.0"}
PARIS = {"latitude": "48.8566", "longitude": "2.3522",
         "timezone": "Europe/Paris", "city": "Paris"}


class LocationValidationTests(unittest.TestCase):
    def test_valid_payload_is_normalised(self):
        location = validate_location(GEO, "ip_geojs")
        self.assertIsInstance(location, Location)
        self.assertAlmostEqual(location.latitude, 51.5074)
        self.assertAlmostEqual(location.longitude, -0.1278)
        self.assertEqual(location.timezone, "Europe/London")
        self.assertEqual(location.city, "London")
        self.assertAlmostEqual(location.accuracy_km, 20.0)
        self.assertEqual(location.source, "ip_geojs")

    def test_bad_accuracy_is_ignored_not_fatal(self):
        raw = dict(GEO, accuracy="not-a-number")
        self.assertIsNone(validate_location(raw, "ip_geojs").accuracy_km)

    def test_invalid_coordinates_and_timezones_are_rejected(self):
        bad = [
            {"longitude": "0", "timezone": "UTC"},
            {"latitude": "0", "timezone": "UTC"},
            dict(GEO, latitude="91"),
            dict(GEO, latitude="-91"),
            dict(GEO, longitude="181"),
            dict(GEO, latitude="nan"),
            dict(GEO, latitude="inf"),
            dict(GEO, longitude="-inf"),
            dict(GEO, timezone=""),
            dict(GEO, timezone="Not/AZone"),
            "not-a-dict",
            None,
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    validate_location(raw, "ip_geojs")

    def test_location_object_round_trips(self):
        location = validate_location(GEO, "ip_geojs")
        cached = validate_location(location, "ip_cache")
        self.assertEqual(cached.latitude, location.latitude)
        self.assertEqual(cached.longitude, location.longitude)
        self.assertEqual(cached.timezone, location.timezone)
        self.assertEqual(cached.city, location.city)
        self.assertEqual(cached.accuracy_km, location.accuracy_km)
        self.assertEqual(cached.source, "ip_cache")


class LocationResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "solar-location.json"

    def tearDown(self):
        self.tmp.cleanup()

    def resolver(self, fetch, now=1000.0, environ=None, retry_s=FAILED_REFRESH_RETRY_S):
        return LocationResolver(cache_path=self.cache, fetch=fetch, now=lambda: now,
                                environ={} if environ is None else environ,
                                retry_s=retry_s)

    def test_cold_start_caches_one_response(self):
        calls = []

        def fetch():
            calls.append(1)
            return GEO

        location = self.resolver(fetch).get()
        self.assertIsNotNone(location)
        self.assertEqual(location.source, "ip_geojs")
        self.assertEqual(location.timezone, "Europe/London")
        self.assertEqual(len(calls), 1)

        saved = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["location"]["latitude"], 51.5074)
        self.assertAlmostEqual(saved["fetched_at"], 1000.0)
        self.assertEqual(saved["retry_after"], 0.0)
        self.assertEqual(stat.S_IMODE(os.stat(self.cache).st_mode), 0o600)

    def test_second_call_uses_the_fresh_cache_without_fetching(self):
        calls = []

        def fetch():
            calls.append(1)
            return GEO

        resolver = self.resolver(fetch)
        first = resolver.get()
        second = resolver.get()
        self.assertEqual(first.latitude, second.latitude)
        self.assertEqual(first.longitude, second.longitude)
        self.assertEqual(first.timezone, second.timezone)
        self.assertEqual(second.source, "ip_cache")
        self.assertEqual(len(calls), 1)

    def test_restart_uses_the_persisted_cache_without_fetching(self):
        self.resolver(lambda: GEO).get()
        calls = []
        restarted = self.resolver(lambda: calls.append(1) or PARIS)
        location = restarted.get()
        self.assertEqual(location.timezone, "Europe/London")
        self.assertEqual(calls, [])

    def test_expired_cache_refreshes_once(self):
        self.resolver(lambda: GEO).get()
        calls = []
        later = 1000.0 + CACHE_TTL_S + 1.0
        resolver = self.resolver(lambda: calls.append(1) or PARIS, now=later)
        location = resolver.get()
        self.assertEqual(location.timezone, "Europe/Paris")
        self.assertEqual(len(calls), 1)

    def test_expired_cache_plus_outage_keeps_stale_and_backs_off(self):
        self.resolver(lambda: GEO).get()
        calls = []

        def outage():
            calls.append(1)
            raise URLError("offline")

        later = 1000.0 + CACHE_TTL_S + 1.0
        resolver = self.resolver(outage, now=later)
        with self.assertLogs("solar_location", level="WARNING"):
            location = resolver.get()
        self.assertEqual(location.timezone, "Europe/London")
        self.assertEqual(len(calls), 1)
        saved = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertAlmostEqual(saved["retry_after"],
                               later + FAILED_REFRESH_RETRY_S)

        # Inside the backoff window the stale value is used with no request.
        again = self.resolver(outage, now=later + 60.0)
        self.assertEqual(again.get().timezone, "Europe/London")
        self.assertEqual(len(calls), 1)

    def test_no_cache_plus_outage_returns_none_and_backs_off(self):
        calls = []

        def outage():
            calls.append(1)
            raise URLError("offline")

        resolver = self.resolver(outage)
        with self.assertLogs("solar_location", level="WARNING"):
            self.assertIsNone(resolver.get())
        self.assertEqual(len(calls), 1)
        saved = json.loads(self.cache.read_text(encoding="utf-8"))
        self.assertIsNone(saved["location"])
        self.assertAlmostEqual(saved["retry_after"],
                               1000.0 + FAILED_REFRESH_RETRY_S)

        self.assertIsNone(self.resolver(outage).get())
        self.assertEqual(len(calls), 1)

    def test_invalid_payload_returns_none_and_is_not_cached(self):
        for payload in (dict(GEO, latitude="91"), dict(GEO, timezone="Bad/Zone"),
                        {"latitude": "1", "longitude": "2"}):
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as directory:
                    cache = Path(directory) / "cache.json"
                    calls = []
                    resolver = LocationResolver(
                        cache_path=cache,
                        fetch=lambda payload=payload: calls.append(1) or payload,
                        now=lambda: 1000.0, environ={})
                    with self.assertLogs("solar_location", level="WARNING"):
                        self.assertIsNone(resolver.get())
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(cache.exists())
                    # The in-memory backoff prevents a poll-by-poll retry.
                    self.assertIsNone(resolver.get())
                    self.assertEqual(len(calls), 1)

    def test_configured_override_bypasses_fetch_and_cache(self):
        env = {"SOLAR_LATITUDE": "40.7128", "SOLAR_LONGITUDE": "-74.0060",
               "SOLAR_TIMEZONE": "America/New_York"}

        def fetch():
            raise AssertionError("GeoJS must not be called for an override")

        location = self.resolver(fetch, environ=env).get()
        self.assertEqual(location.source, "configured")
        self.assertEqual(location.timezone, "America/New_York")
        self.assertFalse(self.cache.exists())

    def test_partial_override_fails_closed_without_a_fetch(self):
        calls = []

        def fetch():
            calls.append(1)
            return GEO

        location = self.resolver(fetch,
                                 environ={"SOLAR_LATITUDE": "51.5"}).get()
        self.assertIsNone(location)
        self.assertEqual(calls, [])
        self.assertFalse(self.cache.exists())

    def test_invalid_override_fails_closed_without_a_fetch(self):
        calls = []
        env = {"SOLAR_LATITUDE": "999", "SOLAR_LONGITUDE": "0",
               "SOLAR_TIMEZONE": "UTC"}
        self.assertIsNone(self.resolver(
            lambda: calls.append(1) or GEO, environ=env).get())
        self.assertEqual(calls, [])

    def test_corrupt_cache_is_ignored_and_refetched(self):
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text("not json", encoding="utf-8")
        calls = []
        location = self.resolver(lambda: calls.append(1) or GEO).get()
        self.assertEqual(location.timezone, "Europe/London")
        self.assertEqual(len(calls), 1)

    def test_cache_from_a_different_location_source_reads_as_ip_cache(self):
        self.resolver(lambda: GEO).get()
        location = self.resolver(lambda: PARIS).get()
        self.assertEqual(location.source, "ip_cache")


class ModuleApiTests(unittest.TestCase):
    def tearDown(self):
        solar_location.reset()

    def test_get_location_is_inert_before_warm(self):
        solar_location.reset()
        self.assertIsNone(solar_location.get_location())

    def test_warm_location_installs_the_resolver(self):
        expected = Location(51.5, -0.12, "Europe/London", source="ip_geojs")

        class FakeResolver:
            def get(self):
                return expected

        with patch.object(solar_location, "LocationResolver",
                          return_value=FakeResolver()):
            self.assertEqual(solar_location.warm_location(), expected)
            self.assertEqual(solar_location.get_location(), expected)
        solar_location.reset()
        self.assertIsNone(solar_location.get_location())


class FetchTests(unittest.TestCase):
    class Response:
        def __init__(self, body):
            self.body = body
            self.read_size = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            self.read_size = size
            return self.body

    def test_fetch_parses_json_with_a_user_agent_and_size_cap(self):
        response = self.Response(json.dumps(GEO).encode("utf-8"))
        with patch.object(solar_location, "urlopen", return_value=response) as urlopen:
            self.assertEqual(fetch_geojs()["timezone"], "Europe/London")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("User-agent"),
                         "anker-tou-solar/1.0")
        self.assertEqual(response.read_size, solar_location.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(urlopen.call_args[1]["timeout"],
                         solar_location.FETCH_TIMEOUT_S)

    def test_fetch_rejects_oversize_and_non_object_payloads(self):
        with patch.object(solar_location, "urlopen",
                          return_value=self.Response(b"x" * 65537)):
            with self.assertRaises(ValueError):
                fetch_geojs()
        with patch.object(solar_location, "urlopen",
                          return_value=self.Response(b"[]")):
            with self.assertRaises(ValueError):
                fetch_geojs()


if __name__ == "__main__":
    unittest.main()
