"""Preference-cookie and interval-bound tests for the local server."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class PreferenceTests(unittest.TestCase):
    def tearDown(self):
        server.CONFIG["secure_cookie"] = False

    def test_bounds_are_five_to_sixty(self):
        bounds = server.interval_bounds()
        self.assertEqual(bounds["min"], 5)
        self.assertEqual(bounds["max"], 60)
        self.assertEqual(bounds["step"], 5)
        self.assertEqual(bounds["default"], 5)

    def test_preference_cookie_is_not_the_session_cookie(self):
        self.assertNotEqual(server.PREF_COOKIE, server.COOKIE)
        cookie = server.pref_cookie_header(60)
        self.assertTrue(cookie.startswith(f"{server.PREF_COOKIE}=60;"))
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn(f"Max-Age={server.PREF_MAX_AGE}", cookie)
        self.assertNotIn("Secure", cookie)

    def test_preference_cookie_clamps_and_marks_secure(self):
        server.CONFIG["secure_cookie"] = True
        cookie = server.pref_cookie_header(999)
        self.assertTrue(cookie.startswith(f"{server.PREF_COOKIE}=60;"))
        self.assertIn("; Secure", cookie)

    def test_invalid_preference_falls_back_to_five(self):
        cookie = server.pref_cookie_header("not-a-number")
        self.assertTrue(cookie.startswith(f"{server.PREF_COOKIE}=5;"))


if __name__ == "__main__":
    unittest.main()
